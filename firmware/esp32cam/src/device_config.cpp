#include "device_config.h"

#include <ArduinoJson.h>
#include <IPAddress.h>
#include <Preferences.h>
#include <mbedtls/sha256.h>
#include <mbedtls/platform_util.h>

namespace {
constexpr const char *kNamespace = "objectmemory";
constexpr size_t kMaxConfigMessage = 1024;

bool validSimpleName(const String &value, size_t maxLength) {
  if (value.isEmpty() || value.length() > maxLength) return false;
  for (size_t i = 0; i < value.length(); ++i) {
    const char c = value.charAt(i);
    if (c == '\r' || c == '\n' || static_cast<uint8_t>(c) < 0x20) return false;
  }
  return true;
}

bool validDeviceId(const String &value) {
  if (value.length() < 4 || value.length() > 64) return false;
  for (size_t i = 0; i < value.length(); ++i) {
    const char c = value.charAt(i);
    if (!(isalnum(static_cast<unsigned char>(c)) || c == '-' || c == '_')) return false;
  }
  return true;
}

bool validPairingCode(const String &value) {
  if (!value.startsWith("OM-") || value.length() < 7 || value.length() > 20) return false;
  for (size_t i = 3; i < value.length(); ++i) {
    if (!isalnum(static_cast<unsigned char>(value.charAt(i)))) return false;
  }
  return true;
}

bool validBackendUrl(const String &value) {
  if (!value.startsWith("http://") || value.length() > 200) return false;
  if (value.indexOf('\r') >= 0 || value.indexOf('\n') >= 0 || value.indexOf('@') >= 0) return false;
  const int hostStart = 7;
  int hostEnd = value.indexOf('/', hostStart);
  if (hostEnd < 0) hostEnd = value.length();
  int portSeparator = value.indexOf(':', hostStart);
  if (portSeparator >= 0 && portSeparator < hostEnd) hostEnd = portSeparator;
  const String host = value.substring(hostStart, hostEnd);
  IPAddress ip;
  if (!ip.fromString(host)) return false;
  const bool privateAddress = ip[0] == 10 ||
      (ip[0] == 172 && ip[1] >= 16 && ip[1] <= 31) ||
      (ip[0] == 192 && ip[1] == 168) ||
      (ip[0] == 169 && ip[1] == 254);
  // 127.0.0.1 on an ESP32 means the ESP32 itself, never the Windows backend.
  return privateAddress;
}

String normalizedBackendUrl(String value) {
  value.trim();
  while (value.endsWith("/")) value.remove(value.length() - 1);
  return value;
}
}  // namespace

bool loadDeviceConfig(DeviceConfig &config) {
  Preferences prefs;
  if (!prefs.begin(kNamespace, true)) return false;
  config.ssid = prefs.getString("ssid", "");
  config.password = prefs.getString("passwd", "");
  config.backendUrl = prefs.getString("backend", "");
  config.pairingCode = prefs.getString("pair", "");
  config.deviceId = prefs.getString("devid", "");
  config.deviceName = prefs.getString("devname", "");
  config.roomName = prefs.getString("room", "");
  config.deviceToken = prefs.getString("token", "");
  config.cameraId = prefs.getString("camid", "");
  config.lastProvisioningHash = prefs.getString("lastcfg", "");
  config.jpegQuality = prefs.getUChar("quality", 12);
  config.frameSize = prefs.getUChar("framesize", 8);
  prefs.end();
  return hasProvisioningConfig(config);
}

bool hasProvisioningConfig(const DeviceConfig &config) {
  return !config.ssid.isEmpty() && !config.backendUrl.isEmpty() &&
         !config.deviceId.isEmpty() &&
         (!config.pairingCode.isEmpty() || !config.deviceToken.isEmpty());
}

