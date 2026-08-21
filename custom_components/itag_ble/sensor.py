from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant

from .const import DOMAIN

async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    handler = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ITagBatterySensor(handler)])

class ITagBatterySensor(SensorEntity):
    def __init__(self, handler):
        self._handler = handler
        self._attr_name = "iTag Battery"
        self._attr_unique_id = f"{handler.mac}_battery"
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_device_class = SensorDeviceClass.BATTERY
        #绑定到同一个设备
        self._attr_device_info = handler.device_info

    @property
    def native_value(self):
        return self._handler.battery_level