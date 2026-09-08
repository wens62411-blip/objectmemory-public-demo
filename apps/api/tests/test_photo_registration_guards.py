"""Independent registration guards with isolated DBs and generated image bytes.

No capture is opened. Stub encoder/proposals are only deterministic control-flow
fixtures, not evidence of DINO accuracy, physical identity, or camera availability.
"""
from __future__ import annotations

import copy
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from apps.api.app.db import Database
from apps.api.app.main import create_app
from apps.api.app.mode_lock import RuntimeModeLease
from apps.api.app.registration import RegistrationError, RegistrationService
from apps.api.app.retention import RetentionService
from services.vision.capture import FramePacket
from services.vision.detectors.base import Detection
from services.vision.engine import VisionEngine


class EncoderFixture:
    model_id = 'unit-control-flow-only'
    model_version = '1'
    dimension = 3

    def health(self):
        return {'available': True, 'model_id': self.model_id,
                'model_version': self.model_version, 'error': None}

    def encode(self, crop):
        return [1., 0., 0.]


class ProposalsFixture:
    def __init__(self, *args, **kwargs):
        pass

    def health(self):
        return {'available': True, 'error': None}

    def detect(self, frame):
        return []


def image_bytes(seed=1):
    frame = np.random.default_rng(seed).integers(0, 256, (96, 128, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode('.png', frame)
    assert ok
    return encoded.tobytes()


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / 'isolated-photo-guards', testing=True)
    app.state.runtime.registration._suggest = lambda frame: ([], None)
    with TestClient(app) as value:
        value.get('/api/session')
        yield value


def item(client):
    response = client.post('/api/items', json={'name': 'control-flow fixture', 'type': 'phone'})
    assert response.status_code == 200
    return response.json()['id']


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr('services.vision.engine.NanoDetDetectorBackend', ProposalsFixture)
    entry = {'id': 'item-a', 'name': 'fixture', 'type': 'phone', 'aruco_id': None,
             'appearance_profile': {'profile_version': 3, 'status': 'ready',
                                    'model_id': EncoderFixture.model_id,
                                    'model_version': EncoderFixture.model_version,
                                    'dimension': 3, 'embeddings': [[1., 0., 0.]]}}
    camera = {'id': 'camera-a', 'name': 'never opened', 'source_type': 'video',
              'source': str(tmp_path / 'never-opened.avi'), 'config': {},
              'runtime_mode': 'TEST', 'inference_fps': 5}
    value = VisionEngine(camera, [entry], [], {'runtime_mode': 'TEST',
                          'show_hands': False, 'media_root': str(tmp_path / 'test-media')},
                         on_event=lambda *_: None)
    try:
        yield value
    finally:
        value.stop()


def test_pending_activation_survives_second_metadata_reload(engine):
    encoder = EncoderFixture()
    engine.reload_profiles(engine.items, encoder, enable=True)
    engine.reload_profiles(copy.deepcopy(engine.items), enable=False)
    engine._apply_profile_updates()
    assert engine._active_detection_mode() == 'experimental'
    assert engine._appearance_encoder is encoder
    assert engine.loaded_profile_versions == {'item-a': 3}
    assert engine.capture.thread is None


def test_reload_invalidates_same_version_diagnostic_snapshot_before_next_frame(engine):
    engine._recognition_snapshot = {'jpeg': b'old-frame', 'created_at': time.monotonic(),
                                    'source_session_id': 'old', 'source_frame': 9,
                                    'loaded_profiles': [{'item_id': 'item-a', 'profile_version': 3}]}
    assert engine.recognition_snapshot() is not None
    engine.reload_profiles(engine.items, EncoderFixture(), enable=True)
    assert engine.recognition_snapshot() is None, 'No new inference frame has applied the pending profile'


def test_diagnostic_candidates_and_decodable_image_share_source_frame(engine):
    engine.reload_profiles(engine.items, EncoderFixture(), enable=True)
    engine._apply_profile_updates()
    engine.experimental.detect = lambda frame: [Detection(
        'generic:phone', 'cell phone', (32, 24, 64, 48), (64., 48.), .9, 'experimental')]
    frame = np.full((96, 128, 3), (30, 70, 160), np.uint8)
    packet = FramePacket(frame, time.monotonic(), time.time(), 17, 'same-frame-contract', 0)
    engine._process(packet)
    snapshot = engine.recognition_snapshot()
    assert snapshot['source_session_id'] == packet.source_session_id
    assert snapshot['source_frame'] == packet.sequence
    source_time = snapshot['source_timestamp']
    if isinstance(source_time, str):
        source_time = datetime.fromisoformat(source_time.replace('Z', '+00:00')).timestamp()
    assert source_time == pytest.approx(packet.wall_time, abs=1e-6)
    assert snapshot['loaded_profiles'][0]['profile_version'] == 3
    assert snapshot['candidates'][0]['bbox'] == [.25, .25, .5, .5]
    assert snapshot['candidates'][0]['accepted'] is True
    decoded = cv2.imdecode(np.frombuffer(snapshot['jpeg'], np.uint8), 1)
    assert decoded.shape == frame.shape
    assert np.abs(decoded.astype(float) - frame).mean() < 2
    assert engine.capture.thread is None


def test_failed_encoder_is_not_reported_ready_in_that_same_diagnostic_frame(engine):
    class FailingEncoder(EncoderFixture):
        error = None

        def health(self):
            return {**super().health(), 'error': self.error}

        def encode(self, crop):
            self.error = 'controlled inference failure, not a physical model test'
            raise RuntimeError(self.error)

    engine.reload_profiles(engine.items, FailingEncoder(), enable=True)
    engine._apply_profile_updates()
    engine.experimental.detect = lambda frame: [Detection(
        'generic:phone', 'cell phone', (32, 24, 64, 48), (64., 48.), .9, 'experimental')]
    engine._process(FramePacket(np.full((96, 128, 3), 120, np.uint8), time.monotonic(),
                                time.time(), 3, 'failure-frame-contract', 0))
    snapshot = engine.recognition_snapshot()
    assert snapshot['candidates'][0]['accepted'] is False
    assert snapshot['candidates'][0]['rejection_reason'] == 'encoder_error'
    assert snapshot['model']['status'] == 'failed'
    assert 'controlled inference failure' in snapshot['model']['error']


@pytest.mark.parametrize('mode', ['DEMO', 'TEST'])
def test_isolated_clear_removes_recognition_profiles_and_preserves_originals(tmp_path, mode):
    data = tmp_path / mode
    db = Database(data / 'database' / f'objectmemory-{mode.lower()}.sqlite', mode)
    db.save('items', {'name': 'fixture', 'type': 'phone'}, 'test-item')
    db.save('item_recognition_profiles', {'item_id': 'test-item', 'status': 'ready',
                                         'profile_version': 1, 'embeddings': [[1., 0., 0.]]}, 'test-item')
    original = data / 'registered-items' / 'protected-original.png'
    original.parent.mkdir(parents=True)
    original.write_bytes(image_bytes())
    before = original.read_bytes()
    lease = RuntimeModeLease(data, mode)
    lease.acquire()
    try:
        RetentionService.clear_isolated_mode(data, mode, lease=lease)
    finally:
        lease.close()
    assert db.count('item_recognition_profiles') == 0
    assert original.read_bytes() == before


def test_capture_rejects_engine_detached_while_snapshot_is_read(client):
    runtime = client.app.state.runtime
    item_id = item(client)
    camera = runtime.db.save('cameras', {'source_type': 'video', 'source': 'never-opened.avi',
                                         'config': {}, 'name': 'fixture', 'enabled': False})
    packet = FramePacket(np.full((96, 128, 3), 200, np.uint8), time.monotonic(),
                         time.time(), 7, 'detached-session', 0)

    def detach(*args, **kwargs):
        runtime.engines.pop(camera['id'], None)
        return (7, packet)

    runtime.engines[camera['id']] = SimpleNamespace(
        capture=SimpleNamespace(preview_frames=SimpleNamespace(latest=detach)))
    response = client.post(f'/api/items/{item_id}/reference-capture', json={'camera_id': camera['id']})
    assert response.status_code == 409, response.text
    assert runtime.db.count('item_reference_images') == 0
    assert not list(runtime.registration.folder.iterdir())


def test_reference_batch_capacity_failure_does_not_partially_save(client):
    runtime = client.app.state.runtime
    item_id = item(client)
    for index in range(11):
        runtime.db.save('item_reference_images', {'item_id': item_id, 'sha256': f'prior-{index}',
                                                 'width': 96, 'height': 128}, f'prior-{index}')
    response = client.post(f'/api/items/{item_id}/reference-images', files=[
        ('files', ('first.png', image_bytes(7), 'image/png')),
        ('files', ('second.png', image_bytes(8), 'image/png')),
    ])
    assert response.status_code == 422
    assert runtime.db.count('item_reference_images') == 11, 'Failed batch must not leave a hidden twelfth image'
    assert not list(runtime.registration.folder.iterdir())


def test_replaced_reference_root_is_rejected_after_service_creation(client, tmp_path):
    service = client.app.state.runtime.registration
    outside = tmp_path / 'outside-data-owned-fixture'
    outside.mkdir()
    protected = outside / 'protected.png'
    protected.write_bytes(image_bytes())
    service.folder.rmdir()
    if sys.platform == 'win32':
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(service.folder), str(outside)],
                                capture_output=True)
        if result.returncode:
            service.folder.mkdir()
            pytest.skip('Local test junction creation unavailable')
    else:
        service.folder.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(RegistrationError):
            service._path('/media/registered-items/protected.png')
        assert protected.read_bytes() == image_bytes()
    finally:
        # Remove the test link entry only, never recurse into its target.
        if sys.platform == 'win32':
            service.folder.rmdir()
        else:
            service.folder.unlink()
        service.folder.mkdir()


@pytest.mark.parametrize('suffix', ['../escape.png', '..\\escape.png', 'x/y.png', 'x\\y.png'])
def test_reference_leaf_paths_reject_traversal(client, suffix):
    with pytest.raises(RegistrationError):
        client.app.state.runtime.registration._path('/media/registered-items/' + suffix)


def test_storage_cleanup_preserves_registered_original_and_display(client):
    item_id = item(client)
    response = client.post(f'/api/items/{item_id}/reference-images', files=[
        ('files', ('photo.png', image_bytes(), 'image/png'))])
    assert response.status_code == 200
    reference = response.json()[0]
    runtime = client.app.state.runtime
    paths = [runtime.registration._path(reference[key]) for key in ('original_path', 'path')]
    before = [path.read_bytes() for path in paths]
    runtime.retention.cleanup(trigger='independent-photo-guard')
    assert [path.read_bytes() for path in paths] == before
    assert runtime.db.count('movement_events') == runtime.db.count('event_media') == 0
