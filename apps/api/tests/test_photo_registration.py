"""Reference registration contract, isolated SQLite and actual image bytes.

Artificial textures test storage/crop/version invariants, not physical identity.
"""
import hashlib
import io
import weakref
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from apps.api.app.main import create_app
from apps.api.app.registration import RegistrationError, RegistrationService


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / '照片测试', testing=True)
    # Avoid spending time on real proposal inference in storage-only tests;
    # real candidate model execution is tested separately, not claimed here.
    app.state.runtime.registration._suggest = lambda frame: ([], None)
    with TestClient(app) as value:
        value.get('/api/session')
        yield value


def register_item(client, name='无标签手机'):
    response = client.post('/api/items', json={'name': name, 'type': '手机'})
    assert response.status_code == 200
    return response.json()['id']


def png(seed=4):
    rng = np.random.default_rng(seed)
    frame = rng.integers(0, 255, (120, 180, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode('.png', frame)
    assert ok
    return encoded.tobytes()


def upload(client, item_id, content=None):
    return client.post(f'/api/items/{item_id}/reference-images', files=[('files', ('照片.png', content or png(), 'image/png'))])


def test_single_photo_is_saved_but_not_ready_and_duplicate_is_not_new_angle(client):
    item_id = register_item(client)
    first = upload(client, item_id)
    assert first.status_code == 200, first.text
    ref = first.json()[0]
    assert ref['region'] is None and ref['region_confirmed'] is False
    assert 'features' not in ref
    assert client.get(ref['original_path']).content == png()
    duplicate = upload(client, item_id).json()[0]
    assert duplicate['id'] == ref['id'] and duplicate['duplicate']
    profile = client.get(f'/api/items/{item_id}/profile').json()
    assert profile['reference_count'] == 1 and profile['registration_status'] == 'images_saved'
    assert profile['loaded_profile_version'] is None
    assert client.post(f'/api/items/{item_id}/profile/build').status_code == 422
    assert client.app.state.runtime.db.count('movement_events') == 0


def test_invalid_files_are_explicit_errors_and_batch_validation_precedes_save(client):
    item_id = register_item(client)
    bad = client.post(f'/api/items/{item_id}/reference-images', files=[('files', ('good.png', png(), 'image/png')), ('files', ('bad.jpg', b'not a photo', 'image/jpeg'))])
    assert bad.status_code == 422
    assert client.app.state.runtime.db.count('item_reference_images') == 0
    assert list((client.app.state.runtime.data / 'registered-items').iterdir()) == []
    assert upload(client, item_id, b'RIFF not supported WEBP').status_code == 422


def test_batch_prepares_unique_new_photos_once_and_releases_full_frames(client, monkeypatch):
    item_id = register_item(client)
    service = client.app.state.runtime.registration
    old, first, second = png(4), png(5), png(6)
    saved = service.add(item_id, old)
    decode = service.decode
    decoded, frames = [], []

    def record_decode(content):
        assert all(frame() is None for frame in frames)
        frame, display = decode(content)
        decoded.append(content)
        frames.append(weakref.ref(frame))
        return frame, display

    monkeypatch.setattr(service, 'decode', record_decode)
    response = client.post(f'/api/items/{item_id}/reference-images', files=[
        ('files', ('photo.png', content, 'image/png'))
        for content in (old, first, first, second)
    ])
    assert response.status_code == 200, response.text
    rows = response.json()
    assert decoded == [first, second]
    assert all(frame() is None for frame in frames)
    assert rows[0]['id'] == saved['id'] and rows[0]['duplicate']
    assert rows[1]['id'] == rows[2]['id'] and rows[2]['duplicate']
    assert not rows[1].get('duplicate') and not rows[3].get('duplicate')
    assert service.db.count('item_reference_images') == 3
    assert service.profile(item_id)['profile_version'] == 3


def test_service_batch_validates_all_photos_before_writing(client):
    service = client.app.state.runtime.registration
    item_id = register_item(client)
    with pytest.raises(RegistrationError):
        service.add_batch(item_id, [png(), b'not a photo'])
    assert service.db.count('item_reference_images') == 0
    assert not list(service.folder.iterdir())


def test_batch_write_failure_cleans_incomplete_photo_and_preserves_saved_photos(client, monkeypatch):
    service = client.app.state.runtime.registration
    item_id = register_item(client)
    write = service._write
    displays = 0

    def fail_second_display(name, content):
        nonlocal displays
        if name.endswith('-display.jpg'):
            displays += 1
            if displays == 2:
                raise OSError('controlled display write failure')
        return write(name, content)

    monkeypatch.setattr(service, '_write', fail_second_display)
    with pytest.raises(OSError, match='controlled display write failure'):
        service.add_batch(item_id, [png(4), png(5)])
    references = service.db.list('item_reference_images')
    assert len(references) == 1
    assert service._path(references[0]['original_path']).read_bytes() == png(4)
    assert {path.name for path in service.folder.iterdir()} == {
        Path(references[0]['original_path']).name, Path(references[0]['path']).name,
    }
    assert service.profile(item_id)['profile_version'] == 1


def test_crop_requires_explicit_valid_target_and_wrong_item_cannot_edit(client):
    item_id = register_item(client)
    other = register_item(client, '另一部手机')
    ref = upload(client, item_id).json()[0]
    endpoint = f"/api/items/{item_id}/reference-images/{ref['id']}"
    for region, confirmed in [([0, 0, 1, 1], False), ([.9, 0, .5, 1], True), ([0, 0, .01, .01], True)]:
        assert client.patch(endpoint, json={'region': region, 'confirmed': confirmed}).status_code == 422
    assert client.patch(f"/api/items/{other}/reference-images/{ref['id']}", json={'region': [0, 0, 1, 1], 'confirmed': True}).status_code == 422
    response = client.patch(endpoint, json={'region': [.1, .1, .8, .8], 'confirmed': True})
    assert response.status_code == 200
    assert response.json()['region_confirmed'] is True
    assert client.get(f'/api/items/{item_id}/profile').json()['profile_version'] == 2


def test_exif_rotation_is_applied_before_crop_coordinates():
    photo = Image.new('RGB', (80, 120), '#aa3322')
    exif = Image.Exif()
    exif[274] = 6
    output = io.BytesIO()
    photo.save(output, format='JPEG', exif=exif)
    frame, display = RegistrationService.decode(output.getvalue())
    assert frame.shape[:2] == (80, 120)
    assert cv2.imdecode(np.frombuffer(display, np.uint8), 1).shape[:2] == (80, 120)


def test_explicit_reference_delete_invalidates_profile_and_preserves_other_item(client):
    item_id = register_item(client)
    other = register_item(client, '另一部手机')
    ref = upload(client, item_id).json()[0]
    keep = upload(client, other, png(5)).json()[0]
    keep_hash = hashlib.sha256(client.get(keep['original_path']).content).hexdigest()
    assert client.delete(f"/api/items/{item_id}/reference-images/{ref['id']}").status_code == 200
    assert client.get(ref['path']).status_code == 404
    assert hashlib.sha256(client.get(keep['original_path']).content).hexdigest() == keep_hash
    assert client.get(f'/api/items/{item_id}/profile').json()['ready_reference_count'] == 0


def test_reference_path_guard_rejects_escape_without_touching_user_file(client, tmp_path):
    service = client.app.state.runtime.registration
    protected = tmp_path / 'protected.jpg'
    protected.write_bytes(b'user owned')
    for value in ['/media/registered-items/../../protected.jpg', str(protected), '/media/registered-items/a/b.jpg']:
        with pytest.raises(RegistrationError):
            service._path(value)
    assert protected.read_bytes() == b'user owned'


def test_no_camera_test_never_uses_registration_photo_as_scene(client):
    item_id = register_item(client)
    upload(client, item_id)
    result = client.post(f'/api/items/{item_id}/recognition-test', json={'camera_id': 'not-present'})
    assert result.status_code == 409
    assert client.app.state.runtime.db.count('movement_events') == 0
