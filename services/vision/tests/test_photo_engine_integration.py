"""Independent engine -> Runtime -> SQLite/JPEG integration in isolated TEST mode.

Appearance embeddings use the actual installed DINO model. Candidate rectangles,
hand observations and moving image frames are explicitly synthetic control-flow
fixtures, not NanoDet accuracy, real hands, physical items or camera evidence.
No capture/start call is permitted and no business persistence API is mocked.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace
import time

import cv2
import numpy as np
import pytest

from apps.api.app.main import Runtime
from apps.api.app.runtime_mode import RuntimeMode, runtime_layout
from services.vision.capture import FramePacket
from services.vision.detectors.appearance import AppearanceEncoder, DEFAULT_MODEL_PATH
from services.vision.detectors.base import Detection
from services.vision.detectors.hands import HandObservation, MediaPipeHandDetector
from services.vision.engine import VisionEngine


ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def actual_appearance():
    if not DEFAULT_MODEL_PATH.is_file():
        pytest.skip("Optional real DINO weights not prepared; no mock model substitutes this test")
    encoder = AppearanceEncoder()
    assert encoder.health()["available"], encoder.health()
    crop = np.full((96, 64, 3), (35, 55, 65), np.uint8)
    cv2.rectangle(crop, (7, 6), (56, 86), (140, 180, 210), -1)
    cv2.circle(crop, (44, 19), 7, (12, 18, 24), -1)
    cv2.line(crop, (12, 66), (48, 72), (30, 40, 50), 4)
    vector = encoder.encode(crop)
    yield encoder, crop, vector
    encoder.close()


@pytest.fixture
def pipeline(tmp_path, monkeypatch, actual_appearance):
    encoder, crop, vector = actual_appearance
    controls = SimpleNamespace(bbox=(20, 70, 64, 96), category="cell phone", visible=True,
                               hand_error=False, hand_timestamps=[], callbacks=[], packets=[])

    class ProposalFixture:
        def __init__(self, *_args, **_kwargs):
            pass

        def health(self):
            return {"available": True, "backend": "explicit_fixed_box_test_fixture", "error": None}

        def detect(self, _frame):
            if not controls.visible:
                return []
            x, y, width, height = controls.bbox
            return [Detection("generic:fixture", controls.category, controls.bbox,
                              (x + width / 2, y + height / 2), .99, "experimental")]

    class HandFixture(MediaPipeHandDetector):
        def __init__(self):
            self.error = None
            self.closed = False

        def detect(self, _frame, timestamp_ms=None):
            controls.hand_timestamps.append(timestamp_ms)
            if controls.hand_error:
                raise RuntimeError("explicit hand processing fixture failure")
            points = [(.3 + index * .002, .3 + index * .002) for index in range(21)]
            return [HandObservation((100, 85), (95, 75, 25, 30), points, confidence=None,
                                    handedness="Left", handedness_score=.95,
                                    source_timestamp_ms=timestamp_ms, timestamp_ms=timestamp_ms)]

        def health(self):
            return {"available": not self.closed, "error": self.error, "backend": "explicit_hand_test_fixture"}

        def close(self):
            self.closed = True

    monkeypatch.setattr("services.vision.engine.NanoDetDetectorBackend", ProposalFixture)
    monkeypatch.setattr(cv2, "VideoCapture", lambda *_args, **_kwargs: pytest.fail("This integration must never open a capture"))
    runtime = Runtime(runtime_layout(ROOT, tmp_path / "isolated", RuntimeMode.TEST), testing=True)
    profile = {"status": "ready", "profile_version": 1, "model_id": encoder.model_id,
               "model_version": encoder.model_version, "dimension": encoder.dimension, "embeddings": [vector]}
    item = runtime.db.save("items", {"name": "synthetic control-flow rectangle", "type": "phone", "aruco_id": None}, "photo-item")
    runtime.db.save("item_recognition_profiles", {"item_id": item["id"], **profile}, item["id"])
    entry = {**item, "appearance_profile": profile}
    camera = runtime.db.save("cameras", {"name": "never-opened file fixture", "room_name": "isolated fixture room",
        "source_type": "video", "source": str(tmp_path / "never-opened.avi"), "config": {}, "enabled": False,
        "inference_fps": 5, "runtime_mode": "TEST", "save_clips": False}, "photo-camera")
    engine = VisionEngine(camera, [entry], [], {"runtime_mode": "TEST", "show_hands": False,
        "detection_mode": "experimental", "media_root": str(runtime.media), "save_clips": False,
        "observation_min_frames": 3, "reference_match_threshold": .8, "reference_match_margin": .06},
        on_event=lambda *_args: pytest.fail("Moving without a baseline must not emit a confirmed event"),
        appearance_encoder=encoder)
    engine.hands.close()
    engine.hands = HandFixture()
    runtime.engines[camera["id"]] = engine
    def receive(payload):
        controls.callbacks.append(deepcopy(payload))
        runtime.track(payload, expected_engine=engine)
    engine.on_track = receive
    started_monotonic, started_wall = time.monotonic(), time.time()

    def process(index, *, x=None, size=(320, 240), session="explicit-test-source", source_frame=None):
        width, height = size
        x = 20 + index * 12 if x is None else x
        controls.bbox = (x, 70, 64, 96)
        frame = np.full((height, width, 3), 35, np.uint8)
        if controls.visible:
            frame[70:166, x:x + 64] = crop
        packet = FramePacket(frame, started_monotonic + index * .25, started_wall + index * .25,
                             source_frame if source_frame is not None else index, session, 0)
        controls.packets.append(packet)
        engine._process(packet)
        assert engine.capture.thread is None
        return packet

    def state():
        return runtime.db.get("item_current_state", "TEST:photo-item")

    value = SimpleNamespace(engine=engine, runtime=runtime, controls=controls, process=process,
                            state=state, encoder=encoder, item=entry)
    try:
        yield value
    finally:
        runtime.engines.clear()
        engine.stop()


def confirm_three(pipeline):
    for frame in (1, 2, 3):
        pipeline.process(frame)
        if frame < 3:
            assert pipeline.state() is None
    result = pipeline.state()
    assert result is not None, {"callbacks": pipeline.controls.callbacks, "error": pipeline.engine.health().get("error")}
    return result


def test_normal_hand_results_survive_snapshot_and_share_exact_source_frame(pipeline):
    packet = pipeline.process(1)
    snapshot = pipeline.engine.recognition_snapshot()
    assert snapshot["hand_count"] == 1, "Successful hands must not be cleared by unrelated JPEG encoding control flow"
    assert len(snapshot["hands"][0]["landmarks"]) == 21
    assert snapshot["hands"][0]["presence_score"] is None
    assert snapshot["hands"][0]["source_timestamp_ms"] == round(packet.monotonic_time * 1000)
    assert snapshot["hands"][0]["bbox_normalized"] == [95 / 320, 75 / 240, 25 / 320, 30 / 240]
    assert snapshot["geometry"]["source_width"] == 320 and snapshot["geometry"]["source_height"] == 240
    assert snapshot["source_frame"] == packet.sequence and snapshot["source_session_id"] == packet.source_session_id
    assert datetime.fromisoformat(snapshot["source_timestamp"]).timestamp() == pytest.approx(packet.wall_time)
    decoded = cv2.imdecode(np.frombuffer(snapshot["jpeg"], np.uint8), 1)
    assert decoded.shape == packet.frame.shape and np.abs(decoded.astype(float) - packet.frame).mean() < 2
    assert snapshot["candidates"][0]["accepted"] is True


def test_three_moving_photo_detections_persist_same_frame_jpeg_without_stable_baseline(pipeline):
    state = confirm_three(pipeline)
    machine = pipeline.engine.state_machines["photo-item"]
    assert machine.baseline is None and not machine.observation_verified
    assert state["status"] == "last_seen" and state["is_simulated"] is True and state["runtime_mode"] == "TEST"
    assert pipeline.runtime.db.count("events") == pipeline.runtime.db.count("movement_events") == 0
    observed = state["last_observed"]
    assert observed["source_frame"] == observed["screenshot_source_frame"] == 3
    assert observed["source_session_id"] == observed["screenshot_source_session_id"] == "explicit-test-source"
    path = pipeline.runtime.event_service.media_path(observed["screenshot_path"])
    content = path.read_bytes()
    assert hashlib.sha256(content).hexdigest() == observed["screenshot_sha256"]
    assert content == pipeline.engine.recognition_snapshot()["jpeg"]
    assert cv2.imdecode(np.frombuffer(content, np.uint8), 1) is not None
    assert pipeline.runtime.db.count("event_media") == 1


def test_hand_exception_does_not_erase_valid_photo_observation(pipeline):
    pipeline.controls.hand_error = True
    state = confirm_three(pipeline)
    assert state["last_observed"]["source_frame"] == 3
    snapshot = pipeline.engine.recognition_snapshot()
    assert snapshot["hand_count"] == 0 and snapshot["candidates"][0]["accepted"] is True
    assert "explicit hand processing fixture failure" in pipeline.engine.hands.health()["error"]
    assert state["last_observed"]["image_status"] == "available"


@pytest.mark.parametrize("reason", ["missing", "wrong_category"])
def test_missing_or_nonmatching_candidate_cannot_refresh_last_seen_or_evidence(pipeline, reason):
    before = confirm_three(pipeline)
    if reason == "missing":
        pipeline.controls.visible = False
    else:
        pipeline.controls.category = "wallet"
    for index in (4, 5, 6, 7):
        pipeline.process(index)
    after = pipeline.state()
    assert after["last_seen_at"] == before["last_seen_at"]
    assert after["current_position"] == before["current_position"]
    for key in ("observed_at", "source_frame", "source_session_id", "screenshot_path", "screenshot_source_frame", "screenshot_sha256"):
        assert after["last_observed"][key] == before["last_observed"][key]
    assert pipeline.runtime.db.count("event_media") == 1 and pipeline.runtime.db.count("movement_events") == 0


def test_profile_reload_requires_new_continuity_and_rejects_old_engine_proof(pipeline):
    before = confirm_three(pipeline)
    old_callback = deepcopy(pipeline.controls.callbacks[-1])
    updated = deepcopy(pipeline.item)
    updated["appearance_profile"]["profile_version"] = 2
    pipeline.runtime.db.save("item_recognition_profiles", {"profile_version": 2}, "photo-item")
    pipeline.engine.reload_profiles([updated], pipeline.encoder, enable=True)
    assert pipeline.engine.recognition_snapshot() is None
    pipeline.runtime.track(old_callback, expected_engine=pipeline.engine)
    assert pipeline.state()["last_seen_at"] == before["last_seen_at"]
    pipeline.process(4)
    assert "photo-item" not in pipeline.engine.observation_gate.verified
    assert pipeline.state()["last_seen_at"] == before["last_seen_at"]
    assert pipeline.engine.recognition_snapshot()["loaded_profiles"][0]["profile_version"] == 2
    pipeline.process(5)
    # Expire only the test's publication throttle, not either identity gate.
    pipeline.runtime.track_writes.clear()
    pipeline.process(6)
    assert pipeline.state()["last_observed"]["source_frame"] == 6
    assert pipeline.state()["last_observed"]["identity_evidence"]["profile_version"] == 2


def test_reload_arriving_during_real_encoder_call_discards_inflight_old_profile(pipeline, monkeypatch):
    before = confirm_three(pipeline)
    encode = pipeline.encoder.encode
    updated = deepcopy(pipeline.item)
    updated["appearance_profile"]["profile_version"] = 2
    def reload_during_encode(crop):
        vector = encode(crop)
        pipeline.engine.reload_profiles([updated], pipeline.encoder, enable=True)
        return vector
    monkeypatch.setattr(pipeline.encoder, "encode", reload_during_encode)
    pipeline.runtime.track_writes.clear()
    pipeline.process(4)
    assert pipeline.engine.recognition_snapshot() is None
    assert pipeline.state()["last_seen_at"] == before["last_seen_at"]
    assert pipeline.state()["last_observed"]["screenshot_sha256"] == before["last_observed"]["screenshot_sha256"]


def test_snapshot_older_than_current_read_window_is_not_returned(pipeline):
    pipeline.process(1)
    assert pipeline.engine.recognition_snapshot() is not None
    pipeline.engine._recognition_snapshot["created_at"] -= 4
    assert pipeline.engine.recognition_snapshot() is None


@pytest.mark.parametrize("change", ["session", "resolution"])
def test_source_or_resolution_change_discards_proof_and_cannot_reuse_prior_frame(pipeline, change):
    before = confirm_three(pipeline)
    kwargs = {"session": "new-test-session", "source_frame": 1} if change == "session" else {"size": (640, 480)}
    pipeline.process(4, **kwargs)
    assert "photo-item" not in pipeline.engine.observation_gate.verified
    assert pipeline.state()["last_seen_at"] == before["last_seen_at"]
    assert pipeline.engine.state_machines["photo-item"].baseline is None
    assert pipeline.runtime.db.count("movement_events") == 0
    if change == "resolution":
        assert pipeline.engine.scene_requires_review
        assert pipeline.engine.recognition_snapshot()["geometry"]["source_width"] == 640


def test_camera_configuration_drift_rejects_old_engine_updates(pipeline):
    before = confirm_three(pipeline)
    pipeline.runtime.db.save("cameras", {"source": "different-never-opened.avi"}, "photo-camera")
    pipeline.runtime.track_writes.clear()
    pipeline.process(4)
    assert pipeline.state()["last_seen_at"] == before["last_seen_at"]
    assert pipeline.state()["last_observed"]["screenshot_path"] == before["last_observed"]["screenshot_path"]


def test_jpeg_encoder_failure_does_not_erase_hands_or_invent_same_frame_media(pipeline, monkeypatch):
    before = confirm_three(pipeline)
    original_encode = cv2.imencode
    def failed_jpeg(extension, frame, *args, **kwargs):
        if extension == ".jpg":
            raise cv2.error("explicit JPEG failure fixture")
        return original_encode(extension, frame, *args, **kwargs)
    monkeypatch.setattr(cv2, "imencode", failed_jpeg)
    pipeline.runtime.track_writes.clear()
    pipeline.process(4, x=100)
    assert pipeline.engine.recognition_snapshot() is None
    assert "explicit JPEG failure" in pipeline.engine.health()["observation_media_error"]
    assert pipeline.controls.callbacks[-1]["hand_near"] is True
    after = pipeline.state()
    assert after["last_seen_at"] >= before["last_seen_at"]
    assert after["last_observed"].get("screenshot_source_frame") != 4
    assert pipeline.runtime.db.count("movement_events") == 0


@pytest.mark.parametrize("component", ["hand_health", "hand_actions", "hand_interaction"])
def test_auxiliary_action_exception_cannot_erase_real_photo_observation(pipeline, monkeypatch, component):
    def fail(*_args, **_kwargs):
        raise RuntimeError(f"controlled {component} failure")
    if component == "hand_health":
        monkeypatch.setattr(pipeline.engine.hands, "health", fail)
    elif component == "hand_actions":
        monkeypatch.setattr(pipeline.engine.hand_actions, "update", fail)
    else:
        monkeypatch.setattr(pipeline.engine.hand_interactions, "update", fail)
    state = confirm_three(pipeline)
    observed = state["last_observed"]
    assert observed["source_frame"] == 3 and observed["image_status"] == "available"
    assert observed["holding_status"] not in {"released", "co_moving"}
    assert not observed["interaction"].get("release_observed")
    snapshot = pipeline.engine.recognition_snapshot()
    assert snapshot["source_frame"] == 3 and snapshot["candidates"][0]["accepted"] is True
    path = pipeline.runtime.event_service.media_path(observed["screenshot_path"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == observed["screenshot_sha256"]
    assert pipeline.runtime.db.count("movement_events") == 0
    assert pipeline.engine.capture.thread is None


def test_interaction_failure_never_reuses_old_released_result(pipeline, monkeypatch):
    confirm_three(pipeline)
    pipeline.engine._item_interactions["photo-item"] = {
        "holding_status": "released", "release_observed": True, "hand_id": "old-hand",
        "source_frame": 3, "source_session_id": "explicit-test-source"}
    def fail(*_args, **_kwargs):
        raise RuntimeError("controlled interaction failure after old release")
    monkeypatch.setattr(pipeline.engine.hand_interactions, "update", fail)
    pipeline.runtime.track_writes.clear()
    pipeline.process(4)
    state = pipeline.state()
    assert state["last_observed"]["source_frame"] == 4
    assert state["last_observed"]["holding_status"] != "released"
    assert state["last_observed"]["interaction"]["release_observed"] is False
    assert pipeline.engine._item_interactions["photo-item"]["source_frame"] == 4
    assert pipeline.runtime.db.count("movement_events") == 0


def test_callback_cannot_forge_holding_or_release_when_observation_proof_matches(pipeline):
    before = confirm_three(pipeline)
    live = deepcopy(pipeline.engine._item_interactions["photo-item"])
    assert live["source_frame"] == 3 and live["holding_status"] != "released"
    callback = deepcopy(pipeline.controls.callbacks[-1])
    callback.update({"holding_status": "released", "hand_interaction": {
        **live, "holding_status": "released", "release_observed": True,
        "hand_id": "forged-hand", "co_motion_frames": 999,
        "release_start_frame": 1, "release_end_frame": 3}})
    pipeline.runtime.track_writes.clear()
    pipeline.runtime.track(callback, expected_engine=pipeline.engine)
    after = pipeline.state()
    assert after["last_observed"]["holding_status"] == live["holding_status"]
    assert after["last_observed"]["interaction"]["hand_id"] == live["hand_id"]
    assert after["last_observed"]["interaction"]["release_observed"] is False
    assert after["last_seen_at"] == before["last_seen_at"]
    assert after["last_observed"]["screenshot_sha256"] == before["last_observed"]["screenshot_sha256"]
    assert pipeline.runtime.db.count("movement_events") == 0


@pytest.mark.parametrize("change", [{"source_frame": 2}, {"source_session_id": "previous-session"}])
def test_prior_engine_interaction_cannot_be_attached_to_current_observation(pipeline, change):
    before = confirm_three(pipeline)
    pipeline.engine._item_interactions["photo-item"].update({**change, "holding_status": "released", "release_observed": True})
    pipeline.runtime.track_writes.clear()
    pipeline.runtime.track(deepcopy(pipeline.controls.callbacks[-1]), expected_engine=pipeline.engine)
    observed = pipeline.state()["last_observed"]
    assert observed["holding_status"] == "not_established"
    assert not observed["interaction"].get("release_observed")
    assert observed["source_frame"] == before["last_observed"]["source_frame"]
    assert pipeline.runtime.db.count("movement_events") == 0
