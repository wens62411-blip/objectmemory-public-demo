"""Profile-generation concurrency contracts; no camera/model/database is opened.

Only the pixel-flow check uses generated numeric texture. It proves generation
propagation, not recognition quality or physical-camera FPS.
"""
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from services.vision.engine import VisionEngine
from services.vision.preview_tracking import PreviewTracker


def bare_engine():
    # Do not construct the full engine: no source, model, media or worker threads.
    engine = VisionEngine.__new__(VisionEngine)
    engine._profile_lock = threading.RLock()
    engine._latest_lock = threading.RLock()
    engine._pending_lock = threading.RLock()
    engine.pending = []
    engine._stop_event = threading.Event()
    engine.capture = SimpleNamespace(health=lambda: {'status': 'ready'})
    engine._profile_revision = 0
    engine._applied_profile_revision = 0
    engine._pending_profile_items = None
    engine.loaded_profile_versions = {'item': 'old'}
    engine._recognition_snapshot = {'old': True}
    engine._tracking_snapshot = None
    engine.preview_tracker = PreviewTracker()
    return engine


def snapshot(*generations):
    return {'created_at': time.monotonic(), 'jpeg': b'unit-not-a-real-jpeg',
            'source_frame': 4, 'source_session_id': 'unit-session',
            'candidates': [{'best_item_id': f'item-{i}', **({'profile_generation': gen} if gen is not None else {})}
                           for i, gen in enumerate(generations)]}


def test_actual_flow_keeps_trusted_offer_generation_not_candidate_claim():
    tracker = PreviewTracker()
    frame = np.zeros((240, 320, 3), np.uint8)
    frame[70:150, 80:140] = np.random.default_rng(7).integers(40, 245, (80, 60, 3), dtype=np.uint8)
    candidate = {'bbox': [80/320, 70/240, 60/320, 80/240], 'accepted': True,
                 'best_item_id': 'unit-item', 'profile_generation': 999}
    tracker.offer(frame, [candidate], 'unit-session', 1, 1., profile_generation=7)
    output = tracker.update(frame.copy(), 'unit-session', 2, 1.03)
    assert len(output) == 1
    assert output[0]['profile_generation'] == 7
    assert output[0]['source_frame'] == 2
    assert output[0]['visual_only'] is True
    assert output[0]['observation_evidence'] is False
    assert candidate['profile_generation'] == 999  # caller object is not mutated


@pytest.mark.parametrize('current,applied,generations,expected', [
    (1, 0, (0, 1), []),  # refresh pending: neither old nor prematurely new proof
    (1, 1, (0, None), []),
    (1, 1, (0, 1, None), ['item-1']),
    (0, 0, (0,), ['item-0']),
])
def test_cache_read_requires_current_and_applied_profile_generation(current, applied, generations, expected):
    engine = bare_engine()
    engine._profile_revision, engine._applied_profile_revision = current, applied
    engine._tracking_snapshot = snapshot(*generations)
    result = engine.tracking_snapshot()
    assert [row['best_item_id'] for row in result['candidates']] == expected
    assert result['jpeg'] == b'unit-not-a-real-jpeg'
    assert len(engine._tracking_snapshot['candidates']) == len(generations)


def test_late_old_preview_publication_after_actual_reload_cannot_restore_identity():
    engine = bare_engine()
    old_output = snapshot(0)
    computed = threading.Event()
    refreshed = threading.Event()
    published = threading.Event()
    errors = []

    def late_preview():
        try:
            computed.set()
            assert refreshed.wait(2), 'test refresh did not run'
            # Emulate a previously computed output publishing after invalidation.
            with engine._latest_lock:
                engine._tracking_snapshot = old_output
            published.set()
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=late_preview, name='unit-late-preview-generation', daemon=True)
    worker.start()
    try:
        assert computed.wait(2)
        engine.reload_profiles([{'id': 'item', 'profile_version': 2}])
        assert engine._profile_revision == 1
        assert engine._tracking_snapshot is None
        assert engine._recognition_snapshot is None
        assert engine.loaded_profile_versions == {}
        refreshed.set()
        assert published.wait(2)
        assert engine.tracking_snapshot()['candidates'] == []
        engine._applied_profile_revision = 1
        assert engine.tracking_snapshot()['candidates'] == []
        engine._tracking_snapshot = snapshot(1)
        assert len(engine.tracking_snapshot()['candidates']) == 1
    finally:
        refreshed.set()
        worker.join(2)
    assert not worker.is_alive()
    assert not errors


@pytest.mark.parametrize('invalid', ['stopped', 'offline', 'expired'])
def test_matching_generation_still_requires_live_fresh_snapshot(invalid):
    engine = bare_engine()
    engine._tracking_snapshot = snapshot(0)
    if invalid == 'stopped':
        engine._stop_event.set()
    elif invalid == 'offline':
        engine.capture = SimpleNamespace(health=lambda: {'status': 'error'})
    else:
        engine._tracking_snapshot['created_at'] -= 1
    assert engine.tracking_snapshot() is None
