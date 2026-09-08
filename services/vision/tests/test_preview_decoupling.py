"""Real encoded file input and bounded caches, never a physical-camera claim."""
from __future__ import annotations

import hashlib
import time

import cv2
import numpy as np
import pytest

from services.vision.capture import FramePacket, LatestFrameSlot
from services.vision.camera_sources.opencv_stream import WebcamSource
from services.vision.engine import VisionEngine


def make_video(path, frames=180, fps=30, width=640, height=360):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (width, height))
    assert writer.isOpened(), "The test requires a real decodable MJPEG file"
    try:
        for sequence in range(frames):
            frame = np.full((height, width, 3), (sequence % 180 + 40, 120, 180), np.uint8)
            cv2.putText(frame, f"TEST VIDEO {sequence:04d}", (70, 185), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (20, 20, 20), 2)
            writer.write(frame)
    finally:
        writer.release()


def engine_for(path, media_root, events, inference_fps=5):
    return VisionEngine(
        {"id": "preview-file", "source_type": "video", "source": str(path),
         "config": {"loop": False, "realtime": True}, "inference_fps": inference_fps},
        [], [], {"runtime_mode": "TEST", "source_type": "video_file", "show_hands": False,
                 "data_dir": str(media_root), "preview_fps": 30},
        lambda *args: events.append(args),
    )


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "Condition did not become true before the bounded deadline"


def test_latest_preview_slot_is_one_frame_and_non_consuming():
    slot = LatestFrameSlot()
    for sequence in range(1, 101):
        slot.publish(FramePacket(np.full((8, 8, 3), sequence, np.uint8), sequence, sequence, sequence, "session-a"))
    revision, packet = slot.latest(timeout=0)
    assert revision == packet.sequence == 100
    assert slot.size == 1
    assert slot.latest(timeout=0)[1] is packet
    assert slot.latest(revision, timeout=0) is None
    slot.clear()
    assert slot.size == 0 and slot.latest(timeout=0) is None
    slot.publish(FramePacket(np.zeros((8, 8, 3), np.uint8), 101, 101, 1, "session-b"))
    next_revision, next_packet = slot.latest(revision, timeout=0)
    assert next_revision > revision and next_packet.source_session_id == "session-b"


@pytest.mark.parametrize("inference_delay", [0.0, 0.35], ids=["five-fps-inference", "slow-inference"])
def test_real_thirty_fps_file_preview_remains_above_twenty_with_independent_inference(tmp_path, monkeypatch, inference_delay):
    source_path = tmp_path / "thirty-fps.avi"
    make_video(source_path, width=1280, height=720)
    events = []
    engine = engine_for(source_path, tmp_path / "media", events)
    original_process = engine._process
    if inference_delay:
        def slow_process(packet):
            time.sleep(inference_delay)
            return original_process(packet)
        monkeypatch.setattr(engine, "_process", slow_process)
    assert engine.start(), engine.health()
    try:
        wait_until(lambda: engine.get_preview_snapshot() is not None and engine.health()["processed_frames"] > 0)
        preview_thread = engine._preview_thread
        capture_thread = engine.capture.thread
        assert engine.start() and engine._preview_thread is preview_thread and engine.capture.thread is capture_thread
        started = time.monotonic()
        snapshots = {}
        while time.monotonic() - started < 2.2:
            snapshot = engine.get_preview_snapshot()
            if snapshot:
                snapshots[snapshot["sequence"]] = snapshot
            time.sleep(0.004)
        elapsed = time.monotonic() - started
        health = engine.health()
        print({"input": "encoded_test_video_not_physical_camera", "inference_delay_seconds": inference_delay,
               "width": 1280, "height": 720,
               "observed_unique_preview_fps": round(len(snapshots) / elapsed, 3),
               "capture_fps": health["capture_fps"], "preview_fps": health["preview_fps"],
               "inference_fps": health["inference_fps"], "unique_frames": len(snapshots)})
        assert len(snapshots) / elapsed >= 20, (len(snapshots), elapsed, health)
        assert 20 <= health["preview_fps"] <= 35, health
        assert 20 <= health["capture_fps"] <= 35, health
        assert 0 < health["inference_fps"] < 10, health
        assert health["preview_target_fps"] == 30 and health["inference_target_fps"] == 5
        assert health["preview_buffer_frames"] == 1 and health["queue_size"] <= 2
        ordered = list(snapshots.values())
        assert all(left["source_frame_sequence"] < right["source_frame_sequence"] for left, right in zip(ordered, ordered[1:]))
        assert len({value["source_session_id"] for value in ordered}) == 1
        assert all(value["source_timestamp"] > 1_000_000_000 for value in ordered)
        assert all(started - 0.2 <= value["created_at"] <= started + elapsed for value in ordered)
        assert len({hashlib.sha256(value["jpeg"]).hexdigest() for value in ordered}) == len(ordered)
        decoded = cv2.imdecode(np.frombuffer(ordered[-1]["jpeg"], np.uint8), cv2.IMREAD_COLOR)
        assert decoded is not None and decoded.shape[:2] == (720, 1280)
        assert events == []
    finally:
        engine.stop()
    assert engine.get_preview_snapshot() is None
    assert engine.get_jpeg() is None
    stopped = engine.health()
    assert stopped["preview_thread_alive"] is False and stopped["capture_thread_alive"] is False
    assert stopped["preview_fps"] == stopped["capture_fps"] == stopped["inference_fps"] == 0
    assert stopped["preview_buffer_frames"] == 0
    assert not list((tmp_path / "media").rglob("*.jpg"))
    assert not list((tmp_path / "media").rglob("*.mp4"))


