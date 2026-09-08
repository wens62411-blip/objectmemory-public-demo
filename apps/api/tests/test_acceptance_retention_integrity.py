"""Synthetic repository-integrity tests; these are not physical camera passes."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
import pytest

from apps.api.app.acceptance import AcceptanceRejected, AcceptanceService
from apps.api.app.db import Database
from apps.api.app.retention import RetentionService
from apps.api.app.runtime_mode import RuntimeMode


@pytest.fixture
def evidence_store(tmp_path):
    data = tmp_path / "data"
    media = data / "real-media"
    db = Database(data / "database" / "objectmemory.sqlite", "REAL")
    item = db.save("items", {"name": "integrity fixture", "aruco_id": 0})
    camera = db.save("cameras", {"name": "fixture", "source_type": "webcam", "source": "0", "config": {}, "enabled": True, "inference_fps": 5, "save_clips": True})
    zones = [db.save("zones", {"name": name, "camera_id": camera["id"], "enabled": True, "points": points}) for name, points in (
        ("A", [[0, 0], [0.3, 0], [0.3, 1], [0, 1]]),
        ("B", [[0.7, 0], [1, 0], [1, 1], [0.7, 1]]),
    )]
    acceptance = AcceptanceService(db, RuntimeMode.REAL, lambda _: ("opencv_camera", False), media)
    suite = acceptance.create_suite({"item_id": item["id"], "camera_id": camera["id"], "zone_a_id": zones[0]["id"], "zone_b_id": zones[1]["id"]})
    retention = RetentionService(db, data, media)
    retention.manage_project_firmware = False
    return db, media, item, camera, acceptance, suite, retention


def completed_movement(store, scenario_index):
    db, media, item, camera, acceptance, suite, _retention = store
    for previous in range(1, scenario_index):
        if not db.list("acceptance_runs", {"suite_id": suite["id"], "scenario_index": previous}):
            run = acceptance.create({"suite_id": suite["id"], "scenario_index": previous})
            db.save("acceptance_runs", {"status": "FAILED", "outcome": "FAILED"}, run["id"])
    run = acceptance.create({"suite_id": suite["id"], "scenario_index": scenario_index})
    session_id = f"fixture-session-{scenario_index}"
    stamp = (datetime.now(timezone.utc) + timedelta(seconds=scenario_index)).isoformat()
    db.save("source_sessions", {"camera_id": camera["id"], "source_type": "opencv_camera", "is_simulated": False}, session_id)
    event_id = f"fixture-event-{scenario_index}"
    from_x, to_x = (0.15, 0.85) if run["origin_zone_id"] == suite["zone_a_id"] else (0.85, 0.15)
    marker = cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), 0, 56)
    detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50))

    def marker_frame(center_x):
        frame = np.full((180, 320, 3), 255, np.uint8)
        left, top = int(round(center_x * 320)) - 28, 90 - 28
        frame[top:top + 56, left:left + 56] = cv2.cvtColor(marker, cv2.COLOR_GRAY2BGR)
        _corners, ids, _rejected = detector.detectMarkers(frame)
        assert ids is not None and ids.flatten().tolist() == [0]
        return frame

    clip_frames = [marker_frame(float(x)) for x in np.linspace(from_x, to_x, 5)]
    event = {
        "event_id": event_id, "validation_run_id": run["id"], "item_id": item["id"], "camera_id": camera["id"],
        "event_type": "movement", "source_type": "opencv_camera", "is_simulated": False,
        "source_session_id": session_id, "evidence_status": "confirmed", "final_status": "confirmed_placed",
        "detection_mode": "aruco_screen_validation", "timestamp_end": stamp,
        "from_zone_id": run["origin_zone_id"], "to_zone_id": run["destination_zone_id"],
        "aruco_id": 0, "from_position": [from_x, 0.5], "to_position": [to_x, 0.5], "final_position": [to_x, 0.5],
    }
    files = {}
    for role in ("before", "after", "clip"):
        kind = "clip" if role == "clip" else "image"
        folder = "event-clips" if role == "clip" else "event-images"
        suffix = "avi" if role == "clip" else "jpg"
        path = media / folder / f"{event_id}-{role}.{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        frame = clip_frames[0] if role == "before" else clip_frames[-1]
        if role == "clip":
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 5, (320, 180))
            assert writer.isOpened()
            for clip_frame in clip_frames:
                writer.write(clip_frame)
            writer.release()
            capture = cv2.VideoCapture(str(path))
            decoded_centers = []
            try:
                while True:
                    ok, decoded = capture.read()
                    if not ok:
                        break
                    corners, ids, _rejected = detector.detectMarkers(decoded)
                    assert ids is not None and ids.flatten().tolist() == [0]
                    decoded_centers.append(float(corners[0][0][:, 0].mean()) / 320)
            finally:
                capture.release()
            assert len(decoded_centers) == len(clip_frames)
            assert decoded_centers[0] == pytest.approx(from_x, abs=0.01)
            assert decoded_centers[-1] == pytest.approx(to_x, abs=0.01)
        else:
            encoded, encoded_bytes = cv2.imencode(".jpg", frame)
            assert encoded
            path.write_bytes(encoded_bytes.tobytes())
            decoded = cv2.imdecode(encoded_bytes, cv2.IMREAD_COLOR)
            _corners, ids, _rejected = detector.detectMarkers(decoded)
            assert ids is not None and ids.flatten().tolist() == [0]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        url = f"/media/{folder}/{path.name}"
        event[f"{role}_screenshot" if role != "clip" else "clip_path"] = url
        event[f"{role}_screenshot_sha256" if role != "clip" else "clip_sha256"] = digest
        db.save("event_media", {"event_id": event_id, "source_session_id": session_id, "path": url, "sha256": digest, "kind": kind, "role": role, "status": "active"})
        files[role] = path
    db.save("events", event, event_id)
    db.save("acceptance_runs", {"status": "PASSED", "outcome": "PASSED", "user_executed": True, "source_session_id": session_id, "event_id": event_id}, run["id"])
    db.save("item_current_state", {"item_id": item["id"], "evidence_event_id": event_id}, item["id"])
    return run, event_id, files


@pytest.mark.parametrize("damage", ["missing_event", "missing_image", "tampered_image", "missing_session", "malicious_path"])
def test_summary_does_not_score_stale_passed_without_current_evidence(evidence_store, damage):
    db, _media, _item, _camera, acceptance, suite, _retention = evidence_store
    run, event_id, files = completed_movement(evidence_store, 3)
    assert acceptance.suite_summary(suite["id"])["summary"]["movement_passed"] == 1
    if damage == "missing_event":
        db.delete("events", event_id)
    elif damage == "missing_image":
        files["before"].unlink()
    elif damage == "tampered_image":
        files["after"].write_bytes(b"tampered")
    elif damage == "missing_session":
        db.delete("source_sessions", "fixture-session-3")
    else:
        db.save("events", {"before_screenshot": "/media/event-images/../../outside.jpg"}, event_id)
    summary = acceptance.suite_summary(suite["id"])
    assert summary["summary"]["movement_passed"] == 0
    assert summary["runs"][-1]["evidence_integrity_status"] == "missing_or_tampered"
    assert summary["runs"][-1]["score_eligible"] is False
    assert db.get("acceptance_runs", run["id"])["status"] == "PASSED"


def test_minimal_four_movements_keep_two_and_auditable_deleted_history(evidence_store):
    db, _media, _item, _camera, acceptance, suite, retention = evidence_store
    generated = [completed_movement(evidence_store, index) for index in range(3, 7)]
    report = retention.cleanup(trigger="test")
    assert report["deleted_events"] == 2
    assert len(db.list("events")) == 2
    assert len(db.list("event_media")) == 6
    assert len(db.list("acceptance_retention_receipts", {"status": "completed"})) == 2
    summary = acceptance.suite_summary(suite["id"])
    assert summary["summary"]["movement_passed"] == 4
    assert summary["summary"]["movement_policy_deleted"] == 2
    assert summary["summary"]["movement_evidence_retained"] == 2
    for run in summary["runs"]:
        if run["scenario_index"] in {3, 4}:
            assert run["evidence_retained"] is False and run["retained_by_policy"] is False
            assert run["evidence_integrity_status"] == "policy_deleted"
    assert all(not path.exists() for _run, _event_id, files in generated[:2] for path in files.values())
    assert all(path.exists() for _run, _event_id, files in generated[2:] for path in files.values())
    # Removing the last retained event by a different route has no receipt.
    db.delete("events", generated[-1][1])
    assert acceptance.suite_summary(suite["id"])["summary"]["movement_passed"] == 3


def test_corrupted_evidence_before_retention_never_gets_a_valid_receipt(evidence_store):
    db, _media, _item, _camera, acceptance, suite, retention = evidence_store
    generated = [completed_movement(evidence_store, index) for index in range(3, 7)]
    generated[0][2]["before"].write_bytes(b"changed before cleanup")
    retention.cleanup(trigger="test")
    assert len(db.list("events")) == 2
    assert len(db.list("acceptance_retention_receipts")) == 1
    assert acceptance.suite_summary(suite["id"])["summary"]["movement_passed"] == 3


def test_pending_or_modified_retention_receipt_never_scores(evidence_store):
    db, _media, _item, _camera, acceptance, suite, retention = evidence_store
    for index in range(3, 6):
        completed_movement(evidence_store, index)
    retention.cleanup(trigger="test")
    receipt = db.list("acceptance_retention_receipts")[0]
    assert acceptance.suite_summary(suite["id"])["summary"]["movement_passed"] == 3
    db.save("acceptance_retention_receipts", {"status": "pending_delete"}, receipt["id"])
    assert acceptance.suite_summary(suite["id"])["summary"]["movement_passed"] == 2
    db.save("acceptance_retention_receipts", {"status": "completed", "evidence_sha256": "0" * 64}, receipt["id"])
    assert acceptance.suite_summary(suite["id"])["summary"]["movement_passed"] == 2


def test_interrupted_policy_delete_remains_unscored_until_recovered(evidence_store, monkeypatch):
    db, _media, _item, _camera, acceptance, suite, retention = evidence_store
    for index in range(3, 6):
        completed_movement(evidence_store, index)
    original_delete_many = db.delete_many

    def interrupt(*_args, **_kwargs):
        raise RuntimeError("controlled crash after media deletion")

    monkeypatch.setattr(db, "delete_many", interrupt)
    with pytest.raises(RuntimeError, match="controlled crash"):
        retention.cleanup(trigger="test")
    assert db.list("acceptance_retention_receipts")[0]["status"] == "pending_delete"
    assert acceptance.suite_summary(suite["id"])["summary"]["movement_passed"] == 2
    monkeypatch.setattr(db, "delete_many", original_delete_many)
    retention.recover_pending()
    retention.cleanup(trigger="recovery-test")
    assert len(db.list("events")) == 2
    assert db.list("acceptance_retention_receipts")[0]["status"] == "completed"
    assert acceptance.suite_summary(suite["id"])["summary"]["movement_passed"] == 3


def test_suite_rejects_changed_camera_source_before_next_round(evidence_store):
    db, _media, _item, camera, acceptance, suite, _retention = evidence_store
    assert "camera_config_sha256" in suite and "camera_source_sha256" in suite
    db.save("cameras", {"source": "1"}, camera["id"])
    with pytest.raises(AcceptanceRejected, match="来源或配置已改变"):
        acceptance.create({"suite_id": suite["id"], "scenario_index": 1})


def test_suite_rejects_changed_camera_configuration_after_round_created(evidence_store):
    db, _media, _item, camera, acceptance, suite, _retention = evidence_store
    run = acceptance.create({"suite_id": suite["id"], "scenario_index": 1})
    db.save("cameras", {"config": {"device_password": "fixture-secret"}}, camera["id"])
    with pytest.raises(AcceptanceRejected, match="来源或配置已改变"):
        acceptance.activate(run["id"], {"ready": True, "camera_id": camera["id"], "source_session_id": "session"})
    assert "fixture-secret" not in str(db.get("acceptance_suites", suite["id"]))


def test_active_run_is_interrupted_when_camera_configuration_drifts(evidence_store):
    db, _media, _item, camera, acceptance, suite, _retention = evidence_store
    run = acceptance.create({"suite_id": suite["id"], "scenario_index": 1})
    acceptance.activate(run["id"], {"ready": True, "camera_id": camera["id"], "source_session_id": "fixture-session"})
    db.save("cameras", {"source": "1"}, camera["id"])
    result = acceptance.sync_engine_status(run["id"], None)
    assert result["status"] == "CAMERA_INTERRUPTED"
    assert result["failure_reason"] == "camera_configuration_changed"
