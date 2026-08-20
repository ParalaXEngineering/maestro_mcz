"""Hybrid controller: cloud model + local BLE state and commands.

This is the primary mode. Identity and the model profile come from the cloud
(so the real two-fan RAY profile is exposed), while state and commands go over
the local BLE link whenever it is up, falling back to the cloud otherwise. A
command whose target register is unknown to the BLE map (second fan, alarm
reset, ...) is delegated to the cloud.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from ..controller.controller_interface import MaestroControllerInterface
from ..controller.maestro_controller import MaestroController
from ..controller.requests.activate_program import ProgramCommand
from ..controller.responses.model import Model
from ..controller.responses.state import State
from ..controller.responses.status import Status
from ..controller.responses.stove_info import StoveInfo
from . import registers
from .transport import MczBleTransport

_LOGGER = logging.getLogger(__name__)


def _overlay(target: State | Status, data: dict[str, object]) -> None:
    """Copy the non-``None`` values of ``data`` onto an existing dataclass.

    Only attributes that already exist on the target are written, so a stale
    mapping can never silently create a phantom field.
    """
    for key, value in data.items():
        if value is not None and hasattr(target, key):
            setattr(target, key, value)


class MaestroHybridController(MaestroControllerInterface):
    """Wrap a cloud controller and a BLE transport."""

    def __init__(
        self,
        hass: HomeAssistant,
        cloud: MaestroController,
        transport: MczBleTransport,
    ) -> None:
        """Initialise from an existing cloud controller and BLE transport."""
        self._hass = hass
        self._cloud = cloud
        self._transport = transport
        self._model: Model | None = None
        self._cloud_identity: dict[str, object] | None = None

    @property
    def transport(self) -> MczBleTransport:
        """Return the underlying BLE transport."""
        return self._transport

    @property
    def is_authenticated(self) -> bool:
        """Report usable as long as either transport works.

        ``MaestroStove.refresh`` turns this into the integration-connected flag
        that gates entity availability, so a cloud outage must not hide the
        entities while local BLE control is still answering.
        """
        return self._cloud.is_authenticated or self._transport.connected

    async def make_request(
        self,
        method: str,
        url: str,
        headers: dict | None = None,
        body=None,
        avoid_retries: bool = False,
    ):
        """Delegate raw requests to the cloud controller."""
        return await self._cloud.make_request(
            method, url, headers, body, avoid_retries
        )

    async def retrieve_linked_stove_infos(self) -> list[StoveInfo]:
        """Cloud provides the identity / stove list; BLE connect is best effort."""
        infos = await self._cloud.retrieve_linked_stove_infos()
        try:
            if await self._transport.ensure_connected():
                await self._transport.cap_scan()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("BLE warm-up during setup failed: %s", err)
        return infos

    async def do_ping_for_stove(self, device_id: str, device_name: str) -> None:
        """Ping the cloud, tolerating a cloud outage while the BLE link is up.

        A cloud failure must not mark the stove unavailable when local control
        still works — that would defeat the point of the local transport.
        """
        ble_up = await self._transport.ensure_connected()
        try:
            await self._cloud.do_ping_for_stove(device_id, device_name)
        except Exception as err:  # noqa: BLE001
            if not ble_up:
                raise
            _LOGGER.debug("Cloud ping failed but BLE is up, continuing: %s", err)

    async def get_stove_model_for_stove(self, model_id: str, device_name: str) -> Model:
        """Return the real cloud model (cached for command resolution)."""
        self._model = await self._cloud.get_stove_model_for_stove(
            model_id, device_name
        )
        return self._model

    async def get_stove_status_for_stove(
        self, device_id: str, device_name: str
    ) -> Status:
        """Return the cloud status overlaid with the fresher local BLE values.

        The cloud payload stays the base so every entity keeps a value, even the
        ones the BLE register map does not cover (second fan, eco, buzzer,
        maintenance counters). When the cloud is unreachable the BLE data alone
        is returned, and vice versa.
        """
        ble_data: dict[str, object] | None = None
        if await self._transport.ensure_connected():
            try:
                await self._transport.poll_status_block()
                ble_data = registers.build_status_dict(
                    self._transport.regs, True, self._transport.caps
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("BLE status read failed: %s", err)

        cloud_status: Status | None = None
        try:
            cloud_status = await self._cloud.get_stove_status_for_stove(
                device_id, device_name
            )
        except Exception as err:  # noqa: BLE001
            if ble_data is None:
                raise
            _LOGGER.debug("Cloud status unavailable, serving BLE only: %s", err)

        if cloud_status is not None:
            self._cache_cloud_identity(cloud_status)
            if ble_data:
                _overlay(cloud_status, ble_data)
            return cloud_status
        return Status(
            {**ble_data, **(self._cloud_identity or {})}, from_mocked_response=True
        )

    async def get_stove_state_for_stove(
        self, device_id: str, device_name: str
    ) -> State:
        """Return the cloud state overlaid with the fresher local BLE values."""
        ble_data: dict[str, object] | None = None
        if self._transport.connected:
            try:
                ble_data = registers.build_state_dict(self._transport.regs, True)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("BLE state build failed: %s", err)

        cloud_state: State | None = None
        try:
            cloud_state = await self._cloud.get_stove_state_for_stove(
                device_id, device_name
            )
        except Exception as err:  # noqa: BLE001
            if ble_data is None:
                raise
            _LOGGER.debug("Cloud state unavailable, serving BLE only: %s", err)

        if cloud_state is not None:
            if ble_data:
                _overlay(cloud_state, ble_data)
            return cloud_state
        return State(ble_data, from_mocked_response=True)

    def _cache_cloud_identity(self, status: Status) -> None:
        """Remember the cloud identity fields, used when only BLE is available."""
        self._cloud_identity = {
            "sm_sn": status.sm_sn,
            "sm_nome_app": status.sm_nome_app,
            "sm_vs_app": status.sm_vs_app,
            "mc_vs_app": status.mc_vs_app,
            "nome_banca_dati_sel": status.nome_banca_dati_sel,
        }

    async def activate_program_with_commands_for_stove(
        self,
        device_id: str,
        model_id: str,
        configuration_id: str,
        sensor_set_type_id: str,
        commands: list[ProgramCommand],
        callback_on_success=None,
    ) -> None:
        """Route commands to BLE when every one maps to a register, else cloud.

        Power is never in this path: it stays a cloud command (see
        ``registers.ONOFF_WRITE``). Once a register has actually been written the
        call never falls back to the cloud, because replaying an already-applied
        command would double-apply it.
        """
        resolved = (
            self._resolve_commands(commands) if self._transport.connected else None
        )
        if resolved is not None:
            written = False
            try:
                for reg, encoding, minv, maxv, value in resolved:
                    raw = registers.encode_write(encoding, value, minv, maxv)
                    await self._transport.write_reg(reg, raw)
                    written = True
                if callback_on_success is not None:
                    callback_on_success()
                return
            except Exception as err:  # noqa: BLE001
                if written:
                    _LOGGER.warning(
                        "BLE command partially applied, not replaying on cloud: %s", err
                    )
                    raise
                _LOGGER.debug("BLE command failed before any write, using cloud: %s", err)

        await self._cloud.activate_program_with_commands_for_stove(
            device_id,
            model_id,
            configuration_id,
            sensor_set_type_id,
            commands,
            callback_on_success,
        )

    def _resolve_commands(
        self, commands: list[ProgramCommand]
    ) -> list[tuple] | None:
        """Resolve every command to a BLE write, or ``None`` if any is unmapped."""
        if self._model is None:
            return None
        resolved: list[tuple] = []
        for command in commands:
            sensor_name = self._sensor_name_for_id(command.SensorId)
            mapping = registers.WRITE_MAP.get(sensor_name) if sensor_name else None
            if mapping is None:
                return None
            reg, encoding, minv, maxv = mapping
            try:
                registers.encode_write(encoding, command.Value, minv, maxv)
            except (ValueError, TypeError):
                return None
            resolved.append((reg, encoding, minv, maxv, command.Value))
        return resolved

    def _sensor_name_for_id(self, sensor_id: str | None) -> str | None:
        """Look up a cloud sensor_id and return its logical sensor_name."""
        if self._model is None or self._model.model_configurations is None:
            return None
        for model_configuration in self._model.model_configurations:
            if model_configuration.configurations is None:
                continue
            for configuration in model_configuration.configurations:
                if configuration.sensor_id == sensor_id:
                    return configuration.sensor_name
        return None

