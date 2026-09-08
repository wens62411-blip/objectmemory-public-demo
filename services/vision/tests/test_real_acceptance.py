from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
import pytest

from apps.api.app.acceptance import AcceptanceService
from apps.api.app.db import Database
from apps.api.app.event_service import EventService
from apps.api.app.runtime_mode import RuntimeMode
from services.vision.acceptance import AcceptanceConfig, AcceptanceRun
from services.vision.capture import FramePacket
from services.vision.detectors import Detection
from services.vision.engine import RingFrame, VisionEngine


FRAME = np.zeros((100, 100, 3), dtype=np.uint8)
ORIGIN = {"id": "origin", "name": "桌面", "points": [[0.05, 0.1], [0.35, 0.1], [0.35, 0.9], [0.05, 0.9]]}
DESTINATION = {"id": "destination", "name": "沙发", "points": [[0.65, 0.1], [0.95, 0.1], [0.95, 0.9], [0.65, 0.9]]}
FAST_THRESHOLDS = {
    "origin_stable_frames": 3,
    "destination_stable_frames": 3,
    "min_stable_seconds": 1.0,
    "stable_jitter_norm": 0.01,
    "stable_speed_norm_s": 0.08,
    "origin_capture_jitter_norm": 0.02,
    "min_exit_frames": 2,
    "min_transit_frames": 2,
    "min_movement_distance_norm": 0.10,
    "min_trajectory_length_norm": 0.15,
    "occlusion_seconds": 0.5,
    "abort_lost_seconds": 2.0,
    "max_frame_gap_seconds": 1.0,
    "max_run_seconds": 30.0,
    "min_confidence": 0.7,
    "min_marker_size_px": 8,
}


def config(
    run_id: str = "run-1",
    *,
    kind: str = "movement",
    duration: float = 0,
    thresholds: dict | None = None,
) -> dict:
    return {
        "validation_run_id": run_id,
        "item_id": "item-1",
        "aruco_id": 1,
        "camera_id": "camera-1",
        "source_session_id": "session-1",
        "reconnect_epoch": 0,
        "trial_kind": kind,
        "minimum_duration_seconds": duration,
        "scenario_index": 5 if kind == "movement" else 3,
        "origin_zone": ORIGIN,
        "destination_zone": DESTINATION if kind == "movement" else ORIGIN,
        "thresholds": {**FAST_THRESHOLDS, **(thresholds or {})},
    }


def packet(sequence: int, monotonic: float, *, session: str = "session-1", epoch: int = 0) -> FramePacket:
    return FramePacket(FRAME.copy(), monotonic, 1_800_000_000.0 + monotonic, sequence, session, epoch)


def marker(x: float, y: float = 0.5, *, marker_id: int = 1, confidence: float = 0.98, size: int = 12) -> Detection:
    cx, cy = x * 100, y * 100
    half = size / 2
    return Detection(
        identity="item-1" if marker_id == 1 else f"aruco:{marker_id}",
        label="手机",
        bbox=(round(cx - half), round(cy - half), size, size),
        center=(cx, cy),
        confidence=confidence,
        detection_mode="aruco",
        raw_id=marker_id,
        corners=[
            (cx - half, cy - half),
            (cx + half, cy - half),
            (cx + half, cy + half),
            (cx - half, cy + half),
        ],
    )


def new_run(value: dict | None = None) -> AcceptanceRun:
    return AcceptanceRun(AcceptanceConfig.from_dict(value or config()), armed_wall_time=1_800_000_000, armed_monotonic=0)


def observe(run: AcceptanceRun, sequence: int, at: float, detection: Detection | None, **packet_options) -> bool:
    return run.observe(packet(sequence, at, **packet_options), detection, FRAME.shape, ring_seconds=5.0)


def establish_origin(run: AcceptanceRun, *, start_sequence: int = 1, start_time: float = 0.0) -> tuple[int, float]:
    sequence, at = start_sequence, start_time
    for _ in range(3):
        observe(run, sequence, at, marker(0.20))
        sequence += 1
        at += 0.5
    assert run.baseline is not None
    return sequence, at


def complete_movement(run: AcceptanceRun, sequence: int, at: float) -> tuple[int, float]:
    for x in (0.42, 0.52, 0.58, 0.75, 0.751, 0.75):
        observe(run, sequence, at, marker(x))
        sequence += 1
        at += 0.5
    return sequence, at


class _AliveThread:
    def is_alive(self) -> bool:
        return True


