# 许可和第三方说明

项目代码以 MIT 许可证发布，见 LICENSE。第三方组件保留原许可。以下是依赖用途索引，并非替代相应软件分发包中的完整许可文本。

| 组件 | 许可 / 用途 |
|---|---|
| FastAPI、React、Vite、TypeScript、pyserial、mss、Pillow | 各组件 MIT/BSD 类许可，详见随包 LICENSE |
| OpenCV / opencv-contrib-python | Apache-2.0 及第三方组件许可；ArUco、图像处理、视频读写 |
| NumPy | BSD-3-Clause |
| PlatformIO Core | Apache-2.0 |
| Arduino ESP32 framework | LGPL-2.1 等，包含 ESP-IDF 及各组件许可 |
| ArduinoJson | MIT |
| FFmpeg / imageio-ffmpeg | 分发构建许可由包内 FFmpeg 文件决定；分发二进制前核查所用构建和对应源码义务 |
| Lucide | ISC |
| MediaPipe（可选） | Apache-2.0，模型另查其模型分发许可 |
| 可选检测模型 | 安装脚本记录下载来源；商业分发前单独检查模型许可，不默认将第三方权重视为 MIT |

自动生成的标签和演示视频是项目测试素材；不是实际家庭录像。测试素材中的“手”是图形模拟，不代表真人手部检测结果。

官方参考链接保存在 docs/ARCHITECTURE.md。不要提交本地 Wi-Fi 配置、摄像头凭据、设备令牌或家庭视频。
