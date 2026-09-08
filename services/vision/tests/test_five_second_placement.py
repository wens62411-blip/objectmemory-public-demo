"""Controlled source timestamps, not physical hand/phone recognition accuracy."""
from copy import deepcopy

import pytest

from services.vision.events import ItemMotionStateMachine, ItemState, MotionObservation
from services.vision.hand_interaction import HandObjectInteractionTracker
from services.vision.release_evidence import verified_release


def hand(x, *, far=False, identifier="hand-a"):
    center = [x + (.2 if far else .1), .44]
    return {"hand_id": identifier, "center": center,
            "bbox": [center[0] - .03, .4, .06, .08], "tracking_stable": True,
            "landmarks": [center[:] for _ in range(21)]}


def step(tracker, frame, x=.25, *, hands=None, detected=True, healthy=True, session="s", time=None):
    return tracker.update("item", [x, .4, .08, .08] if detected else None,
                          [hand(x, far=True)] if hands is None else hands,
                          frame * .2 if time is None else time, session, frame,
                          detected=detected, hand_model_healthy=healthy)


def held(tracker):
    for frame in range(1, 6):
        x = .15 + (frame - 1) * .025
        result = step(tracker, frame, x, hands=[hand(x)])
    assert result["holding_status"] == "co_moving"


def test_default_release_is_not_recordable_until_full_five_seconds():
    tracker = HandObjectInteractionTracker()
    held(tracker)
    for frame in range(6, 31):
        result = step(tracker, frame)
        assert result["holding_status"] == "release_candidate"
        assert result["release_observed"] is False
    result = step(tracker, 31)
    assert result["release_observed"] is True
    assert result["release_stable_seconds"] == pytest.approx(5)
    assert verified_release(result, "s", 1, 31)


def test_hand_can_exit_only_after_its_separation_was_actually_observed():
    tracker = HandObjectInteractionTracker()
    held(tracker)
    for frame in range(6, 10):
        result = step(tracker, frame)
    assert result["separation_verified"] is True
    for frame in range(10, 32):
        result = step(tracker, frame, hands=[])
        assert result["release_observed"] is (frame == 31)
    assert verified_release(result, "s", 1, 31)
    assert result["hand_id"] == "hand-a"


@pytest.mark.parametrize("visible_separation_frames", [0, 1, 2, 3])
def test_hand_disappearance_without_multiframe_separation_never_proves_release(visible_separation_frames):
    tracker = HandObjectInteractionTracker()
    held(tracker)
    for frame in range(6, 6 + visible_separation_frames):
        step(tracker, frame)
    for frame in range(6 + visible_separation_frames, 40):
        result = step(tracker, frame, hands=[])
        assert result["release_observed"] is False
        assert result["release_start_frame"] is None


def test_pickup_before_five_seconds_cancels_then_restarts_full_countdown():
    tracker = HandObjectInteractionTracker()
    held(tracker)
    for frame in range(6, 25):
        assert not step(tracker, frame)["release_observed"]
    for frame in range(25, 30):
        x = .25 + (frame - 25) * .025
        result = step(tracker, frame, x, hands=[hand(x)])
        assert not result["release_observed"]
        assert result["release_start_frame"] is None
    for frame in range(30, 55):
        result = step(tracker, frame, .35)
        assert not result["release_observed"]
    result = step(tracker, 55, .35)
    assert result["release_observed"]
    assert result["release_start_frame"] == 30
    assert verified_release(result, "s", 1, 55)


@pytest.mark.parametrize("problem", ["object_missing", "moving", "unhealthy", "other_hand", "gap", "session"])
def test_uncertain_frame_cannot_be_counted_towards_release(problem):
    tracker = HandObjectInteractionTracker()
    held(tracker)
    for frame in range(6, 25):
        step(tracker, frame)
    kwargs = {"detected": False} if problem == "object_missing" else (
        {"x": .3} if problem == "moving" else {"healthy": False} if problem == "unhealthy" else
        {"hands": [hand(.25, identifier="other-hand")]} if problem == "other_hand" else
        {"time": 10} if problem == "gap" else {"session": "reconnected"})
    result = step(tracker, 25, **kwargs)
    assert not result["release_observed"]
    assert result["release_start_frame"] is None
    for frame in range(26, 60):
        result = step(tracker, frame, hands=[])
        assert not result["release_observed"]


def test_replayed_final_frame_cannot_be_presented_as_new_release_evidence():
    tracker = HandObjectInteractionTracker()
    held(tracker)
    for frame in range(6, 32):
        result = step(tracker, frame)
    assert verified_release(result, "s", 1, 31)
    replayed = step(tracker, 31)
    assert not replayed["input_accepted"]
    assert not verified_release(replayed, "s", 1, 31)