def test_file_ends_without_reencoding_old_frames_or_retaining_nonzero_fps(tmp_path):
    source_path = tmp_path / "short.avi"
    make_video(source_path, frames=15)
    events = []
    engine = engine_for(source_path, tmp_path / "media", events)
    assert engine.start()
    try:
        wait_until(lambda: engine.source.health_check()["status"] == "ended")
        wait_until(lambda: engine.get_preview_snapshot() is None, timeout=3)
        # A final genuine packet may already be encoding when EOF is observed.
        # Once freshness expires, no new encodes may replay that old packet.
        last_sequence = engine.health()["preview_sequence"]
        time.sleep(0.15)
        health = engine.health()
        assert health["preview_sequence"] == last_sequence
        assert health["preview_fps"] == health["capture_fps"] == health["inference_fps"] == 0
        assert events == []
    finally:
        engine.stop()


def test_preview_encoding_failure_does_not_stop_inference_and_can_recover(tmp_path, monkeypatch):
    source_path = tmp_path / "recover.avi"
    make_video(source_path)
    events = []
    engine = engine_for(source_path, tmp_path / "media", events)
    original_encode = cv2.imencode

    def failed_encode(*args, **kwargs):
        raise RuntimeError("controlled preview encoder failure")

    monkeypatch.setattr(cv2, "imencode", failed_encode)
    assert engine.start()
    try:
        wait_until(lambda: engine.health()["processed_frames"] >= 3)
        assert engine.health()["preview_error"]
        assert engine.health()["preview_fps"] == 0
        assert engine.get_jpeg() is None and events == []
        monkeypatch.setattr(cv2, "imencode", original_encode)
        wait_until(lambda: engine.get_preview_snapshot() is not None)
        assert engine.health()["preview_error"] is None
    finally:
        engine.stop()


def test_capture_request_and_driver_report_are_not_reported_as_measured_fps():
    class NegotiationCapture:
        calls = []

        def set(self, prop, value):
            self.calls.append((prop, value))
            return prop != cv2.CAP_PROP_FPS

        def get(self, prop):
            return 7.5 if prop == cv2.CAP_PROP_FPS else cv2.VideoWriter_fourcc(*"MJPG") if prop == cv2.CAP_PROP_FOURCC else 0

    capture = NegotiationCapture()
    source = WebcamSource("60", {"mjpeg": True, "capture_fps": 30})
    source._configure_capture(capture)
    assert capture.calls[0][0] == cv2.CAP_PROP_FOURCC
    assert (cv2.CAP_PROP_FPS, 30) in capture.calls
    health = source.health_check()
    assert health["requested_capture_fps"] == 30
    assert health["reported_capture_fps"] == 7.5
    assert health["capture_fps_request_applied"] is False
    assert health["reported_fourcc"] == "MJPG"
    assert health["fps"] == 0
