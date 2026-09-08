from __future__ import annotations

import queue
import math
import hashlib
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass

import numpy as np

from .camera_sources import CameraSourceAdapter


class PipelineDiagnostics:
    """Opt-in, bounded numerical timings; never retain pixels or identifiers."""

    def __init__(self, enabled: bool, fields: tuple[str, ...], outcomes: tuple[str, ...], counters: tuple[str, ...] = ()):
        self.enabled = enabled
        self.fields, self.outcomes, self.counters = fields, outcomes, counters
        self._lock = threading.Lock()
        self._samples = deque(maxlen=120)
        self._counts: dict[str, int] = {}

    def clear(self) -> None:
        if self.enabled:
            with self._lock:
                self._samples.clear()
                self._counts.clear()

    def record(self, outcome: str, metrics: dict, counts: dict | None = None) -> None:
        if not self.enabled or outcome not in self.outcomes:
            return
        values = {key: float(value) for key, value in metrics.items() if key in self.fields
                  and type(value) in (int, float) and math.isfinite(value) and value >= 0}
        increments = {key: value for key, value in (counts or {}).items() if key in self.counters
                      and type(value) is int and value >= 0}
        # No OpenCV call or aggregation runs while holding this lock.
        with self._lock:
            self._samples.append(values)
            self._counts[outcome] = self._counts.get(outcome, 0) + 1
            for key, value in increments.items():
                self._counts[key] = self._counts.get(key, 0) + value

    def snapshot(self) -> dict:
        if not self.enabled:
            return {"enabled": False}
        with self._lock:
            samples, counts = list(self._samples), dict(self._counts)
        metrics = {}
        for field in self.fields:
            values = sorted(row[field] for row in samples if field in row)
            if values:
                middle = len(values) // 2
                median = values[middle] if len(values) % 2 else (values[middle-1]+values[middle])/2
                metrics[field] = {"count": len(values), "min": round(values[0], 3),
                    "p50": round(median, 3), "p95": round(values[math.ceil(len(values)*.95)-1], 3),
                    "max": round(values[-1], 3)}
        return {"enabled": True, "capacity": 120, "samples_retained": len(samples),
                "cumulative_counts": counts, "milliseconds": metrics}


@dataclass(slots=True)
class FramePacket:
    frame: np.ndarray
    monotonic_time: float
    wall_time: float
    sequence: int
    source_session_id: str = ""
    reconnect_epoch: int = 0


class LatestFrameQueue:
    """Bounded newest-frame queue; producers discard stale frames under load."""

    def __init__(self, maxsize: int = 2) -> None:
        self.queue: queue.Queue[FramePacket] = queue.Queue(maxsize=max(1, maxsize))
        self.dropped = 0

    def put_latest(self, packet: FramePacket) -> None:
        while True:
            try:
                self.queue.put_nowait(packet)
                return
            except queue.Full:
                try:
                    self.queue.get_nowait()
                    self.dropped += 1
                except queue.Empty:
                    pass

    def get(self, timeout: float = 0.5) -> FramePacket:
        return self.queue.get(timeout=timeout)

    def clear(self) -> None:
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                return


