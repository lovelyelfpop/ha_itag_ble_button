DOMAIN = "itag_ble"

# iTag 专属 UUIDs
UUID_FFE0_SERVICE = "0000ffe0-0000-1000-8000-00805f9b34fb"
UUID_FFE1_BUTTON = "0000ffe1-0000-1000-8000-00805f9b34fb"
UUID_FFE2_INIT = "0000ffe2-0000-1000-8000-00805f9b34fb"

UUID_ALERT_SERVICE = "00001802-0000-1000-8000-00805f9b34fb"
UUID_ALERT_LEVEL = "00002a06-0000-1000-8000-00805f9b34fb"

UUID_BATTERY_SERVICE = "0000180f-0000-1000-8000-00805f9b34fb"
UUID_BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"

MODE_SILENT = "静音模式"
MODE_CLICK_BEEP = "按键提示音"
MODE_FIND = "寻物蜂鸣"

# 工作模式取值（实测可用，勿轻易改动）
# 注意：iTag 不同批次固件对 2A06 值的解释并不统一。
#   - 本集成默认这套映射在用户设备上实测有效：0x02=静音、0x00=按键提示音、0x01=寻物蜂鸣
#   - iSearching App 把 2A06 当作“瞬时响铃命令”（写 0x01 响、写 0x00 停），与这里不同
# 若某台设备的模式选择无反应，说明其固件采用另一套映射，可在此调整该设备对应取值。
MODE_MAP = {
    MODE_SILENT: 0x02,
    MODE_CLICK_BEEP: 0x00,
    MODE_FIND: 0x01,
}