"""The maestro_mcz integration."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import functools
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry, ConfigFlowContext
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .config_flow import CONF_POLLING_INTERVAL
from .const import (
    ATTR_COUNT,
    ATTR_END,
    ATTR_ENTRY_ID,
    ATTR_START,
    CONF_MAC,
    CONF_READ_ONLY,
    CONF_TRANSPORT,
    DEFAULT_POLLING_INTERVAL,
    DEFAULT_READ_ONLY,
    DOMAIN,
    DUMP_BLOCK_SIZE,
    DUMP_MAX_REGISTERS,
    MAX_READ_COUNT,
    MOCKED_FOLDER,
    SERVICE_BLE_DUMP,
    SERVICE_BLE_PROBE_FANS,
    SERVICE_BLE_READ_REGISTERS,
    TRANSPORT_BLE,
    TRANSPORT_CLOUD,
    TRANSPORT_HYBRID,
)
from .maestro import MaestroStove
from .maestro.ble import (
    BleConnectionError,
    BleReadTimeout,
    MaestroBleController,
    MaestroHybridController,
    MczBleTransport,
)
from .maestro.ble.registers import SENTINEL16
from .maestro.controller.controller_interface import MaestroControllerInterface
from .maestro.controller.maestro_controller import (
    MaestroAuthenticationException,
    MaestroConnectionException,
    MaestroController,
)
from .maestro.controller.mocked_controller import MockedController
from .maestro.controller.responses.status import Status

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.DATETIME,
    Platform.FAN,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType):
    """Set up the integration from code."""

    # create a new hub when there are mocked files
    mocked_folder = await _has_mocked_folder()

    if mocked_folder is not None:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,  # Required: specifies which integration's flow to trigger
                context=ConfigFlowContext(source=SOURCE_IMPORT),
                data={
                    MOCKED_FOLDER: mocked_folder,
                    CONF_HOST: "MockedHost",
                    CONF_USERNAME: "DummyUsername",
                    CONF_PASSWORD: "DummyPassword",
                },
            )
        )
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Set up maestro_mcz from a config entry."""

    # 1. Check if we have a mocked folder
    mocked_folder = entry.data.get(MOCKED_FOLDER, None)

    # 2. Create the api / controller to use for the coordinator
    #    if mocked_folder is not None it means we want to use mocked data instead of connecting to the real API
    if mocked_folder is not None:
        maestroapi: MaestroControllerInterface = MockedController(mocked_folder)
    else:
        transport_mode = entry.data.get(CONF_TRANSPORT, TRANSPORT_CLOUD)
        read_only = _read_only_for_entry(entry)
        if transport_mode == TRANSPORT_BLE:
            maestroapi = MaestroBleController(
                hass, entry.data[CONF_MAC], entry.title, read_only=read_only
            )
        elif transport_mode == TRANSPORT_HYBRID:
            session = async_get_clientsession(hass)
            cloud_controller = MaestroController(
                session,
                entry.data[CONF_USERNAME],
                entry.data[CONF_PASSWORD],
            )
            transport = MczBleTransport(
                hass, entry.data[CONF_MAC], entry.title, read_only=read_only
            )
            maestroapi = MaestroHybridController(hass, cloud_controller, transport)
        else:
            session = async_get_clientsession(hass)
            maestroapi = MaestroController(
                session,
                entry.data[CONF_USERNAME],
                entry.data[CONF_PASSWORD],
            )

    # 3. Get all stoves stove linked to the account
    try:
        stove_infos = await maestroapi.retrieve_linked_stove_infos()
    except MaestroAuthenticationException as exception:
        _LOGGER.error("Authentication failed: %s", exception)
        return False
    except MaestroConnectionException as exception:
        _LOGGER.error("Connection failed: %s", exception)
        return False

    # 4. Create a coordinator for the account
    coordinator: MczAccountCoordinator = MczAccountCoordinator(
        hass,
        entry,
        name=f"{CONF_USERNAME}",
        stoves=[MaestroStove(maestroapi, stove_info) for stove_info in stove_infos]
        if stove_infos
        else None,
        controller=maestroapi,
    )
    await coordinator.async_config_entry_first_refresh()

    # 5. Store the coordinator in the entry so that they can be accessed by the platforms
    entry.runtime_data = coordinator

    # 6. Add an update listener to handle options updates
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # 6b. Tear down the BLE link (if any) when the entry unloads
    transport = getattr(maestroapi, "transport", None)
    if transport is not None:
        entry.async_on_unload(transport.disconnect)

    # 6c. Register the diagnostic services (idempotent, shared by all entries)
    _async_register_services(hass)

    # 7. Set up all platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and not _other_entries_remaining(hass, entry):
        _async_unregister_services(hass)
    return unload_ok


