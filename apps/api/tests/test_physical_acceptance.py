from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from apps.api.app.acceptance import AcceptanceRejected, AcceptanceService
from apps.api.app.db import Database
from apps.api.app.event_service import EventService, EvidenceRejected
from apps.api.app.main import Runtime, create_app
from apps.api.app.retention import RetentionService
from apps.api.app.runtime_mode import RuntimeMode
from apps.api.app.search import location_result
from services.vision.events.media import EventMediaWriter


ZONE_A = [[0.02, 0.05], [0.34, 0.05], [0.34, 0.95], [0.02, 0.95]]
ZONE_B = [[0.66, 0.05], [0.98, 0.05], [0.98, 0.95], [0.66, 0.95]]


class GuardLock:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.depth = 0

    def acquire(self) -> bool:
        acquired = self._lock.acquire()
        if acquired:
            self.depth += 1
        return acquired

    def release(self) -> None:
        self.depth -= 1
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_args):
        self.release()

    @property
    def held(self) -> bool:
        return self.depth > 0


def acceptance_fixture(tmp_path, *, marker_id=0):
    data = tmp_path / "data"
    media = data / "real-media"
    db = Database(data / "database" / "objectmemory.sqlite", "REAL")
    db.save("settings", {"value": {"min_confidence": 0.55, "retention_policy": "MINIMAL"}}, "main")
    item = db.save("items", {"name": "真实手机", "type": "phone", "aliases": ["手机"], "aruco_id": marker_id})
    camera = db.save("cameras", {
        "name": "电脑摄像头", "room_name": "客厅", "source_type": "webcam", "source": "0",
        "config": {}, "enabled": True, "inference_fps": 5, "save_clips": True,
        "runtime_mode": "REAL",
    })
    first = db.save("zones", {"camera_id": camera["id"], "name": "桌面", "points": ZONE_A, "priority": 1, "enabled": True})
    second = db.save("zones", {"camera_id": camera["id"], "name": "沙发", "points": ZONE_B, "priority": 1, "enabled": True})
    acceptance = AcceptanceService(db, RuntimeMode.REAL, lambda _camera: ("opencv_camera", False))
    suite = acceptance.create_suite({
        "item_id": item["id"], "camera_id": camera["id"],
        "zone_a_id": first["id"], "zone_b_id": second["id"],
    })
    return db, media, item, camera, first, second, suite, acceptance


def activate_run(db, acceptance, suite, item, camera, first, second, *, trial_kind="movement", duration=None):
    scenario_index = {
        "stationary": 1, "minor_adjustment": 2, "movement": 3,
        "occlusion": 7, "disconnect_reconnect": 8,
    }[trial_kind]
    for prior_index in range(1, scenario_index):
        if db.list("acceptance_runs", {"suite_id": suite["id"], "scenario_index": prior_index}, limit=1):
            continue
        prior = acceptance.create({"suite_id": suite["id"], "scenario_index": prior_index})
        db.save("acceptance_runs", {
            "status": "FAILED", "outcome": "FAILED", "failure_reason": "test_prerequisite",
            "ended_at": datetime.now(timezone.utc).isoformat(),
        }, prior["id"])
    payload = {
        "suite_id": suite["id"], "scenario_index": scenario_index,
        "item_id": item["id"], "camera_id": camera["id"],
        "origin_zone_id": first["id"],
        "destination_zone_id": second["id"] if trial_kind == "movement" else first["id"],
        "trial_kind": trial_kind,
    }
    if duration is not None:
        payload["minimum_duration_seconds"] = duration
    run = acceptance.create(payload)
    session_id = f"session-{run['id']}"
    started = datetime.now(timezone.utc) - timedelta(seconds=10)
    db.save("source_sessions", {
        "camera_id": camera["id"], "runtime_mode": "REAL", "source_type": "opencv_camera",
        "is_simulated": False, "started_at": started.isoformat(), "first_frame": 0,
        "last_frame": 100, "last_frame_at": (started + timedelta(seconds=30)).isoformat(),
        "status": "streaming", "continuity_ok": True,
    }, session_id)
    run = acceptance.activate(run["id"], {
        "ready": True, "camera_id": camera["id"], "source_session_id": session_id,
        "reconnect_epoch": 0,
    })
    acceptance.observe_track({
        "camera_id": camera["id"], "item_id": item["id"], "runtime_mode": "REAL",
        "source_type": "opencv_camera", "is_simulated": False, "source_session_id": session_id,
        "reconnect_epoch": 0, "detector_backend": "aruco", "detection_mode": "aruco",
        "source_frame": 9, "source_timestamp": started.isoformat(), "center": [0.2, 0.5],
        "bbox": [10, 10, 80, 80], "state": "stable", "confidence": 0.99,
    })
    return acceptance.require(run["id"]), started


