原贴: https://bbs.hassbian.com/thread-32974-1-1.html

一个ESP蓝牙代理 范围有限，如果要全屋可用，需要每个ESP蓝牙代理都写死iTag的蓝牙MAC地址 和 交互逻辑，不太方便。

所以借助AI，开发了这个HA集成，将和iTag交互的逻辑从ESPHome固件中剥离，然后放到HA集成中