def make_engine(tmp_path, monkeypatch, events: list) -> VisionEngine:
    engine = VisionEngine(
        {
            "id": "camera-1",
            "name": "本机摄像头",
            "room_name": "客厅",
            "source_type": "webcam",
            "source": 0,
            "inference_fps": 2,
            "save_clips": False,
        },
        [{"id": "item-1", "name": "手机", "aruco_id": 1}],
        [ORIGIN, DESTINATION],
        {
            "runtime_mode": "REAL",
            "source_type": "opencv_camera",
            "media_root": str(tmp_path),
            "show_hands": False,
            "save_clips": False,
            "pre_seconds": 0,
            "post_seconds": 0,
            "evidence_max_width": 160,
        },
        lambda event, after, frames: events.append((event, after.copy(), [frame.copy() for frame in frames])) or True,
    )
    engine._thread = _AliveThread()
    engine._active_source_session = "session-1"
    engine._active_reconnect_epoch = 0
    monkeypatch.setattr(
        engine.capture,
        "health",
        lambda: {
            "status": "online",
            "capture_thread_alive": True,
            "source_session_id": "session-1",
            "reconnect_epoch": 0,
        },
    )
    return engine


def feed_engine(engine: VisionEngine, sequence: int, at: float, detection: Detection | None) -> None:
    value = packet(sequence, at)
    engine._switch_source_session(value)
    # Synthetic but optically decodable test input: downstream evidence gates
    # must be able to verify the actual marker pixels instead of trusting only
    # this test's injected Detection metadata. This is not physical-camera proof.
    rendered = np.full_like(FRAME, 255)
    if detection is not None and detection.raw_id is not None:
        size = max(8, min(int(detection.bbox[2]), int(detection.bbox[3])))
        marker_image = cv2.aruco.generateImageMarker(
            cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), int(detection.raw_id), size,
        )
        x = round(detection.center[0] - size / 2)
        y = round(detection.center[1] - size / 2)
        if 0 <= x and 0 <= y and x + size <= rendered.shape[1] and y + size <= rendered.shape[0]:
            rendered[y:y + size, x:x + size] = cv2.cvtColor(marker_image, cv2.COLOR_GRAY2BGR)
    value.frame = rendered
    evidence = engine._append_ring(value, rendered)
    engine._update_acceptance(value, [detection] if detection else [], FRAME.shape, evidence)
    engine._append_pending(value, evidence)


def close_engine(engine: VisionEngine) -> None:
    engine._thread = None
    engine.stop()


def test_config_rejects_bad_polygon_unknown_threshold_and_short_stationary_trial():
    malformed = config()
    malformed["origin_zone"] = {"id": "a", "name": "a", "points": [[0, 0], [0, 0], [0, 0]]}
    with pytest.raises(ValueError, match="面积"):
        AcceptanceConfig.from_dict(malformed)
    unknown = config()
    unknown["thresholds"]["magic"] = 1
    with pytest.raises(ValueError, match="未知"):
        AcceptanceConfig.from_dict(unknown)
    with pytest.raises(ValueError, match="300"):
        AcceptanceConfig.from_dict(config(kind="stationary", duration=299))
    same_zone_negative = config(kind="minor_adjustment", duration=5)
    same_zone_negative["destination_zone"] = same_zone_negative["origin_zone"]
    assert AcceptanceConfig.from_dict(same_zone_negative).origin_zone.id == "origin"
    same_zone_movement = config()
    same_zone_movement["destination_zone"] = same_zone_movement["origin_zone"]
    with pytest.raises(ValueError, match="必须不同"):
        AcceptanceConfig.from_dict(same_zone_movement)


def test_origin_baseline_requires_multiple_stable_same_marker_frames():
    run = new_run()
    observe(run, 1, 0, marker(0.20))
    observe(run, 2, 0.5, marker(0.27))
    observe(run, 3, 1.0, marker(0.20))
    assert run.baseline is None
    observe(run, 4, 1.5, marker(0.20))
    observe(run, 5, 2.0, marker(0.20))
    assert run.baseline is not None
    assert run.status()["state"] == "STABLE_AT_ORIGIN"


def test_wrong_aruco_id_never_builds_target_baseline():
    run = new_run()
    for sequence in range(1, 5):
        observe(run, sequence, (sequence - 1) * 0.5, marker(0.20, marker_id=2))
    status = run.status()
    assert status["baseline"] is None
    assert status["marker_status"] == "LOST"
    assert status["event_count"] == 0


def test_origin_detection_loss_requires_a_new_contiguous_stable_window():
    run = new_run()
    observe(run, 1, 0.0, marker(0.20))
    observe(run, 2, 0.5, marker(0.20))
    observe(run, 3, 1.0, None)
    observe(run, 4, 1.5, marker(0.20))
    assert run.baseline is None
    observe(run, 5, 2.0, marker(0.20))
    assert run.baseline is None
    observe(run, 6, 2.5, marker(0.20))
    assert run.baseline is not None
    assert run.baseline["first_frame_sequence"] == 4


