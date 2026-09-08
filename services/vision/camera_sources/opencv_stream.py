from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .base import CameraSourceAdapter
from ..camera_manager import CameraBusyError, CameraLease, camera_manager


class OpenCvCaptureSource(CameraSourceAdapter):
    """Base for sources OpenCV can open (UVC, files, RTSP and MJPEG)."""

    def __init__(self, source: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(source, config)
        self.capture: cv2.VideoCapture | None = None

    def _capture_argument(self) -> Any:
        return self.source

    def _open_capture(self, argument: Any) -> cv2.VideoCapture:
        return cv2.VideoCapture(argument)

    def connect(self) -> bool:
        self.disconnect()
        self._set_state("STARTING", "connecting")
        with self._lock:
            self._frame_times.clear()
            self._health.fps = 0.0
        argument = self._capture_argument()
        try:
            capture = self._open_capture(argument)
            if not capture.isOpened():
                capture.release()
                self._record_error("无法打开视频源，请检查地址、权限或设备占用")
                return False
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self.capture = capture
            with self._lock:
                self._health.status = "online"
                self._health.status_code = "READY"
                self._health.error = None
                self._health.width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
                self._health.height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            return True
        except Exception as exc:  # hardware/network backends raise varied errors
            self._record_error(f"连接视频源失败：{exc}")
            return False

    def disconnect(self) -> None:
        capture, self.capture = self.capture, None
        if capture is not None:
            capture.release()
        with self._lock:
            if self._health.status != "error":
                self._health.status = "stopped"
                self._health.status_code = "STOPPED"

    def read_frame(self) -> np.ndarray | None:
        capture = self.capture
        if capture is None or not capture.isOpened():
            return None
        started = time.perf_counter()
        ok, frame = capture.read()
        if not ok or frame is None:
            with self._lock:
                self._health.dropped_frames += 1
                self._health.source_read_failures += 1
            return None
        self._record_frame(frame, started)
        return frame

    def get_metadata(self) -> dict[str, Any]:
        metadata = super().get_metadata()
        capture = self.capture
        if capture is not None:
            metadata.update(
                width=int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
                height=int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
                reported_fps=float(capture.get(cv2.CAP_PROP_FPS) or 0),
            )
        return metadata


class WebcamSource(OpenCvCaptureSource):
    source_type = "webcam"

    BACKENDS = {
        "DSHOW": getattr(cv2, "CAP_DSHOW", 700),
        "MSMF": getattr(cv2, "CAP_MSMF", 1400),
        "ANY": getattr(cv2, "CAP_ANY", 0),
    }

    def __init__(self, source: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(source, config)
        self._lease: CameraLease | None = None
        self._primed_frame: np.ndarray | None = None
        self._read_failures = 0
        self._backend_name: str | None = None
        self._requested_capture_fps = 30.0
        self._reported_capture_fps = 0.0
        self._capture_fps_request_applied = False
        self._reported_fourcc = ""

    def _capture_argument(self) -> int:
        try:
            return int(self.source)
        except (TypeError, ValueError):
            return int(self.config.get("index", 0))

    @classmethod
    def _backend_value(cls, value: Any) -> tuple[str, int] | None:
        if isinstance(value, int):
            for name, backend_id in cls.BACKENDS.items():
                if value == backend_id:
                    return name, backend_id
            return f"BACKEND_{value}", value
        name = str(value or "").upper().removeprefix("CAP_")
        if name in cls.BACKENDS:
            return name, cls.BACKENDS[name]
        return None

    def _diagnostic_backends(self, argument: int) -> list[tuple[str, int]]:
        value = self.config.get("diagnostic_report")
        path = Path(str(value)).expanduser() if value else Path(__file__).resolve().parents[3] / "data" / "diagnostics" / "camera-report.json"
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[3] / path
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        successful = []
        for result in report.get("results", []):
            if result.get("device_index") != argument or not result.get("frame_read_success"):
                continue
            backend = self._backend_value(result.get("backend"))
            if backend:
                successful.append((int(result.get("successful_frames") or 0), float(result.get("actual_fps") or 0), backend))
        successful.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return [row[2] for row in successful]

    def _diagnostic_context(self, argument: int) -> tuple[bool, set[int], bool]:
        value = self.config.get("diagnostic_report")
        path = Path(str(value)).expanduser() if value else Path(__file__).resolve().parents[3] / "data" / "diagnostics" / "camera-report.json"
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[3] / path
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError,ValueError,TypeError):
            return False,set(),False
        successful_indices={int(row["device_index"]) for row in report.get("results",[]) if row.get("frame_read_success") and str(row.get("device_index","")).isdigit()}
        return argument in successful_indices,successful_indices,bool(report.get("windows_devices"))

    def _backend_candidates(self, argument: int) -> list[tuple[str, int]]:
        if os.name != "nt":
            return [("ANY", cv2.CAP_ANY)]
        candidates: list[tuple[str, int]] = []
        configured = self._backend_value(self.config.get("backend"))
        if configured:
            candidates.append(configured)
        candidates.extend(self._diagnostic_backends(argument))
        candidates.extend([("DSHOW", cv2.CAP_DSHOW), ("MSMF", cv2.CAP_MSMF), ("ANY", cv2.CAP_ANY)])
        unique: list[tuple[str, int]] = []
        seen: set[int] = set()
        for candidate in candidates:
            if candidate[1] not in seen:
                seen.add(candidate[1])
                unique.append(candidate)
        return unique

    def _open_capture_with_backend(self, argument: int, backend_id: int) -> cv2.VideoCapture:
        return cv2.VideoCapture(argument, backend_id) if backend_id != cv2.CAP_ANY else cv2.VideoCapture(argument)

    def _configure_capture(self, capture: cv2.VideoCapture) -> None:
        width = int(self.config.get("width", 1280))
        height = int(self.config.get("height", 720))
        # Format/rate negotiation is a request, not proof of delivered FPS.
        # Keep MJPEG opt-in because not every UVC/backend combination supports it.
        if self.config.get("mjpeg", False):
            capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        requested = float(self.config.get("capture_fps", 30))
        self._requested_capture_fps = min(60.0, max(1.0, requested)) if np.isfinite(requested) else 30.0
        self._capture_fps_request_applied = bool(capture.set(cv2.CAP_PROP_FPS, self._requested_capture_fps))
        reported = float(capture.get(cv2.CAP_PROP_FPS) or 0)
        self._reported_capture_fps = reported if np.isfinite(reported) and reported > 0 else 0.0
        fourcc = int(capture.get(cv2.CAP_PROP_FOURCC) or 0)
        self._reported_fourcc = "".join(chr((fourcc >> (8 * offset)) & 0xFF) for offset in range(4)).rstrip("\x00")

    @staticmethod
    def _windows_permission_denied() -> bool:
        if os.name != "nt":
            return False
        try:
            import winreg

            path = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                value, _kind = winreg.QueryValueEx(key, "Value")
            return str(value).lower() == "deny"
        except (OSError, ImportError):
            return False

    def connect(self) -> bool:
        self.disconnect()
        argument = self._capture_argument()
        key = camera_manager.webcam_key(argument)
        owner = str(self.config.get("owner") or f"webcam:{argument}:{id(self)}")
        self._set_state("PROBING", "connecting")
        try:
            self._lease = camera_manager.acquire(key, owner, float(self.config.get("lock_timeout_seconds", 2.0)))
        except CameraBusyError as exc:
            self._set_state("BUSY", "error", str(exc))
            return False

        errors: list[str] = []
        opened_any = False
        try:
            for backend_name, backend_id in self._backend_candidates(argument):
                capture: cv2.VideoCapture | None = None
                try:
                    capture = self._open_capture_with_backend(argument, backend_id)
                    if not capture.isOpened():
                        errors.append(f"{backend_name}: 无法打开")
                        capture.release()
                        continue
                    opened_any = True
                    self._set_state("STARTING", "connecting")
                    self._configure_capture(capture)
                    required = max(2, min(30, int(self.config.get("warmup_frames", 5))))
                    timeout = max(1.0, min(3.0, float(self.config.get("warmup_seconds", 2.5))))
                    deadline = time.monotonic() + timeout
                    consecutive = 0
                    last_frame: np.ndarray | None = None
                    while time.monotonic() < deadline and consecutive < required:
                        ok, frame = capture.read()
                        if ok and frame is not None and frame.size:
                            consecutive += 1
                            last_frame = frame
                        else:
                            consecutive = 0
                            time.sleep(0.03)
                    if consecutive < required or last_frame is None:
                        errors.append(f"{backend_name}: 已打开但预热期间无法连续读取 {required} 帧")
                        capture.release()
                        continue
                    self.capture = capture
                    self._primed_frame = last_frame
                    self._read_failures = 0
                    self._backend_name = backend_name
                    with self._lock:
                        self._health.status = "online"
                        self._health.status_code = "READY"
                        self._health.backend = backend_name
                        self._health.backend_id = backend_id
                        self._health.width = int(last_frame.shape[1])
                        self._health.height = int(last_frame.shape[0])
                        self._health.warmup_successful_frames = consecutive
                        self._health.error = None
                        self._frame_times.clear()
                        self._health.fps = 0.0
                    return True
                except Exception as exc:
                    errors.append(f"{backend_name}: {self.redact_credentials(str(exc))}")
                    if capture is not None:
                        capture.release()
            if self._windows_permission_denied():
                self._set_state("PERMISSION_DENIED", "error", "Windows 已关闭桌面应用的摄像头权限，请在隐私设置中允许摄像头访问。")
            elif opened_any:
                message = "；".join(errors)
                self._set_state("FRAME_READ_FAILED", "error", f"摄像头索引 {argument} 可以打开，但预热期间无法连续读取真实画面。{message}")
            else:
                message = "；".join(errors) or "Windows 未找到可读取的摄像头设备。"
                previously_worked,successful_indices,pnp_present=self._diagnostic_context(argument)
                if previously_worked:
                    self._set_state("BUSY", "error", f"摄像头索引 {argument} 在最近诊断中可用，现在无法打开；可能正被其他程序占用或驱动尚未释放。{message}")
                elif successful_indices:
                    self._set_state("NO_DEVICE", "error", f"摄像头索引 {argument} 不可用；本机诊断可用索引为 {sorted(successful_indices)}。{message}")
                elif pnp_present:
                    self._set_state("ERROR", "error", f"Windows检测到摄像头，但OpenCV无法打开索引 {argument}；可能是设备占用、驱动异常或索引不匹配。{message}")
                else:
                    self._set_state("NO_DEVICE", "error", f"摄像头索引 {argument} 无法读取真实画面。{message}")
            return False
        finally:
            if self.capture is None:
                camera_manager.release(self._lease)
                self._lease = None

    def disconnect(self) -> None:
        capture, self.capture = self.capture, None
        self._primed_frame = None
        if capture is not None:
            try:
                capture.release()
            finally:
                camera_manager.release(self._lease)
                self._lease = None
        else:
            camera_manager.release(self._lease)
            self._lease = None
        with self._lock:
            if self._health.status_code not in {"BUSY", "PERMISSION_DENIED", "NO_DEVICE", "FRAME_READ_FAILED", "ERROR"}:
                self._health.status = "stopped"
                self._health.status_code = "STOPPED"

    def read_frame(self) -> np.ndarray | None:
        capture = self.capture
        if capture is None or not capture.isOpened():
            self._set_state("FRAME_READ_FAILED", "recovering", "摄像头已断开，正在尝试重新连接。")
            return None
        started = time.perf_counter()
        if self._primed_frame is not None:
            frame, self._primed_frame = self._primed_frame, None
            ok = True
        else:
            ok, frame = capture.read()
        if not ok or frame is None or not frame.size:
            self._read_failures += 1
            with self._lock:
                self._health.dropped_frames += 1
                self._health.source_read_failures += 1
                self._health.consecutive_failures = self._read_failures
                self._health.max_consecutive_failures = max(self._health.max_consecutive_failures, self._read_failures)
                self._health.status_code = "FRAME_READ_FAILED"
                self._health.status = "recovering"
                self._health.error = f"连续 {self._read_failures} 次没有读到摄像头画面，正在恢复。"
            return None
        self._read_failures = 0
        self._record_frame(frame, started)
        return frame

    def get_metadata(self) -> dict[str, Any]:
        metadata = super().get_metadata()
        index = self._capture_argument()
        metadata.update({
            "device_index": index, "backend": self._backend_name,
            "requested_capture_fps": self._requested_capture_fps,
            "reported_capture_fps": self._reported_capture_fps,
            "capture_fps_request_applied": self._capture_fps_request_applied,
            "reported_fourcc": self._reported_fourcc,
        })
        ownership = camera_manager.describe(camera_manager.webcam_key(index))
        if ownership:
            metadata["ownership"] = ownership
        return metadata

    def health_check(self) -> dict[str, Any]:
        result = super().health_check()
        index = self._capture_argument()
        ownership = camera_manager.describe(camera_manager.webcam_key(index))
        result.update(
            device_index=index,
            requested_capture_fps=self._requested_capture_fps,
            reported_capture_fps=self._reported_capture_fps,
            capture_fps_request_applied=self._capture_fps_request_applied,
            reported_fourcc=self._reported_fourcc,
            current_owner=ownership.get("owner") if ownership else None,
            owner_thread_id=ownership.get("thread_id") if ownership else None,
            owner_started_at=ownership.get("started_at") if ownership else None,
            subscribers=ownership.get("subscribers", 0) if ownership else 0,
        )
        return result

    def probe_resolutions(self) -> list[dict[str, int | bool]]:
        """Try conservative UVC modes; restores the requested mode afterwards."""
        capture = self.capture
        own = capture is None
        if own:
            if not self.connect():
                return []
            capture = self.capture
        if capture is None or not capture.isOpened():
            if own:
                self.disconnect()
            return []
        requested_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or self.config.get("width", 1280))
        requested_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or self.config.get("height", 720))
        results: list[dict[str, int | bool]] = []
        try:
            for width, height in ((640, 480), (1280, 720), (1920, 1080)):
                capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                frame = None
                ok = False
                for _ in range(3):
                    ok, frame = capture.read()
                    if ok and frame is not None:
                        break
                actual_h, actual_w = frame.shape[:2] if ok and frame is not None else (0, 0)
                results.append({"width": actual_w, "height": actual_h, "available": bool(ok)})
            return results
        finally:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, requested_width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, requested_height)
            if own:
                self.disconnect()


