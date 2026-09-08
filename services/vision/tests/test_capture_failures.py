"""Fault-injected adapter tests; no physical camera or real event source is opened."""
from __future__ import annotations

import threading
import time

import cv2
import numpy as np
import pytest

from services.vision.camera_sources import CameraSourceAdapter
from services.vision.capture import CaptureWorker


class _FailingReadSource(CameraSourceAdapter):
    source_type = "test_read_fault"

    def __init__(self, fault="system_memory", *, fail_disconnect=False):
        super().__init__(None, {"connect_wait_seconds": 1})
        self.fault = fault
        self.fail_disconnect = fail_disconnect
        self.allow_failure = threading.Event()
        self.waiting_to_fail = threading.Event()
        self.connect_count = 0
        self.disconnect_count = 0
        self.reads = 0
        self.fail_connect = False

    def connect(self):
        self.connect_count += 1
        self.reads = 0
        if self.fail_connect:
            raise MemoryError("test connection allocation failed")
        self._set_state("READY", "online")
        return True

    def read_frame(self):
        self.reads += 1
        if self.reads == 1:
            frame = np.full((16, 24, 3), self.connect_count, dtype=np.uint8)
            self._record_frame(frame)
            return frame
        self.waiting_to_fail.set()
        self.allow_failure.wait(timeout=5)
        if self.fault == "system_memory":
            try:
                raise MemoryError("test image allocation failed")
            except MemoryError as exc:
                raise SystemError("cv2.read returned a result with an exception set") from exc
        if self.fault == "memory":
            raise MemoryError("test image allocation failed")
        if self.fault == "opencv":
            raise cv2.error("test native read failed")
        if self.fault == "secret":
            raise RuntimeError("read rtsp://viewer:private-password@127.0.0.1/frame?token=private-token failed")
        return None

    def disconnect(self):
        self.disconnect_count += 1
        self._set_state("STOPPED", "stopped")
        if self.fail_disconnect:
            raise RuntimeError("test release failed")


@pytest.mark.parametrize("fault,expected_type", [
    ("system_memory", "SystemError"), ("memory", "MemoryError"), ("opencv", "error"),
])
def test_read_exception_stops_cleanly_and_invalidates_all_cached_frames(fault, expected_type):
    source = _FailingReadSource(fault)
    worker = CaptureWorker(source)
    try:
        assert worker.start()
        assert source.waiting_to_fail.wait(timeout=1)
        assert worker.frames.queue.qsize() == 1
        assert worker.preview_frames.size == 1
        source.allow_failure.set()
        worker.thread.join(timeout=2)
        assert not worker.thread.is_alive()
        health = worker.health()
        assert worker.error and expected_type in worker.error
        if fault == "system_memory":
            assert "MemoryError" in worker.error
        assert health["status"] == "error" and health["status_code"] == "FRAME_READ_FAILED"
        assert health["error"] == worker.error and health["fps"] == 0
        assert health["capture_thread_alive"] is False
        assert health["queue_size"] == health["preview_buffer_frames"] == 0
        assert source.disconnect_count == 1
        assert source.connect_count == 1  # no blind native-read retry loop
        assert worker.stop_event.is_set()
    finally:
        source.allow_failure.set()
        worker.stop()


def test_read_failure_error_is_redacted_and_restart_begins_clean_source_session():
    source = _FailingReadSource("secret")
    worker = CaptureWorker(source)
    try:
        assert worker.start()
        assert source.waiting_to_fail.wait(timeout=1)
        old_session = worker.source_session_id
        source.allow_failure.set()
        worker.thread.join(timeout=2)
        assert worker.error and "RuntimeError" in worker.error
        assert "private-password" not in worker.error and "private-token" not in worker.error
        source.fault = None
        source.allow_failure.clear()
        source.waiting_to_fail.clear()
        assert worker.start()
        assert source.waiting_to_fail.wait(timeout=1)
        packet = worker.frames.get(timeout=1)
        assert packet.source_session_id != old_session
        assert packet.sequence == 1 and int(packet.frame[0, 0, 0]) == 2
        assert worker.error is None and worker.health()["error"] is None
        assert worker.health()["status"] == "online"
    finally:
        worker.stop_event.set()
        source.allow_failure.set()
        worker.stop()