def test_destination_occlusion_requires_fresh_stability_after_recovery():
    run = new_run()
    sequence, at = establish_origin(run)
    for x in (0.42, 0.52, 0.58, 0.75, 0.75):
        observe(run, sequence, at, marker(x))
        sequence += 1
        at += 0.5
    for _ in range(2):
        observe(run, sequence, at, None)
        sequence += 1
        at += 0.5
    assert run.state.value == "OCCLUDED"
    assert not observe(run, sequence, at, marker(0.75))
    assert run.confirmed_after is None
    assert not observe(run, sequence + 1, at + 0.5, marker(0.75))
    assert observe(run, sequence + 2, at + 1.0, marker(0.75))
    assert run.confirmed_after["frame_sequence"] == sequence + 2
    assert all(point["monotonic_time"] >= at for point in run.destination_samples)


def test_exit_detection_loss_cannot_join_nonconsecutive_exit_samples():
    run = new_run()
    sequence, at = establish_origin(run)
    observe(run, sequence, at, marker(0.42))
    observe(run, sequence + 1, at + 0.5, None)
    observe(run, sequence + 2, at + 1.0, marker(0.52))
    assert run.movement_started is None
    assert run.exit_streak == 1
    observe(run, sequence + 3, at + 1.5, marker(0.58))
    assert run.movement_started is not None
    assert run.movement_started["frame_sequence"] == sequence + 2


def test_low_confidence_small_or_non_quad_marker_is_treated_as_missing():
    run = new_run()
    invalid = marker(0.20, confidence=0.4, size=4)
    invalid.corners = invalid.corners[:3]
    observe(run, 1, 0, invalid)
    assert run.status()["lost_frames"] == 1
    assert run.baseline is None


def test_light_jitter_in_origin_never_starts_movement():
    run = new_run()
    sequence, at = establish_origin(run)
    for x in (0.202, 0.198, 0.201, 0.199, 0.20, 0.203):
        observe(run, sequence, at, marker(x))
        sequence += 1
        at += 0.5
    status = run.status()
    assert status["state"] == "STABLE_AT_ORIGIN"
    assert status["event_count"] == 0
    assert status["displacement"] < status["thresholds"]["effective"]["min_movement_distance_norm"]


def test_disappearance_only_becomes_occluded_then_aborted_without_confirmation():
    run = new_run()
    sequence, at = establish_origin(run)
    observe(run, sequence, at, None)
    observe(run, sequence + 1, at + 0.5, None)
    assert run.status()["state"] == "OCCLUDED"
    for offset in range(2, 7):
        observe(run, sequence + offset, at + offset * 0.5, None)
    status = run.status()
    assert status["state"] == "ABORTED"
    assert status["event_count"] == 0
    assert "not_rediscovered" in status["reason"]


def test_direct_origin_to_destination_jump_lacks_transit_and_never_confirms():
    run = new_run()
    sequence, at = establish_origin(run)
    for _ in range(6):
        observe(run, sequence, at, marker(0.75))
        sequence += 1
        at += 0.5
    status = run.status()
    assert status["event_count"] == 0
    assert status["state"] == "MOVEMENT_STARTED"
    assert "中途轨迹" in status["reason"]


def test_source_session_or_epoch_change_is_camera_interrupted():
    run = new_run()
    establish_origin(run)
    observe(run, 4, 1.5, marker(0.20), session="session-2", epoch=1)
    status = run.status()
    assert status["state"] == "CAMERA_INTERRUPTED"
    assert status["event_count"] == 0


def test_frame_gap_duplicate_sequence_and_backwards_wall_clock_fail_closed():
    gap = new_run(config("gap", thresholds={"max_frame_gap_seconds": 0.6}))
    observe(gap, 1, 0, marker(0.20))
    observe(gap, 2, 1.0, marker(0.20))
    assert gap.status()["state"] == "CAMERA_INTERRUPTED"

    duplicate = new_run(config("duplicate"))
    observe(duplicate, 1, 0, marker(0.20))
    observe(duplicate, 1, 0.5, marker(0.20))
    assert duplicate.status()["reason"] == "frame_sequence_not_increasing"

    backwards = new_run(config("backwards"))
    observe(backwards, 1, 0, marker(0.20))
    bad = packet(2, 0.5)
    bad.wall_time -= 10
    backwards.observe(bad, marker(0.20), FRAME.shape, ring_seconds=5)
    assert backwards.status()["reason"] == "frame_wall_clock_moved_backwards"


def test_movement_state_history_requires_exit_transit_destination_and_restabilization():
    run = new_run()
    sequence, at = establish_origin(run)
    complete_movement(run, sequence, at)
    status = run.status()
    states = [entry["state"] for entry in status["state_history"]]
    assert status["state"] == "MOVEMENT_CONFIRMED"
    assert states.index("MOVEMENT_STARTED") < states.index("IN_TRANSIT") < states.index("ENTERED_DESTINATION") < states.index("STABILIZING") < states.index("MOVEMENT_CONFIRMED")
    assert status["event_count"] == 0  # controller confirms; engine owns emission
    assert status["corners"] and len(status["corners"]) == 4
    assert status["baseline"]["observed_jitter_norm"] < 1e-12
    assert status["calibration_snapshot"]["effective_thresholds"]


