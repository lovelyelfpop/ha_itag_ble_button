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
from bleak_retry_connector import BleakClientWithServiceCache

from .const import (
    DOMAIN, UUID_FFE0_SERVICE, UUID_FFE1_BUTTON, UUID_FFE2_INIT,
    UUID_ALERT_LEVEL, UUID_BATTERY_LEVEL, MODE_MAP, MODE_SILENT
)

_LOGGER = logging.getLogger(__name__)

# 直连超时：iTag 常只在“可连接广播窗口”内响应连接请求，
# 短超时 + 外层循环快速重试，比 establish_connection 的长阻塞更易命中窗口
CONNECT_TIMEOUT = 8.0

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    mac = entry.data.get(CONF_MAC, entry.data.get("address", "")).upper()
    device_handler = ITagDeviceHandler(hass, entry, mac)
    
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = device_handler
    
    # 采用标准后台任务管理
    entry.async_create_background_task(hass, device_handler.start_loop(), "itag_ble_loop")
    
    await hass.config_entries.async_forward_entry_setups(
        entry, ["select", "sensor", "event", "binary_sensor", "switch"]
    )
    
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True

async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, ["select", "sensor", "event", "binary_sensor", "switch"]
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
        self.battery_level = None  # 初始为 None，未读取到前不显示 0
        self.current_mode = MODE_SILENT
        
        # 新增：断开连接报警开关（写入 FFE2，0x01 开启 / 0x00 关闭，依据 iSearching 逆向）
        self.alarm_on_disconnect = False

        # 双击开关标记（默认关闭，追求最高响应速度）
        self.double_click_enabled = False

        self._running = True
        self._last_battery_time = 0
        self._last_proxy_refresh = 0.0
        self._last_source = None
        
        # 按键事件判定
        self._click_task = None

        # 实体回调引用
        self.connection_sensor = None
        self.proxy_sensor = None
        self.area_sensor = None
        self.battery_sensor = None
        self.event_entities = {}
        self.alarm_on_disconnect_switch = None

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
        if self.alarm_on_disconnect_switch:
            self.alarm_on_disconnect_switch.async_write_ha_state()

    def _resolve_proxy_and_area(self, source_id: str) -> tuple[str, str]:
        """解析蓝牙代理节点名称及其在 HA 中的区域"""
        if not source_id or source_id == "null":
            return "未知代理", "未指定区域"

        if source_id == "local" or source_id.startswith("hci"):
            return "本机蓝牙适配器", "本机/未指定区域"

        proxy_name = f"网关 ({source_id})"
        proxy_area = "未指定区域"

        # 1. 遍历所有扫描器，按 source 大小写不敏感匹配，取 scanner.name（"设备名 (MAC)"）
        try:
            current_scanners = getattr(bluetooth, "async_current_scanners", None)
            if current_scanners is not None:
                for scanner in current_scanners(self.hass):
                    scanner_source = getattr(scanner, "source", "")
                    if scanner_source.upper() == source_id.upper():
                        scanner_name = getattr(scanner, "name", None)
                        if scanner_name and scanner_name != scanner_source:
                            proxy_name = scanner_name
                        break
            else:
                scanner = bluetooth.async_scanner_by_source(self.hass, source_id)
                if scanner is not None:
                    scanner_name = getattr(scanner, "name", None)
                    scanner_source = getattr(scanner, "source", "")
                    if scanner_name and scanner_name != scanner_source:
                        proxy_name = scanner_name
        except Exception as err:
            _LOGGER.debug("解析蓝牙代理扫描器信息异常: %s", err)

        # 2. 设备注册表：兜底名称（若仍为“网关”默认值）+ 补区域
        try:
            dev_reg = dr.async_get(self.hass)
            area_reg = ar.async_get(self.hass)

            formatted_source = source_id.upper()
            for device in dev_reg.devices.values():
                device_macs = {
                    str(c[1]).upper()
                    for c in device.connections
                    if len(c) > 1
                }
                if formatted_source in device_macs:
                    if proxy_name.startswith("网关") and (device.name_by_user or device.name):
                        proxy_name = device.name_by_user or device.name
                    if device.area_id:
                        area = area_reg.async_get_area(device.area_id)
                        if area:
                            proxy_area = area.name
                    break

        except Exception as err:
            _LOGGER.debug("解析蓝牙代理信息异常: %s", err)

        return proxy_name, proxy_area

    def _find_ble_devices(self):
        """返回该 MAC 对应的所有 BLEDevice（每个代理源一个），按信号强度降序。

        同一 iTag 可能被多个蓝牙代理同时看到，HA 的 async_ble_device_from_address
        只返回“最佳”的一个，但它不一定是真正能连上的那个代理。逐源尝试可显著
        提高命中率。
        """
        devices = []
        seen_sources = set()

        # 1. 从最近扫描结果按 MAC 收集每个代理源（含 RSSI）
        for service_info in bluetooth.async_discovered_service_info(self.hass):
            if not service_info.address or service_info.address.upper() != self.mac.upper():
                continue
            device = getattr(service_info, "device", None)
            if device is None:
                continue
            source = getattr(device, "details", {}).get("source", "local")
            if source in seen_sources:
                continue
            seen_sources.add(source)
            rssi = getattr(service_info, "rssi", None)
            devices.append((rssi if rssi is not None else -1000, device))

        # 2. 兜底：connectable / 普通缓存（可能是唯一来源）
        for connectable in (True, False):
            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, self.mac, connectable=connectable
            )
            if ble_device is None:
                continue
            source = getattr(ble_device, "details", {}).get("source", "local")
            if source in seen_sources:
                continue
            seen_sources.add(source)
            rssi = getattr(ble_device, "rssi", None)
            devices.append((rssi if rssi is not None else -1000, ble_device))

        # 按 RSSI 降序（信号越强越优先尝试）
        devices.sort(key=lambda item: item[0], reverse=True)
        _LOGGER.debug(
            "iTag %s 可见代理源: %s",
            self.mac,
            [(getattr(d, "details", {}).get("source", "?"), r) for r, d in devices],
        )
        return [device for _, device in devices]

    async def start_loop(self):
        """漫游重连与心跳监控核心主循环"""
        fail_count = 0
        not_found_count = 0
        while self._running:
            # 1. 扫描提速：不限制 connectable，尽量捕获设备的广播突发窗口
            ble_devices = self._find_ble_devices()
            if not ble_devices:
                # 设备当前不在广播中：缩短轮询间隔，快速捕获广播突发窗口
                not_found_count += 1
                if not_found_count <= 3 or not_found_count % 20 == 0:
                    _LOGGER.warning(
                        "iTag %s 未在蓝牙代理扫描结果中（第 %d 次，已连续 %.0fs 未发现广播），"
                        "若长时间如此请检查代理主动扫描(esp32_ble_tracker.scan_parameters.active: true)或设备是否休眠",
                        self.mac, not_found_count, not_found_count * 1.5,
                    )
                else:
                    _LOGGER.debug("iTag %s 尚未出现在扫描结果中（第 %d 次）", self.mac, not_found_count)
                await asyncio.sleep(1.5)
                continue

            not_found_count = 0

            # 2. 逐个代理源尝试短超时直连（按信号强度从强到弱）。
            #    部分 iTag 只响应某几个代理的连接请求，逐源尝试可显著提高命中率。
            client = None
            source_id = "?"
            proxy_name = "未知代理"
            proxy_area = "未指定区域"
            for ble_device in ble_devices:
                source_id = getattr(ble_device, "details", {}).get("source", "local")
                proxy_name, proxy_area = self._resolve_proxy_and_area(source_id)
                client = await self._connect_once(ble_device, source_id)
                if client is not None:
                    break

            if client is None:
                fail_count += 1
                if fail_count <= 3 or fail_count % 10 == 0:
                    _LOGGER.warning(
                        "iTag %s 连接失败（第 %d 次，尝试了 %d 个代理源，最后来源 %s）。"
                        "若持续超时，请检查该 iTag 是否为随机地址（MAC 以 FF 开头），"
                        "并在 ESPHome 设备日志里查看连接失败原因",
                        self.mac, fail_count, len(ble_devices), source_id,
                    )
                else:
                    _LOGGER.debug("iTag %s 连接失败或正在漫游（第 %d 次）", self.mac, fail_count)
                # 失败后短暂退避，立即进入下一轮扫描/重试
                await asyncio.sleep(min(0.5 + fail_count * 0.25, 3.0))
                continue

            fail_count = 0
            try:
                # 不用 async with：避免对“已连接客户端”二次 connect 可能引发的掉线
                self.client = client
                self.is_connected = True
                self.connected_proxy = proxy_name
                self.connected_area = proxy_area
                self._last_source = source_id
                self._last_proxy_refresh = time.time()
                _LOGGER.info("iTag 建立链路成功 -> 代理: %s, 区域: %s", self.connected_proxy, self.connected_area)
                self._async_update_states()

                # 4. 初始化序列（带超时保护）：防止因边缘信号抖动导致握手死锁
                await self._async_configure_device(client)

                # 5. 订阅按键通知（部分固件无 CCCD，失败不致命，继续保持连接）
                try:
                    await client.start_notify(UUID_FFE1_BUTTON, self._notification_handler)
                except Exception as err:
                    _LOGGER.debug("FFE1 通知订阅跳过（继续运行）: %s", err)

                # 6. 心跳监控循环：连接期间持续维持
                while client.is_connected and self._running:
                    now = time.time()
                    # 首次连上或超 2 小时读取一次电量
                    if self.battery_level is None or (now - self._last_battery_time > 7200):
                        await self._async_update_battery(timeout=5.0)
                        self._last_battery_time = now

                    # 定期刷新当前代理（设备可能在多台代理间漂移）
                    if now - self._last_proxy_refresh > 10:
                        self._refresh_current_proxy()

                    await asyncio.sleep(1)

            except Exception as e:
                self.is_connected = False
                self.client = None
                self.connected_proxy = "未连接"
                self.connected_area = "未连接"
                self._async_update_states()
                _LOGGER.warning("iTag %s 连接中断: %s: %s", self.mac, type(e).__name__, e)
                await asyncio.sleep(1.0)
            finally:
                # 主动断开，避免代理上残留悬挂连接（悬挂连接会占用设备单链接槽位）
                if client.is_connected:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass

    async def _connect_once(self, ble_device, source_id: str):
        """单次短超时直连，成功返回已连接客户端，失败返回 None。"""
        rssi = getattr(ble_device, "rssi", None)
        _LOGGER.debug("尝试直连 -> MAC: %s, 节点来源: %s, RSSI: %s", self.mac, source_id, rssi)
        client = BleakClientWithServiceCache(
            ble_device,
            disconnected_callback=self._on_disconnected,
        )
        try:
            await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
            _LOGGER.debug("iTag %s 直连成功", self.mac)
            return client
        except asyncio.TimeoutError:
            _LOGGER.debug("iTag %s 直连超时（%.0fs，设备未响应连接请求）", self.mac, CONNECT_TIMEOUT)
        except Exception as err:
            _LOGGER.debug("iTag %s 直连失败: %s: %s", self.mac, type(err).__name__, err)
        try:
            await client.disconnect()
        except Exception:
            pass
        return None

    def _refresh_current_proxy(self):
        """定期刷新当前代理（设备可能在多台代理间漂移）。

        取所有当前能看到该设备的代理源，选 RSSI 最强的一个（与“设置->蓝牙”一致）。
        仅刷新代理名称/区域显示，不影响正在进行的 GATT 连接。
        """
        self._last_proxy_refresh = time.time()
        best_source = None
        best_rssi = -1000
        try:
            devices = bluetooth.async_scanner_devices_by_address(
                self.hass, self.mac, connectable=True
            )
            if not devices:
                devices = bluetooth.async_scanner_devices_by_address(
                    self.hass, self.mac, connectable=False
                )
            for scanner_device in devices:
                source = getattr(scanner_device, "source", None)
                if not source:
                    continue
                advertisement = getattr(scanner_device, "advertisement", None)
                rssi = getattr(advertisement, "rssi", None)
                if rssi is None:
                    rssi = -1000
                if rssi > best_rssi:
                    best_rssi = rssi
                    best_source = source
        except Exception as err:
            _LOGGER.debug("刷新当前代理异常: %s", err)

        # 兜底：扫描器设备接口不可用时，用 HA 的最佳设备来源
        if best_source is None:
            try:
                ble_device = bluetooth.async_ble_device_from_address(
                    self.hass, self.mac, connectable=True
                ) or bluetooth.async_ble_device_from_address(
                    self.hass, self.mac, connectable=False
                )
                if ble_device is not None:
                    best_source = getattr(ble_device, "details", {}).get("source", "local")
            except Exception as err:
                _LOGGER.debug("兜底刷新代理来源异常: %s", err)

        if not best_source or best_source == self._last_source:
            return

        self._last_source = best_source
        proxy_name, proxy_area = self._resolve_proxy_and_area(best_source)
        self.connected_proxy = proxy_name
        self.connected_area = proxy_area
        _LOGGER.info("iTag %s 代理漂移到 -> 代理: %s, 区域: %s", self.mac, proxy_name, proxy_area)
        self._async_update_states()

    async def _async_configure_device(self, client):
        """连接后初始化：FFE2 断开报警开关 + 同步工作模式。

        依据 iSearching 逆向：FFE2 是“断开连接报警”配置特征值，
        0x01=开启（断开即响），0x00=关闭。此处同步当前开关状态。
        """
        payload = bytes([0x01 if self.alarm_on_disconnect else 0x00])
        try:
            service = client.services.get_service(UUID_FFE0_SERVICE)
            if service:
                char = service.get_characteristic(UUID_FFE2_INIT)
                if char:
                    await asyncio.wait_for(
                        client.write_gatt_char(char.handle, payload),
                        timeout=4.0,
                    )
        except Exception as err:
            _LOGGER.debug("FFE2 设置跳过或超时: %s", err)

        # 同步当前工作模式（默认静音，不会误响铃）
        await self._async_apply_mode(self.current_mode, timeout=4.0)

    def _get_write_characteristic(self, uuid: str):
        """在服务表中查找指定 UUID 的特征值，找不到返回 None（适配不同固件）"""
        if not self.client or not self.client.services:
            return None
        for service in self.client.services:
            for char in service.characteristics:
                if char.uuid.lower() == uuid.lower():
                    return char
        # 兜底：按 16 位简写匹配（兼容大小写不一致的广播）
        short = uuid.split("-")[0].lstrip("0").lower()
        if not short:
            return None
        for service in self.client.services:
            for char in service.characteristics:
                if char.uuid.split("-")[0].lstrip("0").lower() == short:
                    return char
        return None

    def _notification_handler(self, sender, data: bytearray):
        """按键响应处理"""
        if data and data[0] == 0x01:
            _LOGGER.info("捕获到 iTag 按键事件")
            
            # 关键改进：如果未启用双击，去掉定时器，即刻触发单击（0 延迟）
            if not self.double_click_enabled:
                self._fire_click_event("single")
                return
            
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
        """向 iTag 发送工作模式指令（带超时）

        依据 iSearching 逆向：Alert Level(2A06) 0x01=鸣叫，0x00=停止。
        优先用 UUID 直接写入（与早期可用版本一致），失败再回退句柄写入。
        """
        val = MODE_MAP.get(mode_str, 0x00)
        payload = bytes([val])

        if not self.client or not self.client.is_connected:
            return

        try:
            await asyncio.wait_for(
                self.client.write_gatt_char(UUID_ALERT_LEVEL, payload),
                timeout=timeout,
            )
            _LOGGER.debug("工作模式 [%s] 写入成功 -> 2A06 = 0x%02x", mode_str, val)
            return
        except Exception as e:
            _LOGGER.debug("2A06 UUID 写入失败，尝试句柄写入: %s", e)

        char = self._get_write_characteristic(UUID_ALERT_LEVEL)
        if char is None:
            _LOGGER.debug("未找到 2A06 特征值，跳过工作模式设置")
            return

        try:
            await asyncio.wait_for(
                self.client.write_gatt_char(char.handle, payload),
                timeout=timeout,
            )
            _LOGGER.debug("工作模式 [%s] 句柄写入成功", mode_str)
        except Exception as e:
            _LOGGER.error(f"指令发送失败: {e}")

    async def set_alarm_on_disconnect(self, enabled: bool):
        """暴露给 switch 实体：断开连接报警开关（写入 FFE2）"""
        self.alarm_on_disconnect = enabled
        if self.client and self.client.is_connected:
            await self._async_apply_alarm_on_disconnect(enabled, timeout=3.0)

    async def _async_apply_alarm_on_disconnect(self, enabled: bool, timeout: float = 3.0):
        """向 FFE2 写入断开报警配置：0x01 开启 / 0x00 关闭"""
        if not self.client or not self.client.is_connected:
            return
        payload = bytes([0x01 if enabled else 0x00])
        try:
            await asyncio.wait_for(
                self.client.write_gatt_char(UUID_FFE2_INIT, payload),
                timeout=timeout,
            )
        except Exception as e:
            _LOGGER.error(f"断开报警指令发送失败: {e}")

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
