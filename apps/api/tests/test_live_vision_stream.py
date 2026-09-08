"""Vision WebSocket protocol and an actual generated-file pipeline.

``cache_unit`` tests deliberately use a controlled latest-frame cache; they do
not prove vision accuracy. ``actual_file_pipeline`` uses real FastAPI, SQLite,
OpenCV file capture, VisionEngine, JPEG and the real WS route. Its source is
synthetic and ArUco mode with no registered markers, never real-phone evidence.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import threading
import time

import anyio
import cv2
from fastapi.testclient import TestClient
import numpy as np
import pytest
from starlette.websockets import WebSocketDisconnect

from apps.api.app.main import create_app


@pytest.fixture
def context(tmp_path):
    app = create_app(tmp_path/"vision-ws-test",testing=True)
    runtime = app.state.runtime
    with TestClient(app) as client:
        assert client.get("/api/session").json()["authenticated"] is True
        response = client.post("/api/cameras",json={"name":"Unopened TEST camera","source_type":"browser",
            "source":"browser","enabled":False,"config":{}})
        assert response.status_code == 200, response.text
        camera = response.json()
        try:
            yield client,runtime,camera
        finally:
            # Only explicit cache test doubles are removed here. Real engines
            # must go through Runtime.stop() so all capture workers are joined.
            for camera_id, engine in list(runtime.engines.items()):
                if isinstance(engine, ControlledLatestCache):
                    runtime.engines.pop(camera_id,None)
                else:
                    runtime.stop(camera_id)


def route(camera):
    return f"/ws/cameras/{camera['id']}/vision"


def business_counts(runtime):
    return {name:runtime.db.count(name) for name in ("movement_events","item_current_state","event_media")}


def wait_until(predicate, timeout=2):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate(), "condition did not become true before the bounded deadline"


def receive_message(ws,timeout=2):
    # The installed Starlette TestClient has no public receive timeout. Its
    # memory stream is used only to bound this test harness, not mock the API.
    async def bounded():
        with anyio.move_on_after(timeout):
            return await ws._send_rx.receive()
        return None
    return ws.portal.call(bounded)


def decode_packet(message):
    assert message and message["type"] == "websocket.send" and isinstance(message.get("bytes"),bytes),message
    body = message["bytes"]
    assert len(body) > 4
    length = int.from_bytes(body[:4],"big")
    assert 0 < length <= 131072 and 4+length < len(body)
    metadata = json.loads(body[4:4+length].decode("utf-8"))
    jpeg = body[4+length:]
    assert metadata["type"] == "frame" and "jpeg" not in metadata and "created_at" not in metadata
    assert jpeg.startswith(b"\xff\xd8\xff")
    decoded = cv2.imdecode(np.frombuffer(jpeg,np.uint8),cv2.IMREAD_COLOR)
    assert decoded is not None and list(decoded.shape[:2]) == [metadata["height"],metadata["width"]]
    return metadata,jpeg,decoded


class ControlledLatestCache:
    """Explicit unit fixture. No capture/model/event callback exists here."""
    def __init__(self, camera):
        self.camera = camera
        self.current = None
        self.lock = threading.Lock()
        self.reads = 0

    def publish(self,frame_id,session="unit-source",value=None):
        frame = np.full((96,128,3),frame_id*20 if value is None else value,np.uint8)
        ok,jpeg = cv2.imencode(".jpg",frame)
        assert ok
        snapshot = {"camera_id":self.camera["id"],"source_frame":frame_id,"source_session_id":session,
            "source_timestamp":datetime.now(timezone.utc).isoformat(),"runtime_mode":"TEST",
            "source_type":"browser_camera","is_simulated":True,"width":128,"height":96,
            "jpeg":jpeg.tobytes(),"created_at":time.monotonic(),"coordinate_space":"source_normalized",
            "candidates":[{"bbox":[.1,.2,.3,.4],"accepted":False,"source_frame":frame_id,"source_session_id":session}],
            "display_only":True,"fixture_kind":"controlled_latest_cache_not_actual_detector"}
        with self.lock:
            self.current = snapshot
        return deepcopy(snapshot)

    def tracking_snapshot(self):
        with self.lock:
            self.reads += 1
            return deepcopy(self.current)

    def clear(self):
        with self.lock:
            self.current = None


@pytest.mark.parametrize("headers,authenticated,code", [
    ({"origin":"https://evil.example"},True,4403), ({},True,4403),
    ({"origin":"http://testserver"},False,4401),
])
def test_cache_unit_websocket_origin_and_session_guards(context,headers,authenticated,code):
    client,runtime,camera = context
    if not authenticated:
        client.cookies.clear()
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(route(camera),headers=headers):
            pytest.fail("unauthorized connection was accepted")
    assert caught.value.code == code
    assert not runtime.engines and runtime.camera_subscribers.get(camera["id"],0) == 0
    assert set(business_counts(runtime).values()) == {0}


def test_cache_unit_missing_camera_is_rejected_without_subscriber(context):
    client,runtime,_ = context
    with client.websocket_connect("/ws/cameras/missing-camera/vision",headers={"origin":"http://testserver"}) as ws:
        message = receive_message(ws)
        assert message["type"] == "websocket.close" and message["code"] == 4404
    assert runtime.camera_subscribers.get("missing-camera",0) == 0 and not runtime.engines


def test_cache_unit_idle_disconnect_releases_subscription_and_never_starts_source(context,monkeypatch):
    client,runtime,camera = context
    monkeypatch.setattr(runtime,"start",lambda *_:pytest.fail("read-only vision subscription must never start a source"))
    before = business_counts(runtime)
    with client.websocket_connect(route(camera),headers={"origin":"http://testserver"}) as ws:
        message = receive_message(ws)
        assert json.loads(message["text"])["type"] == "unavailable"
        assert receive_message(ws,.15) is None  # unavailable status is not spammed
        assert runtime.camera_subscribers[camera["id"]] == 1 and not runtime.engines
        ws.close()
        wait_until(lambda:runtime.camera_subscribers[camera["id"]] == 0)
    assert business_counts(runtime) == before and not runtime.engines


def test_cache_unit_binary_frame_is_atomic_and_ack_skips_to_latest(context):
    client,runtime,camera = context
    cache = ControlledLatestCache(camera)
    first = cache.publish(1)
    runtime.engines[camera["id"]] = cache
    with client.websocket_connect(route(camera),headers={"origin":"http://testserver"}) as ws:
        metadata,jpeg,_ = decode_packet(receive_message(ws))
        assert metadata["source_frame"] == metadata["candidates"][0]["source_frame"] == 1
        assert jpeg == first["jpeg"]
        cache.publish(2)
        newest = cache.publish(9)
        assert receive_message(ws,.1) is None
        ws.send_json({"ack":1,"source_session_id":"unit-source"})
        metadata,jpeg,_ = decode_packet(receive_message(ws))
        assert metadata["source_frame"] == metadata["candidates"][0]["source_frame"] == 9
        assert jpeg == newest["jpeg"] and cache.current["jpeg"] == newest["jpeg"]
        ws.send_json({"ack":9,"source_session_id":"unit-source"})
        assert receive_message(ws,.15) is None
        ws.close()
        wait_until(lambda:runtime.camera_subscribers[camera["id"]] == 0)
    assert set(business_counts(runtime).values()) == {0}


@pytest.mark.parametrize("ack", [
    {"ack":2,"source_session_id":"unit-source"}, {"ack":1,"source_session_id":"other-session"},
    {"ack":"1","source_session_id":"unit-source"}, {"ack":True,"source_session_id":"unit-source"},
    {"ack":1.0,"source_session_id":"unit-source"}, {"source_session_id":"unit-source"}, [],
])
def test_cache_unit_wrong_ack_closes_and_releases_subscription(context,ack):
    client,runtime,camera = context
    cache = ControlledLatestCache(camera)
    cache.publish(1)
    runtime.engines[camera["id"]] = cache
    with client.websocket_connect(route(camera),headers={"origin":"http://testserver"}) as ws:
        decode_packet(receive_message(ws))
        ws.send_json(ack)
        message = receive_message(ws,.8)
        assert message and message["type"] == "websocket.close" and message["code"] == 4400
        wait_until(lambda:runtime.camera_subscribers[camera["id"]] == 0)
    assert set(business_counts(runtime).values()) == {0}


def test_cache_unit_no_ack_does_not_send_backlog_or_repeat(context):
    client,runtime,camera = context
    cache = ControlledLatestCache(camera)
    cache.publish(1)
    runtime.engines[camera["id"]] = cache
    with client.websocket_connect(route(camera),headers={"origin":"http://testserver"}) as ws:
        decode_packet(receive_message(ws))
        cache.publish(5)
        # The next actual protocol message must be timeout close, never frame5
        # or duplicate frame1 while the first frame remains unacknowledged.
        message = receive_message(ws,3.2)
        assert message and message["type"] == "websocket.close"
        wait_until(lambda:runtime.camera_subscribers[camera["id"]] == 0)


def test_cache_unit_reconnect_sequence_is_bound_to_new_source_session(context):
    client,runtime,camera = context
    cache = ControlledLatestCache(camera)
    cache.publish(1,"source-a")
    runtime.engines[camera["id"]] = cache
    with client.websocket_connect(route(camera),headers={"origin":"http://testserver"}) as ws:
        first,_,_ = decode_packet(receive_message(ws))
        cache.publish(1,"source-b",value=190)
        ws.send_json({"ack":first["source_frame"],"source_session_id":first["source_session_id"]})
        second,_,decoded = decode_packet(receive_message(ws))
        assert second["source_frame"] == 1 and second["source_session_id"] == "source-b"
        assert decoded.mean() == pytest.approx(190,abs=1)
        ws.close()
        wait_until(lambda:runtime.camera_subscribers[camera["id"]] == 0)


def test_cache_unit_invalidated_cache_withdraws_old_frame(context):
    client,runtime,camera = context
    cache = ControlledLatestCache(camera)
    cache.publish(1)
    runtime.engines[camera["id"]] = cache
    with client.websocket_connect(route(camera),headers={"origin":"http://testserver"}) as ws:
        decode_packet(receive_message(ws))
        cache.clear()
        ws.send_json({"ack":1,"source_session_id":"unit-source"})
        message = receive_message(ws)
        assert json.loads(message["text"])["type"] == "unavailable"
        assert receive_message(ws,.1) is None
        cache.publish(4)
        metadata,_,_ = decode_packet(receive_message(ws))
        assert metadata["source_frame"] == 4
        ws.close()
        wait_until(lambda:runtime.camera_subscribers[camera["id"]] == 0)


def test_actual_file_pipeline_real_engine_streams_source_bound_jpeg_and_zero_events(context,tmp_path):
    client,runtime,_ = context
    source = tmp_path/"synthetic-gray-sequence.avi"
    writer = cv2.VideoWriter(str(source),cv2.VideoWriter_fourcc(*"MJPG"),20,(256,192))
    assert writer.isOpened()
    try:
        for index in range(120):
            frame = np.full((192,256,3),40,np.uint8)
            # Independent decodable frame identity survives JPEG compression.
            # This is a synthetic video counter, not a proposed object box.
            for bit in range(8):
                left = 16+28*bit
                cv2.rectangle(frame,(left,96),(left+18,160),(255,255,255) if index & (1<<bit) else (0,0,0),-1)
            writer.write(frame)
    finally:
        writer.release()
    settings = client.patch("/api/settings",json={"detection_mode":"aruco","hand_detection_enabled":False,
        "phone_shape_enabled":False,"show_hands":False})
    assert settings.status_code == 200,settings.text
    upload = client.post("/api/cameras/upload-video",files={"file":(source.name,source.read_bytes(),"video/x-msvideo")})
    assert upload.status_code == 200,upload.text
    response = client.post("/api/cameras",json={"name":"Actual generated video TEST","source_type":"video",
        "source":upload.json()["source"],"config":{"loop":False},"inference_fps":5})
    assert response.status_code == 200,response.text
    camera = response.json()
    result = {"test_class":"actual_file_pipeline","runtime_mode":"TEST","synthetic_source":True,
        "physical_camera":False,"http_mock":False,"engine_mock":False,"frame_cache_mock":False,
        "source_sha256":hashlib.sha256(source.read_bytes()).hexdigest(),"source_frames":120,
        "detection_mode":"aruco_no_registered_items","frames":[],"passed":False,
        "source_comparison":"rows48..end exclude real preview branding in rows0..30; independent 8bit frame counter must match"}
    engine = None
    try:
        assert client.post(f"/api/cameras/{camera['id']}/start").status_code == 200
        engine = runtime.engines[camera["id"]]
        capture = engine.capture
        wait_until(lambda:engine.tracking_snapshot() is not None,5)
        with client.websocket_connect(route(camera),headers={"origin":"http://testserver"}) as ws:
            for _ in range(3):
                metadata,jpeg,frame = decode_packet(receive_message(ws))
                assert metadata["runtime_mode"] == "TEST" and metadata["source_type"] == "video_file" and metadata["is_simulated"] is True
                assert metadata["camera_id"] == camera["id"] and metadata["display_only"] is True
                assert metadata["coordinate_space"] == "source_normalized"
                assert not metadata["candidates"]
                assert metadata["model"]["candidate_backend"] == "aruco"
                independent = cv2.VideoCapture(str(source))
                try:
                    assert independent.isOpened() and independent.set(cv2.CAP_PROP_POS_FRAMES,metadata["source_frame"]-1)
                    ok,original = independent.read()
                    assert ok
                finally:
                    independent.release()
                # Preview legitimately draws a source-status banner at the top.
                # Compare the unaffected picture and independently decode its
                # actual frame counter, not merely approximate whole-frame MAE.
                error = float(np.abs(frame[48:].astype(float)-original[48:].astype(float)).mean())
                assert error <= 1,"Transported JPEG does not belong to its declared source frame"
                decoded_index = sum((1<<bit) for bit in range(8) if np.median(frame[108:148,20+28*bit:30+28*bit]) > 127)
                assert decoded_index == metadata["source_frame"]-1,"Header and JPEG frame counter disagree"
                if result["frames"]:
                    assert metadata["source_frame"] > result["frames"][-1]["source_frame"]
                    assert metadata["source_session_id"] == result["frames"][-1]["source_session_id"]
                result["frames"].append({"source_frame":metadata["source_frame"],"source_session_id":metadata["source_session_id"],
                    "source_timestamp":metadata["source_timestamp"],"jpeg_sha256":hashlib.sha256(jpeg).hexdigest(),
                    "source_pixel_mae":error,"decoded_source_index":decoded_index})
                ws.send_json({"ack":metadata["source_frame"],"source_session_id":metadata["source_session_id"]})
            ws.close()
            wait_until(lambda:runtime.camera_subscribers[camera["id"]] == 0)
        assert runtime.engines[camera["id"]] is engine and engine.capture is capture
        assert set(business_counts(runtime).values()) == {0}
        result["database_counts"] = business_counts(runtime)
        result["passed"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if engine is not None:
            assert client.post(f"/api/cameras/{camera['id']}/stop").status_code == 200
            assert engine.capture.thread is None and engine._thread is None and engine._preview_thread is None
        result["workers_stopped"] = engine is None or (engine.capture.thread is None and engine._thread is None and engine._preview_thread is None)
        (tmp_path/"vision-ws-file-result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
