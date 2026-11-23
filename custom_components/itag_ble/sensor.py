from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.const import PERCENTAGE
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    handler = hass.data[DOMAIN][entry.entry_id]
    
    battery_sensor = ITagBatterySensor(handler)
    proxy_sensor = ITagProxySensor(handler)
    area_sensor = ITagProxyAreaSensor(handler)

    handler.battery_sensor = battery_sensor
    handler.proxy_sensor = proxy_sensor
    handler.area_sensor = area_sensor

    async_add_entities([battery_sensor, proxy_sensor, area_sensor])


class ITagBatterySensor(SensorEntity, RestoreEntity):
    """电量传感器（断开不置0，支持重启记忆）"""
    def __init__(self, handler):
        self._handler = handler
        self._attr_name = "iTag Battery"
        self._attr_unique_id = f"{handler.mac}_battery"
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_device_info = handler.device_info

    async def async_added_to_hass(self) -> None:
        """重启后恢复上一次已知的电池百分比"""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            try:
                # 仅当尚未从设备获取到最新电量时，还原上次记录
                if self._handler.battery_level is None:
                    self._handler.battery_level = int(last_state.state)
            except ValueError:
                pass

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