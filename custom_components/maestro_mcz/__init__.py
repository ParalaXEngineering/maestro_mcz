"""The maestro_mcz integration."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from pathlib import Path

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry, ConfigFlowContext
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .config_flow import CONF_POLLING_INTERVAL
from .const import (
    CONF_MAC,
    CONF_TRANSPORT,
    DEFAULT_POLLING_INTERVAL,
    DOMAIN,
    MOCKED_FOLDER,
    TRANSPORT_BLE,
    TRANSPORT_CLOUD,
    TRANSPORT_HYBRID,
)
from .maestro import MaestroStove
from .maestro.ble import (
    MaestroBleController,
    MaestroHybridController,
    MczBleTransport,
)
from .maestro.controller.controller_interface import MaestroControllerInterface
from .maestro.controller.maestro_controller import (
    MaestroAuthenticationException,
    MaestroConnectionException,
    MaestroController,
)
from .maestro.controller.mocked_controller import MockedController

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
        if transport_mode == TRANSPORT_BLE:
            maestroapi = MaestroBleController(hass, entry.data[CONF_MAC], entry.title)
        elif transport_mode == TRANSPORT_HYBRID:
            session = async_get_clientsession(hass)
            cloud_controller = MaestroController(
                session,
                entry.data[CONF_USERNAME],
                entry.data[CONF_PASSWORD],
            )
            transport = MczBleTransport(hass, entry.data[CONF_MAC], entry.title)
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

    # 7. Set up all platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


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