@pytest.mark.parametrize("change", [
    {"release_stable_seconds": 100}, {"release_start_timestamp": 3},
    {"source_timestamp": float("nan")}, {"release_timestamp": 0},
    {"separation_verified": False}, {"input_accepted": False},
    {"co_motion_start_timestamp": float("inf")}, {"co_motion_end_timestamp": 1.5},
    {"release_visible_frames": 1}, {"separation_frame": 1},
])
def test_release_contract_rejects_forged_or_inconsistent_timing(change):
    tracker = HandObjectInteractionTracker()
    held(tracker)
    for frame in range(6, 32):
        result = step(tracker, frame)
    assert verified_release(result, "s", 1, 31)
    assert not verified_release({**result, **change}, "s", 1, 31)


def motion(frame, x, interaction, *, mode="experimental", speed=0, hand_near=False):
    return MotionObservation((x, .44), speed, "desk" if x < .3 else "shelf",
        "桌面" if x < .3 else "柜子", .95, hand_near=hand_near, detection_mode=mode,
        source_frame=frame, source_timestamp=1700000000 + frame * .2, source_session_id="s",
        hand_model_healthy=True, support_surface_confirmed=True, hand_interaction=deepcopy(interaction))


def test_first_static_sighting_is_observed_baseline_never_a_placement():
    machine = ItemMotionStateMachine(min_stable_seconds=.4, min_stable_frames=3)
    for frame in range(1, 41):
        assert machine.update(motion(frame, .2, {}), frame * .2) == []
    assert machine.state == ItemState.VISIBLE_STATIC
    assert machine.observation_verified


def test_actual_tracker_and_motion_machine_share_one_five_second_window():
    tracker = HandObjectInteractionTracker()
    machine = ItemMotionStateMachine(min_stable_seconds=.4, min_stable_frames=3)
    events = []
    for frame in range(1, 6):
        result = step(tracker, frame, .15, hands=[])
        events += machine.update(motion(frame, .19, result), frame * .2)
    for frame in range(6, 11):
        x = .15 + (frame - 6) * .04
        result = step(tracker, frame, x, hands=[hand(x)])
        events += machine.update(motion(frame, x + .04, result, speed=.2, hand_near=True), frame * .2)
    for frame in range(11, 36):
        result = step(tracker, frame, .31, hands=[] if frame >= 15 else None)
        events += machine.update(motion(frame, .35, result), frame * .2)
        assert events == []
    result = step(tracker, 36, .31, hands=[])
    events += machine.update(motion(36, .35, result), 36 * .2)
    assert len(events) == 1
    assert machine.state == ItemState.PLACED_CONFIRMED
    assert events[0].placement_evidence["release_stable_seconds"] == pytest.approx(5)
    for frame in range(37, 65):
        result = step(tracker, frame, .31, hands=[])
        assert machine.update(motion(frame, .35, result), frame * .2) == []


def test_interrupted_placement_and_repickup_are_one_episode_then_next_pickup_is_fresh():
    tracker = HandObjectInteractionTracker()
    machine = ItemMotionStateMachine(min_stable_seconds=.4, min_stable_frames=3)
    events, episodes = [], []

    def feed(frame, x, *, near=False, speed=0.):
        result = step(tracker, frame, x, hands=[hand(x)] if near else None)
        emitted = machine.update(motion(frame, x + .04, result, speed=speed, hand_near=near), frame * .2)
        events.extend(emitted)
        if machine.episode:
            episodes.append(machine.episode.movement_session_id)
        return result

    for frame in range(1, 6):
        feed(frame, .15)
    for frame in range(6, 11):
        feed(frame, .15 + (frame - 6) * .04, near=True, speed=.2)
    for frame in range(11, 30):
        feed(frame, .31)
    # Renew movement after only 3.6 seconds: no intermediate event/media.
    for frame in range(30, 35):
        feed(frame, .31 + (frame - 30) * .04, near=True, speed=.2)
    for frame in range(35, 60):
        feed(frame, .47)
        assert events == []
    feed(60, .47)
    assert len(events) == 1
    assert len(set(episodes)) == 1
    assert events[0].to_position == (.51, .44)
    assert events[0].placement_evidence['hand_interaction']['release_start_frame'] == 35

    # A new pickup after a completed release must establish a new hand episode.
    for frame in range(61, 66):
        result = feed(frame, .47 + (frame - 61) * .03, near=True, speed=.15)
    assert result['co_motion_start_frame'] == 61
    for frame in range(66, 92):
        feed(frame, .59)
    assert len(events) == 2
    assert events[1].movement_session_id != events[0].movement_session_id
    assert events[1].pickup_evidence['source_frame'] >= events[0].source_frame_end
    assert events[1].placement_evidence['hand_interaction']['co_motion_start_frame'] == 61
