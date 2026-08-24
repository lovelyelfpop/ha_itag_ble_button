import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.const import CONF_MAC, CONF_NAME

from .const import DOMAIN

class ITagConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_MAC].upper())
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=user_input.get(CONF_NAME, user_input[CONF_MAC]),
                data=user_input
            )

        scanned_devices = bluetooth.async_discovered_service_info(self.hass)
        device_choices = {
            service_info.address: f"{service_info.name or '未知设备'} ({service_info.address})"
            for service_info in scanned_devices
        }

        data_schema = vol.Schema({
            vol.Required(CONF_MAC): vol.In(device_choices) if device_choices else str,
            vol.Optional(CONF_NAME, default="iTag 钥匙扣"): str,
        })

        return self.async_show_form(step_id="user", data_schema=data_schema)