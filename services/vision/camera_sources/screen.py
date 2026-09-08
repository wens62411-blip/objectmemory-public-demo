from __future__ import annotations

import time
from typing import Any

import cv2
import numpy as np

from .base import CameraSourceAdapter


class AuthorizedScreenCaptureSource(CameraSourceAdapter):
    source_type = "screen"

    def __init__(self, source: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(source, config)
        self._mss = None
        self._region: dict[str, int] | None = None

    def connect(self) -> bool:
        try:
            import mss

            region = self.config.get("region") or self.source
            if not isinstance(region, dict):
                self._record_error("请先由用户选择要采集的屏幕矩形区域")
                return False
            normalized = {key: int(region[key]) for key in ("left", "top", "width", "height")}
            if normalized["width"] < 32 or normalized["height"] < 32:
                self._record_error("采集区域太小")
                return False
            if normalized["width"] > 16384 or normalized["height"] > 16384:
                self._record_error("采集区域超出允许范围")
                return False
            self._region = normalized
            self._mss = mss.mss()
            with self._lock:
                self._health.status = "online"
                self._health.error = None
            return True
        except Exception as exc:
            self._record_error(f"屏幕区域采集初始化失败：{exc}")
            return False

    def disconnect(self) -> None:
        capture, self._mss = self._mss, None
        if capture is not None:
            try:
                capture.close()
            except Exception:
                pass
        with self._lock:
            if self._health.status != "error":
                self._health.status = "stopped"

    def read_frame(self) -> np.ndarray | None:
        if self._mss is None or self._region is None:
            return None
        started = time.perf_counter()
        try:
            shot = np.asarray(self._mss.grab(self._region))
            frame = cv2.cvtColor(shot, cv2.COLOR_BGRA2BGR)
            self._record_frame(frame, started)
            return frame
        except Exception as exc:
            self._record_error(f"屏幕区域当前不可读取（窗口可能被最小化或遮挡）：{exc}")
            return None

    def get_metadata(self) -> dict[str, Any]:
        result = super().get_metadata()
        result.update({"authorized_region_only": True, "region": self._region, "occlusion_sensitive": True})
        return result

