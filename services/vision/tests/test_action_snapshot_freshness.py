"""Controlled lifecycle/timing contracts, not physical hand accuracy evidence."""
from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import pytest

from services.vision.engine import VisionEngine


@pytest.fixture
def subject(monkeypatch):
    engine = object.__new__(VisionEngine)
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr('services.vision.engine.time.monotonic', lambda: clock.now)
    engine.camera_id = 'explicit-test-camera'
    engine._stop_event = threading.Event()
    engine._profile_lock = threading.RLock()
    engine._profile_revision = engine._applied_profile_revision = 7
    engine.loaded_profile_versions = {'explicit-test-item': 7}
    engine.inference_fps = 5
    engine._safe_hand_health = lambda: {'available': True, 'status': 'ready'}
    capture = {'status': 'online', 'source_session_id': 'controlled-session',
               'reconnect_epoch': 0, 'source_frame_sequence': 42}
    engine.capture = SimpleNamespace(health=lambda: deepcopy(capture))
    engine._action_snapshot = {'camera_id': engine.camera_id, 'created_at': 99.5,
        'inference_started_at': 99.6, 'published_at': 99.9, 'profile_generation': 7,
        'source_session_id': 'controlled-session', 'reconnect_epoch': 0, 'source_frame': 40,
        'timestamp': '2026-09-07T15:00:00+00:00', 'hands': [{'hand_id': 'controlled-hand'}],
        'interactions': [], 'runtime_mode': 'TEST', 'source_type': 'test_fixture',
        'is_simulated': True}
    return engine, clock, capture


@pytest.mark.parametrize('change,expected', [
    ({'source_session_id': 'new-controlled-session'}, 'source_session_changed'),
    ({'reconnect_epoch': 1}, 'reconnect_epoch_changed'),
    ({'source_session_id': ''}, 'source_session_changed'),
    *[({'status': status}, 'capture_unavailable') for status in
      ('stopped', 'ended', 'recovering', 'frame_read_failed', 'starting', 'reconnecting', 'error', 'offline')],
])
def test_source_reconnect_and_terminal_state_never_republish_recent_old_actions(subject, change, expected):
    engine, _, capture = subject
    capture.update(change)
    value = engine.action_snapshot()
    assert value['fresh'] is False
    assert value['reason'] == 'stale_frame'  # Preserve clients' public reason contract.
    assert value['diagnostics']['freshness_reason'] == expected
    assert value['hands'] == value['interactions'] == []
    assert value['source_frame'] is None and value['source_session_id'] is None


def test_fresh_result_reports_source_and_completion_ages_without_exposing_internal_clock(subject):
    engine, _, _ = subject
    original = deepcopy(engine._action_snapshot)
    value = engine.action_snapshot()
    assert value['fresh'] and value['source_frame'] == 40
    assert value['diagnostics'] == {
        'freshness_reason': 'fresh', 'freshness_limit_ms': 1000.0,
        'source_age_ms': 500.0, 'result_age_ms': 100.0, 'inference_duration_ms': 300.0,
        'source_to_result_ms': 400.0, 'capture_status': 'online',
        'capture_source_frame': 42, 'snapshot_source_frame': 40,
        'profile_generation': 7, 'applied_profile_generation': 7, 'snapshot_profile_generation': 7,
        'source_session_matches': True, 'reconnect_epoch_matches': True,
    }
    assert not {'created_at', 'published_at', 'inference_started_at'} & value.keys()
    value['hands'][0]['hand_id'] = 'client-change'
    assert engine._action_snapshot == original


@pytest.mark.parametrize('age,expected', [(1.0, True), (1.00001, False), (2.0, False)])
def test_five_fps_source_age_gate_is_not_relaxed_to_hide_stalls(subject, age, expected):
    engine, clock, _ = subject
    engine._action_snapshot['created_at'] = clock.now - age
    value = engine.action_snapshot()
    assert value['fresh'] is expected
    assert value['diagnostics']['freshness_limit_ms'] == 1000.0
    assert value['diagnostics']['freshness_reason'] == ('fresh' if expected else 'source_frame_expired')
    if not expected:
        assert value['hands'] == [] and value['timestamp'] is None
        assert value['diagnostics']['source_age_ms'] == pytest.approx(age*1000, abs=.001)


