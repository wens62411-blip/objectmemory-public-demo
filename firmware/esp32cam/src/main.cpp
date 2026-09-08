#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <esp_mac.h>
#include <esp_task_wdt.h>

#include "camera_server.h"
#include "device_config.h"
#include "board_config.h"

namespace {
constexpr unsigned long kWifiRetryMs = 10000;
constexpr unsigned long kHeartbeatMs = 15000;
constexpr unsigned long kClaimRetryMs = 10000;
constexpr unsigned long kDhcpWaitMs = 30000;
constexpr unsigned long kBackendConnectTimeoutMs = 2000;
constexpr unsigned long kBackendReadTimeoutMs = 2500;
constexpr size_t kMaxControlResponse = 4096;
constexpr size_t kMaxSerialLine = 1024;

DeviceConfig config;
String serialLine;
unsigned long lastWifiAttempt = 0;
unsigned long lastHeartbeat = 0;
unsigned long lastClaimAttempt = 0;
unsigned long lastCameraAttempt = 0;
unsigned long lastReadyPrint = 0;
uint8_t readyPrintCount = 0;
bool pairingMode = false;
bool backendAuthenticated = false;
unsigned long lastApAttempt = 0;
portMUX_TYPE wifiEventMux = portMUX_INITIALIZER_UNLOCKED;
uint8_t wifiEvents = 0;
uint8_t lastDisconnectReason = 0;
bool wifiAssociated = false;
unsigned long associatedAt = 0;

void onWifiEvent(arduino_event_id_t event, arduino_event_info_t info) {
  // Wi-Fi callbacks run in another task. Never print serial protocol records
  // here: report them from loop() so OMACK/OMIDENT cannot be interleaved.
  portENTER_CRITICAL(&wifiEventMux);
  if (event == ARDUINO_EVENT_WIFI_STA_CONNECTED) {
    wifiEvents |= 1;
    wifiAssociated = true;
    associatedAt = millis();
  } else if (event == ARDUINO_EVENT_WIFI_STA_GOT_IP) {
    wifiEvents |= 2;
  } else if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
    wifiEvents |= 4;
    wifiAssociated = false;
    lastDisconnectReason = info.wifi_sta_disconnected.reason;
  } else if (event == ARDUINO_EVENT_WIFI_STA_LOST_IP) {
    wifiEvents |= 8;
  }
  portEXIT_CRITICAL(&wifiEventMux);
}

void logEvent(const char *level, const char *event, const String &detail = "") {
  JsonDocument doc;
  doc["level"] = level;
  doc["event"] = event;
  if (!detail.isEmpty()) doc["detail"] = detail;
  Serial.print("OMLOG:");
  serializeJson(doc, Serial);
  Serial.println();
}

void reportWifiStatus() {
  portENTER_CRITICAL(&wifiEventMux);
  const uint8_t pending = wifiEvents;
  const uint8_t reason = lastDisconnectReason;
  wifiEvents = 0;
  portEXIT_CRITICAL(&wifiEventMux);
  if (pending & (4 | 8)) {
    backendAuthenticated = false;
    configureCameraAccess("");
    if (pending & 4) {
      const char *event = "wifi_disconnected";
      if (reason == WIFI_REASON_NO_AP_FOUND) event = "wifi_no_2_4ghz_ap_found";
      else if (reason == WIFI_REASON_AUTH_FAIL || reason == WIFI_REASON_HANDSHAKE_TIMEOUT ||
               reason == WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT) event = "wifi_authentication_failed";
      logEvent("warn", event, String("reason=") + reason);
    } else logEvent("warn", "wifi_address_lost");
  }
  if (pending & 1) logEvent("info", "wifi_associated_waiting_dhcp");
  if ((pending & 2) && WiFi.status() == WL_CONNECTED) {
    logEvent("info", "wifi_got_ip", WiFi.localIP().toString() + " channel=" + WiFi.channel());
  }
}

bool readControlResponse(HTTPClient &http, String &response) {
  // Our FastAPI JSON replies have Content-Length. Unknown/chunked or oversized
  // bodies fail closed instead of allowing an unbounded String allocation.
  const int size = http.getSize();
  if (size <= 0 || size > static_cast<int>(kMaxControlResponse)) return false;
  response = "";
  response.reserve(size + 1);
  WiFiClient *stream = http.getStreamPtr();
  const unsigned long started = millis();
  while (response.length() < static_cast<size_t>(size) && millis() - started < kBackendReadTimeoutMs) {
    const int available = stream->available();
    if (available > 0) {
      char buffer[128];
      const int wanted = min(min(available, static_cast<int>(sizeof(buffer))), size - static_cast<int>(response.length()));
      const int count = stream->read(reinterpret_cast<uint8_t *>(buffer), wanted);
      if (count <= 0) return false;
      response.concat(buffer, count);
    } else if (!http.connected()) return false;
    else delay(1);
  }
  return response.length() == static_cast<size_t>(size);
}

