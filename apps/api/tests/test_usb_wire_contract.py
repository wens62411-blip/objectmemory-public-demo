"""Serial contract tests against firmware's actual field list; no physical USB."""
import json
import re
from pathlib import Path

import pytest

from apps.api.tests.test_firmware_xiao_api import fw, MODEL, MAC, BRIDGE
from apps.api.tests.test_firmware_usb import ChallengeSerial, challenge_payload


@pytest.mark.parametrize('board_model', [MODEL, 'ai_thinker_esp32cam'])
def test_local_identity_metadata_never_leaks_into_device_config(fw, monkeypatch, board_model):
    source = (Path(__file__).resolve().parents[3]/'firmware/esp32cam/src/device_config.cpp').read_text(encoding='utf-8')
    block = re.search(r'const char \*allowed\[\]\s*=\s*\{([^}]+)\}', source)
    assert block, 'Read the real firmware whitelist, do not silently replace its parser with a permissive mock'
    allowed = set(re.findall(r'"([a-z_]+)"', block.group(1)))
    configs = []
    class StrictSerial(ChallengeSerial):
        def write(self, data):
            if data.startswith(b'OMCFG:'):
                config = json.loads(data[6:]); configs.append(config)
                if set(config)-allowed:
                    self.writes.append(data)
                    self.lines = [b'OMACK:{"success":false,"error":"unknown_field"}']
                    return
            super().write(data)
    connection = StrictSerial()
    opened = []
    def bound(*args):
        opened.append(args)
        assert args[-1] == board_model
        return connection
    monkeypatch.setattr(fw, '_open_bound_serial', bound)
    verified = []
    monkeypatch.setattr(fw, '_verify_serial_ready_and_bind', lambda port, payload, record: verified.append(payload.copy()) or {'contract_only': True})
    payload = {**challenge_payload(), 'board_model': board_model}
    result = fw._provision_worker('contract', payload, expected_mac=MAC, expected_bridge=BRIDGE)
    assert result['hardware_verification']['contract_only'] is True
    assert configs and all(set(config) <= allowed for config in configs)
    assert configs[0]['ssid'] == payload['ssid'] and configs[0]['password'] == payload['password']
    assert payload['port'] == 'COM7' and 'timeout_seconds' in payload
    assert opened and verified[0]['board_model'] == board_model
    assert connection.closed