def _other_entries_remaining(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Return whether another config entry of this domain is still around."""
    return any(
        other.entry_id != entry.entry_id
        for other in hass.config_entries.async_entries(DOMAIN)
    )


def _read_only_for_entry(entry: ConfigEntry) -> bool:
    """Return the effective read-only flag for a config entry.

    Options win over data so the mode can be flipped after setup; an entry
    created before this option existed has neither and keeps the previous
    behaviour (writes allowed).
    """
    return bool(
        entry.options.get(
            CONF_READ_ONLY, entry.data.get(CONF_READ_ONLY, DEFAULT_READ_ONLY)
        )
    )


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _has_mocked_folder() -> str | None:
    """Check if there is a mocked folder."""
    mocked_folder = Path("config/custom_components/maestro_mcz/mocked")
    if mocked_folder.is_dir():
        return str(mocked_folder)
    return None


class MczAccountCoordinator(DataUpdateCoordinator):
    """MCZ Coordinator."""

    _stoves: dict[str, MaestroStove] | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        name: str,
        stoves: list[MaestroStove] | None,
        controller: MaestroControllerInterface | None = None,
    ) -> None:
        """Initialize coordinator."""
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            config_entry=config_entry,
            name=name,
            update_interval=timedelta(
                seconds=config_entry.options.get(
                    CONF_POLLING_INTERVAL, DEFAULT_POLLING_INTERVAL
                )
            ),
        )
        self._stoves = {stove.UniqueCode: stove for stove in stoves} if stoves else None
        self._controller = controller

    async def _async_setup(self):
        """Set up the coordinator."""
        if self._stoves is None:
            raise UpdateFailed("No stoves found for this account.")
        for stove in self._stoves.values():
            try:
                await stove.async_init()
            except MaestroConnectionException as err:
                raise UpdateFailed(
                    f"Error communicating with Maestro API: {err}"
                ) from err

    async def _async_update_data(self):
        """Fetch data from API endpoint."""
        if self._stoves is None:
            raise UpdateFailed("No stoves found for this account.")

        for stove in self._stoves.values():
            try:
                await stove.refresh()
            except MaestroAuthenticationException as err:
                # Cancels future updates & Triggers re-auth flow
                raise ConfigEntryAuthFailed from err
            except MaestroConnectionException as err:
                # This marks entities as unavailable and schedules a retry
                raise UpdateFailed(
                    f"Error communicating with Maestro API: {err}"
                ) from err

    async def update_data_after_set(
        self,
    ):  # should be revised in the future to be more efficient
        """Force refresh of data from API endpoint after a SET was executed."""
        # we need to wait here because there is an actual delay between sending a SET and receiving the updated value from the polled MCZ database
        await asyncio.sleep(3)
        await self.async_refresh()
        await asyncio.sleep(3)
        await self.async_refresh()

    @property
    def stoves(self) -> dict[str, MaestroStove]:
        """Return the stoves."""
        return self._stoves

    @property
    def controller(self) -> MaestroControllerInterface | None:
        """Return the controller driving this entry (diagnostics use it)."""
        return self._controller


# --------------------------------------------------------------------------- #
# Diagnostic services
#
# All three are strictly read-only (Modbus function 0x03 only) and therefore
# work — by design — while the entry is in read-only diagnostic mode. They exist
# to map registers the vendor never documented, in particular the gap right
# after the first room fan where a second fan is suspected.


def _register_address(value: Any) -> int:
    """Coerce a register address given as an int or as a string.

    Accepts ``1018``, ``"1018"``, ``"0x03FA"`` and the bare ``"03FA"`` form the
    register map is written in throughout this integration.
    """
    if isinstance(value, bool):
        raise vol.Invalid("a register address must be a number, not a boolean")
    if isinstance(value, int):
        reg = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise vol.Invalid("empty register address")
        try:
            # int(text, 0) honours the 0x / 0o / 0b prefixes and plain decimal.
            reg = int(text, 0)
        except ValueError:
            try:
                reg = int(text, 16)
            except ValueError as err:
                raise vol.Invalid(
                    f"'{value}' is not a valid register address"
                ) from err
    else:
        raise vol.Invalid(f"'{value}' is not a valid register address")
    if not 0 <= reg <= 0xFFFF:
        raise vol.Invalid(f"register address 0x{reg:X} is outside 0x0000..0xFFFF")
    return reg


SERVICE_BLE_READ_REGISTERS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_START): _register_address,
        vol.Optional(ATTR_COUNT, default=1): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_READ_COUNT)
        ),
        vol.Optional(ATTR_ENTRY_ID): cv.string,
    }
)

SERVICE_BLE_PROBE_FANS_SCHEMA = vol.Schema(
    {vol.Optional(ATTR_ENTRY_ID): cv.string}
)

SERVICE_BLE_DUMP_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_START): _register_address,
        vol.Required(ATTR_END): _register_address,
        vol.Optional(ATTR_ENTRY_ID): cv.string,
    }
)

# Candidate blocks read by ble_probe_fans, as (base, count).
PROBE_READS: tuple[tuple[int, int], ...] = (
    (0x03F7, 6),  # 0x03F7..0x03FC: setpoints then fan setpoints (hypothesis)
    (0x0324, 3),  # 0x0324..0x0326: live fan level, then v2 / v3 (hypothesis)
    (0x05FD, 9),  # 0x05FD..0x0605: three 3-register speed tables (hypothesis)
)

# Candidate register -> (cloud Status attribute, divisor applied to the raw
# value before comparing). Registers with no entry are read but have nothing to
# be compared against.
PROBE_CLOUD_FIELDS: dict[int, tuple[str, int]] = {
    0x03F7: ("set_amb1", 10),
    0x03F8: ("set_amb2", 10),
    0x03F9: ("set_amb3", 10),
    0x03FA: ("set_vent_v1", 1),
    0x03FB: ("set_vent_v2", 1),
    0x03FC: ("set_vent_v3", 1),
    0x0324: ("index_vel_v1", 1),
    0x0325: ("index_vel_v2", 1),
    0x0326: ("index_vel_v3", 1),
    0x05FD: ("v_ven1_v0", 1),
    0x0600: ("v_ven2_v0", 1),
    0x0603: ("v_ven3_v0", 1),
}


def _register_report(reg: int, raw: int) -> dict[str, Any]:
    """Describe one register value under every plausible interpretation."""
    report: dict[str, Any] = {
        "reg": f"0x{reg:04X}",
        "dec": reg,
        "raw": raw,
        "hex": f"0x{raw:04X}",
        "as_div10": round(raw / 10.0, 1),
        "as_signed": raw - 0x10000 if raw & 0x8000 else raw,
    }
    text = "".join(chr(byte) for byte in (raw >> 8, raw & 0xFF) if 32 <= byte < 127)
    if len(text) == 2:
        report["ascii"] = text
    return report


def _resolve_target(
    hass: HomeAssistant, entry_id: str | None
) -> tuple[ConfigEntry, MczAccountCoordinator, MczBleTransport]:
    """Find the config entry to probe and its BLE transport.

    With no ``entry_id`` the first loaded entry that owns a BLE transport wins;
    cloud-only and mocked entries are skipped rather than reported as an error.
    """
    if entry_id:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(
                f"no {DOMAIN} config entry with id '{entry_id}'"
            )
        candidates = [entry]
    else:
        candidates = list(hass.config_entries.async_entries(DOMAIN))

    for candidate in candidates:
        coordinator = getattr(candidate, "runtime_data", None)
        if not isinstance(coordinator, MczAccountCoordinator):
            continue
        transport = getattr(coordinator.controller, "transport", None)
        if isinstance(transport, MczBleTransport):
            return candidate, coordinator, transport

    if entry_id:
        raise ServiceValidationError(
            f"config entry '{entry_id}' has no Bluetooth transport"
            " (cloud-only, mocked, or not loaded)"
        )
    raise ServiceValidationError(
        f"no loaded {DOMAIN} config entry with a Bluetooth transport was found"
    )


async def _async_ensure_link(transport: MczBleTransport) -> None:
    """Bring the BLE link up, or fail with a message the user can act on."""
    if transport.connected:
        return
    if not await transport.ensure_connected():
        raise ServiceValidationError("BLE link is down")


async def _async_read_block(
    transport: MczBleTransport, base: int, count: int
) -> dict[int, int] | None:
    """Read one block, returning ``None`` when the panel does not answer."""
    try:
        return await transport.read_regs(base, count)
    except BleReadTimeout:
        _LOGGER.debug("No answer for block 0x%04X x%d", base, count)
        return None
    except BleConnectionError as err:
        raise HomeAssistantError(
            f"BLE read of 0x{base:04X} x{count} failed: {err}"
        ) from err


async def _async_cloud_status(
    coordinator: MczAccountCoordinator,
) -> Status | None:
    """Fetch a fresh *cloud* status, or ``None`` when there is no cloud.

    The hybrid controller's own status is the cloud payload already overlaid
    with the BLE values, so comparing against it would compare BLE with itself.
    """
    cloud = getattr(coordinator.controller, "cloud", None)
    stoves = coordinator.stoves
    if cloud is None or not stoves:
        return None
    stove = next(iter(stoves.values()))
    try:
        return await cloud.get_stove_status_for_stove(stove.Id, stove.Name)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Cloud status unavailable for the probe: %s", err)
        return None


def _cloud_match(raw: int, divisor: int, cloud_value: object) -> bool | None:
    """Compare a raw register with a cloud value, ``None`` when not comparable."""
    if cloud_value is None or isinstance(cloud_value, bool):
        return None
    try:
        expected = float(cloud_value)
    except (TypeError, ValueError):
        return None
    return abs(raw / divisor - expected) < 0.05


async def _async_ble_read_registers(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Read a register range and return every plausible interpretation."""
    start: int = call.data[ATTR_START]
    count: int = call.data[ATTR_COUNT]
    entry, _coordinator, transport = _resolve_target(
        hass, call.data.get(ATTR_ENTRY_ID)
    )
    await _async_ensure_link(transport)

    values = await _async_read_block(transport, start, count)
    if values is None:
        raise HomeAssistantError(
            f"no answer from the panel reading 0x{start:04X} x{count}"
        )
    return {
        "entry_id": entry.entry_id,
        "address": transport.address,
        "read_only": transport.read_only,
        "start": f"0x{start:04X}",
        "count": count,
        "registers": [_register_report(reg, values[reg]) for reg in sorted(values)],
    }


async def _async_ble_probe_fans(
    hass: HomeAssistant, call: ServiceCall
) -> ServiceResponse:
    """Read the fan-register candidates and confront them with the cloud."""
    entry, coordinator, transport = _resolve_target(
        hass, call.data.get(ATTR_ENTRY_ID)
    )
    await _async_ensure_link(transport)

    values: dict[int, int] = {}
    unanswered: list[str] = []
    for base, count in PROBE_READS:
        block = await _async_read_block(transport, base, count)
        if block is None:
            unanswered.append(f"0x{base:04X}-0x{base + count - 1:04X}")
            continue
        values.update(block)

    cloud_status = await _async_cloud_status(coordinator)

    candidates: list[dict[str, Any]] = []
    confirmed: list[str] = []
    refuted: list[str] = []
    for base, count in PROBE_READS:
        for reg in range(base, base + count):
            field, divisor = PROBE_CLOUD_FIELDS.get(reg, (None, 1))
            raw = values.get(reg)
            if raw is None:
                item: dict[str, Any] = {
                    "reg": f"0x{reg:04X}",
                    "dec": reg,
                    "raw": None,
                    "answered": False,
                }
            else:
                item = {**_register_report(reg, raw), "answered": True}
            item["cloud_field"] = field
            cloud_value = (
                getattr(cloud_status, field, None)
                if cloud_status is not None and field
                else None
            )
            item["cloud"] = cloud_value
            match = (
                _cloud_match(raw, divisor, cloud_value) if raw is not None else None
            )
            item["match"] = match
            if field and match is True:
                confirmed.append(f"0x{reg:04X}={field}")
            elif field and match is False:
                refuted.append(f"0x{reg:04X}!={field}")
            candidates.append(item)

    return {
        "entry_id": entry.entry_id,
        "address": transport.address,
        "read_only": transport.read_only,
        "cloud_available": cloud_status is not None,
        "unanswered_blocks": unanswered,
        "confirmed": confirmed,
        "refuted": refuted,
        "candidates": candidates,
    }


async def _async_ble_dump(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Sweep a register range and report only the registers that carry data."""
    start: int = call.data[ATTR_START]
    end: int = call.data[ATTR_END]
    if end < start:
        raise ServiceValidationError(
            f"'end' (0x{end:04X}) is below 'start' (0x{start:04X})"
        )
    total = end - start + 1
    if total > DUMP_MAX_REGISTERS:
        raise ServiceValidationError(
            f"range too wide: {total} registers requested, at most"
            f" {DUMP_MAX_REGISTERS} per call"
        )

    entry, _coordinator, transport = _resolve_target(
        hass, call.data.get(ATTR_ENTRY_ID)
    )
    await _async_ensure_link(transport)

    found: list[dict[str, Any]] = []
    skipped: list[str] = []
    for base in range(start, end + 1, DUMP_BLOCK_SIZE):
        count = min(DUMP_BLOCK_SIZE, end - base + 1)
        block = await _async_read_block(transport, base, count)
        if block is None:
            skipped.append(f"0x{base:04X}-0x{base + count - 1:04X}")
            continue
        for reg in sorted(block):
            raw = block[reg]
            # 0 and 0xFFFF are the "nothing here" values of this firmware.
            if raw in (0, SENTINEL16):
                continue
            found.append(_register_report(reg, raw))

    return {
        "entry_id": entry.entry_id,
        "address": transport.address,
        "read_only": transport.read_only,
        "start": f"0x{start:04X}",
        "end": f"0x{end:04X}",
        "scanned": total,
        "skipped_blocks": skipped,
        "registers": found,
    }


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the diagnostic services once for the whole domain."""
    if hass.services.has_service(DOMAIN, SERVICE_BLE_READ_REGISTERS):
        return
    for service, handler, schema in (
        (
            SERVICE_BLE_READ_REGISTERS,
            _async_ble_read_registers,
            SERVICE_BLE_READ_REGISTERS_SCHEMA,
        ),
        (SERVICE_BLE_PROBE_FANS, _async_ble_probe_fans, SERVICE_BLE_PROBE_FANS_SCHEMA),
        (SERVICE_BLE_DUMP, _async_ble_dump, SERVICE_BLE_DUMP_SCHEMA),
    ):
        hass.services.async_register(
            DOMAIN,
            service,
            functools.partial(handler, hass),
            schema=schema,
            supports_response=SupportsResponse.ONLY,
        )


def _async_unregister_services(hass: HomeAssistant) -> None:
    """Remove the diagnostic services (last entry unloaded)."""
    for service in (
        SERVICE_BLE_READ_REGISTERS,
        SERVICE_BLE_PROBE_FANS,
        SERVICE_BLE_DUMP,
    ):
        hass.services.async_remove(DOMAIN, service)
