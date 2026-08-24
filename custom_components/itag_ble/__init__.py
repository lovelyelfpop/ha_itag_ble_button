import asyncio
import logging
import time
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_MAC, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers import area_registry as ar, device_registry as dr
from homeassistant.components import bluetooth
from bleak import BleakClient
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache

from .const import (
    DOMAIN, UUID_FFE0_SERVICE, UUID_FFE1_BUTTON, UUID_FFE2_INIT,
    UUID_BATTERY_LEVEL, UUID_ALERT_LEVEL, MODE_MAP
)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    mac = entry.data.get(CONF_MAC, entry.data.get("address", "")).upper()
    device_handler = ITagDeviceHandler(hass, entry, mac)
    
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = device_handler
    
    # 采用标准后台任务管理
    entry.async_create_background_task(hass, device_handler.start_loop(), "itag_ble_loop")
    
    await hass.config_entries.async_forward_entry_setups(
        entry, ["select", "sensor", "event", "binary_sensor"]
    )
    
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True

async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, ["select", "sensor", "event", "binary_sensor"]
    )
    if unload_ok:
        handler = hass.data[DOMAIN].pop(entry.entry_id)
        await handler.stop()
    return unload_ok


class ITagDeviceHandler:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, mac: str):
        self.hass = hass
        self.entry = entry
        self.mac = mac
        self.client: BleakClientWithServiceCache | None = None
        self.is_connected = False
        self.connected_proxy = "查找中..."
        self.connected_area = "未连接"
        self.battery_level = 0
        self.current_mode = "静音模式"
        self._running = True
        self._last_battery_time = 0
        
        # 按键事件判定
        self._click_task = None

        # 实体回调引用
        self.connection_sensor = None
        self.proxy_sensor = None
        self.area_sensor = None
        self.battery_sensor = None
        self.event_entities = {}

        # 挂载设备信息
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, self.mac)},
            name=entry.data.get(CONF_NAME, f"iTag ({self.mac})"),
            manufacturer="iTag",
            model="BLE Tracker Pro",
            connections={("bluetooth", self.mac)},
        )

    def _on_disconnected(self, client: BleakClient):
        """链路一旦断开（如漫游超出当前代理范围），立即标记并允许重连循环快速响应"""
        _LOGGER.debug("iTag %s 链路已断开，准备重新寻找代理节点", self.mac)
        self.is_connected = False
        self.client = None
        self.connected_proxy = "未连接"
        self.connected_area = "未连接"
        self._async_update_states()

    def _async_update_states(self):
        """通知 HA 实体刷新状态"""
        if self.connection_sensor:
            self.connection_sensor.async_write_ha_state()
        if self.proxy_sensor:
            self.proxy_sensor.async_write_ha_state()
        if self.area_sensor:
            self.area_sensor.async_write_ha_state()
        if self.battery_sensor:
            self.battery_sensor.async_write_ha_state()

    def _resolve_proxy_and_area(self, source_id: str) -> tuple[str, str]:
        """解析蓝牙代理节点名称及其在 HA 中的区域"""
        if not source_id or source_id == "null":
            return "未知代理", "未指定区域"

        if source_id == "local" or source_id.startswith("hci"):
            return "本机蓝牙适配器", "本机/未指定区域"

        proxy_name = f"网关 ({source_id})"
        proxy_area = "未指定区域"
        found_name = False

        try:
            dev_reg = dr.async_get(self.hass)
            area_reg = ar.async_get(self.hass)

            formatted_source = source_id.upper()
            for device in dev_reg.devices.values():
                # 检查代理设备连接集 (connections) 是否匹配源 MAC
                device_macs = {
                    str(c[1]).upper()
                    for c in device.connections
                    if len(c) > 1
                }
                if formatted_source in device_macs:
                    proxy_name = device.name_by_user or device.name or proxy_name
                    found_name = True
                    if device.area_id:
                        area = area_reg.async_get_area(device.area_id)
                        if area:
                            proxy_area = area.name
                    break

            if not found_name:
                scanner = bluetooth.async_scanner_by_source(self.hass, source_id)
                if scanner and hasattr(scanner, "name"):
                    proxy_name = scanner.name

        except Exception as err:
            _LOGGER.debug("解析蓝牙代理信息异常: %s", err)

        return proxy_name, proxy_area

    async def start_loop(self):
        """漫游重连与心跳监控核心主循环"""
        while self._running:
            # 1. 扫描提速：若设备不在广播视野中，睡眠从 10s 缩短到 3s，提高漫游捕获速度
            ble_device = bluetooth.async_ble_device_from_address(self.hass, self.mac, connectable=True)
            if not ble_device:
                await asyncio.sleep(3)
                continue

            # 2. 节点与区域名称解析
            source_id = getattr(ble_device, "details", {}).get("source", "local")
            proxy_name, proxy_area = self._resolve_proxy_and_area(source_id)

            try:
                # 3. 漫游加固：传入 disconnected_callback，确保断开后无需等待垃圾回收即刻触发重试
                _LOGGER.debug("尝试建立 BLE 连接 -> MAC: %s, 节点来源: %s", self.mac, source_id)
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    self.mac,
                    disconnected_callback=self._on_disconnected
                )

                async with client:
                    self.client = client
                    self.is_connected = True
                    self.connected_proxy = proxy_name
                    self.connected_area = proxy_area
                    _LOGGER.info("iTag 建立链路成功 -> 代理: %s, 区域: %s", self.connected_proxy, self.connected_area)
                    self._async_update_states()

                    # 4. 初始化序列（带 4.0s 超时保护）：防止因边缘信号抖动导致握手死锁
                    try:
                        # 防丢卡片握手指令 (部分芯片没握手会睡死)
                        service = client.services.get_service(UUID_FFE0_SERVICE)
                        if service:
                            char = service.get_characteristic(UUID_FFE2_INIT)
                            if char:
                                await asyncio.wait_for(
                                    client.write_gatt_char(char.handle, b"\x00", response=False),
                                    timeout=4.0
                                )
                    except Exception as err:
                        _LOGGER.debug("FFE2 初始化握手跳过或超时: %s", err)

                    # 同步当前工作模式
                    await self._async_apply_mode(self.current_mode, timeout=4.0)

                    # 订阅按键通知
                    await client.start_notify(UUID_FFE1_BUTTON, self._notification_handler)

                    # 5. 心跳监控循环：连接期间持续维持
                    while client.is_connected and self._running:
                        now = time.time()
                        # 低功耗策略：每 2 小时 (7200s) 读取一次电量
                        if now - self._last_battery_time > 7200:
                            await self._async_update_battery(timeout=5.0)
                            self._last_battery_time = now

                        await asyncio.sleep(1)

            except Exception as e:
                self.is_connected = False
                self.client = None
                self.connected_proxy = "未连接"
                self.connected_area = "未连接"
                self._async_update_states()
                _LOGGER.debug("iTag 连接失败或正在漫游: %s", e)
                await asyncio.sleep(2)  # 失败后快速进入下一次扫描

    def _notification_handler(self, sender, data: bytearray):
        """按键响应处理"""
        if data and data[0] == 0x01:
            _LOGGER.info("捕获到 iTag 按键事件")
            if self._click_task and not self._click_task.done():
                # 第二次点击：取消单击判定，转为双击
                self._click_task.cancel()
                self._click_task = None
                self._fire_click_event("double")
            else:
                # 第一次点击：启动 600ms 定时器等待双击
                self._click_task = self.hass.async_create_task(self._handle_click_timer())

    async def _handle_click_timer(self):
        """按键判定窗口定时器 (600ms)"""
        await asyncio.sleep(0.6)
        self._fire_click_event("single")
        self._click_task = None

    def _fire_click_event(self, click_type: str):
        """触发展示实体与事件总线"""
        event_entity = self.event_entities.get(click_type)
        if event_entity:
            event_entity.trigger_press()

        action_name = "single" if click_type == "single" else "double"
        self.hass.bus.async_fire("itag_ble_button_click", {
            "mac": self.mac,
            "action": action_name,
            "device_id": self.entry.entry_id
        })

    async def set_work_mode(self, mode_str: str):
        """暴露给 select 实体切换模式"""
        self.current_mode = mode_str
        if self.client and self.client.is_connected:
            await self._async_apply_mode(mode_str, timeout=3.0)

    async def _async_apply_mode(self, mode_str: str, timeout: float = 3.0):
        """向 iTag 发送工作模式指令（带超时）"""
        val = MODE_MAP.get(mode_str, 0x02)
        payload = bytes([val])
        
        if not self.client or not self.client.is_connected:
            return
        
        try:
            await asyncio.wait_for(self.client.write_gatt_char(UUID_ALERT_LEVEL, payload), timeout=timeout)
        except Exception as e:
            _LOGGER.error(f"指令发送失败: {e}")

        #for service in self.client.services:
        #    for char in service.characteristics:
        #        if "2a06" in char.uuid.lower():
        #            try:
        #                if "write-without-response" in char.properties:
        #                    await asyncio.wait_for(
        #                        self.client.write_gatt_char(char.handle, payload, response=False),
        #                        timeout=timeout
        #                    )
        #                else:
        #                    await asyncio.wait_for(
        #                        self.client.write_gatt_char(char.handle, payload, response=True),
        #                        timeout=timeout
        #                    )
        #                _LOGGER.debug("写入工作模式 [%s] 成功", mode_str)
        #                return
        #            except Exception as e:
        #                _LOGGER.warning("设置工作模式指令超时或写入失败: %s", e)

    async def _async_update_battery(self, timeout: float = 5.0):
        """读取电池电量（带超时）"""
        if self.client and self.client.is_connected:
            try:
                data = await asyncio.wait_for(
                    self.client.read_gatt_char(UUID_BATTERY_LEVEL),
                    timeout=timeout
                )
                if data:
                    self.battery_level = int(data[0])
                    self._async_update_states()
            except Exception:
                pass

    async def stop(self):
        """卸载清理"""
        self._running = False
        if self._click_task and not self._click_task.done():
            self._click_task.cancel()
        if self.client and self.client.is_connected:
            try:
                await self.client.disconnect()
            except Exception:
                pass