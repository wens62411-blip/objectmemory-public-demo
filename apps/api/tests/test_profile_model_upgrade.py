"""Version migration control-flow tests; fixture vectors are not model accuracy."""
import hashlib

import cv2
import numpy as np
from fastapi.testclient import TestClient

from apps.api.app.main import create_app


class EncoderFixture:
    model_id = 'migration-fixture'
    model_version = 'preprocessing-v1'
    dimension = 3

    def health(self):
        return {'available': True, 'model_id': self.model_id, 'model_version': self.model_version}

    def encode(self, crop):
        return [1., 0., 0.]


def test_model_preprocessing_upgrade_advances_profile_once_and_preserves_original(tmp_path):
    app = create_app(tmp_path / 'isolated-upgrade', testing=True)
    service = app.state.runtime.registration
    service._suggest = lambda frame: ([], None)
    encoder = EncoderFixture()
    service._encoder = encoder
    with TestClient(app) as client:
        client.get('/api/session')
        item_id = client.post('/api/items', json={'name': 'migration fixture', 'type': 'phone'}).json()['id']
        ok, encoded = cv2.imencode('.png', np.full((100, 140, 3), 80, np.uint8))
        assert ok
        content = encoded.tobytes()
        ref = client.post(f'/api/items/{item_id}/reference-images', files=[('files', ('fixture.png', content, 'image/png'))]).json()[0]
        assert client.patch(f"/api/items/{item_id}/reference-images/{ref['id']}", json={'region': [0., 0., 1., 1.], 'confirmed': True}).status_code == 200
        first = client.post(f'/api/items/{item_id}/profile/build').json()
        assert first['registration_status'] == 'ready'
        encoder.model_version = 'preprocessing-v2'
        upgraded = client.post(f'/api/items/{item_id}/profile/build').json()
        assert upgraded['registration_status'] == 'ready'
        assert upgraded['profile_version'] == first['profile_version'] + 1
        assert upgraded['model_version'] == 'preprocessing-v2'
        repeated = client.post(f'/api/items/{item_id}/profile/build').json()
        assert repeated['profile_version'] == upgraded['profile_version']
        assert hashlib.sha256(client.get(ref['original_path']).content).hexdigest() == hashlib.sha256(content).hexdigest()
        assert app.state.runtime.db.count('item_current_state') == 0
        assert app.state.runtime.db.count('movement_events') == 0
