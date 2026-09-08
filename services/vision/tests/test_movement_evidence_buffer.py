"""Actual state/recorder/TEST SQLite contracts using synthetic pixels/trajectories.

This deliberately does not claim physical phone or hand detector accuracy.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib

import cv2
import numpy as np
import pytest

from apps.api.app.event_service import EvidenceRejected
from apps.api.tests.test_authenticity_storage import client_for, add_item_camera
from services.vision.capture import FramePacket
from services.vision.engine import VisionEngine
from services.vision.events import ItemMotionStateMachine
from services.vision.hand_interaction import HandObjectInteractionTracker
from services.vision.tests.test_five_second_placement import hand, motion
from services.vision.trackers import TrackedObservation


SESSION = 's'
WALL = 1_700_000_000.


def register_source(runtime, camera):
    runtime.db.save('source_sessions', {
        'camera_id': camera['id'], 'runtime_mode': 'TEST', 'source_type': 'opencv_camera',
        'is_simulated': True, 'started_at': datetime.fromtimestamp(WALL, timezone.utc).isoformat(),
        'first_frame': 1, 'last_frame': 38,
        'last_frame_at': datetime.fromtimestamp(WALL + 7.6, timezone.utc).isoformat(),
        'status': 'streaming', 'continuity_ok': True,
    }, SESSION)


def packet(frame, *, session=SESSION, epoch=0):
    image = np.full((64, 96, 3), frame * 3, np.uint8)
    image[frame % 50:frame % 50 + 8, 4:25] = (0, 150, 230)
    return FramePacket(image, frame * .2, WALL + frame * .2, frame, session, epoch)


def make_engine(tmp_path, callback, *, camera=None, item=None, post=.4):
    camera = camera or {'id': 'camera', 'name': 'TEST', 'source_type': 'webcam', 'source': '0', 'inference_fps': 5}
    item = item or {'id': 'item', 'name': 'TEST 手机'}
    engine = VisionEngine(camera, [item], [],
        {'runtime_mode': 'TEST', 'source_type': 'opencv_camera', 'media_root': str(tmp_path),
         'show_hands': False, 'show_persons': False, 'pre_seconds': .2, 'post_seconds': post,
         'inference_fps': 5, 'min_stable_seconds': .4, 'min_stable_frames': 3}, callback)
    engine._ring_capacity = 10
    engine.state_machines[item['id']] = ItemMotionStateMachine(min_stable_seconds=.4, min_stable_frames=3)
    return engine, item['id']


def drive_episode(engine, item_id, *, queue=True, queue_detection_mode='experimental'):
    tracker = HandObjectInteractionTracker()
    machine = engine.state_machines[item_id]
    confirmed = []
    for frame in range(1, 37):
        value = packet(frame)
        evidence = engine._append_ring(value, value.frame)
        if frame < 6:
            x, hands, near, speed = .15, [], False, 0.
        elif frame < 11:
            x = .15 + (frame - 6) * .04
            hands, near, speed = [hand(x)], True, .2
        else:
            x, hands, near, speed = .31, [hand(.31, far=True)] if frame < 15 else [], False, 0.
        interaction = tracker.update(item_id, [x, .4, .08, .08], hands, frame * .2, SESSION, frame)
        observed = motion(frame, x + .04, interaction, speed=speed, hand_near=near)
        emitted = machine.update(observed, frame * .2)
        confirmed.extend(emitted)
        if emitted and queue:
            track = TrackedObservation('track', item_id, 'phone', (30, 25, 8, 8), (34, 29), (.35, .44),
                (0, 0), 0, .95, queue_detection_mode, None, frame, metadata={'category_evidence': 'model'})
            engine._queue_event(track, emitted[0], value, evidence)
    assert len(confirmed) == 1
    return confirmed[0]


def test_movement_buffer_keeps_origin_past_sliding_ring_and_updates_actual_postroll(tmp_path):
    callbacks = []
    engine, item_id = make_engine(tmp_path, lambda *args: callbacks.append(args))
    try:
        # The legacy marker path retains its configured post-roll; photo
        # releases have already collected their five-second tail at admission.
        event = drive_episode(engine, item_id, queue_detection_mode='aruco')
        assert len(engine.ring) > engine._ring_capacity
        pending = engine.pending[0]
        assert pending.event['source_frame_start'] == event.source_frame_start
        assert pending.event['clip_frame_sources'][0]['sequence'] == event.source_frame_start
        assert pending.event['after_frame_index'] == len(pending.frames) - 1
        assert pending.event['clip_fps'] == pytest.approx(5.)
        after_index = pending.event['after_frame_index']
        for frame in (37, 38):
            value = packet(frame)
            engine._append_pending(value, value.frame)
        engine.stop()
        assert len(callbacks) == 1
        saved, after, frames = callbacks[0]
        assert saved['clip_source_frame_end'] == 38
        assert saved['after_frame_index'] == after_index < len(frames) - 1
        assert np.array_equal(after, packet(36).frame)
        assert np.array_equal(frames[after_index], after)
        assert not np.array_equal(frames[-1], after)
        assert saved['clip_frame_sources'][after_index]['sequence'] == 36
    finally:
        engine.stop()


@pytest.mark.parametrize('problem', ['missing_origin', 'wrong_session', 'wrong_epoch', 'wrong_pixels', 'gap', 'budget'])
def test_missing_or_mismatched_episode_evidence_never_reaches_writer(tmp_path, problem):
    callbacks = []
    engine, item_id = make_engine(tmp_path, lambda *args: callbacks.append(args))
    try:
        event = drive_episode(engine, item_id, queue=False)
        value = packet(36)
        if problem == 'missing_origin':
            while engine.ring and engine.ring[0].sequence <= event.source_frame_start:
                engine.ring.popleft()
        elif problem == 'wrong_session':
            value.source_session_id = 'reconnected'
        elif problem == 'wrong_epoch':
            value.reconnect_epoch = 1
        elif problem == 'wrong_pixels':
            value.frame = np.zeros_like(value.frame)
        elif problem == 'gap':
            engine.ring[12].monotonic_time += 1.
        else:
            engine._ring_max_bytes = 1
        track = TrackedObservation('track', item_id, 'phone', (30, 25, 8, 8), (34, 29), (.35, .44),
            (0, 0), 0, .95, 'experimental', None, 36, metadata={'category_evidence': 'model'})
        engine._queue_event(track, event, value, value.frame)
        assert engine.pending == []
        assert callbacks == []
        assert engine._last_error
    finally:
        engine.stop()


def test_episode_protection_never_overrides_hard_memory_limit(tmp_path):
    callbacks = []
    engine, item_id = make_engine(tmp_path, lambda *args: callbacks.append(args))
    engine._ring_max_bytes = packet(1).frame.nbytes * 7
    try:
        event = drive_episode(engine, item_id)
        assert engine._ring_bytes <= engine._ring_max_bytes
        assert len(engine.ring) <= 7
        assert engine.ring[0].sequence > event.source_frame_start
        assert engine.pending == []
        assert callbacks == []
        assert '不完整历史证据' in engine._last_error
    finally:
        engine.stop()


@pytest.mark.parametrize('problem', ['session', 'epoch', 'gap', 'budget'])
def test_bad_postroll_is_discarded_instead_of_joined_to_confirmed_episode(tmp_path, problem):
    callbacks = []
    engine, item_id = make_engine(tmp_path, lambda *args: callbacks.append(args))
    try:
        drive_episode(engine, item_id, queue_detection_mode='aruco')
        value = packet(37)
        if problem == 'session':
            value.source_session_id = 'new-session'
        elif problem == 'epoch':
            value.reconnect_epoch = 1
        elif problem == 'gap':
            value.monotonic_time += 1
        else:
            engine._ring_max_bytes = 1
        engine._append_pending(value, value.frame)
        assert engine.pending == []
        assert callbacks == []
    finally:
        engine.stop()


@pytest.mark.parametrize('queue_mode', ['experimental', 'aruco'])
def test_real_test_database_writer_decodes_bound_five_second_placement_evidence(tmp_path, queue_mode):
    with client_for(tmp_path / 'db') as client:
        item, camera = add_item_camera(client)
        runtime = client.app.state.runtime
        register_source(runtime, camera)
        saved = []

        def persist(event, after, frames):
            result = runtime.event(event, after, frames)
            saved.append(result)
            return result

        engine, item_id = make_engine(tmp_path / 'engine', persist, camera=camera, item=item)
        try:
            drive_episode(engine, item_id, queue_detection_mode=queue_mode)
            if queue_mode == 'experimental':
                assert engine.pending == []  # queued to writer at five seconds, without extra source frames
            for frame in (37, 38):
                value = packet(frame)
                engine._append_pending(value, value.frame)
            engine.stop()
            assert len(saved) == 1, engine._last_error
            result = saved[0]
            assert result['final_status'] == 'confirmed_placed'
            assert result['source_frame_end'] == 36
            assert result['clip_source_frame_end'] == (36 if queue_mode == 'experimental' else 38)
            assert result['placement_evidence']['hand_interaction']['release_stable_seconds'] == pytest.approx(5.)
            assert runtime.db.count('events') == 1
            state = runtime.db.get('item_current_state', f'TEST:{item_id}')
            assert state['last_confirmed_placed_at'] == result['timestamp_end']
            screenshot = runtime.event_service.media_path(result['screenshot_path'])
            clip = runtime.event_service.media_path(result['clip_path'])
            assert hashlib.sha256(screenshot.read_bytes()).hexdigest() == result['screenshot_sha256']
            assert hashlib.sha256(clip.read_bytes()).hexdigest() == result['clip_sha256']
            assert cv2.imdecode(np.frombuffer(screenshot.read_bytes(), np.uint8), cv2.IMREAD_COLOR) is not None
            capture = cv2.VideoCapture(str(clip))
            decoded = []
            try:
                while True:
                    ok, image = capture.read()
                    if not ok:
                        break
                    decoded.append(image)
                assert capture.get(cv2.CAP_PROP_FPS) == pytest.approx(5., abs=.01)
            finally:
                capture.release()
            media = runtime.db.list('event_media', {'event_id': result['event_id']})
            assert len(media) == 2
            binding = media[0]['metadata']['write_binding']
            assert binding['source_bound_clip'] is True
            assert len(decoded) == len(binding['clip_frame_sources'])
            assert (result['after_frame_index'] == len(decoded) - 1) is (queue_mode == 'experimental')
            assert binding['after_frame_index'] == result['after_frame_index']
        finally:
            engine.stop()


@pytest.mark.parametrize('problem', ['session', 'epoch', 'sequence', 'time', 'after_index', 'frame_count', 'outside_session'])
def test_real_database_rejects_conflicting_clip_frame_proofs_before_writing(tmp_path, problem):
    with client_for(tmp_path / 'db') as client:
        item, camera = add_item_camera(client)
        runtime = client.app.state.runtime
        register_source(runtime, camera)
        callbacks = []
        engine, item_id = make_engine(tmp_path / 'engine', lambda *args: callbacks.append(args), camera=camera, item=item)
        try:
            drive_episode(engine, item_id, queue_detection_mode='aruco')
            for frame in (37, 38):
                value = packet(frame)
                engine._append_pending(value, value.frame)
            engine.stop()
            event, after, frames = callbacks[0]
            event = deepcopy(event)
            if problem == 'session':
                event['clip_frame_sources'][1]['source_session_id'] = 'other-camera-session'
            elif problem == 'epoch':
                event['clip_frame_sources'][1]['reconnect_epoch'] = 1
            elif problem == 'sequence':
                event['clip_frame_sources'][1]['sequence'] = event['clip_frame_sources'][0]['sequence']
            elif problem == 'time':
                event['clip_frame_sources'][1]['source_timestamp'] = event['clip_frame_sources'][0]['source_timestamp']
            elif problem == 'after_index':
                event['after_frame_index'] = len(frames) - 1
            elif problem == 'frame_count':
                event['clip_frame_sources'].pop()
            else:
                event['clip_frame_sources'][-1]['sequence'] = 100
            with pytest.raises(EvidenceRejected):
                runtime.event_service.record(event, after, frames)
            assert runtime.db.count('events') == 0
            assert runtime.db.count('event_media') == 0
            assert list((runtime.media / 'event-images').iterdir()) == []
            assert list((runtime.media / 'event-clips').iterdir()) == []
        finally:
            engine.stop()
