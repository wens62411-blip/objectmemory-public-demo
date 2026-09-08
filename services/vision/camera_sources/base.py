from __future__ import annotations

import abc
import re
import threading
import time
from dataclasses import dataclass, asdict
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import numpy as np


SENSITIVE_KEYS = {"password", "passwd", "pwd", "token", "secret", "pairing_code", "device_token",
                  "camera_read_token", "token_hash", "authorization", "headers", "camera_read_headers"}
URL_PATTERN = re.compile(r"(?P<url>(?:https?|rtsps?)://[^\s'\"<>]+)", re.IGNORECASE)


def _redact_url(value: str) -> str:
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    if parsed.username is not None:
        safe_user = quote(unquote(parsed.username), safe="")
        netloc = f"{safe_user}:***@{host}" if parsed.password is not None else f"{safe_user}@{host}"
    else:
        netloc = host
    query = urlencode([
        (key, "***" if key.lower() in SENSITIVE_KEYS else item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
    ])
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


def redact_value(value: Any) -> Any:
    """Return a log-safe copy of a nested configuration value."""
    if isinstance(value, dict):
        return {
            str(key): ("***" if str(key).lower() in SENSITIVE_KEYS else redact_value(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value,str):
        value=re.sub(r"(?i)(\bBearer\s+)[^\s,\"'<>}]+",r"\1***",value)
    if isinstance(value, str) and "://" in value:
        try:
            if URL_PATTERN.fullmatch(value):
                return _redact_url(value)
            return URL_PATTERN.sub(lambda match: _redact_url(match.group("url")), value)
        except (TypeError, ValueError):
            return "***"
    return value


@dataclass
class SourceHealth:
    status: str = "stopped"
    status_code: str = "STOPPED"
    fps: float = 0.0
    latency_ms: float = 0.0
    dropped_frames: int = 0
    reconnects: int = 0
    error: str | None = None
    width: int = 0
    height: int = 0
    backend: str | None = None
    backend_id: int | None = None
    consecutive_failures: int = 0
    max_consecutive_failures: int = 0
    frames_read: int = 0
    source_read_failures: int = 0
    warmup_successful_frames: int = 0


class CameraSourceAdapter(abc.ABC):
    """Uniform interface used by the capture worker.

    ``read_frame`` returns a BGR numpy array or ``None`` for a recoverable read
    failure. Implementations must never leak credentials through health/error
    text.
    """

    source_type = "unknown"

    def __init__(self, source: Any, config: dict[str, Any] | None = None) -> None:
        self.source = source
        self.config = dict(config or {})
        self._health = SourceHealth()
        self._lock = threading.RLock()
        self._frame_times: list[float] = []
        # Adapters may discover a discontinuity without reconnecting the
        # transport (a looping file is the canonical example).  CaptureWorker
        # consumes this marker and creates a new source session before it
        # publishes the first frame of the new epoch.
        self._stream_epoch = 0
        self._stream_discontinuity: tuple[int, str] | None = None

    @abc.abstractmethod
    def connect(self) -> bool: ...

    @abc.abstractmethod
    def disconnect(self) -> None: ...

    @abc.abstractmethod
    def read_frame(self) -> np.ndarray | None: ...

    def health_check(self) -> dict[str, Any]:
        with self._lock:
            result = asdict(self._health)
            # A stopped/stalled adapter must not keep reporting its last rate.
            if self._health.status in {"stopped", "ended", "error", "reconnecting"} or not self._frame_times or time.perf_counter() - self._frame_times[-1] > 2.0:
                result["fps"] = 0.0
        result["source_type"] = self.source_type
        return result

    def get_metadata(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source": self.redact_credentials(self.source),
            "config": self.redact_credentials(self.config),
        }

    def get_fps(self) -> float:
        with self._lock:
            if not self._frame_times or time.perf_counter() - self._frame_times[-1] > 2.0:
                return 0.0
            if len(self._frame_times) < 2:
                return 0.0
            duration = self._frame_times[-1] - self._frame_times[0]
            return (len(self._frame_times) - 1) / duration if duration > 0 else 0.0

    def reconnect(self) -> bool:
        self.disconnect()
        with self._lock:
            self._health.reconnects += 1
        return self.connect()

    def mark_stream_discontinuity(self, reason: str) -> int:
        """Mark a source-timeline reset for the capture worker.

        This is intentionally separate from the transport reconnect counter:
        rewinding a file is a new evidence epoch even though the same
        ``VideoCapture`` remains open.
        """
        with self._lock:
            self._stream_epoch += 1
            self._stream_discontinuity = (self._stream_epoch, str(reason))
            return self._stream_epoch

    def consume_stream_discontinuity(self) -> tuple[int, str] | None:
        with self._lock:
            value = self._stream_discontinuity
            self._stream_discontinuity = None
            return value

    @property
    def stream_epoch(self) -> int:
        with self._lock:
            return self._stream_epoch

    @staticmethod
    def redact_credentials(value: Any) -> Any:
        return redact_value(value)

    def _record_frame(self, frame: np.ndarray, started_at: float | None = None) -> None:
        now = time.perf_counter()
        with self._lock:
            self._frame_times.append(now)
            if len(self._frame_times) > 60:
                del self._frame_times[:-60]
            self._health.fps = self.get_fps()
            self._health.frames_read += 1
            self._health.height, self._health.width = frame.shape[:2]
            self._health.latency_ms = max(0.0, (now - started_at) * 1000) if started_at else 0.0
            self._health.status = "online"
            self._health.status_code = "STREAMING"
            self._health.consecutive_failures = 0
            self._health.error = None

    def _record_error(self, message: str) -> None:
        safe_message = str(self.redact_credentials(message))
        with self._lock:
            self._health.error = safe_message
            self._health.status = "error"
            self._health.status_code = "ERROR"

    def _set_state(self, status_code: str, status: str, error: str | None = None) -> None:
        """Update detailed source state while preserving the legacy UI status."""
        safe_error = str(self.redact_credentials(error)) if error else None
        with self._lock:
            self._health.status_code = status_code
            self._health.status = status
            self._health.error = safe_error
