#include "camera_server.h"

#include <ArduinoJson.h>
#include <WiFi.h>
#include <esp_camera.h>
#include <esp_http_server.h>
#include <mbedtls/md.h>
#include <mbedtls/platform_util.h>
#include <mbedtls/sha256.h>
#include <lwip/sockets.h>
#include "board_config.h"

namespace {
constexpr size_t kMaxHttpConfig = 1024;
constexpr char kCameraReadPurpose[] = "objectmemory/camera-read/v1";

httpd_handle_t server = nullptr;
httpd_handle_t streamServer = nullptr;
DeviceConfig *activeConfig = nullptr;
bool initialized = false;
bool allowConfiguration = false;
portMUX_TYPE accessMux = portMUX_INITIALIZER_UNLOCKED;
char cameraReadToken[65]{};
uint32_t cameraAccessGeneration = 0;

String jsonText(const JsonDocument &doc) {
  String output;
  serializeJson(doc, output);
  return output;
}

esp_err_t sendJson(httpd_req_t *request, const String &body, const char *status = "200 OK") {
  httpd_resp_set_status(request, status);
  httpd_resp_set_type(request, "application/json; charset=utf-8");
  httpd_resp_set_hdr(request, "Cache-Control", "no-store");
  return httpd_resp_send(request, body.c_str(), body.length());
}

esp_err_t denyCameraAccess(httpd_req_t *request) {
  httpd_resp_set_hdr(request, "WWW-Authenticate", "Bearer realm=\"ObjectMemory camera\"");
  return sendJson(request, "{\"detail\":\"camera_read_authorization_required\"}", "401 Unauthorized");
}

bool cameraAccess(httpd_req_t *request, uint32_t &generation) {
  // Never accept query-string secrets, the device heartbeat token, or an
  // anonymous request. Length is bounded before any header is copied.
  if (httpd_req_get_hdr_value_len(request, "Authorization") != 71) return false;
  char authorization[72]{};
  if (httpd_req_get_hdr_value_str(request, "Authorization", authorization, sizeof(authorization)) != ESP_OK) {
    mbedtls_platform_zeroize(authorization, sizeof(authorization));
    return false;
  }
  char expected[65];
  portENTER_CRITICAL(&accessMux);
  memcpy(expected, cameraReadToken, sizeof(expected));
  generation = cameraAccessGeneration;
  portEXIT_CRITICAL(&accessMux);
  uint8_t difference = expected[0] == '\0' ? 1 : 0;
  const char prefix[] = "Bearer ";
  for (size_t index = 0; index < 7; ++index) {
    difference |= static_cast<uint8_t>(authorization[index] ^ prefix[index]);
  }
  for (size_t index = 0; index < 64; ++index) {
    difference |= static_cast<uint8_t>(authorization[index + 7] ^ expected[index]);
  }
  mbedtls_platform_zeroize(authorization, sizeof(authorization));
  mbedtls_platform_zeroize(expected, sizeof(expected));
  return difference == 0;
}

bool cameraAccessCurrent(uint32_t generation) {
  portENTER_CRITICAL(&accessMux);
  const bool current = cameraReadToken[0] != '\0' && generation == cameraAccessGeneration;
  portEXIT_CRITICAL(&accessMux);
  return current;
}

bool configurationUsesAccessPoint(httpd_req_t *request) {
  // In AP+STA fallback, a listening socket is otherwise reachable from the
  // home LAN too. Only the interface the user joined for provisioning may
  // accept this unauthenticated setup action; do not trust forwarded headers.
  sockaddr_in local{};
  socklen_t length = sizeof(local);
  const IPAddress ap = WiFi.softAPIP();
  return ap != IPAddress(0, 0, 0, 0) &&
         getsockname(httpd_req_to_sockfd(request), reinterpret_cast<sockaddr *>(&local), &length) == 0 &&
         local.sin_family == AF_INET && local.sin_addr.s_addr == static_cast<uint32_t>(ap);
}

esp_err_t healthHandler(httpd_req_t *request) {
  JsonDocument doc;
  sensor_t *sensor = initialized ? esp_camera_sensor_get() : nullptr;
  doc["online"] = true;
  doc["uptime"] = millis() / 1000;
  doc["free_heap"] = ESP.getFreeHeap();
  doc["camera_status"] = sensor ? "ready" : "error";
  if (sensor) doc["sensor_pid"] = sensor->id.PID;
  else doc["sensor_pid"] = nullptr;
  doc["camera_auth"] = "hmac-sha256-v1";
  doc["firmware_version"] = "0.1.0";
  doc["board_type"] = OM_BOARD_TYPE;
  doc["chip"] = OM_CHIP_TYPE;
  doc["psram_bytes"] = ESP.getPsramSize();
  doc["flash_bytes"] = ESP.getFlashChipSize();
  return sendJson(request, jsonText(doc));
}

esp_err_t deviceHandler(httpd_req_t *request) {
  uint32_t accessGeneration;
  if (!cameraAccess(request, accessGeneration)) return denyCameraAccess(request);
  JsonDocument doc;
  doc["device_id"] = activeConfig ? activeConfig->deviceId : "";
  doc["device_name"] = activeConfig ? activeConfig->deviceName : "";
  doc["room_name"] = activeConfig ? activeConfig->roomName : "";
  doc["ip"] = WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString() : WiFi.softAPIP().toString();
  doc["mac"] = WiFi.macAddress();
  doc["board_type"] = OM_BOARD_TYPE;
  doc["chip"] = OM_CHIP_TYPE;
  doc["camera_auth"] = "hmac-sha256-v1";
  doc["firmware_version"] = "0.1.0";
  doc["stream_url"] = deviceStreamUrl();
  doc["capture_url"] = deviceBaseUrl() + "/capture";
  return sendJson(request, jsonText(doc));
}

esp_err_t captureHandler(httpd_req_t *request) {
  uint32_t accessGeneration;
  if (!cameraAccess(request, accessGeneration)) return denyCameraAccess(request);
  if (!cameraReady()) return sendJson(request, "{\"detail\":\"camera_not_ready\"}", "503 Service Unavailable");
  camera_fb_t *frame = esp_camera_fb_get();
  if (!frame) return sendJson(request, "{\"detail\":\"capture_failed\"}", "503 Service Unavailable");
  if (!cameraAccessCurrent(accessGeneration)) {
    esp_camera_fb_return(frame);
    return denyCameraAccess(request);
  }
  httpd_resp_set_type(request, "image/jpeg");
  httpd_resp_set_hdr(request, "Cache-Control", "no-store");
  const esp_err_t result = httpd_resp_send(request, reinterpret_cast<const char *>(frame->buf), frame->len);
  esp_camera_fb_return(frame);
  return result;
}

esp_err_t streamHandler(httpd_req_t *request) {
  uint32_t accessGeneration;
  if (!cameraAccess(request, accessGeneration)) return denyCameraAccess(request);
  if (!cameraReady()) return sendJson(request, "{\"detail\":\"camera_not_ready\"}", "503 Service Unavailable");
  static const char *contentType = "multipart/x-mixed-replace;boundary=frame";
  static const char *boundary = "\r\n--frame\r\n";
  httpd_resp_set_type(request, contentType);
  httpd_resp_set_hdr(request, "Cache-Control", "no-store");
  char header[96];
  while (cameraAccessCurrent(accessGeneration)) {
    camera_fb_t *frame = esp_camera_fb_get();
    if (!frame) return ESP_FAIL;
    if (!cameraAccessCurrent(accessGeneration)) {
      esp_camera_fb_return(frame);
      return ESP_FAIL;
    }
    const int headerLength = snprintf(header, sizeof(header),
                                      "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n",
                                      static_cast<unsigned>(frame->len));
    esp_err_t result = httpd_resp_send_chunk(request, boundary, strlen(boundary));
    if (result == ESP_OK) result = httpd_resp_send_chunk(request, header, headerLength);
    if (result == ESP_OK) {
      result = httpd_resp_send_chunk(request, reinterpret_cast<const char *>(frame->buf), frame->len);
    }
    esp_camera_fb_return(frame);
    if (result != ESP_OK) return result;
    delay(25);
  }
  return ESP_FAIL;
}

esp_err_t streamRedirectHandler(httpd_req_t *request) {
  uint32_t accessGeneration;
  if (!cameraAccess(request, accessGeneration)) return denyCameraAccess(request);
  const String location = deviceStreamUrl();
  httpd_resp_set_status(request, "307 Temporary Redirect");
  httpd_resp_set_hdr(request, "Location", location.c_str());
  return httpd_resp_send(request, nullptr, 0);
}

esp_err_t rootHandler(httpd_req_t *request) {
  static const char page[] PROGMEM = R"HTML(<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>物忆摄像头配网</title><style>body{font-family:system-ui;margin:2rem auto;padding:0 1rem;max-width:34rem;color:#20312d;background:#f7faf8}form{display:grid;gap:.7rem;background:white;padding:1.2rem;border-radius:14px;box-shadow:0 8px 30px #2342}label{display:grid;gap:.25rem}input{font:inherit;padding:.65rem;border:1px solid #bdccc7;border-radius:8px}button{font:inherit;padding:.75rem;border:0;border-radius:9px;color:white;background:#286d5d}small{color:#60736e}</style><h1>物忆 ESP32-CAM</h1><p>USB 串口配网失败时使用此本地备用页。请先在物忆设备中心生成一次性配对码。</p><form id="f"><label>Wi-Fi 名称<input name="ssid" maxlength="32" required></label><label>Wi-Fi 密码<input name="password" type="password" maxlength="63"></label><label>电脑局域网地址<input name="backend_url" placeholder="http://192.168.1.20:8018" required></label><label>一次性配对码<input name="pairing_code" placeholder="OM-XXXXXX" required></label><label>设备编号<input name="device_id" placeholder="omcam-xxxx" required></label><label>设备名称<input name="device_name" value="客厅摄像头" required></label><label>房间名称<input name="room_name" value="客厅" required></label><button>保存并重启</button><small id="s">配置只发送到当前设备，不会显示在日志中。</small></form><script>f.onsubmit=async e=>{e.preventDefault();s.textContent='正在保存…';const d=Object.fromEntries(new FormData(f));try{const r=await fetch('/config',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(d)});const j=await r.json();s.textContent=r.ok?'已保存，设备正在重启。':'保存失败：'+(j.error||j.detail)}catch(e){s.textContent='连接中断；如果设备正在重启，这是正常现象。'}}</script></html>)HTML";
  httpd_resp_set_type(request, "text/html; charset=utf-8");
  return httpd_resp_send(request, page, HTTPD_RESP_USE_STRLEN);
}

esp_err_t configHandler(httpd_req_t *request) {
  if (!allowConfiguration || activeConfig == nullptr || !configurationUsesAccessPoint(request)) {
    return sendJson(request, "{\"detail\":\"configuration_locked\"}", "403 Forbidden");
  }
  // JSON-only requests require a browser preflight from another origin. This
  // server supplies no CORS permission, so a hostile website cannot re-pair it
  // with a simple text/plain or form POST while the user is on the setup AP.
  char contentType[64]{};
  const size_t typeLength = httpd_req_get_hdr_value_len(request, "Content-Type");
  if (!typeLength || typeLength >= sizeof(contentType) ||
      httpd_req_get_hdr_value_str(request, "Content-Type", contentType, sizeof(contentType)) != ESP_OK ||
      (strcmp(contentType, "application/json") != 0 && strncmp(contentType, "application/json;", 17) != 0)) {
    return sendJson(request, "{\"detail\":\"json_required\"}", "415 Unsupported Media Type");
  }
  if (request->content_len <= 0 || request->content_len > kMaxHttpConfig) {
    return sendJson(request, "{\"detail\":\"invalid_length\"}", "422 Unprocessable Entity");
  }
  String input;
  input.reserve(request->content_len + 1);
  char buffer[129];
  int remaining = request->content_len;
  while (remaining > 0) {
    const int wanted = min(remaining, 128);
    const int count = httpd_req_recv(request, buffer, wanted);
    if (count <= 0) return sendJson(request, "{\"detail\":\"read_failed\"}", "408 Request Timeout");
    input.concat(buffer, count);
    remaining -= count;
  }
  String error;
  bool reused = false;
  if (!storeProvisioningConfig(input, *activeConfig, error, &reused)) {
    JsonDocument doc;
    doc["success"] = false;
    doc["error"] = error;
    return sendJson(request, jsonText(doc), "422 Unprocessable Entity");
  }
  if (reused) return sendJson(request, "{\"success\":true,\"restart_required\":false}");
  configureCameraAccess("");
  sendJson(request, "{\"success\":true,\"restart_required\":true}");
  delay(250);
  ESP.restart();
  return ESP_OK;
}
}  // namespace