String localStreamUrl() { return deviceStreamUrl(); }
String localCaptureUrl() { return deviceBaseUrl() + "/capture"; }

void printAck(bool success, const String &error = "") {
  JsonDocument doc;
  doc["success"] = success;
  if (success) doc["device_id"] = config.deviceId;
  else doc["error"] = error;
  Serial.print("OMACK:");
  serializeJson(doc, Serial);
  Serial.println();
}

void printReady() {
  if (!backendAuthenticated || WiFi.status() != WL_CONNECTED) return;
  JsonDocument doc;
  doc["device_id"] = config.deviceId;
  doc["camera_id"] = config.cameraId;
  doc["ip"] = WiFi.localIP().toString();
  doc["stream_url"] = localStreamUrl();
  doc["capture_url"] = localCaptureUrl();
  doc["board_type"] = OM_BOARD_TYPE;
  doc["chip"] = OM_CHIP_TYPE;
  doc["auth_ready"] = backendAuthenticated;
  doc["camera_ready"] = cameraReady();
  Serial.print("OMREADY:");
  serializeJson(doc, Serial);
  Serial.println();
  lastReadyPrint = millis();
  if (readyPrintCount < 255) ++readyPrintCount;
}

void processConfigLine(const String &line) {
  if (line.startsWith("OMWHO:")) {
    // No Wi-Fi/configuration required. Match the base eFuse MAC read by ROM,
    // including first boot in AP-only mode; never echo stored credentials.
    const String nonce = line.substring(6);
    if (nonce.length() != 32) return;
    for (size_t index = 0; index < nonce.length(); ++index) {
      const char c = nonce[index];
      if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return;
    }
    uint8_t mac[6];
    if (esp_efuse_mac_get_default(mac) != ESP_OK) return;
    char address[18];
    snprintf(address, sizeof(address), "%02x:%02x:%02x:%02x:%02x:%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    JsonDocument doc;
    doc["nonce"] = nonce;
    doc["mac_address"] = address;
    doc["protocol"] = 1;
    doc["board_type"] = OM_BOARD_TYPE;
    doc["chip"] = OM_CHIP_TYPE;
    Serial.print("OMIDENT:");
    serializeJson(doc, Serial);
    Serial.println();
    return;
  }
  if (!line.startsWith("OMCFG:")) return;
  String error;
  bool reused = false;
  if (!storeProvisioningConfig(line, config, error, &reused)) {
    printAck(false, error);
    return;
  }
  if (reused) {
    printAck(true);
    printReady();  // Only emitted if this boot has actually authenticated.
    return;
  }
  backendAuthenticated = false;
  configureCameraAccess("");
  printAck(true);
  delay(150);
  ESP.restart();
}

void readSerialConfiguration() {
  while (Serial.available()) {
    const char c = static_cast<char>(Serial.read());
    if (c == '\n') {
      serialLine.trim();
      processConfigLine(serialLine);
      serialLine = "";
    } else if (c != '\r') {
      if (serialLine.length() >= kMaxSerialLine) {
        serialLine = "";
        printAck(false, "message_too_long");
      } else {
        serialLine += c;
      }
    }
  }
}

void startPairingAccessPoint() {
  lastApAttempt = millis();
  const uint64_t chip = ESP.getEfuseMac();
  char suffix[5];
  snprintf(suffix, sizeof(suffix), "%04X", static_cast<unsigned>(chip & 0xFFFF));
  const String apName = String("ObjectMemory-") + suffix;
  if (!WiFi.mode(hasProvisioningConfig(config) ? WIFI_AP_STA : WIFI_AP) ||
      !WiFi.softAP(apName.c_str())) {
    pairingMode = false;
    setPairingMode(false);
    logEvent("error", "pairing_ap_failed");
    return;
  }
  pairingMode = true;
  setPairingMode(true);
  logEvent("info", "pairing_ap_ready", apName + " ip=" + WiFi.softAPIP().toString());
}

void connectWifi() {
  if (!hasProvisioningConfig(config)) return;
  lastWifiAttempt = millis();
  if (!WiFi.mode(pairingMode ? WIFI_AP_STA : WIFI_STA)) {
    logEvent("error", "wifi_mode_failed");
    return;
  }
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  WiFi.begin(config.ssid.c_str(), config.password.c_str());
  logEvent("info", "wifi_connecting", "2.4GHz_only");
}

bool parseClaimResponse(const String &body) {
  JsonDocument doc;
  if (deserializeJson(doc, body, DeserializationOption::NestingLimit(4)) || !(doc["success"] | false)) {
    logEvent("warn", "claim_response_invalid");
    return false;
  }
  const String cameraId = doc["camera_id"] | "";
  const String deviceToken = doc["device_token"] | "";
  if (cameraId.isEmpty() || deviceToken.length() < 24 || deviceToken.length() > 256) {
    logEvent("warn", "claim_credentials_invalid");
    return false;
  }
  if (!storeClaimCredentials(config, cameraId, deviceToken)) {
    logEvent("error", "claim_nvs_write_failed");
    return false;
  }
  configureCameraAccess(config.deviceToken);
  backendAuthenticated = true;
  return true;
}

bool claimDevice() {
  if (config.pairingCode.isEmpty() || WiFi.status() != WL_CONNECTED) return false;
  HTTPClient http;
  http.setConnectTimeout(kBackendConnectTimeoutMs);
  http.setTimeout(kBackendReadTimeoutMs);
  if (!http.begin(config.backendUrl + "/api/device-enrollment/claim")) {
    logEvent("warn", "claim_backend_url_invalid");
    return false;
  }
  http.addHeader("Content-Type", "application/json");
  JsonDocument doc;
  doc["device_id"] = config.deviceId;
  doc["pairing_code"] = config.pairingCode;
  doc["mac_address"] = WiFi.macAddress();
  doc["firmware_version"] = "0.1.0";
  doc["ip_address"] = WiFi.localIP().toString();
  doc["stream_url"] = localStreamUrl();
  doc["capture_url"] = localCaptureUrl();
  JsonArray capabilities = doc["capabilities"].to<JsonArray>();
  capabilities.add("camera");
  capabilities.add("mjpeg");
  capabilities.add("capture");
  capabilities.add("camera_read_hmac_v1");
  String body;
  serializeJson(doc, body);
  const int status = http.POST(body);
  String response;
  const bool validResponse = status == HTTP_CODE_OK && readControlResponse(http, response);
  http.end();
  if (status != HTTP_CODE_OK) {
    logEvent("warn", "claim_http_failed", String(status));
    return false;
  }
  if (!validResponse) { logEvent("warn", "claim_response_invalid"); return false; }
  if (!parseClaimResponse(response)) return false;
  logEvent("info", "device_claimed");
  printReady();
  return true;
}

void applyCommand(const String &command) {
  if (command == "reboot") {
    logEvent("info", "reboot_command_received");
    delay(150);
    ESP.restart();
  } else if (command == "factory_reset") {
    logEvent("info", "factory_reset_command_received");
    configureCameraAccess("");
    clearDeviceConfig(config);
    delay(150);
    ESP.restart();
  }
}

bool sendHeartbeat() {
  if (config.deviceToken.isEmpty() || WiFi.status() != WL_CONNECTED) return false;
  HTTPClient http;
  http.setConnectTimeout(kBackendConnectTimeoutMs);
  http.setTimeout(kBackendReadTimeoutMs);
  if (!http.begin(config.backendUrl + "/api/devices/" + config.deviceId + "/heartbeat")) {
    logEvent("warn", "heartbeat_backend_url_invalid");
    return false;
  }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", "Bearer " + config.deviceToken);
  JsonDocument doc;
  doc["uptime"] = millis() / 1000;
  doc["wifi_rssi"] = WiFi.RSSI();
  doc["free_heap"] = ESP.getFreeHeap();
  doc["camera_status"] = cameraReady() ? "ready" : "error";
  doc["stream_status"] = cameraReady() ? "ready" : "error";
  doc["firmware_version"] = "0.1.0";
  doc["ip_address"] = WiFi.localIP().toString();
  String body;
  serializeJson(doc, body);
  const int status = http.POST(body);
  String response;
  const bool validResponse = status == HTTP_CODE_OK && readControlResponse(http, response);
  http.end();
  if (status == HTTP_CODE_UNAUTHORIZED) {
    backendAuthenticated = false;
    configureCameraAccess("");
    clearClaimCredentials(config);
    logEvent("warn", "device_authorization_revoked");
    return false;
  }
  if (status != HTTP_CODE_OK) { logEvent("warn", "heartbeat_http_failed", String(status)); return false; }
  if (!validResponse) { logEvent("warn", "heartbeat_response_invalid"); return false; }
  JsonDocument reply;
  if (deserializeJson(reply, response, DeserializationOption::NestingLimit(5)) || !(reply["success"] | false)) {
    logEvent("warn", "heartbeat_response_invalid");
    return false;
  }
  // Repeated success keeps the same generation: it must not interrupt MJPEG.
  configureCameraAccess(config.deviceToken);
  backendAuthenticated = true;
  for (JsonObject command : reply["commands"].as<JsonArray>()) {
    applyCommand(String(command["command"] | ""));
  }
  return true;
}
}  // namespace

