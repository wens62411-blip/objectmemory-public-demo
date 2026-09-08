# 固件测试说明

固件核心由 PlatformIO 对真实 `esp32cam` 目标编译验证。串口协议、配对码单次使用、令牌校验、构建命令和模拟设备端到端流程由 `apps/api/tests/test_firmware.py` 覆盖。没有连接开发板时不会执行上传测试。

