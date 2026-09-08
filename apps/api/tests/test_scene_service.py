"""Isolated scene contracts; generated images do not establish model accuracy.

Unit suggestion fixtures below exercise admission only. The separately named
real NanoDet test loads the actual ONNX and performs inference without a camera.
"""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import time

import cv2
import numpy as np
import pytest

from apps.api.app.db import Database
from apps.api.app.scene import SceneError, SceneService
from services.vision.detectors.base import Detection
from services.vision.geometry import FrameGeometry


ROOT = Path(__file__).resolve().parents[3]
POLYGON = [[.1, .2], [.7, .2], [.7, .8], [.1, .8]]


class SuggestionFixture:
    """Explicit unit-only controlled detections, not real furniture claims."""
    def __init__(self):
        self.calls = 0
        self.frames = []

    def health(self):
        return {"available": True}

    def detect(self, frame):
        self.calls += 1
        self.frames.append(frame.copy())
        return [Detection(identity=f"fixture:{label}", label=label, bbox=(10, 20, 40, 50),
            center=(30, 45), confidence=.8, detection_mode="experimental")
            for label in ("couch", "dining table", "bed", "person", "cell phone")]


@pytest.fixture
def context(tmp_path):
    db = Database(tmp_path / "database" / "objectmemory-test.sqlite", "TEST")
    camera = db.save("cameras", {"name": "unopened fixture", "source_type": "video", "source": "never-opened.avi", "config": {}, "enabled": False})
    service = SceneService(db, tmp_path / "test-media", ROOT / "data" / "models")
    service._detector = SuggestionFixture()
    return service, db, camera


def packet(sequence=1, **changes):
    value = {"runtime_mode": "TEST", "source_type": "video_file", "is_simulated": True,
        "source_session_id": "generated-scene-session", "source_frame": sequence,
        "source_timestamp": datetime.now(timezone.utc).isoformat(), "created_at": time.monotonic(),
        "width": 160, "height": 120, "geometry": FrameGeometry(160, 120).to_dict()}
    return {**value, **changes}


def jpeg(value=80):
    ok, data = cv2.imencode(".jpg", np.full((120, 160, 3), value, np.uint8))
    assert ok
    return data.tobytes()


def body(scene, **surface_changes):
    return {key: scene[key] for key in ("scene_version", "snapshot_id", "source_session_id", "source_frame")} | {
        "surfaces": [{"name": "user confirmed desk", "surface_type": "table", "image_polygon": POLYGON,
                      "confirmed_by_user": True, **surface_changes}]}


def propose(context, sequence=1, **changes):
    service, _db, camera = context
    service._last_proposal.clear()
    return service.propose(camera, packet(sequence, **changes), jpeg())


def test_auto_scene_draft_only_fills_empty_database_once(context):
    service, db, camera = context
    scene = service.propose(camera, packet(), jpeg(), only_if_empty=True)
    detector_calls = service._detector.calls
    files = sorted(str(p) for p in service.folder.rglob('*'))
    with pytest.raises(SceneError, match='不会覆盖'):
        service.propose(camera, packet(2), jpeg(130), only_if_empty=True)
    assert service.get(camera['id']) == scene
    assert service._detector.calls == detector_calls
    assert db.count('scenes') == 1
    assert sorted(str(p) for p in service.folder.rglob('*')) == files


def test_proposal_is_source_bound_actual_jpeg_not_surface_or_event(context):
    service, db, camera = context
    raw = jpeg(110)
    scene = service.propose(camera, packet(), raw)
    assert scene["runtime_mode"] == "TEST" and scene["is_simulated"] is True
    assert scene["source_type"] == "video_file" and scene["source_frame"] == 1
    assert scene["calibration_status"] == "needs_confirmation" and scene["surfaces"] == []
    assert {value["surface_type"] for value in scene["proposals"]} == {"table", "sofa", "bed"}
    assert all(value["confirmed_by_user"] is False for value in scene["proposals"])
    path = service.folder / scene["screenshot_path"].split("/")[-1]
    assert path.read_bytes() == raw
    assert scene["screenshot_sha256"] == hashlib.sha256(raw).hexdigest()
    assert np.array_equal(service._detector.frames[0], cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR))
    assert db.count("zones") == db.count("items") == db.count("movement_events") == 0
    assert "jpeg" not in scene and "image_data" not in scene


