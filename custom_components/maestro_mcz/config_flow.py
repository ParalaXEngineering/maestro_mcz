"""Config flow for maestro_mcz integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import format_mac

from .const import (
    BLE_LOCAL_NAME_PREFIX,
    CONF_MAC,
    CONF_READ_ONLY,
    CONF_TRANSPORT,
    DEFAULT_POLLING_INTERVAL,
    DEFAULT_READ_ONLY,
    DEFAULT_READ_ONLY_NEW_BLE_ENTRY,
    DOMAIN,
    TRANSPORT_BLE,
    TRANSPORT_CLOUD,
    TRANSPORT_HYBRID,
)
from .maestro.controller.controller_interface import MaestroControllerInterface
from .maestro.controller.maestro_controller import (
    MaestroAuthenticationException,
    MaestroConnectionException,
    MaestroController,
)

_LOGGER = logging.getLogger(__name__)
CONF_POLLING_INTERVAL = "polling_interval"
STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """
    session = async_get_clientsession(hass)
    controller: MaestroControllerInterface = MaestroController(
        session, data[CONF_USERNAME], data[CONF_PASSWORD]
    )
    try:
        await controller.retrieve_linked_stove_infos()

    except MaestroAuthenticationException as exc:
        raise InvalidAuth from exc
    except MaestroConnectionException as exc:
        raise CannotConnect from exc

    # Return info that you want to store in the config entry.
    return {"title": data[CONF_USERNAME]}


class MCZConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for maestro_mcz."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        self._discovered_mac: str | None = None
        self._discovered_name: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user pick a transport."""
        return self.async_show_menu(
            step_id="user",
            menu_options=[TRANSPORT_CLOUD, "bluetooth_pick", TRANSPORT_HYBRID],
        )

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the cloud-only setup (username / password)."""
        if user_input is None:
            return self.async_show_form(
                step_id=TRANSPORT_CLOUD, data_schema=STEP_USER_DATA_SCHEMA
            )

        errors: dict[str, str] = {}
        try:
            info = await validate_input(self.hass, user_input)
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"
        else:
            await self.async_set_unique_id(user_input[CONF_USERNAME])
            self._abort_if_unique_id_configured()
            data = {**user_input, CONF_TRANSPORT: TRANSPORT_CLOUD}
            return self.async_create_entry(title=info["title"], data=data)

        return self.async_show_form(
            step_id=TRANSPORT_CLOUD, data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    def _discovered_ble_devices(self) -> dict[str, str]:
        """Return {mac: label} of MCZ panels seen by the Bluetooth stack."""
        devices: dict[str, str] = {}
        for info in async_discovered_service_info(self.hass, connectable=True):
            name = info.name or ""
            if name.startswith(BLE_LOCAL_NAME_PREFIX):
                devices[info.address] = f"{name} ({info.address})"
        return devices

    def _mac_already_configured(self, mac: str) -> bool:
        """Return whether an entry already drives this panel.

        A panel accepts a single BLE connection, so a second entry pointing at
        the same MAC would have two coordinators fighting over the link.
        """
        wanted = format_mac(mac)
        return any(
            entry.data.get(CONF_MAC) and format_mac(entry.data[CONF_MAC]) == wanted
            for entry in self._async_current_entries()
        )

    async def async_step_bluetooth_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pick a BLE MAC among the discovered panels, or type one in.

        A stove already paired to the MCZ app advertises directed to that phone,
        so it may well be invisible to the scanner until the panel is put in
        pairing mode — hence the free-text fallback instead of a dead end.
        """
        devices = self._discovered_ble_devices()
        errors: dict[str, str] = {}

        if user_input is not None:
            mac = user_input[CONF_MAC].strip()
            if not mac:
                errors[CONF_MAC] = "invalid_mac"
            elif self._mac_already_configured(mac):
                return self.async_abort(reason="already_configured")
            else:
                self._discovered_mac = mac
                self._discovered_name = devices.get(mac)
                return await self.async_step_pairing()

        if devices:
            schema = vol.Schema({vol.Required(CONF_MAC): vol.In(devices)})
        else:
            schema = vol.Schema({vol.Required(CONF_MAC): str})

        return self.async_show_form(
            step_id="bluetooth_pick", data_schema=schema, errors=errors
        )

    async def async_step_pairing(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show the pairing instructions and create the BLE entry.

        The read-only box is ticked by default: a brand-new local link should
        first be proven on reads. Unticking it is a deliberate act, and the
        choice stays editable afterwards through the options flow.
        """
        if user_input is not None:
            if self._mac_already_configured(self._discovered_mac):
                return self.async_abort(reason="already_configured")
            await self.async_set_unique_id(format_mac(self._discovered_mac))
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=self._discovered_name or f"MCZ {self._discovered_mac}",
                data={
                    CONF_TRANSPORT: TRANSPORT_BLE,
                    CONF_MAC: self._discovered_mac,
                    CONF_READ_ONLY: user_input.get(
                        CONF_READ_ONLY, DEFAULT_READ_ONLY_NEW_BLE_ENTRY
                    ),
                },
            )

        return self.async_show_form(
            step_id="pairing",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_READ_ONLY, default=DEFAULT_READ_ONLY_NEW_BLE_ENTRY
                    ): bool
                }
            ),
            description_placeholders={"mac": self._discovered_mac or ""},
        )

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a BLE panel discovered by Home Assistant's Bluetooth stack."""
        await self.async_set_unique_id(format_mac(discovery_info.address))
        self._abort_if_unique_id_configured()
        # A hybrid/cloud entry keys on the account, not the MAC, so the unique-id
        # check above cannot see it; without this the same panel could be set up
        # twice and the two entries would fight over its single BLE connection.
        if self._mac_already_configured(discovery_info.address):
            return self.async_abort(reason="already_configured")
        self._discovered_mac = discovery_info.address
        self._discovered_name = f"{discovery_info.name} ({discovery_info.address})"
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_pairing()

    async def async_step_hybrid(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the hybrid setup: cloud credentials + a BLE MAC."""
        devices = self._discovered_ble_devices()
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(user_input[CONF_USERNAME])
                self._abort_if_unique_id_configured()
                data = {
                    CONF_USERNAME: user_input[CONF_USERNAME],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_MAC: user_input[CONF_MAC],
                    CONF_TRANSPORT: TRANSPORT_HYBRID,
                }
                return self.async_create_entry(title=info["title"], data=data)

        schema: dict[Any, Any] = {
            vol.Required(CONF_USERNAME): str,
            vol.Required(CONF_PASSWORD): str,
        }
        if devices:
            schema[vol.Required(CONF_MAC)] = vol.In(devices)
        else:
            schema[vol.Required(CONF_MAC)] = str
        return self.async_show_form(
            step_id=TRANSPORT_HYBRID,
            data_schema=vol.Schema(schema),
            errors=errors,
        )

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle a flow initiated by the async_setup code."""

        # 1. Set a unique ID based on something static (IP, MAC, or a fixed string)
        await self.async_set_unique_id(import_data[CONF_HOST])

        # 2. This helper immediately stops the flow if the ID is already in HA
        self._abort_if_unique_id_configured()

        # 3. If not already there, create the entry
        return self.async_create_entry(
            title=f"Hub ({import_data[CONF_HOST]})", data=import_data
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlow):
    """Handle a option flow for MCZ."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle options flow.

        ``read_only`` is only offered when a local link exists: on a cloud-only
        entry it would have nothing to lock, and showing a "no command reaches
        the stove" switch that does nothing is worse than not showing it.
        """
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        polling_interval = self.config_entry.options.get(
            CONF_POLLING_INTERVAL, DEFAULT_POLLING_INTERVAL
        )
        read_only = self.config_entry.options.get(
            CONF_READ_ONLY,
            self.config_entry.data.get(CONF_READ_ONLY, DEFAULT_READ_ONLY),
        )

        base_schema = {
            vol.Optional(CONF_POLLING_INTERVAL, default=polling_interval): vol.All(
                vol.Coerce(int), vol.Clamp(min=DEFAULT_POLLING_INTERVAL, max=300)
            ),
        }
        if self.config_entry.data.get(CONF_TRANSPORT) in (
            TRANSPORT_BLE,
            TRANSPORT_HYBRID,
        ):
            base_schema[vol.Optional(CONF_READ_ONLY, default=read_only)] = bool

        return self.async_show_form(step_id="init", data_schema=vol.Schema(base_schema))


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