def test_release_exception_does_not_skip_buffer_cleanup_or_hide_read_failure():
    source = _FailingReadSource(fail_disconnect=True)
    worker = CaptureWorker(source)
    try:
        assert worker.start()
        assert source.waiting_to_fail.wait(timeout=1)
        source.allow_failure.set()
        worker.thread.join(timeout=2)
        assert not worker.thread.is_alive()
        assert worker.frames.queue.qsize() == worker.preview_frames.size == 0
        assert worker.error and "MemoryError" in worker.error and "release" in worker.error
        assert worker.health()["status"] == "error"
        assert source.disconnect_count == 1
    finally:
        worker.stop()


def test_connect_exception_is_reported_without_unhandled_worker_error():
    source = _FailingReadSource()
    source.fail_connect = True
    worker = CaptureWorker(source)
    try:
        assert worker.start() is False
        worker.thread.join(timeout=2)
        assert worker.error and "MemoryError" in worker.error
        assert worker.health()["status"] == "error"
        assert worker.frames.queue.qsize() == worker.preview_frames.size == 0
        assert source.disconnect_count == 1
    finally:
        worker.stop()


def test_read_failure_clears_old_frames_before_blocking_native_release(monkeypatch):
    source = _FailingReadSource()
    releasing = threading.Event()
    release_allowed = threading.Event()
    original_disconnect = source.disconnect

    def blocked_release():
        releasing.set()
        release_allowed.wait(timeout=2)
        original_disconnect()

    monkeypatch.setattr(source, "disconnect", blocked_release)
    worker = CaptureWorker(source)
    try:
        assert worker.start()
        assert source.waiting_to_fail.wait(timeout=1)
        source.allow_failure.set()
        assert releasing.wait(timeout=1)
        assert worker.thread.is_alive()  # native cleanup is still in progress
        assert worker.health()["status"] == "error"
        assert worker.frames.queue.qsize() == worker.preview_frames.size == 0
    finally:
        release_allowed.set()
        worker.stop()


def test_engine_capture_fault_immediately_rejects_fresh_preview_and_recognition_cache(tmp_path, monkeypatch):
    from services.vision import engine as engine_module

    source = _FailingReadSource()
    # Only the adapter is a controlled source. The real worker, image encoding,
    # processing loop, snapshot getters and health aggregation execute normally.
    monkeypatch.setattr(engine_module, "create_camera_source", lambda _camera: source)
    events = []
    engine = engine_module.VisionEngine(
        {"id": "test-native-failure", "source_type": "video", "source": "not-opened-fixture.avi"},
        [], [], {"runtime_mode": "TEST", "source_type": "test_fixture", "show_hands": False,
                 "data_dir": str(tmp_path / "media")},
        lambda *args: events.append(args),
    )
    process_waiting = threading.Event()
    allow_process_return = threading.Event()
    original_process = engine._process

    def hold_completed_process(packet):
        # Real processing produces the caches before _run records completion.
        # Explicitly hold that scheduling window instead of relying on a race.
        original_process(packet)
        process_waiting.set()
        assert allow_process_return.wait(timeout=5), "Test did not release the completed-process barrier"

    monkeypatch.setattr(engine, "_process", hold_completed_process)
    try:
        assert engine.start()
        assert process_waiting.wait(timeout=2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if engine.get_preview_snapshot() is not None and engine.recognition_snapshot() is not None:
                break
            time.sleep(.01)
        assert engine.get_preview_snapshot() is not None
        assert engine.recognition_snapshot() is not None
        before_completion = engine.health()
        assert before_completion["processed_frames"] == 0
        assert list(engine._inference_times) == []
        assert before_completion["status"] == "stalled", before_completion
        allow_process_return.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and engine.health()["status"] != "ready":
            time.sleep(.01)
        assert engine.health()["status"] == "ready", engine.health()
        source.allow_failure.set()
        engine.capture.thread.join(timeout=1)
        assert not engine.capture.thread.is_alive()
        # Cached bytes are still younger than their normal 2/3-second TTLs:
        # their immediate refusal must be due to the native capture failure.
        assert time.monotonic() - engine._preview_snapshot["created_at"] < 2
        assert time.monotonic() - engine._recognition_snapshot["created_at"] < 3
        assert engine.get_preview_snapshot() is None and engine.get_jpeg() is None
        assert engine.recognition_snapshot() is None
        assert engine.health()["status"] == "error"
        assert "MemoryError" in engine.health()["error"]
        assert engine.health()["capture_fps"] == 0
        assert events == []
        assert list((tmp_path / "media").rglob("*.jpg")) == []
        assert list((tmp_path / "media").rglob("*.mp4")) == []
    finally:
        allow_process_return.set()
        source.allow_failure.set()
        engine.stop()