def test_save_confirmed_surfaces_syncs_owned_zones_only(context):
    service, db, camera = context
    unrelated = db.save("zones", {"camera_id": camera["id"], "name": "existing user zone", "points": POLYGON, "enabled": True})
    scene = propose(context)
    saved = service.save(camera, body(scene, proposal_id=scene["proposals"][0]["proposal_id"], height_optional=80))
    surface = saved["surfaces"][0]
    zone = db.get("zones", surface["surface_id"])
    assert surface["source"] == "model_proposal"
    assert surface["map_polygon"] == surface["image_polygon"] == POLYGON
    assert surface["height_unit"] == "user_annotation" and surface["height_optional"] == 80
    assert zone["scene_metadata"]["confirmed_by_user"] is True
    assert zone["scene_metadata"]["calibration_status"] == "schematic"
    assert zone["scene_metadata"]["source_session_id"] == scene["source_session_id"]
    empty = body(saved)
    empty["surfaces"] = []
    service.save(camera, empty)
    assert db.get("zones", surface["surface_id"]) is None
    assert db.get("zones", unrelated["id"]) == unrelated
    assert db.count("movement_events") == db.count("items") == 0


@pytest.mark.parametrize("changes", [
    {"confirmed_by_user": False}, {"surface_id": "unowned-zone"}, {"image_polygon": [[0, 0], [1, 1], [0, 1], [1, 0]]},
    {"image_polygon": [[0, 0], [.000001, 0], [0, .000001]]}, {"image_polygon": [[0, 0], [float("nan"), 1], [1, 0]]},
    {"image_polygon": [[0, 0], [True, 1], [1, 0]]}, {"map_polygon": [[0, 0], [1, 0], [1, 1]]},
    {"height_optional": float("inf")}, {"surface_type": "freeform-measured"}, {"proposal_id": "other-snapshot"},
])
def test_invalid_surface_never_partially_writes(context, changes):
    service, db, camera = context
    scene = propose(context)
    with pytest.raises(SceneError):
        service.save(camera, body(scene, **changes))
    assert service.get(camera["id"]) == scene
    assert db.count("zones") == db.count("movement_events") == 0


def test_existing_manual_zone_id_cannot_be_hijacked(context):
    service, db, camera = context
    existing = db.save("zones", {"camera_id": camera["id"], "name": "do not replace", "points": POLYGON, "enabled": True})
    scene = propose(context)
    with pytest.raises(SceneError):
        service.save(camera, body(scene, surface_id=existing["id"]))
    assert db.get("zones", existing["id"]) == existing


def test_refresh_has_one_keyframe_and_disables_previous_surface(context):
    service, db, camera = context
    initial = propose(context)
    confirmed = service.save(camera, body(initial))
    refreshed = propose(context, 2)
    assert refreshed["scene_version"] == confirmed["scene_version"] + 1
    assert len(list(service.folder.glob("*.jpg"))) == 1
    assert not (service.folder / initial["screenshot_path"].split("/")[-1]).exists()
    assert refreshed["surfaces"][0]["confirmed_by_user"] is False
    assert db.get("zones", confirmed["surfaces"][0]["surface_id"])["enabled"] is False
    with pytest.raises(SceneError) as caught:
        service.save(camera, body(confirmed))
    assert caught.value.status_code == 409


def test_scene_save_uses_version_and_current_snapshot_lock(context):
    service, db, camera = context
    scene = propose(context)
    saved = service.save(camera, body(scene))
    for patch in ({"source_frame": 99}, {"snapshot_id": "old"}, {"source_session_id": "old"}, {"scene_version": 0}):
        with pytest.raises(SceneError) as caught:
            service.save(camera, body(saved) | patch)
        assert caught.value.status_code == 409
    assert service.get(camera["id"]) == saved
    assert db.count("zones") == 1


def test_camera_config_change_and_invalidation_require_fresh_snapshot(context):
    service, db, camera = context
    scene = service.save(camera, body(propose(context)))
    changed = db.save("cameras", {"config": {"mirror": True}}, camera["id"])
    with pytest.raises(SceneError):
        service.save(changed, body(scene))
    invalid = service.invalidate(camera["id"], "geometry changed")
    assert invalid["calibration_status"] == "needs_review"
    assert db.get("zones", scene["surfaces"][0]["surface_id"])["enabled"] is False
    with pytest.raises(SceneError):
        service.save(changed, body(invalid))
    assert db.count("movement_events") == 0


def test_repeated_proposal_is_rate_limited_not_returned_as_new_frame(context):
    service, _db, camera = context
    first = propose(context)
    with pytest.raises(SceneError) as caught:
        service.propose(camera, packet(2), jpeg())
    assert caught.value.status_code == 429 and service._detector.calls == 1
    assert service.get(camera["id"]) == first
    service._last_proposal.clear()
    with pytest.raises(SceneError) as caught:
        service.propose(camera, packet(1), jpeg())
    assert caught.value.status_code == 409


