from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, MODE_SILENT, MODE_CLICK_BEEP, MODE_FIND

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    handler = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ITagModeSelect(handler)])

class ITagModeSelect(SelectEntity):
    def __init__(self, handler):
        self._handler = handler
        self._attr_name = "钥匙扣工作模式"
        self._attr_unique_id = f"{handler.mac}_work_mode"
        self._attr_icon = "mdi:tune-vertical"
        self._attr_options = [MODE_SILENT, MODE_CLICK_BEEP, MODE_FIND]
        self._attr_current_option = MODE_CLICK_BEEP
        # 绑定到同一个设备
        self._attr_device_info = handler.device_info

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        await self._handler.set_work_mode(option)
        self.async_write_ha_state()