def test_stationary_five_minutes_completes_as_zero_event_negative_sample():
    run = new_run(config("stationary", kind="stationary", duration=300, thresholds={"max_run_seconds": 30}))
    for sequence in range(1, 302):
        observe(run, sequence, float(sequence - 1), marker(0.20))
    status = run.status()
    assert status["trial_complete"] is True
    assert status["negative_sample_eligible"] is True
    assert status["state"] == "STABLE_AT_ORIGIN"
    assert status["event_count"] == 0
    assert status["total_lost_frames"] == 0
    assert status["observed_duration_seconds"] >= 300
    assert status["thresholds"]["effective"]["max_run_seconds"] >= 301
    assert status["negative_passed_criteria_met"] is True


def test_minor_adjustment_same_zone_completes_with_zero_events():
    run = new_run(config("minor", kind="minor_adjustment", duration=5))
    positions = [0.20, 0.202, 0.205, 0.21, 0.208, 0.205, 0.20, 0.203, 0.204, 0.202, 0.20]
    for sequence, x in enumerate(positions, 1):
        observe(run, sequence, float(sequence - 1) * 0.5, marker(x))
    status = run.status()
    assert status["negative_sample_eligible"] is True
    assert status["completion_outcome"].startswith("minor_adjustment")
    assert status["outside_origin_frames"] == 0
    assert status["event_count"] == 0


def test_occlusion_trial_completes_without_guessing_destination():
    run = new_run(config("occlusion", kind="occlusion", duration=3))
    sequence, at = establish_origin(run)
    while at <= 3.5:
        observe(run, sequence, at, None)
        sequence += 1
        at += 0.5
    status = run.status()
    assert status["negative_sample_eligible"] is True
    assert status["saw_occlusion"] is True
    assert status["destination_frames"] == 0
    assert status["event_count"] == 0
    assert status["after_frame"] is None


def test_disconnect_reconnect_trial_reports_interrupted_not_movement_passed():
    run = new_run(config("disconnect", kind="disconnect_reconnect"))
    establish_origin(run)
    observe(run, 4, 1.5, marker(0.20), session="new-session", epoch=1)
    status = run.status()
    assert status["state"] == "CAMERA_INTERRUPTED"
    assert status["trial_complete"] is False
    assert status["event_count"] == 0
    assert status["negative_passed_criteria_met"] is True


def test_arm_acceptance_rejects_demo_video_stopped_and_binding_mismatch(tmp_path, monkeypatch):
    demo = VisionEngine(
        {"id": "camera-1", "source_type": "video", "source": "missing.avi"},
        [{"id": "item-1", "name": "手机", "aruco_id": 1}],
        [],
        {"runtime_mode": "DEMO", "source_type": "video_file", "show_hands": False, "media_root": str(tmp_path)},
        lambda *_: True,
    )
    try:
        with pytest.raises(RuntimeError, match="REAL"):
            demo.arm_acceptance(config())
    finally:
        demo.stop()

    events = []
    engine = make_engine(tmp_path, monkeypatch, events)
    try:
        mismatch = config()
        mismatch["source_session_id"] = "forged-session"
        with pytest.raises(ValueError, match="代际"):
            engine.arm_acceptance(mismatch)
        forged_zone = config("forged-zone")
        forged_zone["origin_zone"] = {**ORIGIN, "points": [[0.0, 0.0], [0.3, 0.0], [0.3, 0.9], [0.0, 0.9]]}
        with pytest.raises(ValueError, match="区域配置"):
            engine.arm_acceptance(forged_zone)
        engine._thread = None
        with pytest.raises(RuntimeError, match="未运行"):
            engine.arm_acceptance(config())
    finally:
        close_engine(engine)