@pytest.mark.parametrize("changes", [
    {"source_type": "opencv_camera"}, {"is_simulated": False}, {"runtime_mode": "REAL"},
    {"source_session_id": ""}, {"source_frame": -1}, {"source_frame": True},
    {"created_at": -1}, {"width": 159}, {"geometry": FrameGeometry(120, 160).to_dict()},
    {"source_timestamp": "2026-09-05T12:00:00"},
    {"camera_id": "unrelated-camera"},
])
def test_unbound_or_stale_frame_fails_without_writes(context, changes):
    service, db, camera = context
    with pytest.raises(SceneError):
        service.propose(camera, packet(**changes), jpeg())
    assert db.count("scenes") == 0 and service._detector.calls == 0
    assert not list(service.folder.iterdir())


def test_client_cannot_relabel_scene_and_tampered_keyframe_blocks_save(context):
    service, db, camera = context
    scene = propose(context)
    with pytest.raises(SceneError):
        service.save(camera, body(scene) | {"runtime_mode": "REAL", "is_simulated": False})
    (service.folder / scene["screenshot_path"].split("/")[-1]).write_bytes(jpeg(180))
    with pytest.raises(SceneError) as caught:
        service.save(camera, body(scene))
    assert caught.value.status_code == 409 and db.count("zones") == 0


def test_path_and_mode_guards_reject_traversal(context, tmp_path):
    service, db, _camera = context
    for name in ("../outside.jpg", "..\\outside.jpg", "C:\\outside.jpg", "/absolute.jpg"):
        with pytest.raises(SceneError):
            service._safe_path(name)
    with pytest.raises(SceneError):
        SceneService(db, tmp_path / "real-media", tmp_path)
    other_mode = Database(db.path, "REAL")
    propose(context)
    assert other_mode.count("scenes") == 0


def test_real_mode_rejects_video_source_before_inference(tmp_path):
    db = Database(tmp_path / "database" / "objectmemory.sqlite", "REAL")
    camera = db.save("cameras", {"source_type": "video", "source": "never-opened.avi", "config": {}})
    service = SceneService(db, tmp_path / "real-media", tmp_path)
    with pytest.raises(SceneError):
        service.propose(camera, packet(runtime_mode="REAL"), jpeg())
    assert db.count("scenes", unscoped=True) == 0


def test_restart_restores_scene_hash_and_confirmed_zones(context):
    service, db, camera = context
    scene = service.save(camera, body(propose(context)))
    restarted = SceneService(Database(db.path, "TEST"), service.media, service.model_root)
    assert restarted.get(camera["id"]) == scene
    assert restarted.db.get("zones", scene["surfaces"][0]["surface_id"])["enabled"] is True
    assert restarted._detector is None


def test_actual_nanodet_generated_jpeg_flow_without_mock_or_camera(context):
    service, db, camera = context
    model = ROOT / "data" / "models" / "object_detection_nanodet_2022nov.onnx"
    if not model.is_file():
        pytest.skip("NOT_RUN: actual NanoDet model is absent; no mock substitute")
    service._detector = None
    scene = service.propose(camera, packet(), jpeg())
    assert service._detector.health()["available"] is True
    assert scene["proposal_error"] is None
    assert scene["surfaces"] == [] and scene["calibration_status"] == "needs_confirmation"
    assert db.count("movement_events") == db.count("items") == db.count("zones") == 0
    assert scene["is_simulated"] is True


def test_transaction_failure_removes_only_new_scene_image(context, monkeypatch):
    service, _db, camera = context
    first = propose(context)
    old_path = service.folder / first["screenshot_path"].split("/")[-1]
    def fail_commit(*args, **kwargs):
        raise RuntimeError("controlled database failure")
    monkeypatch.setattr(service, "_persist", fail_commit)
    with pytest.raises(RuntimeError):
        propose(context, 2)
    assert service.get(camera["id"]) == first
    assert old_path.is_file() and len(list(service.folder.iterdir())) == 1


def test_scene_and_zone_changes_roll_back_together(context):
    service, db, camera = context
    scene = propose(context)
    with db.connect() as conn:
        conn.execute("CREATE TRIGGER fixture_block_zone BEFORE INSERT ON zones BEGIN SELECT RAISE(ABORT, 'controlled zone failure'); END")
    with pytest.raises(Exception, match="controlled zone failure"):
        service.save(camera, body(scene))
    assert service.get(camera["id"]) == scene
    assert db.count("zones") == 0


