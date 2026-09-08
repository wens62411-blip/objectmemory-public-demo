"""Settings normalization must not reset live camera identity baselines."""
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from apps.api.app.main import create_app
from apps.api.app.schemas import SettingsInput


def test_default_settings_roundtrip_does_not_expand_into_a_change():
    settings = SettingsInput().model_dump()
    assert SettingsInput.model_validate(settings).model_dump() == settings


@pytest.mark.parametrize('saved_shape', [{}, {'max_candidates': 2}])
def test_legacy_shape_defaults_do_not_restart_camera_on_overlay_change(tmp_path, saved_shape):
    app = create_app(tmp_path, testing=True)
    with TestClient(app) as client:
        client.get('/api/session')
        runtime = app.state.runtime
        runtime.db.save('settings', {'value': {'phone_shape': saved_shape}}, 'main')
        restarted = []
        runtime.restart = lambda camera_id: restarted.append(camera_id)
        engine = SimpleNamespace(settings={'show_hands': True}, stop=lambda: None)
        runtime.engines['settings-contract-stub'] = engine
        try:
            response = client.patch('/api/settings', json={'show_hands': False})
            assert response.status_code == 200
            assert response.json()['phone_shape']['max_candidates'] == saved_shape.get('max_candidates', 3)
            assert engine.settings['show_hands'] is False
            assert restarted == []
            # Runtime provenance is a read-only response envelope, not a
            # writable settings parameter (the API correctly rejects it).
            writable = {key: value for key, value in response.json().items() if key in SettingsInput.model_fields}
            assert client.patch('/api/settings', json=writable).status_code == 200
            assert restarted == []
            assert runtime.db.count('movement_events') == 0
        finally:
            runtime.engines.clear()
