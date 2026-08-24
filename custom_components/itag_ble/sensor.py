from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.const import PERCENTAGE
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    handler: "ITagDeviceHandler" = hass.data[DOMAIN][entry.entry_id]
    
    battery_sensor = ITagBatterySensor(handler)
    proxy_sensor = ITagProxySensor(handler)
    area_sensor = ITagProxyAreaSensor(handler)

    handler.battery_sensor = battery_sensor
    handler.proxy_sensor = proxy_sensor
    handler.area_sensor = area_sensor

    async_add_entities([battery_sensor, proxy_sensor, area_sensor])


class ITagBatterySensor(SensorEntity):
    """电量传感器"""
    def __init__(self, handler):
        self._handler = handler
        self._attr_name = "iTag Battery"
        self._attr_unique_id = f"{handler.mac}_battery"
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_device_info = handler.device_info

    @property
    def native_value(self):
        return self._handler.battery_level


class ITagProxySensor(SensorEntity):
    """连接代理传感器"""
    _attr_icon = "mdi:bluetooth-connect"

    def __init__(self, handler):
        self._handler = handler
        self._attr_name = "连接的蓝牙代理"
        self._attr_unique_id = f"{handler.mac}_connected_proxy"
        self._attr_device_info = handler.device_info

    @property
    def native_value(self) -> str:
        return self._handler.connected_proxy


class ITagProxyAreaSensor(SensorEntity):
    """连接代理所在区域传感器"""
    _attr_icon = "mdi:map-marker-radius"

    def __init__(self, handler):
        self._handler = handler
        self._attr_name = "连接代理所在区域"
        self._attr_unique_id = f"{handler.mac}_connected_proxy_area"
        self._attr_device_info = handler.device_info

    @property
    def native_value(self) -> str:
        return self._handler.connected_area