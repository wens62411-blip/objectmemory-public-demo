"""Deterministic gate controls, not real-world identity accuracy tests."""
from services.vision.observation import ObservationGate
from services.vision.events.state_machine import MotionObservation


def motion(frame, x=.2, session='s'):
    return MotionObservation((x, .3), .3, 'z', '桌面', .9, detection_mode='experimental',
                             source_frame=frame, source_timestamp=100+frame*.2,
                             source_session_id=session)


IDENTITY = {'accepted': True, 'item_id': 'p', 'profile_version': 1}


def test_moving_visible_object_needs_no_stationary_baseline_or_hand():
    gate = ObservationGate()
    assert gate.observe('p', motion(1), .2, IDENTITY) is None
    assert gate.observe('p', motion(2, .23), .4, IDENTITY) is None
    assert gate.observe('p', motion(3, .27), .6, IDENTITY)['motion'].source_frame == 3


def test_missing_and_rejection_do_not_advance_real_observation():
    gate = ObservationGate()
    for n in range(1, 4): gate.observe('p', motion(n), n*.2, IDENTITY)
    gate.missing('p')
    assert gate.verified['p']['motion'].source_frame == 3
    assert gate.observe('p', motion(4), .8, {**IDENTITY, 'accepted': False}) is None
    assert gate.verified['p']['motion'].source_frame == 3


def test_duplicate_frame_cannot_satisfy_continuity():
    gate = ObservationGate()
    for _ in range(20): assert gate.observe('p', motion(1), .2, IDENTITY) is None
    assert not gate.verified


def test_reset_and_profile_change_rebuild_identity_continuity():
    gate = ObservationGate()
    for n in range(1, 4): gate.observe('p', motion(n), n*.2, IDENTITY)
    gate.reset()
    assert not gate.verified
    assert gate.observe('p', motion(1, session='new'), 1, IDENTITY) is None
    assert gate.observe('p', motion(2, session='new'), 1.2, {**IDENTITY, 'profile_version': 2}) is None
    assert gate.candidates['p']['count'] == 1


def test_frame_gap_and_unreachable_jump_restart_identity_checks():
    gate = ObservationGate()
    gate.observe('p', motion(1), .2, IDENTITY)
    gate.observe('p', motion(2), 2, IDENTITY)
    assert gate.candidates['p']['count'] == 1
    gate.observe('p', motion(3, .95), 2.2, IDENTITY)
    assert gate.candidates['p']['count'] == 1
