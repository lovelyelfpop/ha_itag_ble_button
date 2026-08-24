from homeassistant.components.event import EventEntity, EventDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    handler = hass.data[DOMAIN][entry.entry_id]
    
    single_click_event = ITagClickEvent(handler, "single", "iTag 单击")
    double_click_event = ITagClickEvent(handler, "double", "iTag 双击")
    
    handler.event_entities = {
        "single": single_click_event,
        "double": double_click_event,
    }
    
    async_add_entities([single_click_event, double_click_event])

class ITagClickEvent(EventEntity):
    """按键点击事件实体"""
    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = ["press"]

    def __init__(self, handler, click_type: str, name: str):
        self._handler = handler
        self._click_type = click_type
        self._attr_name = name
        self._attr_unique_id = f"{handler.mac}_event_{click_type}"
        self._attr_device_info = handler.device_info

    @callback
    def trigger_press(self):
        self._trigger_event("press")
        self.async_write_ha_state()