def vision_event_payload(run, started):
    event_start = started + timedelta(seconds=11)
    event_end = event_start + timedelta(seconds=3)
    centers = ([0.20, 0.50], [0.38, 0.50], [0.58, 0.50], [0.80, 0.50])
    zones = (
        {"id": run["origin_zone_id"], "name": run["expected_from_zone"]},
        None,
        None,
        {"id": run["destination_zone_id"], "name": run["expected_to_zone"]},
    )
    trajectory = []
    for offset, (center, zone) in enumerate(zip(centers, zones)):
        timestamp = event_start + timedelta(seconds=offset)
        trajectory.append({
            "frame_sequence": 10 + offset,
            "frame_timestamp": timestamp.isoformat(),
            "center": [center[0] * 640, center[1] * 480],
            "center_norm": list(center),
            "zone": zone,
            "confidence": 0.98,
            "aruco_id": run["aruco_id"],
            "source_session_id": run["source_session_id"],
            "reconnect_epoch": run["reconnect_epoch"],
            "lost": False,
        })
    return {
        "id": f"vision-{run['id']}", "event_id": f"vision-{run['id']}",
        "movement_session_id": f"vision-{run['id']}", "validation_run_id": run["id"],
        "scenario_index": run["scenario_index"], "trial_kind": run["trial_kind"],
        "item_id": run["item_id"],
        "aruco_id": run["aruco_id"], "event_type": "movement", "camera_id": run["camera_id"],
        "runtime_mode": "REAL", "source_type": "opencv_camera", "is_simulated": False,
        "source_session_id": run["source_session_id"], "reconnect_epoch": run["reconnect_epoch"],
        "source_frame_start": 10, "source_frame_end": 13,
        "source_timestamp_start": event_start.isoformat(), "source_timestamp_end": event_end.isoformat(),
        "timestamp_start": event_start.isoformat(), "timestamp_end": event_end.isoformat(),
        "detector_backend": "aruco", "tracker_backend": "aruco_acceptance_state_machine",
        "detection_mode": "aruco_screen_validation",
        "from_zone": run["expected_from_zone"], "from_zone_id": run["origin_zone_id"],
        "to_zone": run["expected_to_zone"], "to_zone_id": run["destination_zone_id"],
        "zone_id": run["destination_zone_id"], "zone_name": run["expected_to_zone"],
        "from_position": centers[0],
        "to_position": centers[-1], "final_position": centers[-1], "confidence": 0.98,
        "source_continuity_ok": True, "reconnect_epoch_changed": False,
        "stable_before": True, "stable_after": True, "meaningful_position_change": True,
        "origin_zone": run["origin_zone"], "destination_zone": run["destination_zone"],
        "trajectory": trajectory,
        "pickup_evidence": {"source_frame": 10, "source_timestamp": event_start.isoformat()},
        "placement_evidence": {"source_frame": 13, "source_timestamp": event_end.isoformat()},
        "before_frame_index": 25, "after_frame_index": 35,
        "clip_source_frame_start": 1, "clip_source_frame_end": 60,
        "clip_source_timestamp_start": (event_start - timedelta(seconds=5)).isoformat(),
        "clip_source_timestamp_end": (event_end + timedelta(seconds=5)).isoformat(),
        "clip_required_pre_seconds": 5.0, "clip_required_post_seconds": 5.0,
        "clip_collected_post_seconds": 5.0, "clip_post_roll_complete": True,
    }


