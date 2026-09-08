from __future__ import annotations

import threading

import numpy as np
import pytest

from services.vision.camera_manager import CameraBusyError, CameraManager, camera_manager
from services.vision.camera_sources.opencv_stream import WebcamSource


class _FakeCapture:
    def __init__(self, opened: bool = True, readable: bool = True) -> None:
        self.opened = opened
        self.readable = readable
        self.release_count = 0
        self.width = 1280
        self.height = 720
        self.read_count = 0

    def isOpened(self):
        return self.opened and self.release_count == 0

    def release(self):
        self.release_count += 1

    def set(self, prop, value):
        if prop == 3:
            self.width = int(value)
        elif prop == 4:
            self.height = int(value)
        return True

    def get(self, prop):
        if prop == 3:
            return self.width
        if prop == 4:
            return self.height
        if prop == 5:
            return 30
        return 0

    def read(self):
        self.read_count += 1
        if not self.readable:
            return False, None
        return True, np.full((self.height, self.width, 3), self.read_count % 255, dtype=np.uint8)

    def getBackendName(self):
        return "DSHOW"


def test_camera_manager_enforces_exclusive_owner_and_exposes_diagnostics():
    manager = CameraManager()
    lease = manager.acquire("webcam:0", "camera:first", timeout=0)
    state = manager.describe("webcam:0")
    assert state and state["owner"] == "camera:first"
    assert state["thread_id"] == threading.get_ident()
    assert state["subscribers"] == 0
    assert state["acquisition_count"] == 1
    with pytest.raises(CameraBusyError) as caught:
        manager.acquire("webcam:0", "camera:second", timeout=0)
    assert caught.value.current_owner == "camera:first"
    assert manager.release(lease) is True
    assert manager.describe("webcam:0") is None


def test_camera_manager_reclaims_lease_left_by_dead_worker_thread():
    manager = CameraManager()
    leases = []

    def abandoned_owner():
        leases.append(manager.acquire("webcam:7", "abandoned", timeout=0))

    thread = threading.Thread(target=abandoned_owner)
    thread.start()
    thread.join()
    replacement = manager.acquire("webcam:7", "replacement", timeout=0.1)
    assert manager.describe("webcam:7")["owner"] == "replacement"
    assert manager.release(replacement)


def test_webcam_requires_real_warmup_frames_and_releases_capture(monkeypatch):
    fake = _FakeCapture()
    source = WebcamSource(
        "61",
        {"owner": "camera:test-warmup", "warmup_frames": 4, "warmup_seconds": 1, "lock_timeout_seconds": 0},
    )
    monkeypatch.setattr(source, "_backend_candidates", lambda _index: [("DSHOW", 700)])
    monkeypatch.setattr(source, "_open_capture_with_backend", lambda _index, _backend: fake)
    assert source.connect() is True
    health = source.health_check()
    assert health["status_code"] == "READY"
    assert health["backend"] == "DSHOW"
    assert health["current_owner"] == "camera:test-warmup"
    assert fake.read_count >= 4
    frame = source.read_frame()
    assert frame is not None and frame.shape[:2] == (720, 1280)
    assert source.health_check()["status_code"] == "STREAMING"
    source.disconnect()
    assert fake.release_count == 1
    assert camera_manager.describe(camera_manager.webcam_key(61)) is None


def test_webcam_reports_busy_without_constructing_second_capture(monkeypatch):
    existing = camera_manager.acquire(camera_manager.webcam_key(62), "camera:already-running", timeout=0)
    source = WebcamSource("62", {"owner": "camera:new", "lock_timeout_seconds": 0})
    opened = []
    monkeypatch.setattr(source, "_open_capture_with_backend", lambda *_args: opened.append(True))
    try:
        assert source.connect() is False
        health = source.health_check()
        assert health["status_code"] == "BUSY"
        assert "already-running" in health["error"]
        assert opened == []
    finally:
        camera_manager.release(existing)


def test_webcam_read_failure_is_recoverable_and_has_specific_status(monkeypatch):
    fake = _FakeCapture(readable=True)
    source = WebcamSource("63", {"owner": "camera:failure", "warmup_frames": 2, "warmup_seconds": 1})
    monkeypatch.setattr(source, "_backend_candidates", lambda _index: [("DSHOW", 700)])
    monkeypatch.setattr(source, "_open_capture_with_backend", lambda *_args: fake)
    assert source.connect()
    source._primed_frame = None
    fake.readable = False
    assert source.read_frame() is None
    health = source.health_check()
    assert health["status_code"] == "FRAME_READ_FAILED"
    assert health["consecutive_failures"] == 1
    fake.readable = True
    assert source.read_frame() is not None
    assert source.health_check()["status_code"] == "STREAMING"
    source.disconnect()
