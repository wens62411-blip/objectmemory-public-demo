from __future__ import annotations

import threading
import time
import uuid
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


class CameraBusyError(RuntimeError):
    """Raised when another capture worker owns the same physical camera."""

    def __init__(self, key: str, current_owner: str) -> None:
        self.key = key
        self.current_owner = current_owner
        super().__init__(f"摄像头正被 {current_owner} 使用，请先停止该任务后重试。")


@dataclass(slots=True)
class CameraLease:
    key: str
    owner: str
    token: str


@dataclass(slots=True)
class _Ownership:
    key: str
    owner: str
    token: str
    thread_id: int
    thread: weakref.ReferenceType[threading.Thread]
    started_monotonic: float
    started_at: str
    subscribers: int
    acquisition_count: int


class CameraManager:
    """Coordinates exclusive access to physical cameras inside this process.

    OpenCV camera backends are not reliably shareable on Windows.  All webcam
    adapters acquire a lease before constructing ``VideoCapture`` and release
    it after ``VideoCapture.release``.  Dead worker leases are reclaimed while
    acquiring or inspecting state, so a failed daemon thread cannot block the
    camera until the API process restarts.
    """

    _instance: "CameraManager | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._owners: dict[str, _Ownership] = {}
        self._acquisition_counts: dict[str, int] = {}

    @classmethod
    def instance(cls) -> "CameraManager":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @staticmethod
    def webcam_key(device_index: int) -> str:
        return f"webcam:{int(device_index)}"

    def _reap_dead_locked(self) -> None:
        dead = []
        for key, ownership in self._owners.items():
            thread = ownership.thread()
            if thread is None or not thread.is_alive():
                dead.append(key)
        for key in dead:
            self._owners.pop(key, None)
        if dead:
            self._condition.notify_all()

    def acquire(self, key: str, owner: str, timeout: float = 2.0) -> CameraLease:
        key = str(key)
        owner = str(owner or "未命名采集任务")
        deadline = time.monotonic() + max(0.0, float(timeout))
        current_thread = threading.current_thread()
        with self._condition:
            while True:
                self._reap_dead_locked()
                occupied = self._owners.get(key)
                if occupied is None:
                    token = uuid.uuid4().hex
                    count = self._acquisition_counts.get(key, 0) + 1
                    self._acquisition_counts[key] = count
                    self._owners[key] = _Ownership(
                        key=key,
                        owner=owner,
                        token=token,
                        thread_id=threading.get_ident(),
                        thread=weakref.ref(current_thread),
                        started_monotonic=time.monotonic(),
                        started_at=datetime.now(timezone.utc).isoformat(),
                        subscribers=0,
                        acquisition_count=count,
                    )
                    return CameraLease(key, owner, token)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CameraBusyError(key, occupied.owner)
                self._condition.wait(min(remaining, 0.1))

    def release(self, lease: CameraLease | None) -> bool:
        if lease is None:
            return False
        with self._condition:
            occupied = self._owners.get(lease.key)
            if occupied is None or occupied.token != lease.token:
                return False
            self._owners.pop(lease.key, None)
            self._condition.notify_all()
            return True

    def update_subscribers(self, key: str, change: int) -> int:
        with self._condition:
            self._reap_dead_locked()
            occupied = self._owners.get(str(key))
            if occupied is None:
                return 0
            occupied.subscribers = max(0, occupied.subscribers + int(change))
            return occupied.subscribers

    def describe(self, key: str) -> dict[str, Any] | None:
        with self._condition:
            self._reap_dead_locked()
            occupied = self._owners.get(str(key))
            if occupied is None:
                return None
            return self._serialize(occupied)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._condition:
            self._reap_dead_locked()
            return [self._serialize(value) for value in self._owners.values()]

    @staticmethod
    def _serialize(value: _Ownership) -> dict[str, Any]:
        return {
            "key": value.key,
            "owner": value.owner,
            "thread_id": value.thread_id,
            "started_at": value.started_at,
            "held_seconds": round(max(0.0, time.monotonic() - value.started_monotonic), 3),
            "subscribers": value.subscribers,
            "acquisition_count": value.acquisition_count,
        }


camera_manager = CameraManager.instance()


__all__ = ["CameraBusyError", "CameraLease", "CameraManager", "camera_manager"]
