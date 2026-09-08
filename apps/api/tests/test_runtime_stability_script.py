"""Regression tests for the formal runtime-stability evidence gate."""
from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("objectmemory_runtime_stability", ROOT / "scripts" / "runtime-stability.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize(
    ("camera", "expected"),
    [
        ({"status": "connecting", "status_code": "STREAMING", "capture_thread_alive": True, "source_frame_sequence": 1, "source_read_failures": 0, "error": ""}, True),
        ({"status": "ready", "status_code": "STREAMING", "capture_thread_alive": True, "source_frame_sequence": 100, "source_read_failures": 0, "error": ""}, True),
        ({"status": "offline", "status_code": "OFFLINE", "capture_thread_alive": False, "source_frame_sequence": 100, "source_read_failures": 0, "error": ""}, False),
        ({"status": "ready", "status_code": "STREAMING", "capture_thread_alive": False, "source_frame_sequence": 100, "source_read_failures": 0, "error": ""}, False),
        ({"status": "ready", "status_code": "STREAMING", "capture_thread_alive": True, "source_frame_sequence": 0, "source_read_failures": 0, "error": ""}, False),
        ({"status": "ready", "status_code": "STREAMING", "capture_thread_alive": True, "source_frame_sequence": 100, "source_read_failures": 1, "error": ""}, False),
        ({"status": "ready", "status_code": "STREAMING", "capture_thread_alive": True, "source_frame_sequence": 100, "source_read_failures": 0, "error": "camera failed"}, False),
    ],
)
def test_camera_sample_continuity_uses_capture_evidence(camera, expected):
    assert MODULE.camera_sample_is_continuous({"camera": camera}) is expected


def diagnostic_client(*, fail_frame=False):
    """Transport fixture only; does not establish a real camera test result."""
    frame=np.full((200,200,3),255,np.uint8)
    marker=cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50),49,100)
    frame[50:150,50:150]=cv2.cvtColor(marker,cv2.COLOR_GRAY2BGR)
    encoded,jpeg=cv2.imencode(".jpg",frame)
    assert encoded
    calls=[]

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/frame"):
            return httpx.Response(503,json={"detail":"no frame"}) if fail_frame else httpx.Response(200,content=jpeg.tobytes(),headers={"X-ObjectMemory-Runtime-Mode":"REAL","X-ObjectMemory-Source-Type":"opencv_camera","X-ObjectMemory-Is-Simulated":"false"})
        if request.url.path=="/api/acceptance/marker-status":
            return httpx.Response(200,json={"recognized":True,"aruco_id":49,"frame":100,"source_session_id":"observed-session","last_observed_at":"2026-09-05T00:00:00Z"})
        return httpx.Response(200,json={"id":"camera","health":{"source_frame_sequence":101,"source_session_id":"observed-session","last_frame_at":"2026-09-05T00:00:01Z"}})

    return httpx.Client(base_url="http://diagnostic-fixture.invalid",transport=httpx.MockTransport(handler)),calls,jpeg.tobytes(),frame


def test_anomaly_collector_saves_no_files_and_makes_no_calls_without_trigger(tmp_path):
    client,calls,_jpeg,_frame=diagnostic_client()
    with client:
        collector=MODULE.AnomalyEvidenceCollector(client,"camera","registered-item",tmp_path/"evidence")
        for _ in range(10):collector.observe_websocket({"tracks":[]})
        assert calls==[]
        assert collector.snapshot()["triggered"] is False
        assert not (tmp_path/"evidence").exists()


def test_anomaly_collector_is_bounded_and_never_claims_triggering_frame_identity(tmp_path):
    client,calls,jpeg,_frame=diagnostic_client()
    evidence=tmp_path/"persistent-evidence"
    with client:
        collector=MODULE.AnomalyEvidenceCollector(client,"camera","registered-item",evidence)
        payload={"tracks":[{"item_id":"registered-item","source_session_id":"observed-session","last_seen_at":"2026-09-05T00:00:00Z"}]}
        for _ in range(10):collector.observe_websocket(payload)
        snapshot=collector.snapshot()
    assert snapshot["attempts"]==snapshot["saved_frames"]==3
    assert calls.count("/api/cameras/camera/frame")==3
    assert len(list(evidence.glob("*.jpg")))==len(list(evidence.glob("*.json")))==3
    for record in snapshot["records"]:
        assert record["alignment"]=="not_same_frame" and record["not_same_frame"] is True
        assert record["frame"]["source_frame"] is None and record["frame"]["source_session_id"] is None and record["frame"]["source_timestamp"] is None
        assert record["marker_status"]["frame"]==100
        assert record["camera_health_before"]["source_frame_sequence"]==record["camera_health_after"]["source_frame_sequence"]==101
        assert Path(record["frame"]["path"]).read_bytes()==jpeg
        assert record["frame"]["sha256"]==hashlib.sha256(jpeg).hexdigest()
        assert record["frame"]["independent_jpeg_marker_detection"][0]["aruco_id"]==49
        assert json.loads(Path(record["diagnostic_path"]).read_text(encoding="utf-8"))["not_same_frame"] is True


def test_failed_frame_api_retains_bounded_diagnostic_json(tmp_path):
    client,_calls,_jpeg,_frame=diagnostic_client(fail_frame=True)
    with client:
        collector=MODULE.AnomalyEvidenceCollector(client,"camera","registered-item",tmp_path/"evidence",frame_limit=2)
        for _ in range(10):collector.observe_websocket({"tracks":[{"item_id":"registered-item"}]})
        snapshot=collector.snapshot()
    assert snapshot["attempts"]==2 and snapshot["saved_frames"]==0
    assert len(list((tmp_path/"evidence").glob("*.json")))==2
    assert not list((tmp_path/"evidence").glob("*.jpg"))
    assert all(record["errors"] and record["not_same_frame"] for record in snapshot["records"])


def test_sampled_tag_49_can_trigger_using_exact_existing_jpeg_bytes(tmp_path):
    client,calls,jpeg,frame=diagnostic_client()
    with client:
        collector=MODULE.AnomalyEvidenceCollector(client,"camera","registered-item",tmp_path/"evidence",frame_limit=1)
        response=httpx.Response(200,content=jpeg,request=httpx.Request("GET","http://diagnostic-fixture.invalid/frame"))
        collector.observe_sample(response,frame,{"captured_at":"2026-09-05T00:00:02Z"})
        snapshot=collector.snapshot()
    assert snapshot["first_trigger"]["kind"]=="registered_tag_49_in_sampled_jpeg"
    assert snapshot["saved_frames"]==1
    assert "/api/cameras/camera/frame" not in calls
    assert snapshot["records"][0]["frame"]["was_existing_sample_response"] is True
    assert snapshot["same_inference_frame_claimed"] is False
