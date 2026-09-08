"""Fresh-mode defaults and truthful cached registration feedback, no hardware."""
from fastapi.testclient import TestClient

from apps.api.app.main import create_app


def test_real_has_builtin_object_detection_without_seeding_or_registering(tmp_path):
    app = create_app(tmp_path / 'fresh-real', testing=False, runtime_mode='REAL')
    with TestClient(app) as client:
        client.cookies.set('om_session', app.state.runtime.sessions.issue())
        assert client.get('/api/settings').json()['detection_mode'] == 'experimental'
        assert client.get('/api/items').json() == []
        assert client.get('/api/events').json() == []
        assert not app.state.runtime.engines


def test_reference_warning_never_erases_user_confirmed_profile(tmp_path):
    app = create_app(tmp_path / 'registration-warning', testing=True)
    runtime = app.state.runtime
    item = runtime.db.save('items', {'name': 'my phone', 'type': 'phone'})
    runtime.db.save('item_reference_images', {'item_id': item['id'], 'region_confirmed': True,
        'suggested_regions': [{'label': 'bed'}], 'quality': {'target_confirmation_required': True}})
    runtime.db.save('item_recognition_profiles', {'item_id': item['id'], 'status': 'ready',
        'profile_version': 6, 'reference_ids': [], 'embeddings': []}, item['id'])
    with TestClient(app) as client:
        client.get('/api/session')
        result = client.get('/api/items/' + item['id']).json()
        assert result['recognition_profile']['quality_warnings']
        assert result['recognition_profile']['registration_status'] == 'ready'
        assert result['recognition_profile']['profile_version'] == 6
        assert result['reference_images'][0]['quality']['target_confirmation_required'] is False
        assert runtime.db.count('movement_events') == 0
        assert runtime.db.count('item_current_state') == 0