void configureCameraAccess(const String &deviceToken) {
  // Backend retains only SHA256(device_token), so the HMAC key must be the
  // 32 digest bytes, not the raw device token or its 64-character hex string.
  char next[65]{};
  uint8_t key[32]{};
  uint8_t digest[32]{};
  if (deviceToken.length() >= 24 && deviceToken.length() <= 256) {
    const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    const int hashed = mbedtls_sha256_ret(reinterpret_cast<const unsigned char *>(deviceToken.c_str()),
                                        deviceToken.length(), key, 0);
    if (info && hashed == 0 &&
        mbedtls_md_hmac(info, key, sizeof(key),
                        reinterpret_cast<const unsigned char *>(kCameraReadPurpose),
                        sizeof(kCameraReadPurpose) - 1, digest) == 0) {
      static const char hex[] = "0123456789abcdef";
      for (size_t index = 0; index < sizeof(digest); ++index) {
        next[index * 2] = hex[digest[index] >> 4];
        next[index * 2 + 1] = hex[digest[index] & 15];
      }
    }
  }
  mbedtls_platform_zeroize(key, sizeof(key));
  mbedtls_platform_zeroize(digest, sizeof(digest));
  // Hashing and network I/O must never run under the cross-core lock.
  portENTER_CRITICAL(&accessMux);
  if (memcmp(cameraReadToken, next, sizeof(cameraReadToken)) != 0) {
    memcpy(cameraReadToken, next, sizeof(cameraReadToken));
    ++cameraAccessGeneration;
  }
  portEXIT_CRITICAL(&accessMux);
  mbedtls_platform_zeroize(next, sizeof(next));
}