@pytest.mark.parametrize('change,expected', [('none', 'no_snapshot'), ('stop', 'engine_stopped'),
    ('profile', 'profile_changed'), ('snapshot_profile', 'profile_changed'),
    ('future', 'invalid_source_timestamp'), ('nan', 'invalid_source_timestamp')])
def test_each_rejection_is_distinguishable_and_fail_closed(subject, change, expected):
    engine, clock, _ = subject
    if change == 'none': engine._action_snapshot = None
    elif change == 'stop': engine._stop_event.set()
    elif change == 'profile': engine._profile_revision += 1
    elif change == 'snapshot_profile': engine._action_snapshot['profile_generation'] = 6
    elif change == 'future': engine._action_snapshot['created_at'] = clock.now + .1
    else: engine._action_snapshot['created_at'] = float('nan')
    value = engine.action_snapshot()
    assert value['fresh'] is False and value['hands'] == []
    assert value['diagnostics']['freshness_reason'] == expected
    json.dumps(value, allow_nan=False)


def test_diagnostics_never_copy_unbounded_capture_fields_or_private_error_strings(subject):
    engine, _, capture = subject
    capture.update(status='private-error-'*1000, password='private-password',
                   error='private-url-token', source_frame_sequence=float('inf'))
    value = engine.action_snapshot()
    diagnostics = value['diagnostics']
    assert diagnostics['capture_status'] == 'unknown'
    assert diagnostics['capture_source_frame'] is None
    assert 'private-' not in json.dumps(diagnostics, allow_nan=False)
    assert value['fresh'] is False


@pytest.mark.parametrize('status', [[], {}, None])
def test_invalid_capture_status_is_unavailable_not_a_snapshot_exception(subject, status):
    engine, _, capture = subject
    capture['status'] = status
    value = engine.action_snapshot()
    assert value['fresh'] is False
    assert value['diagnostics']['freshness_reason'] == 'capture_unavailable'
    assert value['diagnostics']['capture_status'] == 'unknown'


def test_production_process_publishes_bound_timings_without_opening_source(tmp_path, monkeypatch):
    """Real blank-frame pipeline; timestamps/pixels are explicitly controlled."""
    import time
    import cv2
    import numpy as np
    from services.vision.capture import FramePacket
    from apps.api.app.schemas import SettingsInput

    def forbidden_source(*_args, **_kwargs):
        pytest.fail('This synthetic timing contract must never open a physical source')
    monkeypatch.setattr(cv2, 'VideoCapture', forbidden_source)
    engine = VisionEngine({'id': 'timing-test', 'source_type': 'webcam', 'source': '0', 'config': {}}, [], [],
        {**SettingsInput().model_dump(), 'runtime_mode': 'TEST', 'media_root': str(tmp_path),
         'detection_mode': 'aruco', 'hand_detection_enabled': False, 'person_detection_enabled': False},
        lambda *_args: pytest.fail('No registered item or movement exists in this blank frame'))
    try:
        engine.capture.health = lambda: {'status': 'online', 'source_session_id': 'synthetic-timing-contract',
                                         'source_frame_sequence': 1, 'reconnect_epoch': 0}
        packet = FramePacket(np.zeros((64, 64, 3), dtype=np.uint8), time.monotonic(), time.time(),
                             1, 'synthetic-timing-contract', 0)
        engine._process(packet)
        result = engine.action_snapshot()
        assert result['fresh'] and result['source_frame'] == 1
        assert result['runtime_mode'] == 'TEST' and result['is_simulated'] is True
        diagnostic = result['diagnostics']
        assert diagnostic['freshness_reason'] == 'fresh'
        assert diagnostic['source_age_ms'] >= diagnostic['result_age_ms'] >= 0
        assert diagnostic['source_to_result_ms'] >= diagnostic['inference_duration_ms'] >= 0
        assert diagnostic['profile_generation'] == diagnostic['snapshot_profile_generation'] == 0
        assert engine.capture.thread is None and engine._thread is None
        assert not list(tmp_path.rglob('*.jpg')) and not list(tmp_path.rglob('*.mp4'))
    finally:
        engine.stop()
