#pragma once

// Pin maps: Seeed XIAO ESP32S3 hardware overview and Espressif CameraWebServer.
// Select exactly one fixed product; an arbitrary ESP32/S3 must not inherit pins.
#if defined(OM_BOARD_XIAO_ESP32S3_SENSE) && defined(OM_BOARD_AI_THINKER_ESP32CAM)
#error "Select exactly one ObjectMemory camera board"
#elif defined(OM_BOARD_XIAO_ESP32S3_SENSE)
#if !CONFIG_IDF_TARGET_ESP32S3 || !ARDUINO_USB_CDC_ON_BOOT || !ARDUINO_USB_MODE
#error "XIAO ESP32S3 Sense requires ESP32S3 and native USB CDC"
#endif
constexpr const char *OM_BOARD_TYPE = "xiao_esp32s3_sense";
constexpr const char *OM_CHIP_TYPE = "esp32s3";
constexpr int PWDN_GPIO_NUM = -1;
constexpr int RESET_GPIO_NUM = -1;
constexpr int XCLK_GPIO_NUM = 10;
constexpr int SIOD_GPIO_NUM = 40;
constexpr int SIOC_GPIO_NUM = 39;
constexpr int Y9_GPIO_NUM = 48;
constexpr int Y8_GPIO_NUM = 11;
constexpr int Y7_GPIO_NUM = 12;
constexpr int Y6_GPIO_NUM = 14;
constexpr int Y5_GPIO_NUM = 16;
constexpr int Y4_GPIO_NUM = 18;
constexpr int Y3_GPIO_NUM = 17;
constexpr int Y2_GPIO_NUM = 15;
constexpr int VSYNC_GPIO_NUM = 38;
constexpr int HREF_GPIO_NUM = 47;
constexpr int PCLK_GPIO_NUM = 13;
#elif defined(OM_BOARD_AI_THINKER_ESP32CAM)
#if !CONFIG_IDF_TARGET_ESP32
#error "AI Thinker ESP32-CAM requires the original ESP32 chip"
#endif
constexpr const char *OM_BOARD_TYPE = "ai_thinker_esp32cam";
constexpr const char *OM_CHIP_TYPE = "esp32";
constexpr int PWDN_GPIO_NUM = 32;
constexpr int RESET_GPIO_NUM = -1;
constexpr int XCLK_GPIO_NUM = 0;
constexpr int SIOD_GPIO_NUM = 26;
constexpr int SIOC_GPIO_NUM = 27;
constexpr int Y9_GPIO_NUM = 35;
constexpr int Y8_GPIO_NUM = 34;
constexpr int Y7_GPIO_NUM = 39;
constexpr int Y6_GPIO_NUM = 36;
constexpr int Y5_GPIO_NUM = 21;
constexpr int Y4_GPIO_NUM = 19;
constexpr int Y3_GPIO_NUM = 18;
constexpr int Y2_GPIO_NUM = 5;
constexpr int VSYNC_GPIO_NUM = 25;
constexpr int HREF_GPIO_NUM = 23;
constexpr int PCLK_GPIO_NUM = 22;
#else
#error "An explicitly supported ObjectMemory camera board is required"
#endif
