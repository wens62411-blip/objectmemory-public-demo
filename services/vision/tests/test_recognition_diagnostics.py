"""Synthetic fault-isolation/frame-accounting contracts, not recognition accuracy."""
from types import SimpleNamespace
import importlib.util
from pathlib import Path
import socket
import threading
import time

import numpy as np
import pytest

from services.vision.capture import FramePacket
from services.vision.detectors.base import Detection
from services.vision.engine import VisionEngine, _PendingEvent


class EncoderFixture:
    model_id = 'unit-encoder'
    model_version = 'unit-v1'
    dimension = 2

    def health(self): return {'available': True, 'error': None}
    def encode(self, crop): return [1., 0.]


@pytest.fixture
def make_engine(tmp_path, monkeypatch):
    class ObjectFixture:
        def __init__(self, *_args, **_kwargs): pass
        def health(self): return {'available': True}
        def detect(self, frame):
            return [Detection('generic:test', 'cell phone', (10, 10, 40, 70),
                              (30, 45), .9, 'experimental')]

    monkeypatch.setattr('services.vision.engine.NanoDetDetectorBackend', ObjectFixture)
    engines = []

    def build(**settings):
        item = {'id': 'unit-phone', 'name': 'Synthetic identity', 'type': 'phone',
                'appearance_profile': {'status': 'ready', 'profile_version': 7,
                    'model_id': 'unit-encoder', 'model_version': 'unit-v1', 'dimension': 2,
                    'embeddings': [[1., 0.]]}}
        engine = VisionEngine({'id': 'never-opened', 'source_type': 'video', 'source': str(tmp_path/'absent.avi')},
            [item], [], {'runtime_mode': 'TEST', 'detection_mode': 'experimental',
                'reference_patch_enabled': False, 'phone_shape_enabled': False,
                'hand_detection_enabled': False, 'person_pose': {'enabled': False},
                'record_events': False, 'media_root': str(tmp_path/'media'), **settings},
            lambda *_args: pytest.fail('No history from this unit fixture'), appearance_encoder=EncoderFixture())
        engines.append(engine)
        return engine

    yield build
    for engine in engines: engine.stop()


def process(engine, sequence=1, session='unit-frame-session'):
    engine._process(FramePacket(np.zeros((100, 160, 3), np.uint8), time.monotonic(),
                               time.time(), sequence, session, 0))
    return engine.recognition_snapshot()


def test_patch_failure_preserves_real_detector_route_and_independent_hands(make_engine):
    engine = make_engine(diagnostic_mode=True)
    def broken(_frame): raise RuntimeError('controlled-patch-failure')
    engine.reference_patches = SimpleNamespace(backend='dinov2_reference_patches',
        health=lambda: {'available': True}, detect=broken)
    engine.hands = SimpleNamespace(detect=lambda _frame: [], health=lambda: {'available': True}, close=lambda: None)
    snapshot = process(engine)
    assert snapshot['candidates'][0]['accepted'] is True
    assert snapshot['candidates'][0]['item_id'] == 'unit-phone'
    facts = snapshot['pipeline_diagnostics']
    assert facts['raw_object_count'] == facts['identity_accepted_count'] == 1
    assert facts['object_model_frames'] == facts['hand_model_frames'] == 1
    assert facts['capture_frames_received'] is None  # Never opened a capture.
    assert facts['raw_detections'][0]['category'] == 'cell phone'
    assert 'controlled-patch-failure' in facts['stage_errors']['reference_patch_localization']
    assert snapshot['source_frame'] == facts['source_frame'] == 1
    assert snapshot['source_session_id'] == facts['source_session_id']
    assert engine.capture.thread is None


def test_optional_patch_initialization_cannot_discard_loaded_identity_profiles(make_engine, monkeypatch):
    def broken(*_args, **_kwargs): raise RuntimeError('controlled-init-failure')
    monkeypatch.setattr('services.vision.detectors.reference_patch.ReferencePatchProposer', broken)
    engine = make_engine(reference_patch_enabled=True)
    assert engine.loaded_profile_versions == {'unit-phone': 7}
    snapshot = process(engine)
    assert snapshot['candidates'][0]['accepted'] is True
    assert 'controlled-init-failure' in snapshot['pipeline_diagnostics']['stage_errors']['reference_patch_localization']


def test_counters_reset_on_new_source_and_raw_boxes_are_opt_in(make_engine):
    engine = make_engine()
    assert process(engine)['pipeline_diagnostics']['processed_frames'] == 1
    assert process(engine, 2)['pipeline_diagnostics']['object_model_frames'] == 2
    facts = process(engine, 1, 'different-session')['pipeline_diagnostics']
    assert facts['processed_frames'] == facts['object_model_frames'] == 1
    assert facts['hand_model_frames'] == 0
    assert 'raw_detections' not in facts


