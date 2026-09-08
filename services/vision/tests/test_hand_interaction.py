"""Synthetic normalized trajectories, not physical grasp/release accuracy."""
from copy import deepcopy

import pytest

from services.vision.hand_interaction import DEFAULTS, HandObjectInteractionTracker
from services.vision.release_evidence import verified_release


def box(x=.2):
    return [x, .4, .08, .08]


def hand(object_x=.2, *, offset=.1, hand_id="hand-a", stable=True):
    center = [object_x+offset, .44]
    return {"hand_id": hand_id, "center": center,
            "bbox": [center[0]-.03, center[1]-.04, .06, .08],
            "landmarks": [list(center) for _ in range(21)], "stable": stable}


def update(tracker, frame, x=.2, *, hands=None, session="session-a", timestamp=None, **kwargs):
    return tracker.update("item", box(x), [hand(x)] if hands is None else hands,
                          frame*.2 if timestamp is None else timestamp, session, frame, **kwargs)


def establish(tracker):
    results = [update(tracker, index+1, .15+index*.025) for index in range(5)]
    assert all(not value["holding_established"] for value in results[:-1])
    assert results[-1]["holding_status"] == "co_moving"
    return results[-1]


def release(tracker, start=6):
    return [update(tracker, frame, .25, hands=[hand(.25, offset=.2)]) for frame in range(start, start+4)]


def short_release_tracker():
    # These legacy component cases exercise expiry/rebinding with an explicit
    # short test setting. Default production five seconds has its own suite.
    return HandObjectInteractionTracker({"hand_interaction_release_stable_seconds": .6})


def test_nearby_passing_hand_with_static_object_never_establishes_holding():
    tracker = HandObjectInteractionTracker({})
    for frame in range(1, 31):
        offset = -.03 + (frame % 8)*.017
        value = update(tracker, frame, hands=[hand(offset=offset)])
        assert value["holding_established"] is False
        assert value["release_observed"] is False
        assert value["co_motion_frames"] == 0


def test_stationary_hand_near_object_is_not_a_grasp_claim():
    tracker = HandObjectInteractionTracker({})
    for frame in range(1, 40):
        value = update(tracker, frame)
        assert value["holding_status"] == "nearby"
        assert value["holding_established"] is value["release_observed"] is False


def test_continuous_joint_motion_binds_one_hand_with_exact_frame_range():
    tracker = HandObjectInteractionTracker({})
    value = establish(tracker)
    assert value["hand_id"] == "hand-a" and value["co_motion_frames"] == 4
    assert value["co_motion_start_frame"] == 1 and value["co_motion_end_frame"] == 5
    assert value["source_frame"] == 5 and value["source_session_id"] == "session-a"
    assert value["release_observed"] is False


def test_fast_joint_frames_still_need_minimum_duration():
    tracker = HandObjectInteractionTracker({})
    for index in range(5):
        value = update(tracker, index+1, .15+index*.025, timestamp=index*.05)
        assert value["holding_established"] is False


def test_jitter_and_opposite_motion_do_not_build_co_motion():
    for variant in ("jitter", "opposite", "far"):
        tracker = HandObjectInteractionTracker({})
        for index in range(5):
            x = .3 + (index%2)*.001 if variant == "jitter" else .3+index*.01
            hands = [hand(x)] if variant == "jitter" else [hand(.3-index*.01)] if variant == "opposite" else [hand(x, offset=.25)]
            value = update(tracker, index+1, x, hands=hands)
            assert value["holding_established"] is False


def test_held_memory_survives_stillness_without_releasing():
    tracker = HandObjectInteractionTracker({})
    establish(tracker)
    for frame in range(6, 36):
        value = update(tracker, frame, .25)
        assert value["holding_status"] == "holding_uncertain"
        assert value["holding_established"] is True and value["release_observed"] is False
        assert value["hand_id"] == "hand-a"


