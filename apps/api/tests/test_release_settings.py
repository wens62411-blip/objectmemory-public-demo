"""Real TEST SQLite/API configuration contracts, not a physical hand test."""
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from apps.api.app.schemas import SettingsInput


def test_default_release_waits_five_seconds():
    assert SettingsInput().hand_interaction_release_stable_seconds == 5


def test_legacy_timer_upgrades_without_changing_other_user_settings(tmp_path):
    app = create_app(tmp_path / 'isolated-settings', testing=True)
    with TestClient(app) as client:
        client.get('/api/session')
        runtime = app.state.runtime
        settings = runtime.settings()
        runtime.db.save('settings', {'value': {**settings,
            'hand_interaction_release_stable_seconds': .6,
            'confidence_threshold': .47, 'show_hands': False}}, 'main')
        response = client.get('/api/settings')
        assert response.status_code == 200
        assert response.json()['hand_interaction_release_stable_seconds'] == 5
        assert response.json()['confidence_threshold'] == .47
        assert response.json()['show_hands'] is False
        assert runtime.db.count('movement_events') == 0


def test_api_cannot_shorten_release_below_five_seconds(tmp_path):
    app = create_app(tmp_path / 'isolated-settings-floor', testing=True)
    with TestClient(app) as client:
        client.get('/api/session')
        response = client.patch('/api/settings', json={'hand_interaction_release_stable_seconds': .6})
        assert response.status_code == 422
        assert client.get('/api/settings').json()['hand_interaction_release_stable_seconds'] == 5
        response = client.patch('/api/settings', json={'hand_interaction_release_stable_seconds': 6})
        assert response.status_code == 200
        assert response.json()['hand_interaction_release_stable_seconds'] == 6
