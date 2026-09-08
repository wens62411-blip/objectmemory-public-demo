"""Controlled source cadence -> real preview loop/JPEG, not camera FPS proof.

Only capture arrival times are controlled. LatestFrameSlot, preview scheduling,
pixel copying, JPEG encoding and published source binding run their real code.
No camera, detector, database, sleeps or model inference is involved.
"""
from collections import deque
import hashlib
import json
import math
import threading
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from services.vision import engine as engine_module
from services.vision.capture import FramePacket, LatestFrameSlot
from services.vision.engine import VisionEngine
from services.vision.preview_tracking import PreviewTracker
from services.vision.zones import ZoneManager


def run_cadence(monkeypatch, tmp_path, arrivals, *, jpeg_delay=0.0, source_clock_step=0.0, on_arrival=None):
    # Virtual QPC and legacy source clocks deliberately have different origins.
    # The slot obeys its timeout and coalesces actual publications made while
    # encoding is busy, so a pending deadline can be tested without real sleeps.
    clock = SimpleNamespace(now=0.0)
    source_now = lambda: 100.0 + (math.floor(clock.now/source_clock_step)*source_clock_step if source_clock_step else clock.now)
    engine = object.__new__(VisionEngine)
    engine.preview_target_fps = 30
    engine._stop_event = threading.Event()
    engine._latest_lock = threading.RLock()
    engine._preview_sequence = 0
    engine._preview_snapshot = engine._tracking_snapshot = engine._preview_error = None
    engine._preview_times = deque(maxlen=120)
    engine._inference_times = deque(maxlen=120)
    engine._profile_revision = engine._applied_profile_revision = 0
    engine._preview_max_width = 128
    engine._preview_jpeg_quality = 80
    engine.preview_tracker = PreviewTracker()
    engine.zone_manager = ZoneManager()
    engine.settings = {'detection_mode': 'aruco'}
    engine.reference_matcher = None
    engine.reference_patches = None
    engine._issued_reference_patch_detections = {}
    engine.camera = {'source_type': 'video'}
    engine.camera_id = 'controlled-cadence'
    engine.source_type, engine.runtime_mode, engine.is_simulated = 'video_file', 'TEST', True
    published = []
    supplied = []
    pending_sizes = []
    end_at = (arrivals[-1][0] if arrivals else 0) + .3

    class ControlledArrivalSlot(LatestFrameSlot):
        def __init__(self):
            super().__init__()
            self.index = 0

        def latest(self, after_revision=0, timeout=.5):
            snapshot = engine._preview_snapshot
            if snapshot and (not published or snapshot['sequence'] != published[-1]['sequence']):
                published.append({**snapshot, '_test_publish_time': clock.now})
            pending_sizes.append(getattr(engine, '_preview_pending_frames', 0))
            if clock.now >= end_at and self.index == len(arrivals):
                engine._stop_event.set()
                return None
            due = min(end_at, clock.now + max(0., timeout))
            if self.index < len(arrivals) and arrivals[self.index][0] <= due + 1e-10:
                clock.now = max(clock.now, arrivals[self.index][0])
                while self.index < len(arrivals) and arrivals[self.index][0] <= clock.now + 1e-10:
                    entry = arrivals[self.index]
                    at, sequence, source_age = entry[:3]
                    session = entry[3] if len(entry) > 3 else 'cadence-session'
                    self.index += 1
                    if len(entry) > 3 or sequence is not None:
                        if engine.capture.source_session_id != session:
                            super().clear()
                        engine.capture.source_session_id = session
                    if sequence is not None:
                        token = len(supplied)+1
                        frame = np.full((96, 128, 3), (token % 181 + 40, 120, 180), np.uint8)
                        cv2.putText(frame, f'{token:05d}', (3, 74), cv2.FONT_HERSHEY_SIMPLEX, .5, (10, 10, 10), 1)
                        # A delayed consumer cannot rewrite producer timestamps.
                        produced = 100.0 + (math.floor(at/source_clock_step)*source_clock_step if source_clock_step else at)
                        packet = FramePacket(frame, produced-source_age, 1_788_685_000+at-source_age, sequence, session)
                        supplied.append(packet)
                        super().publish(packet)
                    if on_arrival:
                        on_arrival(engine, entry)
            else:
                clock.now = due
            return super().latest(after_revision, timeout=0)

    slot = ControlledArrivalSlot()
    engine.capture = SimpleNamespace(preview_frames=slot, health=lambda: {'status': 'running'}, source_session_id='cadence-session', stop_event=threading.Event())
    # Do not replace the process-wide time module used by unrelated components.
    monkeypatch.setattr(engine_module, 'time', SimpleNamespace(monotonic=source_now, perf_counter=lambda: 10_000.0+clock.now))
    if jpeg_delay:
        encode = cv2.imencode
        def slow_encode(*args, **kwargs):
            result = encode(*args, **kwargs)
            clock.now += jpeg_delay
            return result
        monkeypatch.setattr(cv2, 'imencode', slow_encode)
    engine._run_preview()
    assert engine._preview_error is None
    assert engine._preview_sequence == len(published)
    assert slot.size <= 1
    assert len({row['sequence'] for row in published}) == len(published)
    assert len({(row['source_session_id'], row['source_frame_sequence']) for row in published}) == len(published)
    source_by_frame = {(packet.source_session_id, packet.sequence, packet.wall_time): packet for packet in supplied}
    hashes = []
    for row in published:
        packet = source_by_frame[row['source_session_id'], row['source_frame_sequence'], row['source_timestamp']]
        assert row['source_session_id'] == packet.source_session_id
        assert row['source_timestamp'] == packet.wall_time
        assert 0 <= row['created_at'] - packet.monotonic_time <= 2
        decoded = cv2.imdecode(np.frombuffer(row['jpeg'], np.uint8), cv2.IMREAD_COLOR)
        assert decoded is not None and decoded.shape == (96, 128, 3)
        hashes.append(hashlib.sha256(row['jpeg']).hexdigest())
    assert len(set(hashes)) == len(hashes), 'A repeated old image cannot count as a fresh preview'
    seconds = (published[-1]['_test_publish_time']-published[0]['_test_publish_time']) if len(published) > 1 else 0
    result = {'physical_camera': False, 'actual_preview_loop': True, 'actual_jpeg_encoding': True,
        'clock': 'controlled separate QPC/source clocks and condition timeouts', 'input_frames': len(supplied), 'output_frames': len(published),
        'output_fps': (len(published)-1)/seconds if seconds else 0,
        'target_fps': 30, 'source_frames': [row['source_frame_sequence'] for row in published],
        'source_sessions': [row['source_session_id'] for row in published],
        'published_times': [row['_test_publish_time'] for row in published], 'distinct_jpegs': len(set(hashes)),
        'max_pending_frames': max(pending_sizes, default=0)}
    (tmp_path/'preview-pacing.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result


def test_thirty_hz_alternating_six_ms_early_arrivals_do_not_lose_cadence(monkeypatch, tmp_path):
    arrivals = [(index/30-(.006 if index % 2 else 0), index+1, 0) for index in range(300)]
    result = run_cadence(monkeypatch, tmp_path, arrivals)
    assert result['output_frames'] >= 294, result
    assert 29.8 <= result['output_fps'] <= 30.2, result


@pytest.mark.parametrize('source_fps', [30, 60, 120])
def test_faster_sources_remain_target_limited_without_reusing_frames(monkeypatch, tmp_path, source_fps):
    arrivals = [(index/source_fps, index+1, 0) for index in range(source_fps*10)]
    result = run_cadence(monkeypatch, tmp_path, arrivals)
    assert 295 <= result['output_frames'] <= 301, result
    assert 29.5 <= result['output_fps'] <= 30.2, result
    for index, stamp in enumerate(result['published_times']):
        # A one-second sliding window may include one boundary frame, never
        # an extra burst proportional to the faster input source.
        assert sum(stamp <= value < stamp+1 for value in result['published_times'][index:]) <= 31
        assert index+1 <= 1+math.floor((stamp+1/60+1e-8)*30)


def test_idle_polls_and_long_gap_do_not_reencode_or_catch_up_old_frames(monkeypatch, tmp_path):
    result = run_cadence(monkeypatch, tmp_path, [
        (0, 1, 0), (1/30, 2, 0), (.1, None, 0), (.2, None, 0),
        (2.9, None, 0), (5, 3, 3), (5+1/30, 4, 0),
        (5+2/30, 5, 0), (6, None, 0), (8, None, 0),
    ])
    assert result['source_frames'] == [1, 2, 4, 5], result
    assert result['output_frames'] == result['distinct_jpegs'] == 4
    assert result['published_times'][2:] == pytest.approx([5+1/30, 5+2/30])