def acceptance_clip_frames(shape=(144, 192, 3)):
    """Eleven seconds of 5 FPS evidence with distinct A/B keyframes."""
    pre = np.full(shape, 5, np.uint8)
    before = np.full(shape, 35, np.uint8)
    transit = np.full(shape, 110, np.uint8)
    after = np.full(shape, 190, np.uint8)
    post = np.full(shape, 245, np.uint8)
    frames = [pre.copy() for _ in range(25)]
    frames.append(before)
    frames.extend(transit.copy() for _ in range(9))
    frames.append(after)
    frames.extend(post.copy() for _ in range(20))
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    size = max(12, min(36, shape[1] // 6))
    tag = cv2.cvtColor(cv2.aruco.generateImageMarker(dictionary, 0, size), cv2.COLOR_GRAY2BGR)
    for index, image in enumerate(frames):
        image[:] = 235
        x_norm = 0.2 if index <= 25 else 0.8 if index >= 35 else 0.2 + 0.6 * (index - 25) / 10
        x, y = round(x_norm * shape[1]) - size // 2, shape[0] // 2 - size // 2
        image[max(0,y-3):y+size+3, max(0,x-3):x+size+3] = 255
        image[y:y+size, x:x+size] = tag
    return frames, frames[25], frames[35]


def test_real_vision_payload_commits_one_atomic_three_media_bundle_and_replays(tmp_path, monkeypatch):
    db, media, item, camera, first, second, suite, acceptance = acceptance_fixture(tmp_path)
    run, started = activate_run(db, acceptance, suite, item, camera, first, second)
    retention = RetentionService(db, tmp_path / "data", media, max_storage_mb=500)
    guard = GuardLock()
    retention.lock = guard
    retention.trigger_async = lambda _trigger="event": False
    service = EventService(db, RuntimeMode.REAL, media, retention, lambda _camera: ("opencv_camera", False), acceptance)

    original_image = service._atomic_image
    writes_while_locked = []

    def guarded_image(*args, **kwargs):
        writes_while_locked.append(guard.held)
        return original_image(*args, **kwargs)

    original_clip = EventMediaWriter.write_clip

    def guarded_clip(self, *args, **kwargs):
        writes_while_locked.append(guard.held)
        return original_clip(self, *args, **kwargs)

    monkeypatch.setattr(service, "_atomic_image", guarded_image)
    monkeypatch.setattr(EventMediaWriter, "write_clip", guarded_clip)

    frames, before, after = acceptance_clip_frames()
    payload = vision_event_payload(run, started)

    saved = service.record(payload, after, frames)
    assert saved["validation_run_id"] == run["id"]
    assert saved["tracker_backend"] == "aruco_acceptance_state_machine"
    assert saved["trajectory"][0]["source_frame"] == 10
    assert saved["trajectory"][0]["center"] == [0.2, 0.5]
    assert saved["trajectory"][0]["zone_name"] == "桌面"
    assert saved["before_frame_index"] == 25 and saved["after_frame_index"] == 35
    assert saved["before_frame_sha256"] == service._frame_sha256(before)
    assert saved["after_frame_sha256"] == service._frame_sha256(after)
    assert all(writes_while_locked) and len(writes_while_locked) == 3

    completed = acceptance.require(run["id"])
    assert completed["status"] == "PASSED" and completed["event_id"] == saved["event_id"]
    assert completed["result"]["source_session_id"] == run["source_session_id"]
    assert completed["result"]["before_screenshot_sha256"] == saved["before_screenshot_sha256"]
    media_rows = db.list("event_media", {"event_id": saved["event_id"]}, limit=10)
    assert {(row["kind"], row["role"]) for row in media_rows} == {
        ("image", "before"), ("image", "after"), ("clip", "clip")
    }
    assert all(service.media_path(row["path"]).is_file() for row in media_rows)
    state = db.get("item_current_state", f"REAL:{item['id']}")
    assert state["evidence_event_id"] == saved["event_id"] and state["validation_run_id"] == run["id"]

    replay = service.record(payload, after.copy(), [frame.copy() for frame in frames])
    assert replay["event_id"] == saved["event_id"]
    assert db.count("events", {"validation_run_id": run["id"]}) == 1
    assert db.count("event_media", {"event_id": saved["event_id"]}) == 3

    answer = location_result(item, [saved], "REAL", state)
    assert answer["last_confirmed"]["event_id"] == saved["event_id"]
    assert answer["evidence_provenance"]["before_screenshot_sha256"]
    assert saved["id"] not in retention.preview()["event_ids"]
    media_paths = [service.media_path(row["path"]) for row in media_rows]
    removed = retention.delete_event(saved["id"], force=True)
    assert removed["deleted_events"] == 1 and removed["deleted_media"] == 3
    assert db.count("event_media", {"event_id": saved["event_id"]}) == 0
    assert all(not path.exists() for path in media_paths)


@pytest.mark.parametrize(
    ("tamper", "expected_status"),
    (("none", "verified"), ("delete", "media_missing"), ("content", "media_missing"), ("path", "media_missing")),
)
def test_real_acceptance_read_revalidates_all_three_active_media(tmp_path, tamper, expected_status):
    db, media, item, camera, first, second, suite, acceptance = acceptance_fixture(tmp_path)
    run, started = activate_run(db, acceptance, suite, item, camera, first, second)
    service = EventService(db, RuntimeMode.REAL, media, None, lambda _camera: ("opencv_camera", False), acceptance)
    frames, _before, after = acceptance_clip_frames()
    saved = service.record(vision_event_payload(run, started), after, frames)
    rows = db.list("event_media", {"event_id": saved["event_id"]}, limit=10)
    assert len(rows) == 3
    after_media = next(row for row in rows if row["role"] == "after")
    after_path = service.media_path(after_media["path"], "image")
    assert after_path and after_path.is_file()
    if tamper == "delete":
        after_path.unlink()
    elif tamper == "content":
        after_path.write_bytes(b"changed-after-evidence")
    elif tamper == "path":
        db.save("event_media", {"path": "/media/../outside.jpg"}, after_media["id"])

    runtime = object.__new__(Runtime)
    runtime.mode = RuntimeMode.REAL
    runtime.db = db
    runtime.event_service = service
    event_view = runtime.verified_search_record(saved)
    state_view = runtime.verified_search_record(db.get("item_current_state", f"REAL:{item['id']}"))

    assert event_view["evidence_integrity_status"] == expected_status
    if expected_status == "verified":
        assert event_view["evidence_status"] == "confirmed"
        assert event_view["final_status"] == "confirmed_placed"
        assert state_view["status"] == "confirmed_placed"
    else:
        assert event_view["evidence_status"] == "media_missing"
        assert event_view["final_status"] == "media_missing"
        assert state_view["evidence_status"] == "media_missing"
        assert state_view["status"] == "last_seen"


@pytest.mark.parametrize("run_change", ({"status": "FAILED"}, {"event_id": "different-event"}))
def test_real_acceptance_read_rejects_broken_pass_binding(tmp_path, run_change):
    db, media, item, camera, first, second, suite, acceptance = acceptance_fixture(tmp_path)
    run, started = activate_run(db, acceptance, suite, item, camera, first, second)
    service = EventService(db, RuntimeMode.REAL, media, None, lambda _camera: ("opencv_camera", False), acceptance)
    frames, _before, after = acceptance_clip_frames()
    saved = service.record(vision_event_payload(run, started), after, frames)
    db.save("acceptance_runs", run_change, run["id"])

    runtime = object.__new__(Runtime)
    runtime.mode = RuntimeMode.REAL
    runtime.db = db
    runtime.event_service = service
    event_view = runtime.verified_search_record(saved)
    state_view = runtime.verified_search_record(db.get("item_current_state", f"REAL:{item['id']}"))

    assert event_view["evidence_integrity_status"] == "source_unverified"
    assert event_view["evidence_status"] == "source_unverified"
    assert event_view["final_status"] == "source_unverified"
    assert state_view["status"] == "last_seen"


def test_after_frame_index_preserves_post_roll_and_rejects_wrong_binding(tmp_path):
    db, media, item, camera, first, second, suite, acceptance = acceptance_fixture(tmp_path)
    run, started = activate_run(db, acceptance, suite, item, camera, first, second)
    service = EventService(db, RuntimeMode.REAL, media, None, lambda _camera: ("opencv_camera", False), acceptance)
    frames = [np.full((32, 48, 3), value, np.uint8) for value in (1, 30, 100, 180, 250)]
    payload = vision_event_payload(run, started)
    payload["before_frame_index"] = 1
    payload["after_frame_index"] = 4
    with pytest.raises(EvidenceRejected, match="after_frame_index"):
        service.record(payload, frames[3], frames)
    assert db.count("events") == 0
    assert not list((media / "event-images").glob("*"))
    assert not list((media / "event-clips").glob("*"))


def test_acceptance_rejects_truncated_encoded_clip_and_removes_bundle(tmp_path, monkeypatch):
    db, media, item, camera, first, second, suite, acceptance = acceptance_fixture(tmp_path)
    run, started = activate_run(db, acceptance, suite, item, camera, first, second)
    service = EventService(db, RuntimeMode.REAL, media, None, lambda _camera: ("opencv_camera", False), acceptance)
    frames, _before, after = acceptance_clip_frames((32, 48, 3))
    payload = vision_event_payload(run, started)
    original_clip = EventMediaWriter.write_clip

    def truncated_clip(self, event_id, supplied, fps):
        return original_clip(self, event_id, list(supplied)[:1], fps)

    monkeypatch.setattr(EventMediaWriter, "write_clip", truncated_clip)
    with pytest.raises(EvidenceRejected, match="编码后帧数"):
        service.record(payload, after, frames)
    assert db.count("events") == 0
    assert not list((media / "event-images").glob("*"))
    assert not list((media / "event-clips").glob("*"))


@pytest.mark.parametrize("substitution", ["middle", "keyframes"])
def test_acceptance_rejects_substituted_video_content_not_just_bad_frame_count(tmp_path, monkeypatch, substitution):
    db, media, item, camera, first, second, suite, acceptance = acceptance_fixture(tmp_path)
    run, started = activate_run(db, acceptance, suite, item, camera, first, second)
    service = EventService(db, RuntimeMode.REAL, media, None, lambda _camera: ("opencv_camera", False), acceptance)
    frames, _before, after = acceptance_clip_frames()
    original_clip = EventMediaWriter.write_clip

    def replaced_clip(self, event_id, supplied, fps):
        replaced = [frame.copy() for frame in supplied]
        if substitution == "middle":
            # Preserve both endpoints and the number of encoded frames.
            replaced[30] = np.zeros_like(replaced[30])
        else:
            replaced[25] = np.full_like(replaced[25], 10)
            replaced[35] = np.full_like(replaced[35], 165)
        return original_clip(self, event_id, replaced, fps)

    monkeypatch.setattr(EventMediaWriter, "write_clip", replaced_clip)
    with pytest.raises(EvidenceRejected, match="替换|关键帧"):
        service.record(vision_event_payload(run, started), after, frames)
    assert db.count("events") == 0 and db.count("event_media") == 0
    assert acceptance.require(run["id"])["status"] != "PASSED"
    assert not list((media / "event-images").glob("*"))
    assert not list((media / "event-clips").glob("*"))


def test_small_marker_frozen_video_is_rejected_even_when_global_mae_is_small(tmp_path, monkeypatch):
    db, media, item, camera, first, second, suite, acceptance = acceptance_fixture(tmp_path)
    run, started = activate_run(db, acceptance, suite, item, camera, first, second)
    service = EventService(db, RuntimeMode.REAL, media, None, lambda _camera: ("opencv_camera", False), acceptance)
    frames, _before, after = acceptance_clip_frames((480, 640, 3))
    # The target occupies <1% of the image; make the room background constant.
    for image in frames:
        image[(image[:, :, 0] != 0) & (image[:, :, 0] != 255)] = 235
    original_clip = EventMediaWriter.write_clip
    def frozen_clip(self, event_id, supplied, fps):
        supplied = list(supplied)
        return original_clip(self, event_id, [supplied[0].copy() for _ in supplied], fps)
    monkeypatch.setattr(EventMediaWriter, "write_clip", frozen_clip)
    with pytest.raises(EvidenceRejected, match="标记位置|轨迹端点"):
        service.record(vision_event_payload(run, started), after, frames)
    assert db.count("events") == 0 and db.count("event_media") == 0
    assert not list((media / "event-images").glob("*"))
    assert not list((media / "event-clips").glob("*"))


@pytest.mark.parametrize("mutation", ["declared_zone", "endpoint", "snapshot", "scenario", "nan_position"])
def test_acceptance_recomputes_frozen_zone_geometry_and_event_contract(tmp_path, mutation):
    db, media, item, camera, first, second, suite, acceptance = acceptance_fixture(tmp_path)
    run, started = activate_run(db, acceptance, suite, item, camera, first, second)
    service = EventService(db, RuntimeMode.REAL, media, None, lambda _camera: ("opencv_camera", False), acceptance)
    frames, _before, after = acceptance_clip_frames()
    payload = vision_event_payload(run, started)
    if mutation == "declared_zone":
        # Coordinate is still in A; a string claiming B must not be trusted.
        payload["trajectory"][-1]["center_norm"] = [0.21, 0.5]
    elif mutation == "endpoint":
        payload["final_position"] = [0.2, 0.5]
    elif mutation == "snapshot":
        payload["origin_zone"] = {**payload["origin_zone"], "points": ZONE_B}
    elif mutation == "scenario":
        payload["scenario_index"] = 4
    else:
        payload["from_position"] = [float("nan"), 0.5]
    with pytest.raises(EvidenceRejected):
        service.record(payload, after, frames)
    assert db.count("events") == 0 and db.count("event_media") == 0
    assert not list((media / "event-images").glob("*"))


def test_acceptance_allows_configured_origin_baseline_averaging_jitter(tmp_path):
    db, media, item, camera, first, second, suite, acceptance = acceptance_fixture(tmp_path)
    run, started = activate_run(db, acceptance, suite, item, camera, first, second)
    service = EventService(db, RuntimeMode.REAL, media, None, lambda _camera: ("opencv_camera", False), acceptance)
    frames, _before, after = acceptance_clip_frames()
    payload = vision_event_payload(run, started)
    payload["from_position"] = [0.201, 0.5]
    assert service.record(payload, after, frames)["event_id"] == payload["event_id"]


def test_no_active_real_run_cannot_create_history(tmp_path):
    db, media, item, camera, first, second, suite, acceptance = acceptance_fixture(tmp_path)
    run, started = activate_run(db, acceptance, suite, item, camera, first, second)
    acceptance.cancel(run["id"], "test_cancel")
    service = EventService(db, RuntimeMode.REAL, media, None, lambda _camera: ("opencv_camera", False), acceptance)
    frames = [np.full((32, 48, 3), value, np.uint8) for value in (1, 30, 100, 180, 250)]
    payload = vision_event_payload(run, started)
    payload["before_frame_index"] = 1
    payload["after_frame_index"] = 3
    with pytest.raises(EvidenceRejected, match="已经结束"):
        service.record(payload, frames[3], frames)
    assert db.count("events") == 0


def test_negative_trials_are_same_zone_and_stationary_timeout_covers_five_minutes(tmp_path):
    db, _media, item, camera, first, second, suite, acceptance = acceptance_fixture(tmp_path)
    stationary = acceptance.create({
        "suite_id": suite["id"], "scenario_index": 1,
        "item_id": item["id"], "camera_id": camera["id"],
        "origin_zone_id": first["id"], "destination_zone_id": first["id"],
        "trial_kind": "stationary", "minimum_duration_seconds": 300,
    })
    assert stationary["minimum_duration_seconds"] == 300
    assert stationary["thresholds"]["max_run_seconds"] >= 301
    with pytest.raises(AcceptanceRejected, match="destination_zone_id"):
        acceptance.create({
            "suite_id": suite["id"], "scenario_index": 1,
            "item_id": item["id"], "camera_id": camera["id"],
            "origin_zone_id": first["id"], "destination_zone_id": second["id"],
            "trial_kind": "stationary",
        })
    with pytest.raises(AcceptanceRejected, match="trial_kind"):
        acceptance.create({
            "suite_id": suite["id"],
            "item_id": item["id"], "camera_id": camera["id"],
            "origin_zone_id": first["id"], "destination_zone_id": first["id"],
            "trial_kind": "movement", "scenario_index": 1,
        })
    with pytest.raises(AcceptanceRejected, match="1 到 14"):
        acceptance.create({
            "suite_id": suite["id"],
            "item_id": item["id"], "camera_id": camera["id"],
            "origin_zone_id": first["id"], "destination_zone_id": second["id"],
            "trial_kind": "movement", "scenario_index": 99,
        })


def test_acceptance_suite_enforces_order_once_only_and_terminal_predecessor(tmp_path):
    db, _media, _item, _camera, _first, _second, suite, acceptance = acceptance_fixture(tmp_path)
    with pytest.raises(AcceptanceRejected, match="套件不存在"):
        acceptance.create({"scenario_index": 1})
    with pytest.raises(AcceptanceRejected, match="下一轮应为 1"):
        acceptance.create({"suite_id": suite["id"], "scenario_index": 2})
    first_run = acceptance.create({"suite_id": suite["id"], "scenario_index": 1})
    with pytest.raises(AcceptanceRejected, match="下一轮应为 2"):
        acceptance.create({"suite_id": suite["id"], "scenario_index": 1})
    with pytest.raises(AcceptanceRejected, match="前一轮尚未结束"):
        acceptance.create({"suite_id": suite["id"], "scenario_index": 2})
    acceptance.cancel(first_run["id"], "intentional_negative_result")
    second_run = acceptance.create({"suite_id": suite["id"], "scenario_index": 2})
    acceptance.cancel(second_run["id"], "intentional_negative_result")
    with pytest.raises(AcceptanceRejected, match="下一轮应为 3"):
        acceptance.create({"suite_id": suite["id"], "scenario_index": 2})
    third_run = acceptance.create({"suite_id": suite["id"], "scenario_index": 3})
    assert third_run["origin_zone_id"] == suite["zone_a_id"]
    assert third_run["destination_zone_id"] == suite["zone_b_id"]
    acceptance.cancel(third_run["id"], "intentional_negative_result")
    fourth_run = acceptance.create({"suite_id": suite["id"], "scenario_index": 4})
    assert fourth_run["origin_zone_id"] == suite["zone_b_id"]
    assert fourth_run["destination_zone_id"] == suite["zone_a_id"]
    assert db.count("acceptance_runs", {"suite_id": suite["id"]}) == 4


def test_acceptance_suite_concurrent_duplicate_creates_exactly_one_round(tmp_path):
    db, _media, _item, _camera, _first, _second, suite, acceptance = acceptance_fixture(tmp_path)
    barrier = threading.Barrier(3)
    results = []
    result_lock = threading.Lock()

    def create_first_round():
        barrier.wait()
        try:
            acceptance.create({"suite_id": suite["id"], "scenario_index": 1})
            outcome = "created"
        except AcceptanceRejected:
            outcome = "rejected"
        with result_lock:
            results.append(outcome)

    workers = [threading.Thread(target=create_first_round) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=10)
    assert results.count("created") == 1 and results.count("rejected") == 1
    assert db.count("acceptance_runs", {"suite_id": suite["id"], "scenario_index": 1}) == 1


def test_acceptance_suite_rejects_mixed_identity_direction_and_contract(tmp_path):
    db, _media, item, camera, first, second, suite, acceptance = acceptance_fixture(tmp_path)
    other_item = db.save("items", {"name": "另一个物品", "type": "phone", "aruco_id": 49})
    other_camera = db.save("cameras", {
        "name": "另一摄像头", "room_name": "书房", "source_type": "webcam", "source": "1",
        "config": {}, "enabled": True, "inference_fps": 5, "save_clips": True, "runtime_mode": "REAL",
    })
    invalid_payloads = (
        {"item_id": other_item["id"]},
        {"camera_id": other_camera["id"]},
        {"origin_zone_id": second["id"]},
        {"destination_zone_id": second["id"]},
        {"trial_kind": "movement"},
        {"minimum_duration_seconds": 301},
    )
    for override in invalid_payloads:
        with pytest.raises(AcceptanceRejected, match="合同|固定"):
            acceptance.create({"suite_id": suite["id"], "scenario_index": 1, **override})
    assert db.count("acceptance_runs", {"suite_id": suite["id"]}) == 0

    db.save("acceptance_suites", {"contract_version": "foreign-contract-v9"}, suite["id"])
    with pytest.raises(AcceptanceRejected, match="合同版本"):
        acceptance.create({"suite_id": suite["id"], "scenario_index": 1})


def test_acceptance_suite_summary_is_server_computed_from_all_fourteen_rounds(tmp_path):
    db, _media, _item, _camera, _first, _second, suite, acceptance = acceptance_fixture(tmp_path)
    movement_passes = 0
    passed_movement_ids = []
    for scenario_index in range(1, 15):
        run = acceptance.create({"suite_id": suite["id"], "scenario_index": scenario_index})
        if run["trial_kind"] == "movement":
            passed = movement_passes < 8
            movement_passes += int(passed)
            update = {
                "status": "PASSED" if passed else "FAILED",
                "outcome": "PASSED" if passed else "FAILED",
                "event_id": f"event-{scenario_index}" if passed else None,
                "failure_reason": None if passed else "movement_not_confirmed",
            }
            if passed:
                passed_movement_ids.append(run["id"])
        else:
            update = {
                "status": "PASSED", "outcome": "PASSED", "event_id": None,
                "event_count_after": run["event_count_before"],
            }
        db.save("acceptance_runs", {**update, "ended_at": datetime.now(timezone.utc).isoformat()}, run["id"])

    summary = acceptance.suite_summary(suite["id"])
    assert summary["summary"] == {
        "contract_complete": True, "created_runs": 14, "terminal_runs": 14,
        "movement_total": 10, "movement_passed": 0, "movement_required": 8,
        "movement_evidence_retained": 0, "movement_policy_deleted": 0, "movement_evidence_invalid": 8,
        "negative_total": 4, "negative_passed": 4, "negative_required": 4,
        "next_scenario_index": None,
    }
    # Client-like PASSED flags and invented event IDs are not evidence.
    assert summary["overall_passed"] is False and summary["suite_status"] == "FAILED"

    db.save("acceptance_runs", {
        "status": "FAILED", "outcome": "FAILED", "event_id": None,
        "failure_reason": "post_audit_invalidated",
    }, passed_movement_ids[0])
    failed = acceptance.suite_summary(suite["id"])
    assert failed["summary"]["movement_passed"] == 0
    assert failed["overall_passed"] is False and failed["suite_status"] == "FAILED"


def test_preflight_rejects_frozen_frame_and_requires_server_counter_advancement(tmp_path):
    db, _media, _item, camera, _first, _second, _suite, acceptance = acceptance_fixture(tmp_path)
    stale = datetime.now(timezone.utc) - timedelta(seconds=30)
    db.save("source_sessions", {
        "camera_id": camera["id"], "runtime_mode": "REAL", "source_type": "opencv_camera",
        "is_simulated": False, "started_at": (stale - timedelta(seconds=10)).isoformat(),
        "first_frame": 1, "last_frame": 9, "last_frame_at": stale.isoformat(),
        "status": "streaming", "continuity_ok": True,
    }, "freshness-session")
    health = {
        "status": "ready", "source_session_id": "freshness-session",
        "source_frame_sequence": 9, "reconnect_epoch": 0, "fps": 5,
    }
    frozen = acceptance.preflight(camera["id"], health)
    assert frozen["ready"] is False
    assert frozen["checks"]["fresh_frame"] is False
    assert frozen["checks"]["frame_sequence_advancing"] is False
    advancing = acceptance.preflight(camera["id"], {**health, "source_frame_sequence": 10})
    assert advancing["ready"] is True
    assert advancing["checks"]["fresh_frame"] is True
    assert advancing["checks"]["frame_sequence_advancing"] is True


def test_controlled_reconnect_first_frame_cannot_pass_before_stable_baseline(tmp_path):
    db, _media, item, camera, first, second, suite, acceptance = acceptance_fixture(tmp_path)
    run, _started = activate_run(db, acceptance, suite, item, camera, first, second, trial_kind="disconnect_reconnect")
    old_session = run["source_session_id"]
    acceptance.begin_disconnect_reconnect(run["id"])
    new_session = "session-after-controlled-reconnect"
    stamp = datetime.now(timezone.utc)
    db.save("source_sessions", {
        "camera_id": camera["id"], "runtime_mode": "REAL", "source_type": "opencv_camera",
        "is_simulated": False, "started_at": stamp.isoformat(), "first_frame": 1, "last_frame": 30,
        "last_frame_at": (stamp + timedelta(seconds=5)).isoformat(), "status": "streaming", "continuity_ok": True,
    }, new_session)
    run = acceptance.rebind_after_reconnect(run["id"], {
        "ready": True, "camera_id": camera["id"], "source_session_id": new_session, "reconnect_epoch": 0,
    })
    assert old_session != run["source_session_id"] and run["user_executed"] is False
    acceptance.observe_track({
        "camera_id": camera["id"], "item_id": item["id"], "runtime_mode": "REAL",
        "source_type": "opencv_camera", "is_simulated": False, "source_session_id": new_session,
        "reconnect_epoch": 0, "detector_backend": "aruco", "source_frame": 1,
        "source_timestamp": stamp.isoformat(), "center": [0.2, 0.5], "confidence": 0.99,
    })
    incomplete = {
        "validation_run_id": run["id"], "item_id": item["id"], "camera_id": camera["id"],
        "source_session_id": new_session, "reconnect_epoch": 0, "state": "STABLE_AT_ORIGIN",
        "marker_status": "DETECTED", "detected_frames": 1, "event_count": 0, "terminal": False,
        "current_zone": {"id": first["id"], "name": first["name"]}, "observed_duration_seconds": 0,
        "baseline": None, "calibration_snapshot": None,
    }
    assert acceptance.sync_engine_status(run["id"], incomplete)["status"] == "ACTIVE"
    stable = {
        **incomplete, "marker_status": "STABLE_AT_ORIGIN", "detected_frames": 10,
        "baseline": {"frame_sequence": 10, "stable_duration_seconds": 3.0},
        "calibration_snapshot": {"sample_count": 10},
    }
    completed = acceptance.sync_engine_status(run["id"], stable)
    assert completed["status"] == "PASSED"
    assert completed["event_id"] is None and db.count("events") == 0


def test_acceptance_http_control_plane_has_no_client_completion_endpoint(tmp_path):
    app = create_app(tmp_path / "api", testing=False, runtime_mode="REAL")
    with TestClient(app) as client:
        client.cookies.set("om_session", app.state.runtime.sessions.issue())
        assert client.get("/api/session").json()["authenticated"] is True
        runtime = app.state.runtime
        item = runtime.db.save("items", {"name": "手机", "type": "phone", "aruco_id": 7})
        camera = runtime.db.save("cameras", {
            "name": "电脑摄像头", "room_name": "客厅", "source_type": "webcam", "source": "0",
            "config": {}, "enabled": True, "inference_fps": 5, "save_clips": True, "runtime_mode": "REAL",
        })
        first = runtime.db.save("zones", {"camera_id": camera["id"], "name": "桌面", "points": ZONE_A, "priority": 1, "enabled": True})
        second = runtime.db.save("zones", {"camera_id": camera["id"], "name": "沙发", "points": ZONE_B, "priority": 1, "enabled": True})
        marker = client.get("/api/acceptance/markers/7.png")
        assert marker.status_code == 200 and marker.headers["content-type"] == "image/png"
        created_suite = client.post("/api/acceptance/suites", json={
            "item_id": item["id"], "camera_id": camera["id"],
            "zone_a_id": first["id"], "zone_b_id": second["id"],
        })
        assert created_suite.status_code == 200, created_suite.text
        suite = created_suite.json()
        assert suite["contract_version"] == "p0-real-movement-v1"
        assert len(suite["contract"]) == 14 and suite["overall_passed"] is False
        assert client.get(f"/api/acceptance/suites/{suite['id']}").json()["summary"]["next_scenario_index"] == 1
        assert client.get("/api/acceptance/suites").json()[0]["id"] == suite["id"]
        assert client.post("/api/acceptance/suites", json={
            "item_id": item["id"], "camera_id": camera["id"],
            "zone_a_id": first["id"], "zone_b_id": second["id"],
            "overall_passed": True,
        }).status_code == 409
        created = client.post("/api/acceptance/runs", json={
            "suite_id": suite["id"],
            "item_id": item["id"], "camera_id": camera["id"],
            "origin_zone_id": first["id"], "destination_zone_id": first["id"],
            "trial_kind": "stationary", "scenario_index": 1,
            "minimum_duration_seconds": 300,
        })
        assert created.status_code == 200, created.text
        run = created.json()
        assert run["user_executed"] is False and run["status"] == "CREATED"
        listed = client.get("/api/acceptance/runs", params={"validation_run_id": run["id"]}).json()[0]
        assert listed["id"] == run["id"]
        assert listed["source_type"] == "opencv_camera"
        assert listed["response_source_type"] == "physical_acceptance_control"
        preflight = client.get("/api/acceptance/preflight", params={"camera_id": camera["id"]}).json()
        assert preflight["ready"] is False
        assert preflight["source_type"] == "opencv_camera"
        assert preflight["response_source_type"] == "physical_acceptance_preflight"
        assert client.post(f"/api/acceptance/runs/{run['id']}/complete", json={"outcome": "PASSED"}).status_code in {404, 405}
        assert client.post("/api/acceptance/runs", json={
            "suite_id": suite["id"],
            "item_id": item["id"], "camera_id": camera["id"],
            "origin_zone_id": first["id"], "destination_zone_id": first["id"],
            "trial_kind": "stationary", "scenario_index": 1, "outcome": "PASSED",
        }).status_code == 409
