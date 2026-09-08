#pragma once

#include <Arduino.h>
#include "device_config.h"

bool initializeCamera(const DeviceConfig &config);
bool cameraReady();
// Purpose-separated read credential is derived once and kept only in RAM.
// Passing an empty token revokes new requests and any active MJPEG stream.
void configureCameraAccess(const String &deviceToken);
bool startCameraServer(DeviceConfig *config, bool pairingMode);
void setPairingMode(bool enabled);
String deviceBaseUrl();
String deviceStreamUrl();
