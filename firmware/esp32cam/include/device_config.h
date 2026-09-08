#pragma once

#include <Arduino.h>

struct DeviceConfig {
  String ssid;
  String password;
  String backendUrl;
  String pairingCode;
  String deviceId;
  String deviceName;
  String roomName;
  String deviceToken;
  String cameraId;
  String lastProvisioningHash;  // Local retry receipt only; never sent or logged.
  uint8_t jpegQuality = 12;
  uint8_t frameSize = 8;  // FRAMESIZE_VGA in esp32-camera sensor.h
};

bool loadDeviceConfig(DeviceConfig &config);
bool storeProvisioningConfig(const String &json, DeviceConfig &config, String &error,
                             bool *reused = nullptr);
bool storeClaimCredentials(DeviceConfig &config, const String &cameraId,
                           const String &deviceToken);
void clearClaimCredentials(DeviceConfig &config);
void clearDeviceConfig(DeviceConfig &config);
bool hasProvisioningConfig(const DeviceConfig &config);
