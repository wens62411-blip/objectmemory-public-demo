"""Cross-process runtime-mode leases for one ObjectMemory data root.

The lock file is deliberately persistent: process liveness is represented by
the operating-system lock, never by the presence or contents of the file.  The
kernel therefore releases a lease even when a process crashes or is killed.
"""
from __future__ import annotations

import json
import os
import threading
import weakref
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO


class RuntimeModeBusy(RuntimeError):
    """Raised when another app instance owns the requested mode lease."""


class RuntimeModeLease:
    _registry_lock = threading.RLock()
    # A registry entry must not itself keep an abandoned, never-started app
    # alive.  The lease finalizer owns the descriptor until that app is either
    # explicitly closed or garbage-collected.
    _registry: weakref.WeakValueDictionary[str, "RuntimeModeLease"] = weakref.WeakValueDictionary()

    def __init__(self, data_root: Path, mode: str) -> None:
        self.data_root = Path(data_root).resolve()
        self.mode = str(mode).strip().upper()
        if self.mode not in {"REAL", "DEMO", "TEST"}:
            raise ValueError("运行锁模式必须是 REAL、DEMO 或 TEST。")
        self.path = (self.data_root / ".runtime-locks" / f"{self.mode.lower()}.lock").resolve()
        if not self.path.is_relative_to(self.data_root):
            raise ValueError("运行锁必须位于数据根目录内。")
        self._finalizer_state: dict[str, BinaryIO | None] = {"handle": None}
        self._finalizer = weakref.finalize(self, self._finalize_handle, self._finalizer_state)

    @property
    def held(self) -> bool:
        handle = self._finalizer_state.get("handle")
        return handle is not None and not handle.closed

    @property
    def _registry_key(self) -> str:
        return os.path.normcase(str(self.path))

    @staticmethod
    def _lock(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _finalize_handle(state: dict[str, BinaryIO | None]) -> None:
        handle = state.get("handle")
        state["handle"] = None
        if handle is None:
            return
        try:
            if not handle.closed:
                RuntimeModeLease._unlock(handle)
        except BaseException:
            pass
        try:
            handle.close()
        except BaseException:
            pass

    def acquire(self) -> "RuntimeModeLease":
        with self._registry_lock:
            if self.held:
                return self
            if self._registry_key in self._registry:
                raise RuntimeModeBusy(
                    f"{self.mode} 模式已有活动进程使用当前数据目录；请先停止该进程再重试。"
                )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=True)
            handle = self.path.open("r+b", buffering=0)
            try:
                if self.path.stat().st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                self._lock(handle)
            except OSError as exc:
                handle.close()
                raise RuntimeModeBusy(
                    f"{self.mode} 模式已有活动进程使用当前数据目录；请先停止该进程再重试。"
                ) from exc
            self._finalizer_state["handle"] = handle
            self._registry[self._registry_key] = self
            try:
                metadata = json.dumps(
                    {
                        "mode": self.mode,
                        "pid": os.getpid(),
                        "acquired_at": datetime.now(timezone.utc).isoformat(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                # Byte zero remains the locked sentinel; metadata is diagnostic only.
                handle.seek(1)
                handle.truncate()
                handle.write(metadata)
                handle.flush()
            except OSError:
                self.release()
                raise
            return self

    def release(self) -> None:
        with self._registry_lock:
            handle = self._finalizer_state.get("handle")
            if handle is None:
                return
            self._finalizer_state["handle"] = None
            try:
                if not handle.closed:
                    self._unlock(handle)
            except OSError:
                # Closing the descriptor still releases an OS-owned lock.
                pass
            finally:
                try:
                    handle.close()
                finally:
                    if self._registry.get(self._registry_key) is self:
                        self._registry.pop(self._registry_key, None)

    close = release

    def __enter__(self) -> "RuntimeModeLease":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
