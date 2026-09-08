"""Engine routing/safety tests; synthetic rectangles do NOT prove phone identity."""
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from services.vision.capture import FramePacket
from services.vision.detectors.base import Detection
from services.vision.engine import VisionEngine


class EmptyModelFixture:
    def __init__(self, *args, **kwargs):
        pass

    def health(self):
        return {'available': True, 'error': None}

    def detect(self, frame):
        return []


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr('services.vision.engine.NanoDetDetectorBackend', EmptyModelFixture)
    value = VisionEngine({'id': 'no-camera-opened', 'source_type': 'video', 'source': str(tmp_path / 'absent.avi')},
        [], [], {'runtime_mode': 'TEST', 'detection_mode': 'experimental', 'hand_detection_enabled': False,
                 'media_root': str(tmp_path / 'test-media')}, lambda *_: None)
    try:
        yield value
    finally:
        value.stop()


def test_generic_shape_candidate_is_visible_but_has_no_registered_identity(engine):
    frame = np.full((360, 640, 3), 220, np.uint8)
    cv2.rectangle(frame, (230, 70), (330, 280), (20, 20, 20), -1)
    engine._process(FramePacket(frame, 100., 1000., 1, 'synthetic-shape-test', 0))
    candidates = engine._recognition_candidates
    assert candidates and all(c['category_evidence'] == 'geometry_only' for c in candidates)
    assert all(c['proposal_backend'] == 'phone_shape_proposal' and c['detector_score'] is None for c in candidates)
    assert all(c.get('accepted') is not True and c.get('item_id') is None for c in candidates)
    assert engine.state_machines == {}
    assert not engine.pending


def test_shape_metadata_survives_identity_diagnostic_without_forging_model_score(engine):
    frame = np.full((100, 200, 3), 80, np.uint8)
    detection = Detection('generic:shape:1', 'cell phone', (0, 0, 200, 100), (100., 50.), .9,
                          'experimental', metadata={'backend': 'phone_shape_proposal',
                          'category_evidence': 'geometry_only', 'geometry_score': .9})
    engine._assign_reference_identities(frame, [detection])
    candidate = engine._recognition_candidates[0]
    assert candidate['category_evidence'] == 'geometry_only'
    assert candidate['geometry_score'] == .9 and candidate['detector_score'] is None
    assert not candidate['accepted']
    assert detection.identity.startswith('generic:')


def test_geometry_does_not_run_identity_even_if_encoder_would_accept(engine):
    def forbidden(*args, **kwargs):
        pytest.fail('An unclassified black rectangle must not invoke the identity matcher')
    engine.reference_matcher = SimpleNamespace(match=forbidden)
    detection = Detection('generic:shape:1', 'cell phone', (0, 0, 150, 70), (75., 35.), .99,
        'experimental', metadata={'backend': 'phone_shape_proposal', 'category_evidence': 'geometry_only'})
    engine._assign_reference_identities(np.zeros((70, 150, 3), np.uint8), [detection])
    candidate = engine._recognition_candidates[0]
    assert candidate['rejection_reason'] == 'shape_requires_category_confirmation'
    assert candidate['appearance_match_attempted'] is False
    assert candidate['best_score'] is None and not candidate['accepted']


def test_geometry_only_detection_never_queues_confirmation_media(engine):
    # Guard must run before even consulting an event id or allocating media.
    track = SimpleNamespace(metadata={'category_evidence': 'geometry_only'})
    engine._queue_event(track, SimpleNamespace(), SimpleNamespace(), np.zeros((40, 40, 3), np.uint8))
    assert not engine.pending and not engine._media_futures