bool initializeCamera(const DeviceConfig &config) {
  if (initialized) return true;
  camera_config_t camera{};
  camera.ledc_channel = LEDC_CHANNEL_0;
  camera.ledc_timer = LEDC_TIMER_0;
  camera.pin_d0 = Y2_GPIO_NUM;
  camera.pin_d1 = Y3_GPIO_NUM;
  camera.pin_d2 = Y4_GPIO_NUM;
  camera.pin_d3 = Y5_GPIO_NUM;
  camera.pin_d4 = Y6_GPIO_NUM;
  camera.pin_d5 = Y7_GPIO_NUM;
  camera.pin_d6 = Y8_GPIO_NUM;
  camera.pin_d7 = Y9_GPIO_NUM;
  camera.pin_xclk = XCLK_GPIO_NUM;
  camera.pin_pclk = PCLK_GPIO_NUM;
  camera.pin_vsync = VSYNC_GPIO_NUM;
  camera.pin_href = HREF_GPIO_NUM;
  camera.pin_sccb_sda = SIOD_GPIO_NUM;
  camera.pin_sccb_scl = SIOC_GPIO_NUM;
  camera.pin_pwdn = PWDN_GPIO_NUM;
  camera.pin_reset = RESET_GPIO_NUM;
  camera.xclk_freq_hz = 20000000;
  camera.pixel_format = PIXFORMAT_JPEG;
  camera.frame_size = static_cast<framesize_t>(min<uint8_t>(config.frameSize, FRAMESIZE_SVGA));
  camera.jpeg_quality = constrain(config.jpegQuality, 8, 30);
  camera.fb_count = psramFound() ? 2 : 1;
  camera.grab_mode = psramFound() ? CAMERA_GRAB_LATEST : CAMERA_GRAB_WHEN_EMPTY;
  camera.fb_location = psramFound() ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;
  const esp_err_t result = esp_camera_init(&camera);
  initialized = result == ESP_OK;
  return initialized;
}

