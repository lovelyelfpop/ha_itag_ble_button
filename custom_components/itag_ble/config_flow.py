import re
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.const import CONF_MAC, CONF_NAME

from .const import DOMAIN, UUID_FFE0_SERVICE

# 依据 iSearching.apk 逆向：iTag 通过 FFE0 服务 UUID 广播
_FFE0_SHORT = UUID_FFE0_SERVICE.split("-")[0].lstrip("0").lower()
_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

class ITagConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        """入口：提供『扫描选择』与『手动输入』两种方式"""
        return self.async_show_menu(
            step_id="user",
            menu_options=["scan", "manual"],
        )

    async def async_step_scan(self, user_input=None):
        """从扫描到的 iTag（广播 FFE0 服务）中选择设备"""
        if user_input is not None:
            mac = user_input[CONF_MAC]
            await self.async_set_unique_id(mac.upper())
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=user_input.get(CONF_NAME, mac),
                data={CONF_MAC: mac.upper(), CONF_NAME: user_input.get(CONF_NAME, mac)},
            )

        scanned_devices = bluetooth.async_discovered_service_info(self.hass)
        itag_devices = {}
        other_devices = {}
        for service_info in scanned_devices:
            uuid_list = {u.uuid.lower() for u in (service_info.service_uuids or [])}
            is_itag = any(
                u.lstrip("0") == _FFE0_SHORT or u == UUID_FFE0_SERVICE.lower()
                for u in uuid_list
            )
            label = f"{service_info.name or '未知设备'} ({service_info.address})"
            if is_itag:
                itag_devices[service_info.address] = label
            else:
                other_devices[service_info.address] = label

        choices = dict(itag_devices)
        choices.update(other_devices)

        data_schema = vol.Schema({
            vol.Required(CONF_MAC): vol.In(choices) if choices else str,
            vol.Optional(CONF_NAME, default="iTag 钥匙扣"): str,
        })

        errors = {}
        if not choices:
            errors["base"] = "no_devices"

        return self.async_show_form(
            step_id="scan",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_manual(self, user_input=None):
        """手动输入 MAC 地址（设备暂时不在广播中也可添加）"""
        if user_input is not None:
            mac = user_input[CONF_MAC].strip().upper()
            if not _MAC_RE.match(mac):
                return self.async_show_form(
                    step_id="manual",
                    data_schema=self._manual_schema(),
                    errors={"base": "invalid_mac"},
                )
            await self.async_set_unique_id(mac)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=user_input.get(CONF_NAME, mac),
                data={CONF_MAC: mac, CONF_NAME: user_input.get(CONF_NAME, mac)},
            )

        return self.async_show_form(
            step_id="manual",
            data_schema=self._manual_schema(),
        )

    @staticmethod
    def _manual_schema():
        return vol.Schema({
            vol.Required(CONF_MAC): str,
            vol.Optional(CONF_NAME, default="iTag 钥匙扣"): str,
        })