@pytest.mark.parametrize("problem", ["no_hand", "model_failed", "object_missing", "unstable_hand", "new_hand_id", "ambiguous"])
def test_missing_or_ambiguous_evidence_never_releases_held_object(problem):
    tracker = HandObjectInteractionTracker({})
    establish(tracker)
    for frame in range(6, 12):
        if problem == "object_missing":
            value = tracker.update("item", None, [hand(.25)], frame*.2, "session-a", frame, detected=False)
        elif problem == "model_failed":
            value = update(tracker, frame, .25, hand_model_healthy=False)
        else:
            hands = {"no_hand": [], "unstable_hand": [hand(.25, stable=False)],
                     "new_hand_id": [hand(.25, hand_id="replacement-hand")],
                     "ambiguous": [hand(.25), hand(.25, hand_id="other-hand")]}[problem]
            value = update(tracker, frame, .25, hands=hands)
        assert value["holding_status"] == "holding_uncertain"
        assert value["holding_established"] is True and value["release_observed"] is False
        assert value["release_start_frame"] is value["release_end_frame"] is None


def test_same_hand_visible_separation_and_stability_is_release_candidate_then_release():
    tracker = short_release_tracker()
    establish(tracker)
    results = release(tracker)
    assert [value["holding_status"] for value in results] == ["release_candidate"]*3 + ["released"]
    value = results[-1]
    assert value["release_observed"] is True
    assert value["release_start_frame"] == 6 and value["release_end_frame"] == 9
    assert verified_release(value, "session-a", 1, 9, min_stable_seconds=.6)
    assert not verified_release(value, "session-a", 1, 9)
    next_value = update(tracker, 10, .25, hands=[hand(.25, offset=.2)])
    assert next_value["release_end_frame"] == next_value["source_frame"] == 10
    assert next_value["release_timestamp"] == value["release_timestamp"]
    assert verified_release(next_value, "session-a", 1, 10, min_stable_seconds=.6)


def test_same_hand_returns_far_after_missing_is_not_visible_release():
    tracker = HandObjectInteractionTracker({})
    establish(tracker)
    update(tracker, 6, .25, hands=[])
    for frame in range(7, 16):
        value = update(tracker, frame, .25, hands=[hand(.25, offset=.2)])
        assert value["holding_status"] == "holding_uncertain"
        assert value["release_observed"] is False


def test_missing_object_cannot_bridge_a_release_even_if_same_hand_remains():
    tracker = HandObjectInteractionTracker({})
    establish(tracker)
    tracker.update("item", None, [hand(.25)], 1.2, "session-a", 6, detected=False)
    for frame in range(7, 14):
        value = update(tracker, frame, .25, hands=[hand(.25, offset=.2)])
        assert value["release_observed"] is False


def test_moving_object_after_separation_is_not_release_stability():
    tracker = HandObjectInteractionTracker({})
    establish(tracker)
    for frame in range(6, 12):
        value = update(tracker, frame, .25+(frame-5)*.02, hands=[hand(.25, offset=.35)])
        assert value["release_observed"] is False


def test_unhealthy_hand_interrupts_release_and_cannot_resume_from_far():
    tracker = HandObjectInteractionTracker({})
    establish(tracker)
    update(tracker, 6, .25, hands=[hand(.25, offset=.2)])
    update(tracker, 7, .25, hands=[hand(.25, offset=.2)], hand_model_healthy=False)
    for frame in range(8, 15):
        value = update(tracker, frame, .25, hands=[hand(.25, offset=.2)])
        assert value["release_observed"] is False


def test_release_proof_expires_without_advancing_its_first_timestamp():
    tracker = short_release_tracker()
    establish(tracker)
    release(tracker)
    for frame in range(10, 25):
        value = update(tracker, frame, .25, hands=[hand(.25, offset=.2)])
    assert value["release_observed"] is True
    value = update(tracker, 25, .25, hands=[hand(.25, offset=.2)])
    assert value["release_observed"] is False
    assert value["holding_status"] == "not_established"
    assert value["reason"] == "previous_release_expired"


@pytest.mark.parametrize("loss_before_expiry", [False, True])
def test_completed_release_expiry_cannot_resurrect_permanent_held_memory(loss_before_expiry):
    tracker = short_release_tracker()
    establish(tracker)
    release(tracker)
    for frame in range(10, 25):
        update(tracker, frame, .25, hands=[] if loss_before_expiry else [hand(.25, offset=.2)])
    expired = update(tracker, 25, .25, hands=[] if loss_before_expiry else [hand(.25, offset=.2)])
    assert expired["holding_status"] == "not_established"
    assert expired["reason"] == "previous_release_expired"
    assert expired["holding_established"] is False and expired["release_observed"] is False
    assert expired["hand_id"] is None
    assert expired["co_motion_frames"] == 0 and expired["co_motion_start_frame"] is None
    assert expired["release_start_frame"] is expired["release_end_frame"] is None
    for frame in range(26, 36):
        value = update(tracker, frame, .25, hands=[])
        assert value["holding_status"] == "not_established"
        assert value["holding_established"] is value["release_observed"] is False