bool cameraReady() { return initialized && esp_camera_sensor_get() != nullptr; }

String deviceBaseUrl() {
  const IPAddress address = WiFi.status() == WL_CONNECTED ? WiFi.localIP() : WiFi.softAPIP();
  return String("http://") + address.toString();
}

String deviceStreamUrl() {
  const IPAddress address = WiFi.status() == WL_CONNECTED ? WiFi.localIP() : WiFi.softAPIP();
  return String("http://") + address.toString() + ":81/stream";
}

void setPairingMode(bool enabled) { allowConfiguration = enabled; }

bool startCameraServer(DeviceConfig *config, bool pairingMode) {
  activeConfig = config;
  allowConfiguration = pairingMode;
  if (server) return true;
  httpd_config_t httpConfig = HTTPD_DEFAULT_CONFIG();
  httpConfig.max_uri_handlers = 8;
  httpConfig.stack_size = 8192;
  if (httpd_start(&server, &httpConfig) != ESP_OK) return false;

  // A MJPEG handler is intentionally long-lived. Run it on a second HTTP
  // server task so /health, /device, and /capture remain responsive while the
  // vision service consumes the stream.
  httpd_config_t streamConfig = HTTPD_DEFAULT_CONFIG();
  streamConfig.server_port = 81;
  streamConfig.ctrl_port = httpConfig.ctrl_port + 1;
  streamConfig.max_uri_handlers = 2;
  streamConfig.stack_size = 8192;
  if (httpd_start(&streamServer, &streamConfig) != ESP_OK) {
    httpd_stop(server);
    server = nullptr;
    return false;
  }

  const httpd_uri_t root = {.uri = "/", .method = HTTP_GET, .handler = rootHandler, .user_ctx = nullptr};
  const httpd_uri_t health = {.uri = "/health", .method = HTTP_GET, .handler = healthHandler, .user_ctx = nullptr};
  const httpd_uri_t device = {.uri = "/device", .method = HTTP_GET, .handler = deviceHandler, .user_ctx = nullptr};
  const httpd_uri_t capture = {.uri = "/capture", .method = HTTP_GET, .handler = captureHandler, .user_ctx = nullptr};
  const httpd_uri_t streamRedirect = {.uri = "/stream", .method = HTTP_GET, .handler = streamRedirectHandler, .user_ctx = nullptr};
  const httpd_uri_t stream = {.uri = "/stream", .method = HTTP_GET, .handler = streamHandler, .user_ctx = nullptr};
  const httpd_uri_t configure = {.uri = "/config", .method = HTTP_POST, .handler = configHandler, .user_ctx = nullptr};
  httpd_register_uri_handler(server, &root);
  httpd_register_uri_handler(server, &health);
  httpd_register_uri_handler(server, &device);
  httpd_register_uri_handler(server, &capture);
  httpd_register_uri_handler(server, &streamRedirect);
  httpd_register_uri_handler(server, &configure);
  httpd_register_uri_handler(streamServer, &stream);
  return true;
}
