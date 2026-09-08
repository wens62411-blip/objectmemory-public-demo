"""Board source contracts; do not claim these are physical Wi-Fi/USB tests."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
FIRMWARE = ROOT / "firmware/esp32cam"


def source(name):
    return (FIRMWARE / "src" / name).read_text(encoding="utf-8")


def function(text, name):
    start = re.search(r"^(?:bool|void|String) " + name + r"\(", text, re.M)
    assert start, name
    return text[start.end():].split("\n}", 1)[0]


def test_wire_allowlist_does_not_expand_for_retry_metadata():
    text = source("device_config.cpp")
    fields = re.findall(r'"([^"]+)"', text.split("const char *allowed[] = {", 1)[1].split("};", 1)[0])
    assert set(fields) == {"ssid", "password", "backend_url", "pairing_code", "device_id", "device_name", "room_name", "stream_quality", "frame_size"}


def test_normalized_full_config_hash_includes_new_authorization_but_never_logs_secrets():
    text = source("device_config.cpp")
    body = function(text, "storeProvisioningConfig")
    assert 'canonical["pairing_code"] = pairingCode;' in body
    assert 'canonical["backend_url"] = backendUrl;' in body
    assert 'canonical["frame_size"] = requestedFrameSize;' in body
    assert 'mbedtls_sha256_ret(' in body
    assert 'prefs.getString("lastcfg", "") == digest' in body
    assert body.index('prefs.getString("lastcfg", "") == digest') < body.index('prefs.putString("ssid"')
    assert 'prefs.putString("lastcfg", digest)' in body
    assert 'config.lastProvisioningHash = digest;' in body
    assert 'config.lastProvisioningHash = prefs.getString("lastcfg", "");' in text
    assert "logEvent" not in body and "Serial." not in body


def test_repeat_survives_claim_clearing_pairing_code_but_new_config_invalidates_old_hash_first():
    text = source("device_config.cpp")
    claim = function(text, "storeClaimCredentials")
    assert 'prefs.remove("pair")' in claim
    assert 'lastcfg' not in claim and 'lastProvisioningHash' not in claim
    body = function(text, "storeProvisioningConfig")
    assert body.index('prefs.remove("lastcfg")') < body.index('prefs.putString("ssid"')
    assert body.index('if (!ok)') < body.index('prefs.putString("lastcfg", digest)')


def test_password_is_read_back_even_for_empty_open_network_before_success_hash():
    body = function(source("device_config.cpp"), "storeProvisioningConfig")
    assert 'prefs.getString("passwd", "") == password' in body
    assert 'password.isEmpty() || passwordWritten > 0' in body
    assert body.index('prefs.getString("passwd", "") == password') < body.index('prefs.putString("lastcfg", digest)')


@pytest.mark.parametrize("filename,name", [("main.cpp", "processConfigLine"), ("camera_server.cpp", "configHandler")])
def test_duplicate_acks_before_any_auth_clear_or_restart(filename, name):
    body = function(source(filename), name) if name != "configHandler" else source(filename).split('esp_err_t configHandler(', 1)[1].split('\n}', 1)[0]
    assert 'bool reused = false;' in body and '&reused)' in body
    assert body.index('if (reused)') < body.index('configureCameraAccess("")')
    duplicate = body.split('if (reused)', 1)[1].split('}', 1)[0]
    assert 'return' in duplicate and 'ESP.restart' not in duplicate


def test_ap_readiness_and_shutdown_require_actual_network_results():
    main = source("main.cpp")
    ap = function(main, "startPairingAccessPoint")
    assert '!WiFi.softAP(apName.c_str())' in ap and 'pairing_ap_failed' in ap
    assert 'WiFi.softAPIP().toString()' in ap
    loop = function(main, "loop")
    assert 'pairingMode && backendAuthenticated' in loop
    assert loop.index('claimDevice();') < loop.index('WiFi.softAPdisconnect(true)')
    assert '!backendAuthenticated' in loop.split('wifi_fallback_pairing_enabled')[0]


def test_wifi_diagnostics_distinguish_association_dhcp_and_failure_without_ssid_or_password():
    main = source("main.cpp")
    assert 'WiFi.onEvent(onWifiEvent);' in main
    assert 'ARDUINO_EVENT_WIFI_STA_CONNECTED' in main and 'ARDUINO_EVENT_WIFI_STA_GOT_IP' in main
    assert 'info.wifi_sta_disconnected.reason' in main
    for code in ['wifi_associated_waiting_dhcp', 'wifi_got_ip', 'wifi_no_2_4ghz_ap_found', 'wifi_authentication_failed', 'wifi_dhcp_timeout']:
        assert code in main
    status = function(main, "reportWifiStatus")
    assert 'config.ssid' not in status and 'config.password' not in status
    assert 'kDhcpWaitMs' in function(main, "loop")


def test_ready_requires_current_backend_auth_and_exposes_actual_camera_readiness():
    main = source("main.cpp")
    ready = function(main, "printReady")
    assert 'if (!backendAuthenticated || WiFi.status() != WL_CONNECTED) return;' in ready
    assert 'doc["auth_ready"] = backendAuthenticated;' in ready
    assert 'doc["camera_ready"] = cameraReady();' in ready
    assert 'backendAuthenticated = true;' in function(main, "parseClaimResponse")
    assert 'backendAuthenticated = true;' in function(main, "sendHeartbeat")
    assert 'backendAuthenticated = false;' in function(main, "reportWifiStatus")


def test_claim_error_categories_are_sanitized_and_control_responses_bounded():
    main = source("main.cpp")
    claim = function(main, "claimDevice")
    parse = function(main, "parseClaimResponse")
    for code in ['claim_backend_url_invalid', 'claim_http_failed', 'claim_response_invalid', 'claim_credentials_invalid', 'claim_nvs_write_failed']:
        assert code in claim + parse
    assert 'readControlResponse(http, response)' in claim
    assert 'http.getString()' not in main
    response = function(main, "readControlResponse")
    assert 'kMaxControlResponse' in response and 'kBackendReadTimeoutMs' in response
    for line in claim.splitlines():
        if 'logEvent' in line:
            assert not re.search(r',\s*(?:response|body|config\.)', line)


def test_retry_receipt_requires_current_and_persisted_config_not_only_hash():
    body = function(source("device_config.cpp"), "storeProvisioningConfig")
    assert 'if (sameFields && (samePendingPair || sameClaimedPair) &&' in body
    for key in ['ssid', 'passwd', 'backend', 'devid', 'devname', 'room', 'quality', 'framesize']:
        assert f'"{key}"' in body.split('const bool sameFields =', 1)[1].split('const bool samePendingPair', 1)[0]
    claim = body.split('const bool sameClaimedPair =', 1)[1].split('if (sameFields', 1)[0]
    assert '!config.deviceToken.isEmpty()' in claim and '!config.cameraId.isEmpty()' in claim
    assert 'prefs.getString("token", "") == config.deviceToken' in claim
    assert 'prefs.getString("camid", "") == config.cameraId' in claim


def test_failed_ap_start_cannot_log_enabled():
    assert 'if (pairingMode) logEvent("warn", "wifi_fallback_pairing_enabled");' in function(source("main.cpp"), "loop")
