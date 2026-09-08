from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.metadata
import math
from pathlib import Path
import threading
import time
from typing import Any

import cv2
import numpy as np


@dataclass(slots=True)
class HandObservation:
    center: tuple[float, float]
    bbox: tuple[int, int, int, int]
    landmarks: list[tuple[float, float]]
    # MediaPipe's result API does not expose the palm/hand-presence probability.
    # Left/right classification confidence is kept separate, never substituted.
    confidence: float | None = None
    handedness: str | None = None
    handedness_score: float | None = None
    source_timestamp_ms: int | None = None
    timestamp_ms: int | None = None


class MediaPipeHandDetector:
    """Optional MediaPipe helper. Unavailability never blocks ArUco mode."""

    def __init__(self, enabled: bool = True, max_hands: int = 2, model_path: str | Path | None = None, *,
                 min_detection_confidence: float = 0.55, min_presence_confidence: float = 0.5,
                 min_tracking_confidence: float = 0.5) -> None:
        self.enabled = enabled
        self.available = False
        self.error: str | None = None
        self._hands = None
        self._mode: str | None = None
        self._lock = threading.RLock()
        self._status = "loading" if enabled else "disabled"
        self._last_timestamp_ms: int | None = None
        self._last_input_timestamp_ms: int | None = None
        self._timestamp_adjustments = 0
        self._last_latency_ms: float | None = None
        self._last_hand_count = 0
        self._frames_processed = 0
        self._model_sha256: str | None = None
        self._package_version: str | None = None
        self._closed = False
        self.max_hands = max_hands
        self._thresholds = {"detection": min_detection_confidence, "presence": min_presence_confidence,
                            "tracking": min_tracking_confidence}
        if not enabled:
            return
        try:
            if type(max_hands) is not int or not 1 <= max_hands <= 2:
                raise ValueError("max_hands must be 1 or 2")
            if any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 for value in self._thresholds.values()):
                raise ValueError("Hand detection/presence/tracking thresholds must be finite values between 0 and 1")
            import mediapipe as mp  # type: ignore
            self._package_version = importlib.metadata.version("mediapipe")
            resolved_model = Path(model_path).expanduser().resolve() if model_path else Path(__file__).resolve().parents[3] / "data" / "models" / "hand_landmarker.task"
            if resolved_model.is_file() and hasattr(mp, "tasks"):
                from mediapipe.tasks import python as mp_python  # type: ignore
                from mediapipe.tasks.python import vision  # type: ignore

                model_bytes = resolved_model.read_bytes()
                self._model_sha256 = hashlib.sha256(model_bytes).hexdigest()
                options = vision.HandLandmarkerOptions(
                    # Bytes avoid the MediaPipe native layer's narrow Windows
                    # path handling when the username/project contains Chinese.
                    base_options=mp_python.BaseOptions(model_asset_buffer=model_bytes),
                    running_mode=vision.RunningMode.VIDEO,
                    num_hands=max_hands,
                    min_hand_detection_confidence=min_detection_confidence,
                    min_hand_presence_confidence=min_presence_confidence,
                    min_tracking_confidence=min_tracking_confidence,
                )
                self._hands = vision.HandLandmarker.create_from_options(options)
                self._mode = "tasks"
                self._mp = mp
                self.available = True
            elif hasattr(mp, "solutions"):
                self._hands = mp.solutions.hands.Hands(
                    static_image_mode=False, max_num_hands=max_hands,
                    min_detection_confidence=min_detection_confidence, min_tracking_confidence=min_tracking_confidence,
                )
                self._mode = "legacy"
                self.available = True
            else:
                raise FileNotFoundError(f"Hand Landmarker 模型不存在：{resolved_model}；请运行 scripts/download-models.py --hands")
            self._status = "ready"
        except Exception as exc:
            self.error = f"MediaPipe 未启用：{exc}"
            self._status = "failed"

    def detect(self, frame: np.ndarray, timestamp_ms: int | None = None) -> list[HandObservation]:
        """Synchronously return this frame's observations; no callback or retained frame queue.

        An explicit timestamp belongs to the caller's source frame. If repeated
        or reset, only the native VIDEO timestamp is advanced; both values remain
        observable, so an adjusted clock cannot masquerade as a new source frame.
        """
        with self._lock:
            if not self.available or self._hands is None:
                return []
            started = time.perf_counter()
            try:
                if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3 or min(frame.shape[:2]) < 1 or frame.shape[0] * frame.shape[1] > 16_777_216:
                    raise ValueError("Hand input must be a nonempty bounded uint8 BGR image")
                if timestamp_ms is not None and (type(timestamp_ms) is not int or not 0 <= timestamp_ms < 2**53 - 1):
                    raise ValueError("Hand source timestamp_ms must be a nonnegative bounded integer")
                requested = timestamp_ms if timestamp_ms is not None else int(time.monotonic() * 1000)
                submitted = max((self._last_timestamp_ms + 1) if self._last_timestamp_ms is not None else 0, requested)
                if submitted >= 2**53 - 1:
                    raise ValueError("Hand VIDEO timestamp exceeded its native range")
                self._last_input_timestamp_ms = timestamp_ms
                self._last_timestamp_ms = submitted
                self._timestamp_adjustments += int(submitted != requested)
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                if self._mode == "tasks":
                    media_image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
                    result = self._hands.detect_for_video(media_image, submitted)
                    detected_landmarks = result.hand_landmarks
                    categories = result.handedness
                else:
                    result = self._hands.process(rgb)
                    detected_landmarks = result.multi_hand_landmarks or []
                    categories = [list(group.classification) for group in (result.multi_handedness or [])]
                height, width = frame.shape[:2]
                observations: list[HandObservation] = []
                for hand_index, hand in enumerate(detected_landmarks[:self.max_hands]):
                    points = hand.landmark if hasattr(hand, "landmark") else hand
                    normalized = [(float(point.x), float(point.y)) for point in points]
                    if len(normalized) != 21 or not np.isfinite(normalized).all():
                        raise ValueError("Hand model returned nonfinite or incomplete landmarks")
                    pixels = np.asarray([(x * width, y * height) for x, y in normalized], dtype=np.float32)
                    x, y, w, h = cv2.boundingRect(pixels)
                    # The raw normalized landmarks remain unclamped; raster boxes
                    # are clipped explicitly so consumers cannot crop outside a frame.
                    left, top = max(0, min(width, x)), max(0, min(height, y))
                    right, bottom = max(left, min(width, x + w)), max(top, min(height, y + h))
                    if right <= left or bottom <= top:
                        continue
                    category = categories[hand_index][0] if hand_index < len(categories) and categories[hand_index] else None
                    label = getattr(category, "category_name", getattr(category, "label", None))
                    score = getattr(category, "score", None)
                    score = float(score) if score is not None and math.isfinite(float(score)) and 0 <= float(score) <= 1 else None
                    center = tuple(float(v) for v in pixels.mean(axis=0))
                    observations.append(HandObservation(center, (left, top, right - left, bottom - top), normalized,
                        confidence=None, handedness=label if label in ("Left", "Right") else None,
                        handedness_score=score, source_timestamp_ms=timestamp_ms, timestamp_ms=submitted))
                self._last_hand_count = len(observations)
                self._frames_processed += 1
                self._status, self.error = ("ready" if observations else "no_hands"), None
                return observations
            except Exception as exc:
                self._status, self.error = "failed", f"手部处理失败：{exc}"
                self._last_hand_count = 0
                return []
            finally:
                self._last_latency_ms = round((time.perf_counter() - started) * 1000, 3)

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {"name": "mediapipe_hands", "enabled": self.enabled, "available": self.available,
                    "backend": self._mode, "running_mode": "VIDEO" if self._mode == "tasks" else "legacy_video" if self._mode else None,
                    "status": self._status, "error": self.error, "max_hands": self.max_hands,
                    "model_sha256": self._model_sha256, "model_version": self._model_sha256,
                    "mediapipe_version": self._package_version, "confidence_kind": "presence_not_exposed",
                    "thresholds": dict(self._thresholds), "last_hand_count": self._last_hand_count,
                    "last_latency_ms": self._last_latency_ms, "frames_processed": self._frames_processed,
                    "last_input_timestamp_ms": self._last_input_timestamp_ms, "last_timestamp_ms": self._last_timestamp_ms,
                    "timestamp_adjustments": self._timestamp_adjustments, "closed": self._closed}

    def close(self) -> None:
        with self._lock:
            hands, self._hands = self._hands, None
            self.available = False
            self._closed = True
            self._status = "disabled"
            self._last_hand_count = 0
            if hands is not None:
                try:
                    hands.close()
                except Exception as exc:
                    self._status, self.error = "failed", f"手部模型释放失败：{exc}"
