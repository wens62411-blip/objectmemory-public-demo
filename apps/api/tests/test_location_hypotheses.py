"""Adversarial inference contracts using explicit generated TEST evidence.

These verify logic/SQLite/JPEG integrity, not object identity or hand accuracy.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib

import cv2
import numpy as np
import pytest

from apps.api.app.db import Database
from apps.api.app.event_service import EventService
from apps.api.app.location_hypotheses import candidate_locations
from apps.api.app.runtime_mode import RuntimeMode
from apps.api.app.search import location_result


START = datetime(2026, 9, 5, 15, tzinfo=timezone.utc)
PROFILE_VERSION = 1
MODEL_VERSION = "generated-hypothesis-fixture-v1"


def stamp(seconds=0):
    return (START + timedelta(seconds=seconds)).isoformat()


def evidence():
    observed = {"item_id": "item", "camera_id": "camera", "source_session_id": "session",
        "runtime_mode": "TEST", "source_type": "video_file", "is_simulated": True,
        "detection_mode": "experimental", "identity_evidence": {"accepted": True},
        "interaction": {"hand_near": True, "hand_model_healthy": True},
        "observed_at": stamp(), "source_frame": 12, "position": [.5, .5], "zone_id": "table",
        "image_status": "available", "screenshot_path": "/media/event-images/observed-fixture.jpg",
        "screenshot_sha256": "a" * 64, "screenshot_camera_id": "camera",
        "screenshot_source_session_id": "session", "screenshot_source_frame": 11,
        "screenshot_observed_at": stamp(-.2),
        "trajectory": [{"source_session_id": "session", "timestamp": stamp(seconds),
                        "source_frame": 10+i, "center": [x, .5]} for i, (seconds, x) in enumerate([(-.4, .3), (-.2, .4), (0, .5)])]}
    scene = {"camera_id": "camera", "source_session_id": "session", "runtime_mode": "TEST",
        "source_type": "video_file", "is_simulated": True, "source_frame": 1,
        "source_timestamp": stamp(-2), "scene_version": 1, "calibration_status": "schematic",
        "surfaces": [{"surface_id": "table", "name": "explicit fixture table", "confirmed_by_user": True,
                      "calibration_status": "schematic", "image_polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]}]}
    return observed, scene


def test_all_six_evidence_groups_produce_only_inferred_candidate_without_mutating_time():
    observed, scene = evidence()
    before = deepcopy((observed, scene))
    result = candidate_locations(observed, scene, "occluded", stamp(.7))
    assert len(result) == 1
    assert result[0]["evidence_type"] == "inferred"
    assert "尚未" in result[0]["label"] and "不是放置或抓握证明" in result[0]["reason"]
    assert result[0]["observed_at"] == stamp()
    assert result[0]["source_frame"] == 12
    assert result[0]["screenshot_source_frame"] == 11
    assert result[0]["screenshot_observed_at"] == stamp(-.2)
    assert (observed, scene) == before


@pytest.mark.parametrize("key", ["item_id", "identity_evidence", "interaction", "trajectory", "observed_at", "zone_id",
    "screenshot_path", "screenshot_sha256", "screenshot_observed_at", "screenshot_source_frame", "screenshot_source_session_id", "screenshot_camera_id"])
def test_missing_required_evidence_has_no_candidate(key):
    observed, scene = evidence()
    observed.pop(key)
    assert candidate_locations(observed, scene, "occluded", stamp(.7)) == []


@pytest.mark.parametrize("key,value", [
    ("source_type", "unknown"), ("is_simulated", False), ("source_session_id", "other"), ("camera_id", "other"),
    ("runtime_mode", "REAL"), ("calibration_status", "needs_review"), ("invalid_reason", "camera moved"),
    ("source_timestamp", stamp(1)), ("source_frame", 99), ("scene_version", True),
])
def test_scene_provenance_or_context_mismatch_rejects(key, value):
    observed, scene = evidence()
    scene[key] = value
    assert candidate_locations(observed, scene, "occluded", stamp(.7)) == []


@pytest.mark.parametrize("field,value", [("hand_near", False), ("hand_model_healthy", False),
    ("hand_model_healthy", None), ("hand_model_healthy", "true")])
def test_no_healthy_nearby_hand_does_not_infer(field, value):
    observed, scene = evidence()
    observed["interaction"][field] = value
    assert candidate_locations(observed, scene, "occluded", stamp(.7)) == []


@pytest.mark.parametrize("status", ["last_seen", "visible_static", "carried", "moving", "offline", "unknown"])
def test_visibility_or_source_offline_cannot_become_hypothesis(status):
    observed, scene = evidence()
    assert candidate_locations(observed, scene, status, stamp(.7)) == []


def test_unknown_and_seed_cannot_be_relabelled_with_matching_scene_fields():
    for source in ("unknown", "mock", "demo_seed", "test_fixture", "esp32_unverified"):
        observed, scene = evidence()
        observed["source_type"] = scene["source_type"] = source
        assert candidate_locations(observed, scene, "occluded", stamp(.7)) == []


def test_reused_old_trajectory_cannot_bind_to_new_observation_time():
    observed, scene = evidence()
    for point in observed["trajectory"]:
        point["timestamp"] = (datetime.fromisoformat(point["timestamp"]) - timedelta(seconds=3)).isoformat()
    assert candidate_locations(observed, scene, "occluded", stamp(.7)) == []


@pytest.mark.parametrize("variant", ["gap", "duplicate", "session", "position", "bool_frame", "nan", "malformed"])
def test_broken_trajectory_continuity_cannot_create_candidate(variant):
    observed, scene = evidence()
    trail = observed["trajectory"]
    if variant == "gap":
        trail[0]["timestamp"], trail[1]["timestamp"] = stamp(-1.8), stamp(-.9)
    elif variant == "duplicate":
        trail[1]["source_frame"] = trail[0]["source_frame"]
    elif variant == "session":
        trail[0]["source_session_id"] = "previous-session"
    elif variant == "position":
        trail[-1]["center"] = [.7, .8]
    elif variant == "bool_frame":
        trail[0]["source_frame"] = True
    elif variant == "nan":
        trail[0]["center"] = [float("nan"), .5]
    else:
        trail[0] = None
    assert candidate_locations(observed, scene, "occluded", stamp(.7)) == []


@pytest.mark.parametrize("change", ["unconfirmed", "uncalibrated", "not_contained", "invalid_polygon"])
def test_surface_evidence_cannot_be_replaced_by_scene_header_alone(change):
    observed, scene = evidence()
    surface = scene["surfaces"][0]
    if change == "unconfirmed":
        surface["confirmed_by_user"] = False
    elif change == "uncalibrated":
        surface["calibration_status"] = "needs_review"
    elif change == "not_contained":
        surface["image_polygon"] = [[0, 0], [.1, 0], [.1, .1], [0, .1]]
    else:
        surface["image_polygon"] = [[0, 0], [float("nan"), 1], [1, 0]]
    assert candidate_locations(observed, scene, "occluded", stamp(.7)) == []


@pytest.mark.parametrize("changes", [
    {"screenshot_path": "/media/event-images/../../outside.jpg"}, {"screenshot_sha256": "not-a-hash"},
    {"screenshot_source_frame": 13}, {"screenshot_observed_at": stamp(1)},
    {"screenshot_observed_at": stamp(-31)}, {"image_status": "write_failed"},
])
def test_invalid_screenshot_evidence_is_not_displayed_as_support(changes):
    observed, scene = evidence()
    observed.update(changes)
    assert candidate_locations(observed, scene, "occluded", stamp(.7)) == []


@pytest.mark.parametrize("age", [-.1, 8.001, 100])
def test_aged_or_future_observation_clears_hypothesis(age):
    observed, scene = evidence()
    assert candidate_locations(observed, scene, "occluded", stamp(age)) == []


def test_edge_loss_needs_outward_trajectory_not_merely_edge_position():
    observed, scene = evidence()
    for point, x in zip(observed["trajectory"], [.8, .9, .96]):
        point["center"] = [x, .5]
    observed["position"] = [.96, .5]
    assert len(candidate_locations(observed, scene, "lost", stamp(.7))) == 1
    for point, x in zip(observed["trajectory"], [.02, .04, .06]):
        point["center"] = [x, .5]
    observed["position"] = [.06, .5]
    assert candidate_locations(observed, scene, "lost", stamp(.7)) == []


@pytest.fixture
def store(tmp_path):
    db = Database(tmp_path / "database" / "objectmemory-test.sqlite", "TEST")
    camera = db.save("cameras", {"source_type": "video", "source": "never-opened.avi", "config": {}} , "camera")
    item = db.save("items", {"name": "generated fixture", "type": "phone"}, "item")
    # This is an explicit generated profile fixture, not a model-accuracy test.
    # The real SQLite transaction must find the same ready profile generation
    # as the server-owned observation proof; accepted=True alone is insufficient.
    db.save("item_recognition_profiles", {"item_id": item["id"], "status": "ready",
        "profile_version": PROFILE_VERSION, "model_id": "generated-fixture",
        "model_version": MODEL_VERSION, "dimension": 2, "embeddings": [[1.0, 0.0]]}, item["id"])
    _observed, scene = evidence()
    db.save("scenes", scene, "scene:TEST:camera")
    service = EventService(db, RuntimeMode.TEST, tmp_path / "test-media")
    return service, db, item, camera


def track(elapsed=0, **changes):
    observed, _scene = evidence()
    value = {"item_id": "item", "camera_id": "camera", "source_session_id": "session",
        "source_type": "video_file", "runtime_mode": "TEST", "is_simulated": True,
        "source_frame": 12, "observation_source_frame": 12, "last_seen": stamp(elapsed), "source_timestamp": stamp(elapsed),
        "center": [.5, .5], "state": "CARRIED", "hand_near": True, "hand_model_healthy": True,
        "holding_status": "possibly_held", "detection_mode": "experimental", "confidence": .9,
        "identity_evidence": {"accepted": True, "profile_version": PROFILE_VERSION,
                              "model_version": MODEL_VERSION},
        "history": observed["trajectory"], "zone_id": "table", "zone_name": "fixture table"}
    return value | changes


def actual_jpeg():
    ok, encoded = cv2.imencode(".jpg", np.full((96, 128, 3), (20, 70, 160), np.uint8))
    assert ok
    return encoded.tobytes()


def test_actual_sqlite_observation_to_occlusion_keeps_original_time_jpeg_and_zero_events(store):
    service, db, item, _camera = store
    content = actual_jpeg()
    initial = service.observe(track(), observation_verified=True, observation_jpeg=content)
    assert initial is not None
    missing = service.observe(track(.7, state="OCCLUDED", center=[.9, .9]), observation_verified=True)
    assert len(missing["location_hypotheses"]) == 1
    assert missing["last_seen_at"] == initial["last_seen_at"] == stamp()
    assert missing["current_position"] == [.5, .5]
    hypothesis = missing["location_hypotheses"][0]
    path = service.media_path(hypothesis["screenshot_path"])
    assert path.read_bytes() == content and hashlib.sha256(content).hexdigest() == hypothesis["screenshot_sha256"]
    assert cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR) is not None
    result = location_result(item, [], "TEST", missing)
    assert result["status"] == "occluded" and result["last_confirmed"] is None
    assert result["location_hypotheses"][0]["evidence_type"] == "inferred"
    assert db.count("events") == 0
    visible = service.observe(track(1, source_frame=13, observation_source_frame=13), observation_verified=True, observation_jpeg=content)
    assert visible["location_hypotheses"] == [] and visible["last_seen_at"] == stamp(1)


@pytest.mark.parametrize("condition", ["expired", "scene_invalid", "hand_missing", "no_image", "session_mismatch"])
def test_actual_store_missing_never_invents_without_complete_inputs(store, condition):
    service, db, *_ = store
    first = track(hand_model_healthy=False) if condition == "hand_missing" else track()
    initial = service.observe(first, observation_verified=True, observation_jpeg=None if condition == "no_image" else actual_jpeg())
    assert initial is not None
    if condition == "scene_invalid":
        db.save("scenes", {"calibration_status": "needs_review"}, "scene:TEST:camera")
    if condition == "session_mismatch":
        db.save("scenes", {"source_session_id": "previous-session"}, "scene:TEST:camera")
    missing = service.observe(track(9 if condition == "expired" else .7, state="OCCLUDED"), observation_verified=True)
    assert missing["location_hypotheses"] == []
    assert missing["last_seen_at"] == initial["last_seen_at"]
    assert db.count("events") == 0


@pytest.mark.parametrize("condition", ["missing", "not_ready", "profile_version", "model_version"])
def test_accepted_fixture_without_matching_ready_profile_cannot_persist_or_infer(store, condition):
    service, db, item, _camera = store
    if condition == "missing":
        db.delete("item_recognition_profiles", item["id"])
    else:
        changes = {"not_ready": {"status": "images_saved"},
            "profile_version": {"profile_version": PROFILE_VERSION + 1},
            "model_version": {"model_version": MODEL_VERSION + "-different"}}[condition]
        db.save("item_recognition_profiles", changes, item["id"])
    assert service.observe(track(), observation_verified=True, observation_jpeg=actual_jpeg()) is None
    assert service.observe(track(.7, state="OCCLUDED"), observation_verified=True) is None
    assert db.count("item_current_state") == db.count("event_media") == db.count("movement_events") == 0
    assert not list(service.media_root.rglob("*.jpg"))