def test_public_status_health_cancel_and_run_id_replay_are_thread_safe(tmp_path, monkeypatch):
    events = []
    engine = make_engine(tmp_path, monkeypatch, events)
    try:
        armed = engine.arm_acceptance(config("thread-safe"))
        assert armed["event_count"] == 0 and armed["ring_seconds"] == 0
        assert engine.health()["acceptance"]["validation_run_id"] == "thread-safe"
        snapshots = []
        workers = [threading.Thread(target=lambda: snapshots.append(engine.acceptance_status())) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        cancelled = engine.cancel_acceptance("thread-safe", "operator_cancelled")
        assert len(snapshots) == 8
        assert cancelled["state"] == "ABORTED"
        with pytest.raises(ValueError, match="已使用"):
            engine.arm_acceptance(config("thread-safe"))
    finally:
        close_engine(engine)


def test_ring_frame_metadata_and_legacy_tuple_unpacking(tmp_path, monkeypatch):
    events = []
    engine = make_engine(tmp_path, monkeypatch, events)
    try:
        value = packet(9, 4.5)
        saved = engine._append_ring(value, FRAME)
        entry = engine.ring[-1]
        assert isinstance(entry, RingFrame)
        assert (entry.sequence, entry.source_session_id, entry.wall_time, entry.monotonic_time) == (9, "session-1", value.wall_time, 4.5)
        legacy_timestamp, legacy_frame = entry
        assert legacy_timestamp == 4.5
        assert legacy_frame is saved
    finally:
        close_engine(engine)


def test_engine_emits_once_after_forced_five_second_postroll_with_precise_before_index(tmp_path, monkeypatch):
    events = []
    engine = make_engine(tmp_path, monkeypatch, events)
    try:
        engine.arm_acceptance(config("success"))
        sequence = 1
        at = 0.0
        while at <= 5.0:
            feed_engine(engine, sequence, at, marker(0.20))
            sequence += 1
            at += 0.5
        for x in (0.42, 0.52, 0.58, 0.75, 0.751, 0.75):
            feed_engine(engine, sequence, at, marker(x))
            sequence += 1
            at += 0.5
        status = engine.acceptance_status("success")
        assert status["state"] == "MOVEMENT_CONFIRMED"
        assert status["event_count"] == 0
        assert len(engine.pending) == 1
        pending_after = engine.pending[0].evidence_frame
        while at <= 13.0:
            feed_engine(engine, sequence, at, marker(0.75))
            sequence += 1
            at += 0.5
        deadline = time.monotonic() + 3
        while not events and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(events) == 1
        event, callback_after, clip = events[0]
        assert event["validation_run_id"] == "success"
        assert event["trial_kind"] == "movement"
        assert event["detection_mode"] == "aruco_screen_validation"
        assert event["created_by"] == "vision_pipeline"
        assert event["source_session_id"] == "session-1"
        assert event["clip_required_pre_seconds"] == 5
        assert event["clip_required_post_seconds"] == 5
        assert event["clip_post_roll_complete"] is True
        assert datetime.fromisoformat(event["clip_source_timestamp_start"]) <= (
            datetime.fromisoformat(event["pickup_evidence"]["source_timestamp"])
            - timedelta(seconds=5)
        )
        assert datetime.fromisoformat(event["clip_source_timestamp_end"]) >= (
            datetime.fromisoformat(event["placement_evidence"]["source_timestamp"])
            + timedelta(seconds=5)
        )
        assert event["origin_zone"]["id"] == "origin" and event["destination_zone"]["id"] == "destination"
        assert event["before_frame"]["zone"]["id"] == "origin"
        assert event["after_frame"]["zone"]["id"] == "destination"
        assert event["source_frame_start"] == event["before_frame"]["source_frame"]
        assert event["source_timestamp_start"] == event["before_frame"]["source_timestamp"]
        assert event["trajectory"][0]["zone_name"] == "桌面"
        assert event["trajectory"][-1]["zone_name"] == "沙发"
        assert all(
            event["source_frame_start"] <= point["source_frame"] <= event["source_frame_end"]
            and point["source_timestamp"]
            and len(point["center"]) == 2
            and all(0 <= coordinate <= 1 for coordinate in point["center"])
            for point in event["trajectory"]
        )
        assert 0 <= event["before_frame_index"] < event["after_frame_index"] < len(clip)
        assert np.array_equal(callback_after, pending_after)
        assert not np.shares_memory(callback_after, clip[event["before_frame_index"]])
        assert event["source_frame_start"] < event["source_frame_end"]
        assert engine.acceptance_status("success")["event_count"] == 1
        # Terminal frames and an explicit second finalize cannot re-emit this run.
        feed_engine(engine, sequence, at, marker(0.75))
        time.sleep(0.05)
        assert len(events) == 1
    finally:
        close_engine(engine)


def test_event_origin_uses_actual_before_frame_after_permitted_in_zone_adjustment(tmp_path, monkeypatch):
    events = []
    engine = make_engine(tmp_path, monkeypatch, events)
    try:
        engine.arm_acceptance(config("origin-adjustment-before-movement"))
        sequence, at = 1, 0.0
        while at <= 5.0:
            feed_engine(engine, sequence, at, marker(0.20))
            sequence += 1
            at += 0.5
        # A 6% change inside A is below the movement threshold but above the
        # 1% stable jitter tolerance. Calibration remains at the original .20.
        for _ in range(3):
            feed_engine(engine, sequence, at, marker(0.26))
            sequence += 1
            at += 0.5
        assert engine.acceptance_status()["baseline"]["center_norm"][0] == pytest.approx(0.20)
        for x in (0.42, 0.52, 0.58, 0.75, 0.75, 0.75):
            feed_engine(engine, sequence, at, marker(x))
            sequence += 1
            at += 0.5
        confirmed_at = engine.acceptance_status()["after_frame"]["monotonic_time"]
        while at <= confirmed_at + 5.0:
            feed_engine(engine, sequence, at, marker(0.75))
            sequence += 1
            at += 0.5
        deadline = time.monotonic() + 3
        while not events and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(events) == 1
        event = events[0][0]
        assert event["from_position"] == [0.26, 0.5]
        assert event["from_position"] == event["before_frame"]["center_norm"]
        assert event["from_position"] == event["trajectory"][0]["center_norm"]
        assert event["baseline"]["center_norm"][0] == pytest.approx(0.20)
    finally:
        close_engine(engine)


def test_engine_preserves_boundary_predecessor_when_frame_cadence_has_jitter(tmp_path, monkeypatch):
    events = []
    engine = make_engine(tmp_path, monkeypatch, events)
    try:
        engine.arm_acceptance(config("jittered-pre-roll"))
        sequence = 1
        at = 0.0
        while at <= 5.2:
            feed_engine(engine, sequence, at, marker(0.20))
            sequence += 1
            at += 0.47
        for x in (0.42, 0.52, 0.58, 0.75, 0.751, 0.75):
            feed_engine(engine, sequence, at, marker(x))
            sequence += 1
            at += 0.47
        while engine.acceptance_status()["state"] != "MOVEMENT_CONFIRMED":
            feed_engine(engine, sequence, at, marker(0.75))
            sequence += 1
            at += 0.47
        confirmed_at = float(engine.acceptance_status()["after_frame"]["monotonic_time"])
        while at < confirmed_at + 5.0:
            feed_engine(engine, sequence, at, marker(0.75))
            sequence += 1
            at += 0.2
        feed_engine(engine, sequence, confirmed_at + 5.0, marker(0.75))
        deadline = time.monotonic() + 3
        while not events and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(events) == 1
        event, _after, _clip = events[0]
        clip_started = datetime.fromisoformat(event["clip_source_timestamp_start"])
        pickup_at = datetime.fromisoformat(event["pickup_evidence"]["source_timestamp"])
        assert clip_started <= pickup_at - timedelta(seconds=5)
    finally:
        close_engine(engine)


def test_postroll_includes_first_continuous_frame_crossing_boundary_at_slower_cadence(tmp_path, monkeypatch):
    events = []
    engine = make_engine(tmp_path, monkeypatch, events)
    try:
        engine.arm_acceptance(config("slower-postroll"))
        sequence, at = 1, 0.0
        while at <= 5.0:
            feed_engine(engine, sequence, at, marker(0.20))
            sequence += 1
            at += 0.5
        for x in (0.42, 0.52, 0.58, 0.75, 0.75, 0.75):
            feed_engine(engine, sequence, at, marker(x))
            sequence += 1
            at += 0.5
        confirmed_at = engine.acceptance_status()["after_frame"]["monotonic_time"]
        for offset in range(1, 10):
            feed_engine(engine, sequence, confirmed_at + offset * 0.59, marker(0.75))
            sequence += 1
        deadline = time.monotonic() + 3
        while not events and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(events) == 1
        assert events[0][0]["clip_collected_post_seconds"] == pytest.approx(5.31)
        assert events[0][0]["clip_post_roll_complete"] is True
        assert engine.acceptance_status()["state"] == "MOVEMENT_CONFIRMED"
    finally:
        close_engine(engine)


def test_default_three_second_stability_gate_works_at_five_fps_for_both_zones():
    value = config("default-five-fps")
    value["thresholds"].update({
        "origin_stable_frames": 10,
        "destination_stable_frames": 10,
        "min_stable_seconds": 3.0,
    })
    run = new_run(value)
    sequence = 1
    at = 0.0
    # Ten frames are deliberately not enough at 5 FPS: they span 1.8 s.
    for _ in range(10):
        observe(run, sequence, at, marker(0.20))
        sequence += 1
        at += 0.2
    assert run.baseline is None
    # The implementation must retain additional samples until both the frame
    # count and three-second duration gates are true.
    while at <= 5.0:
        observe(run, sequence, at, marker(0.20))
        sequence += 1
        at += 0.2
    assert run.baseline is not None
    assert run.baseline["stable_duration_seconds"] >= 3.0

    for x in (0.42, 0.52, 0.58, 0.75):
        observe(run, sequence, at, marker(x))
        sequence += 1
        at += 0.2
    for _ in range(20):
        observe(run, sequence, at, marker(0.75))
        sequence += 1
        at += 0.2
    assert run.state.value == "MOVEMENT_CONFIRMED"
    assert run.confirmed_after is not None


@pytest.mark.parametrize("callback_mode", ["false", "raise"])
def test_persistence_failure_never_marks_acceptance_confirmed(tmp_path, monkeypatch, callback_mode):
    engine = make_engine(tmp_path, monkeypatch, [])
    terminal_updates = []
    def mirror_terminal(status):
        terminal_updates.append(status)
        # Reproduce one transient database failure.  The exact same terminal
        # state must be retried instead of being deduplicated forever.
        return True if len(terminal_updates) >= 2 else None
    engine.on_acceptance_status = mirror_terminal
    if callback_mode == "false":
        engine.on_event = lambda _event, _after, _frames: False
    else:
        def fail(_event, _after, _frames):
            raise RuntimeError("database unavailable")
        engine.on_event = fail
    try:
        engine.arm_acceptance(config(f"persist-{callback_mode}"))
        sequence = 1
        at = 0.0
        while at <= 5.0:
            feed_engine(engine, sequence, at, marker(0.20))
            sequence += 1
            at += 0.5
        for x in (0.42, 0.52, 0.58, 0.75, 0.751, 0.75):
            feed_engine(engine, sequence, at, marker(x))
            sequence += 1
            at += 0.5
        while at <= 13.0:
            feed_engine(engine, sequence, at, marker(0.75))
            sequence += 1
            at += 0.5
        deadline = time.monotonic() + 3
        status = engine.acceptance_status()
        while status["state"] != "CAMERA_INTERRUPTED" and time.monotonic() < deadline:
            time.sleep(0.01)
            status = engine.acceptance_status()
        assert status["state"] == "CAMERA_INTERRUPTED"
        assert status["reason"] == "event_persistence_failed"
        assert status["event_count"] == 0
        assert status["marker_status"] != "CONFIRMED"
        assert len(terminal_updates) == 1
        feed_engine(engine, sequence, at, marker(0.75))
        assert len(terminal_updates) == 2
        feed_engine(engine, sequence + 1, at + 0.5, marker(0.75))
        assert len(terminal_updates) == 2
        assert terminal_updates[-1]["state"] == "CAMERA_INTERRUPTED"
        assert terminal_updates[-1]["reason"] == "event_persistence_failed"
    finally:
        close_engine(engine)


def test_engine_stop_during_postroll_cancels_event_and_never_calls_backend(tmp_path, monkeypatch):
    events = []
    engine = make_engine(tmp_path, monkeypatch, events)
    engine.arm_acceptance(config("stop-postroll"))
    sequence = 1
    at = 0.0
    while at <= 5.0:
        feed_engine(engine, sequence, at, marker(0.20))
        sequence += 1
        at += 0.5
    for x in (0.42, 0.52, 0.58, 0.75, 0.751, 0.75):
        feed_engine(engine, sequence, at, marker(x))
        sequence += 1
        at += 0.5
    assert engine.acceptance_status()["state"] == "MOVEMENT_CONFIRMED"
    close_engine(engine)
    status = engine.acceptance_status("stop-postroll")
    assert status["state"] == "CAMERA_INTERRUPTED"
    assert status["event_count"] == 0
    assert events == []


def test_engine_postroll_frame_gap_cancels_before_due_callback(tmp_path, monkeypatch):
    events = []
    engine = make_engine(tmp_path, monkeypatch, events)
    try:
        engine.arm_acceptance(config("post-gap"))
        sequence = 1
        at = 0.0
        while at <= 5.0:
            feed_engine(engine, sequence, at, marker(0.20))
            sequence += 1
            at += 0.5
        for x in (0.42, 0.52, 0.58, 0.75, 0.751, 0.75):
            feed_engine(engine, sequence, at, marker(x))
            sequence += 1
            at += 0.5
        assert engine.acceptance_status()["state"] == "MOVEMENT_CONFIRMED"
        feed_engine(engine, sequence, at + 5.0, marker(0.75))
        assert engine.acceptance_status()["state"] == "CAMERA_INTERRUPTED"
        assert engine.acceptance_status()["reason"] == "post_roll_frame_gap_exceeded"
        assert events == []
    finally:
        close_engine(engine)


def test_engine_source_switch_cancels_postroll_and_clears_session_ring(tmp_path, monkeypatch):
    events = []
    engine = make_engine(tmp_path, monkeypatch, events)
    try:
        engine.arm_acceptance(config("switch"))
        feed_engine(engine, 1, 0, marker(0.20))
        assert engine.ring
        engine._switch_source_session(packet(1, 1, session="session-2", epoch=1))
        status = engine.acceptance_status("switch")
        assert status["state"] == "CAMERA_INTERRUPTED"
        assert status["event_count"] == 0
        assert list(engine.ring) == []
    finally:
        close_engine(engine)


def test_actual_engine_callback_passes_real_event_service_and_sqlite_gate(tmp_path, monkeypatch):
    def iso(timestamp: float) -> str:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()

    database = Database(tmp_path / "database" / "objectmemory.sqlite", "REAL")
    item = database.save("items", {"name": "手机", "aruco_id": 1}, "item-1")
    database.save(
        "cameras",
        {
            "name": "本机摄像头",
            "room_name": "客厅",
            "source_type": "webcam",
            "source": "0",
            "config": {"simulated": False},
            "enabled": True,
            "inference_fps": 2,
            "runtime_mode": "REAL",
        },
        "camera-1",
    )
    database.save("settings", {"value": {"min_confidence": 0.55}}, "main")
    database.save(
        "source_sessions",
        {
            "camera_id": "camera-1",
            "runtime_mode": "REAL",
            "source_type": "opencv_camera",
            "is_simulated": False,
            "started_at": iso(1_800_000_000),
            "first_frame": 1,
            "last_frame": 100,
            "last_frame_at": iso(1_800_000_100),
            "status": "streaming",
            "continuity_ok": True,
            "metadata": {"reconnect_epoch": 0},
        },
        "session-1",
    )
    origin = database.save("zones", {**ORIGIN, "camera_id": "camera-1", "enabled": True}, "origin")
    destination = database.save("zones", {**DESTINATION, "camera_id": "camera-1", "enabled": True}, "destination")
    acceptance = AcceptanceService(database, RuntimeMode.REAL, lambda _camera: ("opencv_camera", False))
    suite = acceptance.create_suite({
        "item_id": item["id"],
        "camera_id": "camera-1",
        "zone_a_id": origin["id"],
        "zone_b_id": destination["id"],
        "thresholds": {
            "origin_stable_frames": 3,
            "destination_stable_frames": 3,
            "min_stable_seconds": 2.0,
            "stable_jitter_norm": 0.01,
            "stable_speed_norm_s": 0.08,
            "min_exit_frames": 2,
            "min_transit_frames": 2,
            "min_movement_distance_norm": 0.10,
            "min_trajectory_length_norm": 0.15,
            "occlusion_seconds": 0.5,
            "abort_lost_seconds": 2.0,
            "max_frame_gap_seconds": 1.0,
            "max_run_seconds": 30.0,
            "min_confidence": 0.7,
            "min_marker_size_px": 8,
        },
    })
    # The suite contract is sequential and immutable.  Preserve the two
    # negative attempts as genuine failures rather than bypassing them to cherry
    # pick the first movement slot.
    for scenario_index in (1, 2):
        prior = acceptance.create({"suite_id": suite["id"], "scenario_index": scenario_index})
        acceptance.cancel(prior["id"], "cross_layer_prerequisite", status="FAILED")
    run = acceptance.create({"suite_id": suite["id"], "scenario_index": 3})
    run = acceptance.activate(run["id"], {
        "ready": True,
        "camera_id": "camera-1",
        "source_session_id": "session-1",
        "reconnect_epoch": 0,
    })
    acceptance.observe_track({
        "camera_id": "camera-1",
        "item_id": "item-1",
        "runtime_mode": "REAL",
        "source_type": "opencv_camera",
        "is_simulated": False,
        "source_session_id": "session-1",
        "reconnect_epoch": 0,
        "detector_backend": "aruco",
        "source_frame": 1,
        "source_timestamp": iso(1_800_000_000),
        "center": [0.2, 0.5],
        "confidence": 0.98,
    })
    run = acceptance.require(run["id"])
    armed_config = acceptance.arm_config(run)
    service = EventService(
        database,
        RuntimeMode.REAL,
        tmp_path / "real-media",
        provenance_resolver=lambda _camera: ("opencv_camera", False),
        acceptance=acceptance,
    )
    persisted = []
    engine = make_engine(tmp_path / "vision", monkeypatch, [])
    engine.on_event = lambda event, after, frames: persisted.append(service.record(event, after, frames)) or True
    try:
        engine.arm_acceptance(armed_config)
        sequence = 1
        at = 0.0
        while at <= 5.0:
            feed_engine(engine, sequence, at, marker(0.20))
            sequence += 1
            at += 0.5
        for x in (0.42, 0.52, 0.58, 0.75, 0.751, 0.75):
            feed_engine(engine, sequence, at, marker(x))
            sequence += 1
            at += 0.5
        # The suite fixes a two-second destination stability gate, so
        # confirmation occurs later than the fast unit-test profile. Continue
        # far enough to collect the mandatory five-second post-roll as well.
        while at <= 15.0:
            feed_engine(engine, sequence, at, marker(0.75))
            sequence += 1
            at += 0.5
        deadline = time.monotonic() + 5
        while not persisted and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(persisted) == 1
        stored = persisted[0]
        assert database.count("events") == 1
        assert stored["validation_run_id"] == run["id"]
        assert stored["source_frame_start"] == stored["trajectory"][0]["source_frame"]
        assert stored["source_frame_end"] == stored["trajectory"][-1]["source_frame"]
        assert stored["before_frame_index"] < stored["after_frame_index"]
        assert stored["clip_post_roll_complete"] is True
        assert stored["before_screenshot"] != stored["after_screenshot"]
        clip_path = service.media_path(stored["clip_path"], "clip")
        capture = cv2.VideoCapture(str(clip_path))
        try:
            readable, decoded = capture.read()
        finally:
            capture.release()
        assert readable and decoded is not None
        completed_run = database.get("acceptance_runs", run["id"])
        assert completed_run["status"] == "PASSED"
        assert completed_run["event_id"] == stored["event_id"]
    finally:
        close_engine(engine)
