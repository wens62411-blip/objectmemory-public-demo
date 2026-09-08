# 固件本地库目录

当前固件不复制第三方源码。ArduinoJson 由 `platformio.ini` 精确锁定并由 PlatformIO 拉取；摄像头、Wi-Fi、HTTPClient 与 Preferences 来自锁定的 Arduino ESP32 framework。