@pytest.mark.parametrize("change", ["session", "gap", "time_reversal"])
def test_source_discontinuity_cannot_reuse_prior_holding_or_release(change):
    tracker = HandObjectInteractionTracker({})
    establish(tracker)
    kwargs = {"session": "new-session"} if change == "session" else {"timestamp": 3} if change == "gap" else {"timestamp": .9}
    value = update(tracker, 6, .25, hands=[hand(.25, offset=.2)], **kwargs)
    assert value["holding_established"] is False and value["release_observed"] is False
    assert value["co_motion_start_frame"] is None


def test_replayed_frames_do_not_increment_motion_or_release_counters():
    tracker = HandObjectInteractionTracker({})
    establish(tracker)
    before = deepcopy(tracker.states["item"])
    for _ in range(20):
        value = update(tracker, 5, .5)
        assert value["input_accepted"] is False and value["release_observed"] is False
    assert tracker.states["item"] == before


def test_unknown_posture_but_stable_tracking_can_establish_interaction():
    tracker = HandObjectInteractionTracker({})
    for index in range(5):
        x = .15 + index*.025
        value = update(tracker, index+1, x, hands=[{**hand(x, stable=False), "tracking_stable": True, "posture": "unknown"}])
    assert value["holding_status"] == "co_moving"


@pytest.mark.parametrize("wait_frames", [0, 18])
def test_second_pickup_requires_fresh_co_motion_frame_range(wait_frames):
    tracker = short_release_tracker()
    establish(tracker)
    first_release = release(tracker)[-1]
    assert first_release["co_motion_start_frame"] == 1
    for frame in range(10, 10+wait_frames):
        update(tracker, frame, .25, hands=[hand(.25, offset=.2)])
    start = 10+wait_frames
    for offset in range(5):
        value = update(tracker, start+offset, .25+offset*.025)
        if offset < 4:
            assert value["holding_established"] is False
    assert value["holding_status"] == "co_moving"
    assert value["co_motion_start_frame"] == start
    for frame in range(start+5, start+9):
        value = update(tracker, frame, .35, hands=[hand(.35, offset=.2)])
    assert value["release_observed"] is True
    assert verified_release(value, "session-a", start, start+8, min_stable_seconds=.6)


def test_unstable_tracking_cannot_borrow_a_stable_posture():
    tracker = HandObjectInteractionTracker({})
    for index in range(5):
        x = .15+index*.025
        value = update(tracker, index+1, x, hands=[{**hand(x, stable=True), "tracking_stable": False}])
        assert value["holding_established"] is False


@pytest.mark.parametrize("bbox", [None, [0, 0, -1, 1], [float("nan"), 0, .1, .1], [0, 0, 1.1, 1], [True, 0, .1, .1]])
def test_invalid_or_predicted_object_box_never_establishes_or_releases(bbox):
    tracker = HandObjectInteractionTracker({})
    value = tracker.update("item", bbox, [hand()], .2, "s", 1)
    assert value["item_detected"] is False and value["release_observed"] is False


def test_state_capacity_expiry_and_reset_are_bounded():
    tracker = HandObjectInteractionTracker({"hand_interaction_max_items": 3, "hand_interaction_state_ttl_seconds": 2.})
    for index in range(10):
        tracker.update(str(index), box(), [hand()], .2, "s", 1)
        assert len(tracker.states) <= 3
    tracker.forget("9")
    assert "9" not in tracker.states
    assert tracker.expire(3) == 2
    update(tracker, 1)
    tracker.reset()
    assert not tracker.states


@pytest.mark.parametrize("settings", [{"hand_interaction_min_motion_frames": 1},
    {"hand_interaction_max_items": 1000000}, {"hand_interaction_near_distance": float("nan")},
    {"hand_interaction_separation_distance": .01}, {"hand_interaction_release_stable_frames": True}])
def test_invalid_thresholds_fail_closed(settings):
    with pytest.raises(ValueError):
        HandObjectInteractionTracker(settings)
    assert all(key.startswith("hand_interaction_") for key in DEFAULTS)