class VideoFileSource(OpenCvCaptureSource):
    source_type = "video"

    def __init__(self, source: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(source, config)
        self.loop = bool(self.config.get("loop", True))
        self.realtime = bool(self.config.get("realtime", True))
        self._next_frame_at = 0.0

    def _capture_argument(self) -> str:
        return str(Path(str(self.source)).expanduser().resolve())

    def connect(self) -> bool:
        if not Path(self._capture_argument()).is_file():
            self._record_error("测试视频不存在")
            return False
        ok = super().connect()
        self._next_frame_at = time.perf_counter()
        return ok

    def read_frame(self) -> np.ndarray | None:
        capture = self.capture
        if capture is None:
            return None
        fps = float(capture.get(cv2.CAP_PROP_FPS) or self.config.get("fps", 10) or 10)
        if self.realtime and fps > 0:
            delay = self._next_frame_at - time.perf_counter()
            if delay > 0:
                time.sleep(min(delay, 0.2))
            self._next_frame_at = max(self._next_frame_at + 1.0 / fps, time.perf_counter())
        frame = super().read_frame()
        if frame is not None:
            return frame
        if not self.loop:
            with self._lock:
                self._health.status = "ended"
            return None
        capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
        self._next_frame_at = time.perf_counter()
        frame = super().read_frame()
        if frame is not None:
            # The next frame belongs to a different evidence timeline.  This
            # prevents the state machine from treating EOF -> frame zero as a
            # continuous movement and lets simulated-loop dedupe operate on a
            # well-defined epoch.
            self.mark_stream_discontinuity("video_loop")
        return frame

    def get_metadata(self) -> dict[str, Any]:
        result = super().get_metadata()
        result.update({"test_playback": True, "loop": self.loop, "stream_epoch": self.stream_epoch})
        if self.capture is not None:
            result["frame_count"] = int(self.capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        return result
