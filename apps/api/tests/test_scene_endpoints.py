"""Real FastAPI/SQLite/FrameSlot scene routes, never physical-camera evidence.

Frames are generated and explicitly TEST. No business HTTP result or NanoDet
inference is mocked; only controlled stale/detach conditions are injected.
"""
from pathlib import Path
import time

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from services.vision.capture import FramePacket
from services.vision.engine import VisionEngine


POLYGON = [[.1, .2], [.7, .2], [.7, .8], [.1, .8]]


@pytest.fixture
def context(tmp_path):
    app = create_app(tmp_path / "scene-route-test", testing=True)
    runtime = app.state.runtime
    with TestClient(app) as client:
        client.get("/api/session")
        camera = runtime.db.save("cameras", {"name": "not opened", "source_type": "video",
            "source": str(tmp_path / "never-opened.avi"), "room_name": "fixture room",
            "config": {}, "enabled": False, "inference_fps": 5, "save_clips": True})
        engine = VisionEngine(camera, [], [], {**runtime.settings(), "show_hands": False,
            "runtime_mode": "TEST", "media_root": str(runtime.media)}, on_event=lambda *_: None)
        runtime.engines[camera["id"]] = engine
        frame = np.random.default_rng(32).integers(0, 256, (240, 320, 3), dtype=np.uint8)
        value = (client, runtime, camera, engine, frame)
        publish(value)
        try:
            yield value
        finally:
            runtime.engines.pop(camera["id"], None)
            engine.stop()
            assert engine.capture.thread is None
    runtime.mode_lease.close()


def publish(context, sequence=1, session="test-scene-http", age=0, frame=None):
    _client, _runtime, _camera, engine, original = context
    packet = FramePacket(original.copy() if frame is None else frame.copy(), time.monotonic()-age,
        time.time()-age, sequence, session, 0)
    engine.capture.preview_frames.publish(packet)
    return packet


def path(context):
    return f"/api/cameras/{context[2]['id']}/scene"


def propose(context):
    publish(context)
    response = context[0].post(path(context) + "/propose")
    assert response.status_code == 200, response.text
    return response.json()["scene"]


def body(scene):
    return {key: scene[key] for key in ("scene_version", "snapshot_id", "source_session_id", "source_frame")} | {
        "surfaces": [{"name": "explicit test table", "surface_type": "table", "image_polygon": POLYGON,
                      "confirmed_by_user": True}]}


def test_scene_get_never_opens_camera_and_empty_is_honest(context, monkeypatch):
    client, runtime, camera, engine, _frame = context
    runtime.engines.pop(camera["id"])
    monkeypatch.setattr(runtime, "start", lambda *_: pytest.fail("scene GET must not start capture"))
    response = client.get(path(context))
    assert response.status_code == 200 and response.json()["scene"] is None
    assert response.json()["items"] == [] and response.json()["runtime_mode"] == "TEST"
    assert runtime.db.count("events") == 0
    assert client.post(path(context) + "/propose").status_code == 409
    assert engine.capture.thread is None


def test_actual_frame_slot_propose_media_and_explicit_save(context):
    client, runtime, camera, engine, frame = context
    scene = propose(context)
    assert scene["source_type"] == "video_file" and scene["is_simulated"] is True
    assert scene["source_session_id"] == "test-scene-http" and scene["source_frame"] == 1
    assert scene["surfaces"] == [] and scene["calibration_status"] == "needs_confirmation"
    assert runtime.db.count("zones") == runtime.db.count("events") == 0
    image = client.get(scene["screenshot_path"])
    assert image.status_code == 200
    decoded = cv2.imdecode(np.frombuffer(image.content, np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape == frame.shape
    publish(context, 2)
    result = client.put(path(context), json=body(scene))
    assert result.status_code == 200, result.text
    saved = result.json()["scene"]
    assert saved["calibration_status"] == "schematic"
    assert saved["surfaces"][0]["confirmed_by_user"] is True
    assert runtime.db.count("zones") == 1 and runtime.db.count("events") == 0
    zone = runtime.db.get("zones", saved["surfaces"][0]["surface_id"])
    assert zone["scene_metadata"]["scene_id"] == saved["id"]
    assert engine.capture.thread is None


@pytest.mark.parametrize("age", [2.1, 20])
def test_stale_frame_rejects_proposal_without_snapshot(context, age):
    client, runtime, *_ = context
    publish(context, age=age)
    response = client.post(path(context) + "/propose")
    assert response.status_code == 409
    assert runtime.db.count("scenes") == 0
    assert not list((runtime.media / "scene-images").glob("*.jpg"))


def test_engine_detaches_during_slot_read_is_rejected(context, monkeypatch):
    client, runtime, camera, engine, _ = context
    original = engine.capture.preview_frames.latest
    def detach(*args, **kwargs):
        result = original(*args, **kwargs)
        runtime.engines.pop(camera["id"])
        return result
    monkeypatch.setattr(engine.capture.preview_frames, "latest", detach)
    response = client.post(path(context) + "/propose")
    assert response.status_code == 409
    assert runtime.db.count("scenes") == 0


@pytest.mark.parametrize("change", ["session", "geometry", "camera_config", "version"])
def test_edit_rejects_changed_source_context(context, change):
    client, runtime, camera, _engine, _frame = context
    scene = propose(context)
    data = body(scene)
    if change == "session":
        publish(context, 2, session="reconnected-test-session")
    elif change == "geometry":
        publish(context, 2, frame=np.zeros((320, 240, 3), np.uint8))
    elif change == "camera_config":
        publish(context, 2)
        runtime.db.save("cameras", {"config": {"mirror": True}}, camera["id"])
    else:
        publish(context, 2)
        data["scene_version"] += 1
    response = client.put(path(context), json=data)
    assert response.status_code == 409, response.text
    assert runtime.db.count("zones") == runtime.db.count("events") == 0


def test_visible_scene_shift_while_editing_rejects_confirmation(context):
    client, runtime, _camera, _engine, frame = context
    scene = propose(context)
    matrix = np.float32([[1, 0, 40], [0, 1, 20]])
    shifted = cv2.warpAffine(frame, matrix, (frame.shape[1], frame.shape[0]))
    publish(context, 2, frame=shifted)
    response = client.put(path(context), json=body(scene))
    assert response.status_code == 409, response.text
    assert runtime.db.count("zones") == 0
    assert runtime.scenes.get(context[2]["id"])["calibration_status"] == "needs_review"


def test_get_reports_guard_invalidated_scene_without_new_capture(context):
    client, runtime, camera, engine, _frame = context
    scene = propose(context)
    publish(context, 2)
    assert client.put(path(context), json=body(scene)).status_code == 200
    engine.scene_requires_review = True
    response = client.get(path(context))
    assert response.status_code == 200
    assert response.json()["scene"]["calibration_status"] == "needs_review"
    zones = runtime.db.list("zones", {"camera_id": camera["id"]})
    assert zones and all(zone["enabled"] is False for zone in zones)
    assert engine.capture.thread is None and runtime.db.count("events") == 0


def test_save_cannot_relabel_test_source_real_or_auto_accept(context):
    client, runtime, *_ = context
    scene = propose(context)
    for bad in (body(scene) | {"runtime_mode": "REAL", "is_simulated": False},
                body(scene) | {"surfaces": [{**body(scene)["surfaces"][0], "confirmed_by_user": False}]}):
        publish(context, 2)
        response = client.put(path(context), json=bad)
        assert response.status_code == 422, response.text
    assert runtime.db.count("zones") == runtime.db.count("events") == 0
