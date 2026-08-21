import asyncio
import logging
import time
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_MAC, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.components import bluetooth
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.const import CONF_MAC, CONF_NAME

from .const import (
    DOMAIN, UUID_FFE1_BUTTON, UUID_FFE2_INIT,
    UUID_BATTERY_LEVEL, UUID_ALERT_LEVEL,
    MODE_MAP
)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    mac = entry.data[CONF_MAC].upper()
    device_handler = ITagDeviceHandler(hass, entry, mac)
    
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = device_handler
    
    # 启动后台守护任务负责持续维持蓝牙连接
    entry.async_create_background_task(hass, device_handler.start_loop(), "itag_ble_loop")
    
    await hass.config_entries.async_forward_entry_setups(entry, ["select", "sensor", "event"])
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    handler = hass.data[DOMAIN].pop(entry.entry_id)
    await handler.stop()
    return await hass.config_entries.async_forward_entry_unload(entry, "select") and \
           await hass.config_entries.async_forward_entry_unload(entry, "sensor") and \
           await hass.config_entries.async_forward_entry_unload(entry, "event")


class ITagDeviceHandler:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, mac: str):
        self.hass = hass
        self.entry = entry
        self.mac = mac
        self.client: BleakClientWithServiceCache | None = None
        self.is_connected = False
        self._running = True
        self.battery_level = None
        
        # 多击判定相关变量
        self._click_timestamps = []
        self._click_timer = None

        # 定义统一的设备信息
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, self.mac)},
            name=entry.data.get(CONF_NAME, f"iTag ({self.mac})"),
            manufacturer="iTag",
            model="BLE Tracker",
            connections={("bluetooth", self.mac)},
        )

    async def start_loop(self):
        """蓝牙后台连接与重连循环"""
        while self._running:
            try:
                ble_device = bluetooth.async_ble_device_from_address(
                    self.hass, self.mac, connectable=True
                )
                if not ble_device:
                    await asyncio.sleep(5)
                    continue

                _LOGGER.info("正在尝试连接 iTag: %s", self.mac)
                self.client = await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    self.mac,
                    disconnected_callback=self._on_disconnected
                )
                
                self.is_connected = True
                _LOGGER.info("iTag 链路已建立，状态同步为：在家")

                # 1. 初始化写入 FFE2 (0x00)
                try:
                    await self.client.write_gatt_char(UUID_FFE2_INIT, bytearray([0x00]), response=False)
                except Exception as err:
                    _LOGGER.warning("写入 FFE2 初始化指令失败: %s", err)

                # 2. 订阅按键通知 (FFE1)
                await self.client.start_notify(UUID_FFE1_BUTTON, self._notification_handler)

                # 3. 读取电池电量
                await self._async_update_battery()

                # 保持连接，直到断开
                while self.client and self.client.is_connected and self._running:
                    await asyncio.sleep(3600)  # 每小时可定时读一次电池
                    await self._async_update_battery()

            except Exception as e:
                _LOGGER.debug("iTag BLE 连接异常: %s", e)
                self.is_connected = False
                await asyncio.sleep(5)

    def _on_disconnected(self, client):
        self.is_connected = False
        _LOGGER.info("iTag 链路已断开，状态同步为：离家")

    def _notification_handler(self, sender, data: bytearray):
        """接收蓝牙发来的按键事件通知并计算多击"""
        if data and data[0] == 0x01:
            _LOGGER.info("【iTag】捕获到硬件按键按下事件！")
            now = time.time()
            self._click_timestamps.append(now)

            if self._click_timer:
                self._click_timer.cancel()

            # 400ms 内触发判决
            self._click_timer = self.hass.loop.call_later(0.4, self._process_clicks)

    def _process_clicks(self):
        count = len(self._click_timestamps)
        self._click_timestamps.clear()
        
        click_type = None
        if count == 1:
            click_type = "single"
        elif count == 2:
            click_type = "double"

        if click_type and hasattr(self, "event_entities"):
            # 触发对应的 Event 实体状态更新
            event_entity = self.event_entities.get(click_type)
            if event_entity:
                event_entity.trigger_press()

            # 保留广播总线事件（兼顾原有配置）
            action_name = "单击 (Single Click)" if click_type == "single" else "双击 (Double Click)"
            self.hass.bus.async_fire("itag_ble_button_click", {
                "mac": self.mac,
                "action": action_name,
                "device_id": self.entry.entry_id
            })

    async def set_work_mode(self, mode_str: str):
        """控制 iTag 报警蜂鸣器/工作模式"""
        if not self.client or not self.client.is_connected:
            _LOGGER.error("iTag 未连接，无法切换模式")
            return
        
        val = MODE_MAP.get(mode_str)
        if val is not None:
            await self.client.write_gatt_char(UUID_ALERT_LEVEL, bytearray([val]), response=True)
            _LOGGER.info("【模式切换】iTag 当前设为: %s", mode_str)

    async def _async_update_battery(self):
        if self.client and self.client.is_connected:
            try:
                data = await self.client.read_gatt_char(UUID_BATTERY_LEVEL)
                if data:
                    self.battery_level = int(data[0])
            except Exception:
                pass

    async def stop(self):
        self._running = False
        if self.client and self.client.is_connected:
            await self.client.disconnect()