class LatestFrameSlot:
    """One shared, non-consuming preview frame, independent of inference load."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._packet: FramePacket | None = None
        self._revision = 0

    def publish(self, packet: FramePacket) -> None:
        with self._condition:
            self._packet = packet
            self._revision += 1
            self._condition.notify_all()

    def latest(self, after_revision: int = 0, timeout: float = 0.5) -> tuple[int, FramePacket] | None:
        with self._condition:
            self._condition.wait_for(
                lambda: self._packet is not None and self._revision > after_revision,
                timeout=max(0.0, timeout),
            )
            if self._packet is None or self._revision <= after_revision:
                return None
            return self._revision, self._packet

    def clear(self) -> None:
        with self._condition:
            self._packet = None
            self._condition.notify_all()

    @property
    def size(self) -> int:
        with self._condition:
            return int(self._packet is not None)


class CaptureWorker:
    def __init__(self, source: CameraSourceAdapter, queue_size: int = 2, reconnect_after_failures: int = 8, *, diagnostic_mode: bool = False, deduplicate_identical_frames: bool = False) -> None:
        if type(deduplicate_identical_frames) is not bool:
            raise ValueError('deduplicate_identical_frames must be boolean')
        self.source = source
        self.deduplicate_identical_frames = deduplicate_identical_frames
        self.frames = LatestFrameQueue(queue_size)
        self.preview_frames = LatestFrameSlot()
        self.reconnect_after_failures = reconnect_after_failures
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.sequence = 0
        self.error: str | None = None
        self._ready = threading.Event()
        self._connect_ok = False
        self._session_lock = threading.RLock()
        self.source_session_id = ""
        self.reconnect_epoch = 0
        self.session_reason = "not_started"
        self.session_started_at: float | None = None
        self._last_content_digest: bytes | None = None
        self._last_unique_at: float | None = None
        self._unique_frame_times: deque[float] = deque(maxlen=120)
        self.duplicate_source_reads = 0
        self.diagnostics = PipelineDiagnostics(diagnostic_mode,
            ("read_ms", "read_return_interval_ms", "publish_interval_ms", "queue_publish_ms", "slot_publish_ms", "read_to_publish_ms", "fingerprint_ms"),
            ("published", "read_empty", "duplicate_read"))

    def _begin_source_session(self, reason: str, *, increment_epoch: bool) -> None:
        """Start a clean evidence timeline and discard frames from the old one."""
        with self._session_lock:
            if increment_epoch:
                self.reconnect_epoch += 1
            else:
                self.reconnect_epoch = 0
            self.source_session_id = str(uuid.uuid4())
            self.session_reason = str(reason)
            self.session_started_at = time.time()
            self.sequence = 0
            self._last_content_digest = None
            self._last_unique_at = None
            self._unique_frame_times.clear()
            self.frames.clear()
            self.preview_frames.clear()

    def start(self) -> bool:
        if self.thread and self.thread.is_alive():
            return self._connect_ok
        self.stop_event.clear()
        self._ready.clear()
        self._connect_ok = False
        self.error = None
        self.duplicate_source_reads = 0
        self.diagnostics.clear()
        self._begin_source_session("start", increment_epoch=False)
        self.thread = threading.Thread(target=self._run, name=f"capture-{self.source.source_type}", daemon=True)
        self.thread.start()
        # Source connect runs in the capture thread. This is required by mss,
        # whose Windows handles are thread-affine.
        if not self._ready.wait(float(self.source.config.get("connect_wait_seconds", 20.0))):
            self.error = "摄像头连接超时"
            self.stop_event.set()
            return False
        return self._connect_ok

    @staticmethod
    def _frame_digest(frame: np.ndarray) -> bytes:
        """Exact pixels only: no perceptual comparison or extra retained image."""
        pixels = frame if frame.flags.c_contiguous else np.ascontiguousarray(frame)
        digest = hashlib.sha256()
        digest.update(str(frame.shape).encode('ascii'))
        digest.update(frame.dtype.str.encode('ascii'))
        digest.update(memoryview(pixels))
        return digest.digest()

    def _accept_content(self, digest: bytes, now: float) -> bool:
        with self._session_lock:
            if digest == self._last_content_digest:
                self.duplicate_source_reads += 1
                # Equal pixels alone do NOT prove a repeated exposure/PTS.
                # Static decoded file frames remain valid continuity evidence.
                # Opt-in filtering is conservative and never declares offline.
                return not self.deduplicate_identical_frames
            self._last_content_digest = digest
            self._last_unique_at = now
            self._unique_frame_times.append(now)
            return True

    def _capture_failed(self, exc: Exception, phase: str) -> None:
        # Native OpenCV may wrap a numpy MemoryError inside SystemError. Keep
        # its bounded, redacted cause names without retaining traceback frames
        # (which can themselves keep large image arrays alive).
        kinds = [type(exc).__name__]
        cause = exc.__cause__
        if cause is not None:
            kinds.append(type(cause).__name__)
        detail = str(self.source.redact_credentials(str(exc)))[:240]
        message = f"摄像头采集已停止 [{phase}: {' <- '.join(kinds)}] {detail}"
        self.error = f"{self.error}; {message}"[:640] if phase == "release" and self.error else message
        self._connect_ok = False
        self.stop_event.set()
        self.source._set_state("FRAME_READ_FAILED", "error", self.error)
        # Release can block in a native driver. Invalidate queued evidence
        # before waiting for it, not only after disconnect returns.
        self.frames.clear()
        self.preview_frames.clear()

    def _run(self) -> None:
        failed = False
        phase = "connect"
        try:
            self._connect_ok = self.source.connect()
            self.error = None if self._connect_ok else self.source.health_check().get("error")
            self._ready.set()
            if not self._connect_ok:
                return
            phase = "read"
            failures = 0
            diagnostic = self.diagnostics.enabled
            previous_return = previous_publish = None
            while not self.stop_event.is_set():
                read_started = time.perf_counter() if diagnostic else 0.0
                frame = self.source.read_frame()
                if diagnostic:
                    read_returned = time.perf_counter()
                    metrics = {"read_ms": (read_returned-read_started)*1000}
                    if previous_return is not None:
                        metrics["read_return_interval_ms"] = (read_returned-previous_return)*1000
                    previous_return = read_returned
                if frame is not None:
                    failures = 0
                    discontinuity = self.source.consume_stream_discontinuity()
                    if discontinuity is not None:
                        _adapter_epoch, reason = discontinuity
                        self._begin_source_session(reason, increment_epoch=True)
                    fingerprint_started = time.perf_counter() if diagnostic else 0.0
                    digest = self._frame_digest(frame)
                    if diagnostic:
                        metrics['fingerprint_ms'] = (time.perf_counter()-fingerprint_started)*1000
                    if not self._accept_content(digest, time.monotonic()):
                        if diagnostic:
                            self.diagnostics.record('duplicate_read', metrics)
                        continue
                    with self._session_lock:
                        self.sequence += 1
                        packet = FramePacket(
                            frame,
                            time.monotonic(),
                            time.time(),
                            self.sequence,
                            self.source_session_id,
                            self.reconnect_epoch,
                        )
                    queue_started = time.perf_counter() if diagnostic else 0.0
                    self.frames.put_latest(packet)
                    slot_started = time.perf_counter() if diagnostic else 0.0
                    self.preview_frames.publish(packet)
                    if diagnostic:
                        published = time.perf_counter()
                        metrics.update(queue_publish_ms=(slot_started-queue_started)*1000,
                            slot_publish_ms=(published-slot_started)*1000,
                            read_to_publish_ms=(published-read_returned)*1000)
                        if previous_publish is not None:
                            metrics['publish_interval_ms'] = (published-previous_publish)*1000
                        previous_publish = published
                        self.diagnostics.record('published', metrics)
                    continue
                if diagnostic:
                    self.diagnostics.record('read_empty', metrics)
                failures += 1
                status = self.source.health_check().get("status")
                if status == "ended":
                    time.sleep(0.05)
                    continue
                if failures >= self.reconnect_after_failures:
                    self.source._set_state("RECONNECTING", "reconnecting", f"连续 {failures} 次没有读到画面，正在重新连接。")
                    if self.stop_event.wait(0.5):
                        break
                    reconnected = self.source.reconnect()
                    if not reconnected:
                        self.error = self.source.health_check().get("error") or "摄像头重新连接失败"
                        if self.stop_event.wait(1.0):
                            break
                    else:
                        self.error = None
                        self._begin_source_session("reconnect", increment_epoch=True)
                    failures = 0
                else:
                    time.sleep(0.02)
        except Exception as exc:
            # Exceptions are not a recoverable None-frame. End this source
            # session once; an explicit restart creates a clean baseline.
            failed = True
            self._capture_failed(exc, phase)
        finally:
            try:
                self.source.disconnect()
            except Exception as exc:
                failed = True
                self._capture_failed(exc, "release")
            finally:
                self.frames.clear()
                self.preview_frames.clear()
                if failed:
                    # Some adapters overwrite health with STOPPED in release.
                    # Preserve the actual failure, not an apparently clean stop.
                    self.source._set_state("FRAME_READ_FAILED", "error", self.error)
                self._ready.set()

    def stop(self) -> None:
        self.stop_event.set()
        thread = self.thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=3.0)
            if thread.is_alive():
                # A backend read may block inside a native driver. Releasing the
                # capture is the only bounded escape available to OpenCV.
                self.source.disconnect()
                thread.join(timeout=1.0)
        self.thread = thread if thread and thread.is_alive() else None
        if self.thread:
            self.error = "摄像头驱动没有在4秒内停止；资源已请求释放，请稍后重试。"
        self.frames.clear()
        self.preview_frames.clear()

    def health(self) -> dict:
        value = self.source.health_check()
        # Keep content novelty separate from delivery/exposure rate. A static
        # scene can legitimately deliver identical pixels with new source PTS.
        value['raw_read_fps'] = value.get('fps', 0.0)
        value["queue_size"] = self.frames.queue.qsize()
        value["preview_buffer_frames"] = self.preview_frames.size
        value["queue_dropped_frames"] = self.frames.dropped
        value["source_read_failures"] = int(value.get("source_read_failures", 0))
        value["dropped_frames"] = int(value.get("dropped_frames", 0)) + self.frames.dropped
        value["capture_thread_alive"] = bool(self.thread and self.thread.is_alive())
        value["capture_pipeline_diagnostics"] = self.diagnostics.snapshot()
        with self._session_lock:
            now = time.monotonic()
            recent = [at for at in self._unique_frame_times if now-at <= 2.0]
            value['unique_pixel_fps'] = ((len(recent)-1)/(recent[-1]-recent[0])
                            if len(recent) >= 2 and recent[-1] > recent[0] else 0.0)
            value['duplicate_source_reads'] = self.duplicate_source_reads
            value['pixel_cadence_is_exposure_proof'] = False
            value['deduplicate_identical_frames'] = self.deduplicate_identical_frames
            value['last_unique_frame_age_ms'] = max(0., (now-self._last_unique_at)*1000) if self._last_unique_at is not None else None
            if value.get('status') in {'stopped', 'ended', 'error', 'reconnecting'} or self.stop_event.is_set():
                value['fps'] = 0.0
                value['unique_pixel_fps'] = 0.0
            value["source_session_id"] = self.source_session_id
            value["reconnect_epoch"] = self.reconnect_epoch
            value["source_session_reason"] = self.session_reason
            value["source_session_started_at"] = self.session_started_at
            value["source_frame_sequence"] = self.sequence
        return value