void setup() {
  Serial.begin(115200);
  // Native USB on XIAO must also run headlessly: never wait indefinitely for a
  // serial monitor. The loop accepts a fresh OMWHO challenge after USB opens.
  Serial.setTimeout(100);
  serialLine.reserve(kMaxSerialLine + 1);
  delay(300);
  logEvent("info", "boot", "0.1.0");
  WiFi.onEvent(onWifiEvent);

  // Arduino 2.0.17 uses the two-argument watchdog API.
  esp_task_wdt_init(30, true);
  esp_task_wdt_add(nullptr);

  const bool configured = loadDeviceConfig(config);
  // Persisted tokens may have been revoked while powered off. Video remains
  // closed until the backend confirms a heartbeat or issues a fresh claim.
  configureCameraAccess("");
  for (int attempt = 0; attempt < 3 && !cameraReady(); ++attempt) {
    if (!initializeCamera(config)) delay(500);
  }
  if (!cameraReady()) logEvent("error", "camera_init_failed");

  if (configured) connectWifi();
  else startPairingAccessPoint();
  if (!startCameraServer(&config, pairingMode)) logEvent("error", "camera_http_server_failed");
}

void loop() {
  esp_task_wdt_reset();
  readSerialConfiguration();
  reportWifiStatus();
  const unsigned long now = millis();
  portENTER_CRITICAL(&wifiEventMux);
  const bool associated = wifiAssociated;
  const unsigned long dhcpStarted = associatedAt;
  portEXIT_CRITICAL(&wifiEventMux);
  const bool waitingForDhcp = associated && now - dhcpStarted < kDhcpWaitMs;
  if (hasProvisioningConfig(config) && WiFi.status() != WL_CONNECTED && !waitingForDhcp && now - lastWifiAttempt >= kWifiRetryMs) {
    if (associated) logEvent("warn", "wifi_dhcp_timeout");
    WiFi.disconnect();
    connectWifi();
  }
  if (!backendAuthenticated && !pairingMode && now > 30000 && now - lastApAttempt >= kWifiRetryMs) {
    startPairingAccessPoint();
    if (pairingMode) logEvent("warn", "wifi_fallback_pairing_enabled");
  }
  if (!cameraReady() && now - lastCameraAttempt >= kWifiRetryMs) {
    lastCameraAttempt = now;
    initializeCamera(config);
  }
  if (WiFi.status() == WL_CONNECTED) {
    if (config.deviceToken.isEmpty() && now - lastClaimAttempt >= kClaimRetryMs) {
      lastClaimAttempt = now;
      claimDevice();
    } else if (!config.deviceToken.isEmpty() && now - lastHeartbeat >= kHeartbeatMs) {
      lastHeartbeat = now;
      if (!sendHeartbeat()) logEvent("warn", "heartbeat_failed");
    }
    if (pairingMode && backendAuthenticated) {
      if (WiFi.softAPdisconnect(true)) {
        WiFi.mode(WIFI_STA);
        pairingMode = false;
        setPairingMode(false);
        logEvent("info", "pairing_ap_closed_authenticated");
      } else logEvent("warn", "pairing_ap_close_failed");
    }
    // Repeat READY briefly after boot so a Windows serial port that reconnects
    // slowly after reset can still observe the completed binding.
    if (!config.deviceToken.isEmpty() && !config.cameraId.isEmpty() &&
        readyPrintCount < 12 && now - lastReadyPrint >= 5000) {
      printReady();
    }
  }
  delay(10);
}
