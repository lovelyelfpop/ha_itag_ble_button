from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    handler = hass.data[DOMAIN][entry.entry_id]
    sensor = ITagConnectionBinarySensor(handler)
    handler.connection_sensor = sensor
    async_add_entities([sensor])

class ITagConnectionBinarySensor(BinarySensorEntity):
    """链路状态二值传感器"""
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, handler):
        self._handler = handler
        self._attr_name = "钥匙扣连接状态"
        self._attr_unique_id = f"{handler.mac}_connectivity"
        self._attr_device_info = handler.device_info

    @property
    def is_on(self) -> bool:
        return self._handler.is_connected