"""Frames explicitly captured by a browser and pushed into the local vision pipeline."""
from __future__ import annotations

import queue
import threading
import time
from typing import Any
from uuid import uuid4

import cv2
import numpy as np

from .base import CameraSourceAdapter


_JPEG_SOF_MARKERS = frozenset({
    0xC0, 0xC1, 0xC2, 0xC3,
    0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB,
    0xCD, 0xCE, 0xCF,
})
_JPEG_STANDALONE_MARKERS = frozenset({0x01, 0xD8, 0xD9, *range(0xD0, 0xD8)})
_MAX_JPEG_HEADER_BYTES = 256 * 1024
_MAX_JPEG_HEADER_SEGMENTS = 256


def _jpeg_dimensions(content: bytes) -> tuple[int, int]:
    """Read JPEG SOF dimensions without asking an image codec to allocate pixels."""
    data = memoryview(content)
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        raise ValueError("浏览器发送的内容不是有效 JPEG 图像。")

    offset = 2
    scan_limit = min(len(data), _MAX_JPEG_HEADER_BYTES)
    segments = 0
    while offset < scan_limit and segments < _MAX_JPEG_HEADER_SEGMENTS:
        if data[offset] != 0xFF:
            raise ValueError("JPEG 头部结构无效。")
        while offset < scan_limit and data[offset] == 0xFF:
            offset += 1
        if offset >= scan_limit:
            break
        marker = int(data[offset])
        offset += 1
        if marker == 0x00:
            raise ValueError("JPEG 头部结构无效。")
        if marker in _JPEG_STANDALONE_MARKERS:
            if marker == 0xD9:
                break
            continue
        if marker == 0xDA:  # Start of scan: SOF must already have appeared.
            break
        if offset + 2 > len(data) or offset + 2 > scan_limit:
            raise ValueError("JPEG 头部不完整。")
        segment_length = (int(data[offset]) << 8) | int(data[offset + 1])
        if segment_length < 2:
            raise ValueError("JPEG 段长度无效。")
        segment_end = offset + segment_length
        if segment_end > len(data):
            raise ValueError("JPEG 头部不完整。")
        segments += 1
        if marker in _JPEG_SOF_MARKERS:
            if segment_length < 8 or offset + 8 > scan_limit:
                raise ValueError("JPEG 尺寸段不完整。")
            height = (int(data[offset + 3]) << 8) | int(data[offset + 4])
            width = (int(data[offset + 5]) << 8) | int(data[offset + 6])
            if width <= 0 or height <= 0:
                raise ValueError("JPEG 画面尺寸无效。")
            return width, height
        if segment_end > scan_limit:
            break
        offset = segment_end

    raise ValueError("无法在受限 JPEG 头部内验证画面尺寸。")


class BrowserCameraSource(CameraSourceAdapter):
    source_type = "browser"
    _registry: dict[str, "BrowserCameraSource"] = {}
    _registry_lock = threading.RLock()

    def __init__(self, source: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(source, config)
        self.camera_id = str(self.config.get("camera_id") or source)
        self._frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=2)
        self._connected = False
        self._sender_token: str | None = None

    @classmethod
    def get(cls, camera_id: str) -> "BrowserCameraSource | None":
        with cls._registry_lock:
            return cls._registry.get(str(camera_id))

    def connect(self) -> bool:
        with self._registry_lock:
            self._registry[self.camera_id] = self
        with self._lock:
            self._connected = True
            self._health.status = "starting"
            self._health.error = None
        return True

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False
            self._sender_token = None
        with self._registry_lock:
            if self._registry.get(self.camera_id) is self:
                self._registry.pop(self.camera_id, None)
        while True:
            try:self._frames.get_nowait()
            except queue.Empty:break
        with self._lock:
            self._health.status = "stopped"

    def sender_connected(self) -> str:
        with self._lock:
            if not self._connected:
                raise RuntimeError("浏览器摄像头尚未启动。")
            if self._sender_token is not None:
                raise RuntimeError("这个浏览器摄像头已有一个发送页面。")
            self._sender_token = uuid4().hex
            self._health.status = "starting"
            self._health.status_code = "STARTING"
            self._health.error = None
            return self._sender_token

    def sender_disconnected(self, sender_token: str) -> bool:
        with self._lock:
            if not sender_token or sender_token != self._sender_token:
                return False
            self._sender_token = None
            if self._connected:
                self._health.status = "frame_read_failed"
                self._health.status_code = "FRAME_READ_FAILED"
                self._health.error = "浏览器摄像头已断开，请重新授权并选择摄像头。"
            return True

    def reconnect(self) -> bool:
        """Keep the browser-owned transport alive during a temporary frame gap.

        The generic reconnect implementation removes the source from the
        registry and clears sender ownership.  A browser WebSocket is the
        transport itself, so closing that registry entry during a short tab or
        camera stall can orphan a still-authenticated sender.  Here a reconnect
        is a bounded logical retry; the current sender may resume with the next
        acknowledged JPEG frame.
        """
        with self._lock:
            self._health.reconnects += 1
            sender_connected = self._sender_token is not None
            self._health.status = "starting" if sender_connected else "frame_read_failed"
            self._health.status_code = "RECONNECTING" if sender_connected else "FRAME_READ_FAILED"
            self._health.error = (
                "浏览器画面暂时中断，正在等待下一帧。"
                if sender_connected
                else "等待浏览器重新授权并发送画面。"
            )
        return self._connected

    def _require_sender(self, sender_token: str) -> None:
        with self._lock:
            if not self._connected:
                raise RuntimeError("浏览器摄像头尚未启动。")
            if not sender_token or sender_token != self._sender_token:
                raise RuntimeError("这个浏览器发送连接已经失效，请重新连接。")

    def push_jpeg(self, content: bytes, sender_token: str) -> dict[str, Any]:
        self._require_sender(sender_token)
        if not 100 <= len(content) <= 2 * 1024 * 1024:
            raise ValueError("JPEG 帧大小必须在 100 字节到 2 MB 之间。")
        width,height = _jpeg_dimensions(content)
        if height > 2160 or width > 3840:
            raise ValueError("浏览器画面不能超过 3840×2160。")
        started = time.perf_counter()
        frame = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)
        if frame is None or frame.ndim != 3:
            raise ValueError("浏览器发送的内容不是有效 JPEG 图像。")
        if frame.shape[0] > 2160 or frame.shape[1] > 3840:
            raise ValueError("浏览器画面不能超过 3840×2160。")
        # An administrator can stop or replace the engine while JPEG decoding is
        # running.  Re-check the lease before publishing into the queue so a
        # stale WebSocket can never feed its replacement.
        self._require_sender(sender_token)
        dropped = 0
        while self._frames.full():
            try:self._frames.get_nowait();dropped += 1
            except queue.Empty:break
        self._frames.put_nowait(frame)
        if dropped:
            with self._lock:self._health.dropped_frames += dropped
        self._record_frame(frame, started)
        return {"width":int(frame.shape[1]),"height":int(frame.shape[0]),"fps":round(self.get_fps(),2),"dropped_frames":dropped}

    def read_frame(self) -> np.ndarray | None:
        try:
            return self._frames.get(timeout=2.0)
        except queue.Empty:
            with self._lock:
                if self._connected and self._sender_token is None:
                    self._health.status = "starting"
                    self._health.error = "等待浏览器授权并发送画面。"
            return None

    def health_check(self) -> dict[str, Any]:
        value=super().health_check()
        with self._lock:
            sender_connected=self._sender_token is not None
        value.update({"sender_connected":sender_connected,"buffered_frames":self._frames.qsize(),"camera_id":self.camera_id})
        return value
