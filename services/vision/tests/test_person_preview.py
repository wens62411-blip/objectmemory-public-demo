"""Explicit pose/detector fixtures plus actual pixel flow, not physical people.

Only body boxes enter display tracking; foot/map evidence stays on the pose's
original source frame and is not promoted to a tracked-frame observation.
"""
from copy import deepcopy
from datetime import datetime, timezone
import time

import pytest

from services.vision.capture import FramePacket
from services.vision.tests.test_person_engine import factory
from services.vision.tests.test_preview_tracking import frame


def test_person_original_frame_and_real_optical_flow_display_have_separate_evidence(factory):
    test = factory()
    test.controls.feet = True
    test.controls.boxes = [(80,70,60,80)]
    stamp = time.monotonic()-.2
    packet = FramePacket(frame(),stamp,time.time(),1,'person-preview-fixture',0)
    test.engine._process(packet)
    original = test.engine.recognition_snapshot()
    assert len(original['persons']) == 1
    person = original['persons'][0]
    assert person['source_frame'] == 1 and person['feet_visible'] is True
    candidates = [row for row in original['candidates'] if row.get('entity_type') == 'person']
    assert len(candidates) == 1 and candidates[0]['category'] == 'person'
    assert candidates[0]['person_track_id'] == person['person_track_id']
    assert candidates[0]['detector_score'] == .9
    assert not candidates[0]['accepted'] and candidates[0]['item_id'] is None and candidates[0]['best_item_id'] is None
    assert candidates[0]['visual_only'] and candidates[0]['observation_evidence'] is False
    before = deepcopy(test.engine.person_snapshot())
    tracked = test.engine.preview_tracker.update(frame(4),packet.source_session_id,2,stamp+.05)
    humans = [row for row in tracked if row.get('entity_type') == 'person']
    assert len(humans) == 1
    result = humans[0]
    assert result['source_frame'] == 2 and result['source_session_id'] == packet.source_session_id
    assert result['person_track_id'] == person['person_track_id']
    assert result['bbox'][0] == pytest.approx(84/320,abs=.01)
    assert result['tracker_backend'] == 'lk_forward_backward' and result['visual_only']
    assert result['accepted'] is False and result['observation_evidence'] is False
    assert result['profile_generation'] == test.engine._applied_profile_revision
    assert all(key not in result for key in ('foot_point','feet_visible','foot_confidence','landmarks','map_position','scene_version','posture'))
    assert test.engine.person_snapshot() == before
    assert len(test.controls.models[0].calls) == 1  # Preview never invokes pose.
    assert test.controls.tracker_labels == [['unit phone']]
    assert not test.engine.pending


@pytest.mark.parametrize('change', [
    {'source_frame':7},{'source_session_id':'old-source'},
    {'source_timestamp':'2000-01-01T00:00:00+00:00'},
])
def test_recognition_metadata_rejects_person_from_other_frame_session_or_time(factory,monkeypatch,change):
    test = factory()
    packet = test.process()
    stale = test.engine.person_snapshot()[0]
    sequence = test.controls.sequence+1
    wall = time.time()
    person = {**stale,'source_frame':sequence,'source_timestamp':datetime.fromtimestamp(wall,timezone.utc).isoformat(),**change}
    monkeypatch.setattr(test.engine,'person_snapshot',lambda:[person])
    test.process(wall=wall)
    snapshot = test.engine.recognition_snapshot()
    assert snapshot['source_frame'] == sequence and snapshot['persons'] == []
    assert all(row.get('entity_type') != 'person' for row in snapshot['candidates'])
    assert snapshot['candidates'][0]['accepted'] is True  # Object path is unchanged.


def test_missing_person_replaces_display_seed_without_reusing_old_body_box(factory):
    test = factory()
    test.controls.boxes = [(80,70,60,80)]
    stamp = time.monotonic()-.2
    packet = FramePacket(frame(),stamp,time.time(),1,'person-preview-fixture',0)
    test.engine._process(packet)
    assert any(row.get('entity_type') == 'person' for row in test.engine.preview_tracker.update(frame(2),packet.source_session_id,2,stamp+.03))
    test.controls.boxes = []
    packet = FramePacket(frame(4),stamp+.06,time.time(),3,packet.source_session_id,0)
    test.engine._process(packet)
    assert test.engine.recognition_snapshot()['persons'] == []
    assert all(row.get('entity_type') != 'person' for row in test.engine.preview_tracker.update(frame(6),packet.source_session_id,4,stamp+.09))


@pytest.mark.parametrize('mode,backend', [('aruco','aruco'),('experimental','nanodet')])
def test_raw_snapshot_names_actual_mode_backend_not_a_hardcoded_model(factory,mode,backend):
    test = factory(mode=mode)
    if mode == 'aruco':
        # An allocated auxiliary object is still inactive in ArUco mode.
        test.engine.phone_shape = object()
    test.process()
    assert test.engine.recognition_snapshot()['model']['candidate_backend'] == backend
    assert test.engine.recognition_snapshot()['model']['auxiliary_candidate_backend'] is None
