"""Board source/crypto contracts, not proof of HTTP behavior on physical silicon."""
import hashlib
import hmac
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SERVER = ROOT / 'firmware/esp32cam/src/camera_server.cpp'
MAIN = ROOT / 'firmware/esp32cam/src/main.cpp'


def function(source, name):
    """These firmware functions are top-level, with a column-zero closing brace."""
    declaration = re.search(r'^(?:esp_err_t|bool|void|String) ' + re.escape(name) + r'\(', source, re.M)
    assert declaration, f'Missing function {name}'
    return source[declaration.end():].split('\n}', 1)[0]


@pytest.mark.parametrize('name', ['deviceHandler', 'captureHandler', 'streamHandler', 'streamRedirectHandler'])
def test_video_and_device_routes_authenticate_before_accessing_frames_or_identity(name):
    body = function(SERVER.read_text(encoding='utf-8'), name)
    assert body.lstrip().startswith('httpd_req_t *request) {\n  uint32_t accessGeneration;\n  if (!cameraAccess(request, accessGeneration)) return denyCameraAccess(request);')
    assert 'cameraAccess(request, accessGeneration)' in body


def test_camera_auth_is_purpose_separated_cached_bounded_and_constant_time():
    source = SERVER.read_text(encoding='utf-8')
    derive = function(source, 'configureCameraAccess')
    assert 'objectmemory/camera-read/v1' in source
    assert 'mbedtls_sha256_ret(' in derive
    assert 'mbedtls_md_hmac(' in derive
    assert 'sizeof(key)' in derive and 'sizeof(kCameraReadPurpose) - 1' in derive
    assert 'deviceToken.length() >= 24 && deviceToken.length() <= 256' in derive
    assert 'mbedtls_platform_zeroize(key, sizeof(key));' in derive
    assert 'mbedtls_platform_zeroize(digest, sizeof(digest));' in derive
    assert 'portENTER_CRITICAL(&accessMux)' in derive
    assert '++cameraAccessGeneration;' in derive
    check = function(source, 'cameraAccess')
    assert 'httpd_req_get_hdr_value_len(request, "Authorization") != 71' in check
    assert 'char authorization[72]' in check
    assert 'difference |=' in check and 'index < 64' in check
    assert 'httpd_req_get_url_query_str' not in check
    assert 'deviceToken' not in check


def test_stream_revocation_and_frame_failure_paths_release_frames_without_public_cors():
    source = SERVER.read_text(encoding='utf-8')
    assert 'Access-Control-Allow-Origin' not in source
    stream = function(source, 'streamHandler')
    assert 'while (cameraAccessCurrent(accessGeneration))' in stream
    assert stream.index('if (!cameraAccessCurrent(accessGeneration))') < stream.index('httpd_resp_send_chunk')
    revoke = stream.split('if (!cameraAccessCurrent(accessGeneration))', 1)[1].split('}', 1)[0]
    assert 'esp_camera_fb_return(frame);' in revoke and 'return ESP_FAIL;' in revoke
    capture = function(source, 'captureHandler')
    revoke = capture.split('if (!cameraAccessCurrent(accessGeneration))', 1)[1].split('}', 1)[0]
    assert 'esp_camera_fb_return(frame);' in revoke and 'denyCameraAccess(request)' in revoke
    denied = function(source, 'denyCameraAccess')
    assert '401 Unauthorized' in denied and 'WWW-Authenticate' in denied


def test_public_health_reports_actual_sensor_and_no_identity_or_credentials():
    source = SERVER.read_text(encoding='utf-8')
    health = function(source, 'healthHandler')
    assert 'esp_camera_sensor_get()' in health and 'sensor->id.PID' in health
    assert 'sensor ? "ready" : "error"' in health
    assert 'doc["camera_auth"] = "hmac-sha256-v1";' in health
    assert 'ESP.getPsramSize()' in health and 'ESP.getFlashChipSize()' in health
    for private in ['"mac"', '"ip"', '"device_id"', '"device_name"', '"room_name"', '"wifi_rssi"', 'deviceToken', 'password']:
        assert private not in health
    assert 'doc["camera_auth"] = "hmac-sha256-v1";' in function(source, 'deviceHandler')


def test_auth_cache_is_initialized_rotated_and_cleared_for_both_provisioning_routes():
    main = MAIN.read_text(encoding='utf-8')
    assert 'configureCameraAccess("");' in function(main, 'setup')
    assert 'configureCameraAccess(config.deviceToken);' in function(main, 'parseClaimResponse')
    assert 'configureCameraAccess("");' in function(main, 'processConfigLine')
    assert 'configureCameraAccess("");' in function(main, 'applyCommand')
    server = SERVER.read_text(encoding='utf-8')
    assert 'configureCameraAccess("");' in function(server, 'configHandler')
    assert 'capabilities.add("camera_read_hmac_v1");' in main
    assert '"Bearer " + config.deviceToken' in function(main, 'sendHeartbeat')


def test_revocation_is_persisted_but_wifi_is_preserved_and_heartbeat_does_not_reset_stream():
    main = MAIN.read_text(encoding='utf-8')
    beat = function(main, 'sendHeartbeat')
    revoked = beat.split('if (status == HTTP_CODE_UNAUTHORIZED)', 1)[1].split('}', 1)[0]
    assert 'configureCameraAccess("");' in revoked and 'clearClaimCredentials(config);' in revoked
    assert 'configureCameraAccess(config.deviceToken);' in beat
    source = (ROOT / 'firmware/esp32cam/src/device_config.cpp').read_text(encoding='utf-8')
    clear = function(source, 'clearClaimCredentials')
    assert 'prefs.remove("token");' in clear and 'prefs.remove("camid");' in clear
    assert 'prefs.clear()' not in clear and '"ssid"' not in clear and '"passwd"' not in clear
    derive = function(SERVER.read_text(encoding='utf-8'), 'configureCameraAccess')
    assert 'if (memcmp(cameraReadToken, next, sizeof(cameraReadToken)) != 0)' in derive


def test_fallback_configuration_requires_actual_ap_interface_and_non_simple_json_request():
    source = SERVER.read_text(encoding='utf-8')
    guard = function(source, 'configurationUsesAccessPoint')
    assert 'getsockname(httpd_req_to_sockfd(request)' in guard
    assert 'local.sin_addr.s_addr == static_cast<uint32_t>(ap)' in guard
    assert 'X-Forwarded' not in guard
    config = function(source, 'configHandler')
    assert '!configurationUsesAccessPoint(request)' in config
    assert '415 Unsupported Media Type' in config and '"application/json"' in config
    assert config.index('configurationUsesAccessPoint') < config.index('storeProvisioningConfig')


def test_backend_and_firmware_agree_on_raw_digest_key_not_hex_text_or_device_bearer():
    from apps.api.app.firmware import camera_read_token
    raw = b'synthetic-device-token-for-contract-only'
    digest = hashlib.sha256(raw).digest()
    expected = hmac.new(digest, b'objectmemory/camera-read/v1', hashlib.sha256).hexdigest()
    assert camera_read_token(digest.hex()) == expected
    assert expected == '04f6af15026511707ca65872276ab5305423e11264eafce9775c5abd6529fa6f'
    assert expected != hmac.new(digest.hex().encode(), b'objectmemory/camera-read/v1', hashlib.sha256).hexdigest()
    assert expected != raw.decode()
    assert camera_read_token(hashlib.sha256(raw + b'rotated').hexdigest()) != expected
