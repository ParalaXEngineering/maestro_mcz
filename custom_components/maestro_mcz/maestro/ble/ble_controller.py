"""BLE-only controller: implements the controller interface over the local link.

This is the fallback / standalone transport. It synthesises a **minimal** model
from the capability scan (climate, manual power, one room fan, mode, silent) and
serves ``State`` / ``Status`` from the live register image. In the hybrid setup
the model instead comes from the cloud; see :mod:`.hybrid_controller`.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant

from ..controller.controller_interface import MaestroControllerInterface
from ..controller.maestro_controller import MaestroConnectionException
from ..controller.requests.activate_program import ProgramCommand
from ..controller.responses.model import Model
from ..controller.responses.state import State
from ..controller.responses.status import Status
from ..controller.responses.stove_info import AccessControl, Node, StoveInfo
from . import registers
from .transport import BleConnectionError, MczBleTransport

_LOGGER = logging.getLogger(__name__)

BLE_MODEL_ID = "ble-local"
BLE_SENSOR_SET_TYPE_ID = "ble"

# Upper bound on the connect + capability scan + first poll performed during
# setup, so a silent panel cannot stall Home Assistant's startup.
SETUP_WARMUP_TIMEOUT = 25.0


def _mac_slug(address: str) -> str:
    """Return a MAC address without separators, lowercased."""
    return address.replace(":", "").replace("-", "").lower()


def build_synth_model(
    caps: dict[str, object], model_id: str, sensor_set_type_id: str
) -> Model:
    """Synthesise a minimal :class:`Model` from a capability dict.

    Only the configurations backed by a real BLE write register are emitted, so
    the platforms expose exactly what the local link can drive.
    """
    fan_levels = int(caps.get("fan_levels", 5) or 5)
    banca = caps.get("banca_dati") or ""
    model_name = f"MCZ Maestro (BLE){f' {banca}' if banca else ''}"

    def cfg(sensor_name, type_, **extra) -> dict:
        base = {
            "sensor_name": sensor_name,
            "type": type_,
            "visible": True,
            "variants": None,
            "sensor_id": sensor_name,  # BLE-only: sensor_id == logical name
            "enabled": True,
            "min": None,
            "max": None,
            "mappings": None,
        }
        base.update(extra)
        return base

    def model_cfg(name, configuration_id, configuration) -> dict:
        return {
            "timed": False,
            "configuration_name": name,
            "configurations": [configuration],
            "configuration_id": configuration_id,
            "limitations": None,
        }

    model_configurations = [
        # Declared INT (not BOOLEAN) on purpose: the boolean power path in
        # climate.py sends a constant ``True`` for both directions because the
        # cloud treats com_on_off as a button press. 0x038A is a toggle too, but
        # the local link knows the real phase, so it needs the *requested* state
        # to decide whether to press at all — the INT path supplies it as 0/1.
        model_cfg(
            "Spegnimento",
            "ble-onoff",
            cfg(
                "com_on_off",
                "int",
                min="0",
                max="1",
                variants=["off", "on"],
                mappings={"off": 0, "on": 1},
            ),
        ),
        model_cfg(
            "Set_amb_temp", "ble-temp", cfg("set_amb1", "double", min="5", max="45")
        ),
        model_cfg(
            "set_mod",
            "ble-mode",
            cfg(
                "mod_funz",
                "int",
                min="0",
                max="2",
                variants=list(registers.MODE_KEYS_CONFIRMED.values()),
                mappings={
                    key: code for code, key in registers.MODE_KEYS_CONFIRMED.items()
                },
            ),
        ),
        model_cfg(
            "Set_pot", "ble-pot", cfg("set_pot_man", "int", min="1", max="5")
        ),
    ]

    # one room fan (0x03FA), exposed across every mode configuration the fan
    # entity looks for
    fan_mode_configs = {
        "Manuale": "ble-fan-manual",
        "Auto": "ble-fan-auto",
        "Overnight": "ble-fan-overnight",
        "Comfort": "ble-fan-comfort",
        "Turbo": "ble-fan-turbo",
    }
    for cfg_name, cfg_id in fan_mode_configs.items():
        model_configurations.append(
            model_cfg(
                cfg_name,
                cfg_id,
                cfg("set_vent_v1", "int", min="1", max=str(fan_levels)),
            )
        )

    return Model(
        {
            "model_configurations": model_configurations,
            "model_name": model_name,
            "model_id": model_id,
            "sensor_set_type_id": sensor_set_type_id,
            "sensor_ids": [],
            "properties": [],
        },
        from_mocked_response=True,
    )


class MaestroBleController(MaestroControllerInterface):
    """Controller that speaks only to the local BLE panel."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        name: str | None = None,
        read_only: bool = False,
    ) -> None:
        """Initialise a BLE-only controller for one panel MAC.

        ``read_only`` is the diagnostic mode: reads keep feeding the entities,
        every command is rejected before anything reaches the stove.
        """
        self._hass = hass
        self._address = address
        self._name = name
        self._read_only = read_only
        self._transport = MczBleTransport(hass, address, name, read_only=read_only)
        self._unique_code: str | None = None

    @property
    def transport(self) -> MczBleTransport:
        """Return the underlying transport."""
        return self._transport

    @property
    def read_only(self) -> bool:
        """Return whether the diagnostic read-only mode is armed."""
        return self._read_only

    @property
    def is_authenticated(self) -> bool:
        """BLE-only has no cloud auth; always authenticated."""
        return True

    async def make_request(
        self,
        method: str,
        url: str,
        headers: dict | None = None,
        body=None,
        avoid_retries: bool = False,
    ):
        """No HTTP layer in BLE-only mode."""
        return None

    def _identity(self) -> dict[str, object]:
        """Return the identity fields used to populate ``Status``."""
        return {
            "unique_code": self._unique_code,
            "sm_nome_app": "Maestro BLE",
            "sm_vs_app": "",
            "mc_vs_app": "",
        }

    async def retrieve_linked_stove_infos(self) -> list[StoveInfo]:
        """Return a single :class:`StoveInfo` for the local panel.

        The warm-up is time-boxed: this runs inside ``async_setup_entry``, and a
        panel that is out of range or refusing connections would otherwise hold
        up Home Assistant's startup for minutes (the connector alone retries
        eleven times, and each unanswered register read costs its full timeout).
        Whatever is missing here is filled in by the next coordinator refresh.
        """
        try:
            async with asyncio.timeout(SETUP_WARMUP_TIMEOUT):
                if await self._transport.ensure_connected():
                    await self._transport.cap_scan()
                    await self._transport.poll_status_block()
        except TimeoutError:
            _LOGGER.debug(
                "BLE warm-up for %s exceeded %ss; continuing with what was read",
                self._address,
                SETUP_WARMUP_TIMEOUT,
            )

        serial = self._transport.serial
        slug = _mac_slug(self._address)
        self._unique_code = f"MCZ_{serial}" if serial else f"MCZ_{slug}"
        name = self._name or (f"MCZ {serial}" if serial else f"MCZ {self._address}")

        node = Node(None, from_mocked_response=True)
        node.id = self._address
        node.name = name
        node.model_id = BLE_MODEL_ID
        node.sensor_set_type_id = BLE_SENSOR_SET_TYPE_ID
        node.unique_code = self._unique_code
        node.mac_address_ble = self._address

        info = StoveInfo(None, from_mocked_response=True)
        info.node = node
        info.access_control = AccessControl(None, from_mocked_response=True)
        return [info]

    async def do_ping_for_stove(self, device_id: str, device_name: str) -> None:
        """Verify the BLE link is (or can be) up."""
        if not await self._transport.ensure_connected():
            raise MaestroConnectionException(
                f"BLE link down for device '{device_name}'"
            )

    async def get_stove_model_for_stove(self, model_id: str, device_name: str) -> Model:
        """Return the synthesised model built from the capability scan."""
        if not self._transport.caps and self._transport.connected:
            await self._transport.cap_scan()
        return build_synth_model(
            self._transport.caps, BLE_MODEL_ID, BLE_SENSOR_SET_TYPE_ID
        )

    async def get_stove_status_for_stove(
        self, device_id: str, device_name: str
    ) -> Status:
        """Return a :class:`Status` from the live register image."""
        link_ok = await self._transport.ensure_connected()
        if link_ok:
            await self._transport.poll_status_block()
        data = registers.build_status_dict(
            self._transport.regs, link_ok, self._transport.caps, self._identity()
        )
        return Status(data, from_mocked_response=True)

    async def get_stove_state_for_stove(
        self, device_id: str, device_name: str
    ) -> State:
        """Return a :class:`State` from the live register image.

        The status refresh (run concurrently) already polls the image, so no
        second poll is issued here.
        """
        link_ok = self._transport.connected
        data = registers.build_state_dict(self._transport.regs, link_ok)
        return State(data, from_mocked_response=True)

    async def activate_program_with_commands_for_stove(
        self,
        device_id: str,
        model_id: str,
        configuration_id: str,
        sensor_set_type_id: str,
        commands: list[ProgramCommand],
        callback_on_success=None,
    ) -> None:
        """Resolve each command to a register write over BLE.

        In read-only diagnostic mode nothing is attempted at all: the call fails
        before the link is even used, so no register — power included — can be
        touched by an entity, an automation or a script.
        """
        if self._read_only:
            _LOGGER.warning(
                "Read-only diagnostic mode: refusing %d command(s) for device %s",
                len(commands),
                device_id,
            )
            raise MaestroConnectionException(
                "read-only diagnostic mode: no command is sent to the stove"
            )

        if not await self._transport.ensure_connected():
            raise MaestroConnectionException("BLE link down; cannot send command")

        for command in commands:
            await self._apply_command(command.SensorId, command.Value)

        if callback_on_success is not None:
            callback_on_success()

    async def _apply_command(self, sensor_name: str | None, value: object) -> None:
        """Write a single logical command to its register."""
        mapping = registers.WRITE_MAP.get(sensor_name)
        if mapping is None and sensor_name == "com_on_off":
            mapping = registers.ONOFF_WRITE
        if mapping is None:
            raise MaestroConnectionException(
                f"no BLE register for sensor '{sensor_name}'"
            )
        reg, encoding, minv, maxv = mapping
        if encoding == "onoff_toggle":
            await self._set_onoff(value)
            return
        try:
            raw = registers.encode_write(encoding, value, minv, maxv)
        except (ValueError, TypeError) as err:
            raise MaestroConnectionException(
                f"cannot encode '{value}' for '{sensor_name}': {err}"
            ) from err
        await self._transport.write_reg(reg, raw)

    async def _set_onoff(self, value: object) -> None:
        """Bring the stove to the requested power state.

        ``0x038A`` is a toggle, not an absolute state: the coarse phase
        ``0x0322`` is read first and the button is only pressed when the stove
        is not already in the requested state, otherwise a redundant command
        would invert it.

        The read-only check is repeated here even though the caller already
        performs it: this is the one code path that can light a stove, so it
        does not rely on a guard placed elsewhere.
        """
        if self._read_only:
            raise MaestroConnectionException(
                "read-only diagnostic mode: refusing to press the power button"
            )
        desired_on = registers.decode_onoff(value)
        if desired_on is None:
            raise MaestroConnectionException(
                f"cannot interpret '{value}' as a power state"
            )
        try:
            regs = await self._transport.read_regs(registers.REG_PHASE, 1)
        except BleConnectionError as err:
            raise MaestroConnectionException(
                f"refusing to switch the stove without a fresh phase reading: {err}"
            ) from err
        phase = regs.get(registers.REG_PHASE)
        if not registers.is_known_phase(phase):
            raise MaestroConnectionException(
                f"refusing to switch the stove: unexpected phase {phase!r}"
            )
        if not registers.needs_onoff_toggle(phase, desired_on):
            _LOGGER.debug(
                "Stove already %s; not pressing power", "on" if desired_on else "off"
            )
            return
        await self._transport.write_reg(registers.REG_ONOFF, 1)
