import logging
from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    handler = hass.data[DOMAIN][entry.entry_id]
    double_click_switch = ITagDoubleClickSwitch(handler)
    alarm_on_disconnect_switch = ITagAlarmOnDisconnectSwitch(handler)
    handler.double_click_switch = double_click_switch
    handler.alarm_on_disconnect_switch = alarm_on_disconnect_switch
    async_add_entities([double_click_switch, alarm_on_disconnect_switch])


class ITagDoubleClickSwitch(SwitchEntity, RestoreEntity):
    """控制是否开启双击识别的开关实体（带状态记忆）"""

    def __init__(self, handler):
        self._handler = handler
        self._attr_name = "启用双击功能"
        self._attr_unique_id = f"{handler.mac}_enable_double_click"
        self._attr_icon = "mdi:gesture-double-tap"
        self._attr_device_info = handler.device_info

    async def async_added_to_hass(self) -> None:
        """实体加载到 HA 时调用：从数据库还原上次状态"""
        await super().async_added_to_hass()
        
        last_state = await self.async_get_last_state()
        if last_state is not None:
            # 判断上一状态是否为 'on'
            is_on = last_state.state == "on"
            self._handler.double_click_enabled = is_on
            _LOGGER.debug("已恢复 iTag 双击开关状态: %s", is_on)
        else:
            # 默认关闭（追求最快响应速度）
            self._handler.double_click_enabled = False

    @property
    def is_on(self) -> bool:
        """获取当前双击功能的开启状态"""
        return self._handler.double_click_enabled

    async def async_turn_on(self, **kwargs):
        """开启双击（会有 600ms 判定等待）"""
        self._handler.double_click_enabled = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        """关闭双击（无等待，单击即刻响应）"""
        self._handler.double_click_enabled = False
        self.async_write_ha_state()


class ITagAlarmOnDisconnectSwitch(SwitchEntity, RestoreEntity):
    """断开连接报警开关（写入 FFE2，0x01 开启 / 0x00 关闭）

    依据 iSearching.apk 逆向：FFE2 为“断开报警”配置特征值，
    开启后钥匙扣与手机断开连接时会自动发出报警音。
    """

    def __init__(self, handler):
        self._handler = handler
        self._attr_name = "断开连接报警"
        self._attr_unique_id = f"{handler.mac}_alarm_on_disconnect"
        self._attr_icon = "mdi:link-off"
        self._attr_device_info = handler.device_info

    async def async_added_to_hass(self) -> None:
        """实体加载到 HA 时调用：从数据库还原上次状态"""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None:
            is_on = last_state.state == "on"
            self._handler.alarm_on_disconnect = is_on
            _LOGGER.debug("已恢复 iTag 断开报警开关状态: %s", is_on)
        else:
            # 默认关闭，避免意外触发设备报警
            self._handler.alarm_on_disconnect = False

    @property
    def is_on(self) -> bool:
        return self._handler.alarm_on_disconnect

    async def async_turn_on(self, **kwargs):
        """开启断开连接报警"""
        self._handler.alarm_on_disconnect = True
        await self._handler.set_alarm_on_disconnect(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        """关闭断开连接报警"""
        self._handler.alarm_on_disconnect = False
        await self._handler.set_alarm_on_disconnect(False)
        self.async_write_ha_state()