def test_profile_reload_invalidates_diagnostic_frame_with_identity(make_engine):
    engine = make_engine()
    assert process(engine)['pipeline_diagnostics']['loaded_profile_count'] == 1
    engine.reload_profiles([])
    assert engine.recognition_snapshot() is None
    snapshot = process(engine, 2)
    assert snapshot['pipeline_diagnostics']['profile_generation'] == 1
    assert snapshot['pipeline_diagnostics']['loaded_profile_count'] == 0
    assert not any(row.get('accepted') for row in snapshot['candidates'])


def diagnostic_script():
    path = Path(__file__).resolve().parents[3]/'scripts/diagnose-recognition-frame.py'
    spec = importlib.util.spec_from_file_location('unit_frame_diagnostic', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_offline_diagnostic_validates_hash_and_never_promotes_video_to_real(tmp_path):
    script = diagnostic_script()
    frame = tmp_path/'fixture.jpg'
    frame.write_bytes(b'fixture bytes for hash contract only, not image evidence')
    row = {'raw_sha256': script.sha256(frame), 'runtime_mode': 'TEST', 'source_type': 'video_file',
           'is_simulated': True, 'source_session_id': 'test-only', 'source_frame': 3,
           'source_timestamp_seconds': 1.5}
    assert script.validate_source(frame, row) == 1.5
    with pytest.raises(ValueError, match='provenance'):
        script.validate_source(frame, {**row, 'runtime_mode': 'REAL', 'is_simulated': False})
    with pytest.raises(ValueError, match='hash'):
        script.validate_source(frame, {**row, 'raw_sha256': '0'*64})


def test_extraction_manifest_selects_only_exact_path_and_keeps_file_origin(tmp_path):
    script = diagnostic_script()
    frame = (tmp_path/'fixture.jpg').resolve()
    row = {'path': str(frame), 'sha256': 'a'*64, 'frame_index': 3, 'seconds': .1,
           'runtime_mode': 'TEST', 'source_type': 'video_file', 'is_simulated': True}
    result = script.capture_provenance([row], frame, 'b'*64)
    assert result['source_type'] == 'video_file' and result['is_simulated'] is True
    assert result['source_timestamp'] is None
    assert result['source_timestamp_seconds'] == .1
    with pytest.raises(ValueError, match='one hash-bound entry'):
        script.capture_provenance([row, row], frame, 'b'*64)


def test_diagnostic_blocks_camera_and_network_without_changing_model_interfaces():
    import cv2
    script = diagnostic_script()
    original = cv2.VideoCapture
    with script.no_capture_or_network():
        with pytest.raises(RuntimeError, match='Offline diagnostic'):
            cv2.VideoCapture(0)
        with socket.socket() as stream, pytest.raises(RuntimeError, match='Offline diagnostic'):
            stream.connect(('127.0.0.1', 1))
    assert cv2.VideoCapture is original


def test_reload_discards_old_photo_pending_without_deleting_marker_pending(make_engine):
    engine = make_engine()
    photo = SimpleNamespace(event={'detection_mode': 'experimental', 'profile_generation': 0})
    marker = SimpleNamespace(event={'detection_mode': 'aruco'})
    engine.pending = [photo, marker]
    engine.reload_profiles([])
    assert engine.pending == [marker]
    engine.pending = []  # This narrow fixture is not a full media job.


def test_queued_media_job_cannot_publish_old_identity_after_reload(make_engine):
    engine = make_engine()
    blocker_started, unblock = threading.Event(), threading.Event()
    callbacks = []
    engine.on_event = lambda *_args: callbacks.append('unexpected-old-profile')
    frame = np.zeros((32, 32, 3), np.uint8)
    pending = _PendingEvent({'event_id': 'unit-photo-event', 'detection_mode': 'experimental',
        'profile_generation': 0}, frame, [frame], 1., 1., 1.)
    def block_worker():
        blocker_started.set()
        assert unblock.wait(5)
    blocked = engine._media_executor.submit(block_worker)
    try:
        assert blocker_started.wait(2)
        engine._finalize(pending, stopped_early=False)
        with engine._media_lock:
            queued = list(engine._media_futures)
        assert len(queued) == 1
        engine.reload_profiles([])
        unblock.set()
        blocked.result(timeout=3)
        with pytest.raises(RuntimeError, match='旧身份媒体任务'):
            queued[0].result(timeout=3)
        assert callbacks == []
    finally:
        unblock.set()


@pytest.mark.parametrize('changed', [
    {'status': 'reconnecting'}, {'source_session_id': 'new-source'}, {'reconnect_epoch': 1},
])
def test_reconnect_cannot_serve_a_recent_but_old_source_recognition_frame(make_engine, monkeypatch, changed):
    engine = make_engine()
    assert process(engine) is not None
    capture = {'capture_thread_alive': True, 'status': 'online', 'reconnect_epoch': 0,
               'source_session_id': 'unit-frame-session'}
    monkeypatch.setattr(engine.capture, 'health', lambda: dict(capture))
    assert engine.recognition_snapshot() is not None
    capture.update(changed)
    assert engine.recognition_snapshot() is None
