from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ..detectors import Detection


@dataclass(slots=True)
class TrackedObservation:
    track_id: str
    identity: str
    label: str
    bbox: tuple[int, int, int, int]
    center: tuple[float, float]
    center_norm: tuple[float, float]
    velocity_norm_s: tuple[float, float]
    speed_norm_s: float
    confidence: float
    detection_mode: str
    raw_id: int | str | None
    age_frames: int
    recovered: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Track:
    track_id: str
    last_detection: Detection
    center_norm: tuple[float, float]
    last_seen: float
    age_frames: int = 1
    missing_since: float | None = None


class StableIdentityTracker:
    """Stable tracks keyed by registered identity (ideal for ArUco IDs)."""

    def __init__(self, recovery_seconds: float = 12.0) -> None:
        self.recovery_seconds = recovery_seconds
        self._tracks: dict[str, _Track] = {}

    def update(self, detections: list[Detection], frame_shape: tuple[int, ...], timestamp: float) -> tuple[list[TrackedObservation], set[str]]:
        height, width = frame_shape[:2]
        observed: list[TrackedObservation] = []
        seen: set[str] = set()
        # A matcher may assign multiple generic candidate boxes to one
        # registered item. Keep only its strongest candidate so duplicate
        # same-timestamp updates cannot create artificial high velocity.
        strongest: dict[str, Detection] = {}
        for detection in detections:
            current = strongest.get(detection.identity)
            if current is None or detection.confidence > current.confidence:
                strongest[detection.identity] = detection
        for detection in strongest.values():
            identity = detection.identity
            seen.add(identity)
            center_norm = (detection.center[0] / max(width, 1), detection.center[1] / max(height, 1))
            previous = self._tracks.get(identity)
            recovered = bool(previous and previous.missing_since is not None)
            if previous is None or timestamp - previous.last_seen > self.recovery_seconds:
                track = _Track(f"track:{identity}", detection, center_norm, timestamp)
                velocity = (0.0, 0.0)
                self._tracks[identity] = track
            else:
                dt = max(timestamp - previous.last_seen, 1e-6)
                velocity = ((center_norm[0] - previous.center_norm[0]) / dt, (center_norm[1] - previous.center_norm[1]) / dt)
                previous.last_detection = detection
                previous.center_norm = center_norm
                previous.last_seen = timestamp
                previous.age_frames += 1
                previous.missing_since = None
                track = previous
            speed = math.hypot(*velocity)
            observed.append(TrackedObservation(
                track_id=track.track_id, identity=identity, label=detection.label,
                bbox=detection.bbox, center=detection.center, center_norm=center_norm,
                velocity_norm_s=velocity, speed_norm_s=speed, confidence=detection.confidence,
                detection_mode=detection.detection_mode, raw_id=detection.raw_id,
                age_frames=track.age_frames, recovered=recovered, metadata=detection.metadata,
            ))
        missing: set[str] = set()
        for identity, track in list(self._tracks.items()):
            if identity in seen:
                continue
            missing.add(identity)
            if track.missing_since is None:
                track.missing_since = timestamp
            if timestamp - track.last_seen > self.recovery_seconds * 4:
                del self._tracks[identity]
        return observed, missing

    def track(self, identity: str) -> _Track | None:
        return self._tracks.get(identity)

    def reset(self) -> None:
        """Drop all temporal tracks after a source discontinuity."""
        self._tracks.clear()
