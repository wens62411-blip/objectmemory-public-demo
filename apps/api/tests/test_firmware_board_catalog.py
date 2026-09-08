"""Real HTTP/SQLite boundary; fixture build bytes are not hardware acceptance."""
import hashlib
import json

from fastapi.testclient import TestClient
from apps.api.app.main import create_app
from apps.api.app.firmware import FIRMWARE_BOARDS


def test_catalog_is_authenticated_read_only_and_never_claims_hardware(tmp_path, monkeypatch):
    app = create_app(data_dir=tmp_path / 'data', testing=True)
    with TestClient(app) as client:
        assert client.get('/api/firmware/boards').status_code == 401
        client.get('/api/session').raise_for_status()
        service = app.state.firmware_service
        service.project_root = tmp_path / 'source'
        def no_hardware(*args, **kwargs):
            raise AssertionError('Catalog must not enumerate/open serial, build or flash')
        monkeypatch.setattr(service, 'discover_ports', no_hardware)
        monkeypatch.setattr(service, 'start_build', no_hardware)
        monkeypatch.setattr(service, 'start_flash', no_hardware)
        for path in ('firmware/esp32cam/src/main.cpp', 'firmware/esp32cam/include/board_config.h',
                     'firmware/esp32cam/platformio.ini', 'scripts/firmware-usb.py'):
            target = service.project_root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('fixture only')
        model = 'xiao_esp32s3_sense'
        board = FIRMWARE_BOARDS[model]
        artifact = service.project_root / board['build'] / 'firmware.bin'
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b'fixture bytes, never flashed')
        manifest = service.project_root / board['manifest']
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({'board_model': model, 'environment': board['environment'],
            'artifacts': [{'name': artifact.name, 'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}]}))
        response = client.get('/api/firmware/boards')
        assert response.status_code == 200
        result = response.json()
        rows = {row['board_model']: row for row in result['boards']}
        assert set(rows) == set(FIRMWARE_BOARDS)
        assert rows[model]['build_verified'] is True
        assert rows['ai_thinker_esp32cam']['build_verified'] is False
        assert all(row['physical_acceptance'] == 'not_asserted_by_catalog' for row in rows.values())
        assert all(row['auto_flash_unknown_device'] is False for row in rows.values())
        assert result['selection_policy'] == 'restore_bound_board_else_explicit_selection'
        assert 'mac_address' not in response.text and 'password' not in response.text
        artifact.write_bytes(b'tampered')
        changed = client.get('/api/firmware/boards').json()
        assert next(row for row in changed['boards'] if row['board_model'] == model)['build_verified'] is False