bool storeProvisioningConfig(const String &input, DeviceConfig &config, String &error,
                             bool *reused) {
  if (reused) *reused = false;
  error = "";
  if (input.length() > kMaxConfigMessage) {
    error = "message_too_long";
    return false;
  }
  String payload = input;
  payload.trim();
  if (payload.startsWith("OMCFG:")) payload.remove(0, 6);

  JsonDocument doc;
  const DeserializationError jsonError =
      deserializeJson(doc, payload, DeserializationOption::NestingLimit(3));
  if (jsonError || !doc.is<JsonObject>()) {
    error = "invalid_json";
    return false;
  }

  const char *allowed[] = {"ssid", "password", "backend_url", "pairing_code",
                           "device_id", "device_name", "room_name",
                           "stream_quality", "frame_size"};
  for (JsonPairConst pair : doc.as<JsonObjectConst>()) {
    bool known = false;
    for (const char *key : allowed) {
      if (strcmp(pair.key().c_str(), key) == 0) {
        known = true;
        break;
      }
    }
    if (!known) {
      error = "unknown_field";
      return false;
    }
  }

  const String ssid = doc["ssid"] | "";
  const String password = doc["password"] | "";
  const String backendUrl = normalizedBackendUrl(String(doc["backend_url"] | ""));
  const String pairingCode = doc["pairing_code"] | "";
  const String deviceId = doc["device_id"] | "";
  const String deviceName = doc["device_name"] | "";
  const String roomName = doc["room_name"] | "";
  const int streamQuality = doc["stream_quality"] | 12;
  String requestedFrameSize = doc["frame_size"] | "VGA";
  requestedFrameSize.toUpperCase();
  uint8_t frameSize = 8;
  if (requestedFrameSize == "QVGA") frameSize = 5;
  else if (requestedFrameSize == "SVGA") frameSize = 9;

  if (!validSimpleName(ssid, 32)) error = "invalid_ssid";
  else if (password.length() > 63 || (password.length() > 0 && password.length() < 8)) error = "invalid_wifi_password";
  else if (!validBackendUrl(backendUrl)) error = "invalid_backend_url";
  else if (!validPairingCode(pairingCode)) error = "invalid_pairing_code";
  else if (!validDeviceId(deviceId)) error = "invalid_device_id";
  else if (!validSimpleName(deviceName, 64)) error = "invalid_device_name";
  else if (!validSimpleName(roomName, 64)) error = "invalid_room_name";
  else if (streamQuality < 8 || streamQuality > 30) error = "invalid_stream_quality";
  else if (requestedFrameSize != "QVGA" && requestedFrameSize != "VGA" && requestedFrameSize != "SVGA") error = "invalid_frame_size";
  if (!error.isEmpty()) return false;

  // Canonical field order/defaults make JSON whitespace/order irrelevant. The
  // one-time pairing code distinguishes a new administrator authorization from
  // a retransmission even after successful claim has removed the stored code.
  JsonDocument canonical;
  canonical["ssid"] = ssid;
  canonical["password"] = password;
  canonical["backend_url"] = backendUrl;
  canonical["pairing_code"] = pairingCode;
  canonical["device_id"] = deviceId;
  canonical["device_name"] = deviceName;
  canonical["room_name"] = roomName;
  canonical["stream_quality"] = streamQuality;
  canonical["frame_size"] = requestedFrameSize;
  String normalized;
  serializeJson(canonical, normalized);
  uint8_t hash[32]{};
  if (mbedtls_sha256_ret(reinterpret_cast<const unsigned char *>(normalized.c_str()),
                         normalized.length(), hash, 0) != 0) {
    error = "config_hash_failed";
    return false;
  }
  char hex[65]{};
  const char *digits = "0123456789abcdef";
  for (size_t i = 0; i < sizeof(hash); ++i) {
    hex[i * 2] = digits[hash[i] >> 4];
    hex[i * 2 + 1] = digits[hash[i] & 15];
  }
  mbedtls_platform_zeroize(hash, sizeof(hash));
  const String digest(hex);
  Preferences prefs;
  if (!prefs.begin(kNamespace, false)) {
    error = "nvs_open_failed";
    return false;
  }
  // A hash receipt surviving independently damaged/missing NVS keys is not a
  // successful duplicate. Verify both loaded state and stored fields first.
  const bool sameFields = config.ssid == ssid && config.password == password &&
      config.backendUrl == backendUrl && config.deviceId == deviceId &&
      config.deviceName == deviceName && config.roomName == roomName &&
      config.jpegQuality == streamQuality && config.frameSize == frameSize &&
      prefs.getString("ssid", "") == ssid && prefs.getString("passwd", "") == password &&
      prefs.getString("backend", "") == backendUrl && prefs.getString("devid", "") == deviceId &&
      prefs.getString("devname", "") == deviceName && prefs.getString("room", "") == roomName &&
      prefs.getUChar("quality", 255) == streamQuality && prefs.getUChar("framesize", 255) == frameSize;
  const bool samePendingPair = config.pairingCode == pairingCode && prefs.getString("pair", "") == pairingCode;
  const bool sameClaimedPair = config.pairingCode.isEmpty() && prefs.getString("pair", "").isEmpty() &&
      !config.deviceToken.isEmpty() && !config.cameraId.isEmpty() &&
      prefs.getString("token", "") == config.deviceToken && prefs.getString("camid", "") == config.cameraId;
  if (sameFields && (samePendingPair || sameClaimedPair) &&
      config.lastProvisioningHash == digest && prefs.getString("lastcfg", "") == digest) {
    prefs.end();
    if (reused) *reused = true;
    return true;  // No writes, credential rotation or reboot for an accepted retry.
  }
  // A partial write must never retain a receipt for either the previous or new
  // configuration. Publish the receipt last, only after all writes are checked.
  if (prefs.isKey("lastcfg") && !prefs.remove("lastcfg")) {
    prefs.end();
    error = "nvs_write_failed";
    return false;
  }
  bool ok = true;
  ok &= prefs.putString("ssid", ssid) > 0;
  // An open Wi-Fi network is allowed, so an empty stored password is valid.
  const size_t passwordWritten = prefs.putString("passwd", password);
  ok &= (password.isEmpty() || passwordWritten > 0) && prefs.getString("passwd", "") == password;
  ok &= prefs.putString("backend", backendUrl) > 0;
  ok &= prefs.putString("pair", pairingCode) > 0;
  ok &= prefs.putString("devid", deviceId) > 0;
  ok &= prefs.putString("devname", deviceName) > 0;
  ok &= prefs.putString("room", roomName) > 0;
  ok &= prefs.putUChar("quality", static_cast<uint8_t>(streamQuality)) == 1;
  ok &= prefs.putUChar("framesize", frameSize) == 1;
  prefs.remove("token");
  prefs.remove("camid");
  ok &= prefs.getString("token", "").isEmpty() && prefs.getString("camid", "").isEmpty();
  if (!ok) {
    prefs.end();
    error = "nvs_write_failed";
    return false;
  }
  if (prefs.putString("lastcfg", digest) != digest.length() || prefs.getString("lastcfg", "") != digest) {
    prefs.end();
    error = "nvs_write_failed";
    return false;
  }
  prefs.end();

  config.ssid = ssid;
  config.password = password;
  config.backendUrl = backendUrl;
  config.pairingCode = pairingCode;
  config.deviceId = deviceId;
  config.deviceName = deviceName;
  config.roomName = roomName;
  config.jpegQuality = static_cast<uint8_t>(streamQuality);
  config.frameSize = frameSize;
  config.deviceToken = "";
  config.cameraId = "";
  config.lastProvisioningHash = digest;
  return true;
}

bool storeClaimCredentials(DeviceConfig &config, const String &cameraId,
                           const String &deviceToken) {
  if (cameraId.isEmpty() || deviceToken.length() < 24 || deviceToken.length() > 256) return false;
  Preferences prefs;
  if (!prefs.begin(kNamespace, false)) return false;
  const bool ok = prefs.putString("camid", cameraId) > 0 &&
                  prefs.putString("token", deviceToken) > 0;
  if (ok) prefs.remove("pair");
  prefs.end();
  if (ok) {
    config.cameraId = cameraId;
    config.deviceToken = deviceToken;
    config.pairingCode = "";
  }
  return ok;
}

void clearClaimCredentials(DeviceConfig &config) {
  // A revoked enrollment must not erase the user's Wi-Fi/network settings.
  Preferences prefs;
  if (prefs.begin(kNamespace, false)) {
    prefs.remove("token");
    prefs.remove("camid");
    prefs.end();
  }
  config.deviceToken = "";
  config.cameraId = "";
}

void clearDeviceConfig(DeviceConfig &config) {
  Preferences prefs;
  if (prefs.begin(kNamespace, false)) {
    prefs.clear();
    prefs.end();
  }
  config = DeviceConfig{};
}
