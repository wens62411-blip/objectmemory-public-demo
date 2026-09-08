"""SQLite/encoder boundary regressions. Generated frames are not camera acceptance."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import numpy as np
import pytest

from apps.api.tests.test_authenticity_storage import client_for, add_item_camera, complete_event
from services.vision.events.media import EventMediaWriter


def photo_fixture(client):
    runtime = client.app.state.runtime
    item, camera = add_item_camera(client)
    event = complete_event(client, item, camera)
    runtime.db.save('source_sessions', {'runtime_mode': 'REAL', 'is_simulated': False}, event['source_session_id'])
    profile = {'item_id': item['id'], 'status': 'ready', 'profile_version': 1,
               'model_id': 'fixture-encoder', 'model_version': 'fixture-v1'}
    runtime.db.save('item_recognition_profiles', profile, item['id'])
    event.update({'runtime_mode': 'REAL', 'is_simulated': False, 'detection_mode': 'experimental',
                  'detector_backend': 'nanodet', 'profile_generation': 1,
                  'identity_evidence': {**profile, 'accepted': True}})
    frame = np.full((48, 64, 3), 80, np.uint8)
    frames = [np.full_like(frame, 20), frame, frame]
    return runtime, item, camera, event, frame, frames


def assert_empty(runtime):
    assert runtime.db.count('movement_events') == 0
    assert runtime.db.count('item_current_state') == 0
    assert runtime.db.count('event_media') == 0
    assert not list(runtime.media.rglob('*.jpg'))
    assert not list(runtime.media.rglob('*.mp4'))
    assert not list(runtime.media.rglob('*.avi'))


@pytest.mark.parametrize('damage', ['missing_proof', 'rejected_identity', 'wrong_item', 'wrong_version', 'failed_profile'])
def test_real_photo_event_requires_current_ready_identity_before_encoding(tmp_path, monkeypatch, damage):
    with client_for(tmp_path, 'REAL') as client:
        runtime, item, _camera, event, frame, frames = photo_fixture(client)
        if damage == 'missing_proof': event.pop('identity_evidence')
        elif damage == 'rejected_identity': event['identity_evidence']['accepted'] = False
        elif damage == 'wrong_item': event['identity_evidence']['item_id'] = 'different-item'
        elif damage == 'wrong_version': event['identity_evidence']['profile_version'] = 2
        else: runtime.db.save('item_recognition_profiles', {'status': 'failed'}, item['id'])
        encoded = []
        original = EventMediaWriter.write_clip
        def spy(*args, **kwargs):
            encoded.append(True)
            return original(*args, **kwargs)
        monkeypatch.setattr(EventMediaWriter, 'write_clip', spy)
        assert runtime.event(event, frame, frames) is False
        assert encoded == []
        assert_empty(runtime)


def test_matching_photo_profile_is_recorded_with_its_proof_without_claiming_placement(tmp_path):
    with client_for(tmp_path, 'REAL') as client:
        runtime, _item, _camera, event, frame, frames = photo_fixture(client)
        result = runtime.event(event, frame, frames)
        assert result['final_status'] == 'position_changed'
        assert result['placement_evidence']['identity_evidence'] == event['identity_evidence']
        assert result['placement_evidence']['profile_generation'] == 1
        assert runtime.db.count('movement_events') == 1


def test_profile_invalidated_while_video_encodes_rolls_back_entire_bundle(tmp_path, monkeypatch):
    with client_for(tmp_path, 'REAL') as client:
        runtime, item, _camera, event, frame, frames = photo_fixture(client)
        original = EventMediaWriter.write_clip
        reached = threading.Event()
        proceed = threading.Event()
        def blocked(*args, **kwargs):
            result = original(*args, **kwargs)
            reached.set()
            assert proceed.wait(5)
            return result
        monkeypatch.setattr(EventMediaWriter, 'write_clip', blocked)
        with ThreadPoolExecutor(max_workers=1) as pool:
            job = pool.submit(runtime.event, event, frame, frames)
            try:
                assert reached.wait(5)
                runtime.db.save('item_recognition_profiles', {'status': 'building', 'profile_version': 2}, item['id'])
            finally:
                proceed.set()
            assert job.result(5) is False
        assert_empty(runtime)


@pytest.mark.parametrize('phase', ['before_callback', 'during_encoding'])
def test_pending_engine_reload_cannot_commit_old_photo_generation(tmp_path, monkeypatch, phase):
    with client_for(tmp_path, 'REAL') as client:
        runtime, _item, camera, event, frame, frames = photo_fixture(client)
        engine = SimpleNamespace(_profile_lock=threading.Lock(), _profile_revision=1,
                                 _applied_profile_revision=1, health=lambda: {
                                     'source_session_id': event['source_session_id'],
                                     'source_frame_sequence': event['source_frame_end'], 'status': 'streaming'})
        runtime.engines[camera['id']] = engine
        reached, proceed = threading.Event(), threading.Event()
        original = EventMediaWriter.write_clip
        def blocked(*args, **kwargs):
            result = original(*args, **kwargs)
            reached.set()
            assert proceed.wait(5)
            return result
        try:
            if phase == 'before_callback':
                engine._profile_revision = 2
                assert runtime.event(event, frame, frames, expected_engine=engine) is False
            else:
                monkeypatch.setattr(EventMediaWriter, 'write_clip', blocked)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    job = pool.submit(runtime.event, event, frame, frames, engine)
                    try:
                        assert reached.wait(5)
                        with engine._profile_lock:
                            engine._profile_revision = 2
                    finally:
                        proceed.set()
                    assert job.result(5) is False
            assert_empty(runtime)
        finally:
            runtime.engines.pop(camera['id'], None)


def test_current_engine_generation_succeeds_and_lock_is_held_only_during_database_commit(tmp_path, monkeypatch):
    with client_for(tmp_path, 'REAL') as client:
        runtime, _item, camera, event, frame, frames = photo_fixture(client)
        engine = SimpleNamespace(_profile_lock=threading.Lock(), _profile_revision=1,
                                 _applied_profile_revision=1, health=lambda: {
                                     'source_session_id': event['source_session_id'],
                                     'source_frame_sequence': event['source_frame_end'], 'status': 'streaming'})
        runtime.engines[camera['id']] = engine
        original_encode = EventMediaWriter.write_clip
        original_insert = runtime.db.insert_event_bundle_idempotent
        phases = []
        def encoding(*args, **kwargs):
            assert not engine._profile_lock.locked()
            phases.append('encode_without_generation_lock')
            return original_encode(*args, **kwargs)
        def commit(*args, **kwargs):
            assert engine._profile_lock.locked()
            assert kwargs['expected_profile']['profile_version'] == 1
            phases.append('transaction_with_generation_lock')
            return original_insert(*args, **kwargs)
        monkeypatch.setattr(EventMediaWriter, 'write_clip', encoding)
        monkeypatch.setattr(runtime.db, 'insert_event_bundle_idempotent', commit)
        try:
            assert runtime.event(event, frame, frames, engine)['final_status'] == 'position_changed'
            assert phases == ['encode_without_generation_lock', 'transaction_with_generation_lock']
            assert not engine._profile_lock.locked()
        finally:
            runtime.engines.pop(camera['id'], None)