def test_media_link_cannot_delete_or_write_through_external_target(context, tmp_path):
    service, _db, camera = context
    target = tmp_path / "unrelated-fixture"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("fixture target remains untouched")
    # Only this empty, test-owned directory entry is replaced. Windows supports
    # junction creation without symbolic-link privilege; unlink only in finally.
    service.folder.rmdir()
    if os.name == "nt":
        import subprocess
        subprocess.run(["cmd", "/c", "mklink", "/J", str(service.folder), str(target)], check=True, capture_output=True)
    else:
        service.folder.symlink_to(target, target_is_directory=True)
    try:
        with pytest.raises(SceneError):
            service.propose(camera, packet(), jpeg())
        assert sentinel.read_text() == "fixture target remains untouched"
        assert list(target.iterdir()) == [sentinel]
    finally:
        if os.name == "nt":
            service.folder.rmdir()  # junction entry only; never recursive
        else:
            service.folder.unlink()
        service.folder.mkdir()


def test_clear_test_removes_scene_rows_but_keeps_original_reference_bytes(context, tmp_path):
    from apps.api.app.retention import RetentionService
    from apps.api.app.mode_lock import RuntimeModeLease
    service, db, _camera = context
    propose(context)
    originals = tmp_path / "registered-items"
    originals.mkdir()
    reference = originals / "user-original.png"
    reference.write_bytes(b"user reference fixture")
    lease = RuntimeModeLease(tmp_path, "TEST")
    lease.acquire()
    try:
        RetentionService.clear_isolated_mode(tmp_path, "TEST", lease=lease)
        assert db.count("scenes") == 0
        assert reference.read_bytes() == b"user reference fixture"
        assert not list(service.folder.glob("*.jpg"))
    finally:
        lease.close()


def test_delete_removes_only_scene_owned_image_rows_and_zones(context):
    service, db, camera = context
    scene = service.save(camera, body(propose(context)))
    other = db.save("zones", {"camera_id": camera["id"], "name": "keep user zone", "points": POLYGON})
    user_file = service.folder / "unknown-user-image.jpg"
    user_file.write_bytes(jpeg(180))
    result = service.delete(camera["id"])
    assert result == {"deleted_scenes": 1, "deleted_images": 1, "pending_delete": False}
    assert service.get(camera["id"]) is None
    assert db.get("zones", scene["surfaces"][0]["surface_id"]) is None
    assert db.get("zones", other["id"]) == other
    assert user_file.is_file()
    assert service.delete(camera["id"])["deleted_scenes"] == 0


def test_failed_file_delete_stays_pending_and_restart_finishes(context, monkeypatch):
    service, db, camera = context
    scene = service.save(camera, body(propose(context)))
    path = service.folder / scene["screenshot_path"].split("/")[-1]
    original_unlink = Path.unlink
    def deny_target(value, *args, **kwargs):
        if value == path:
            raise PermissionError("controlled locked fixture")
        return original_unlink(value, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", deny_target)
    result = service.delete(camera["id"])
    assert result["pending_delete"] is True and path.exists()
    assert service.get(camera["id"])["calibration_status"] == "pending_delete"
    assert db.get("zones", scene["surfaces"][0]["surface_id"])["enabled"] is False
    with pytest.raises(SceneError):
        service.save(camera, body(service.get(camera["id"])))
    monkeypatch.setattr(Path, "unlink", original_unlink)
    restarted = SceneService(db, service.media, service.model_root)
    assert restarted.pending_cleanup == [] and restarted.get(camera["id"]) is None
    assert not path.exists()


def test_malicious_database_scene_path_stays_pending_without_external_delete(context, tmp_path):
    service, db, camera = context
    scene = propose(context)
    outside = tmp_path / "keep-user-file.jpg"
    outside.write_bytes(jpeg(210))
    db.save("scenes", {"screenshot_path": "/media/scene-images/../../keep-user-file.jpg"}, scene["id"])
    result = service.delete(camera["id"])
    assert result["pending_delete"] is True and outside.exists()
    assert service.get(camera["id"])["calibration_status"] == "pending_delete"


def test_missing_image_and_missing_camera_are_safe_deletion_retries(context):
    service, db, camera = context
    scene = propose(context)
    (service.folder / scene["screenshot_path"].split("/")[-1]).unlink()
    db.delete("cameras", camera["id"])
    assert service.delete(camera["id"]) == {"deleted_scenes": 1, "deleted_images": 0, "pending_delete": False}
