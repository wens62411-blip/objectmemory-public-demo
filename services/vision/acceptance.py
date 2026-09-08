from __future__ import annotations

import math
import statistics
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable

from .capture import FramePacket
from .detectors import Detection


class AcceptanceState(str, Enum):
    STABLE_AT_ORIGIN = "STABLE_AT_ORIGIN"
    MOVEMENT_STARTED = "MOVEMENT_STARTED"
    IN_TRANSIT = "IN_TRANSIT"
    ENTERED_DESTINATION = "ENTERED_DESTINATION"
    STABILIZING = "STABILIZING"
    MOVEMENT_CONFIRMED = "MOVEMENT_CONFIRMED"
    OCCLUDED = "OCCLUDED"
    ABORTED = "ABORTED"
    CAMERA_INTERRUPTED = "CAMERA_INTERRUPTED"


TERMINAL_ACCEPTANCE_STATES = {
    AcceptanceState.MOVEMENT_CONFIRMED,
    AcceptanceState.ABORTED,
    AcceptanceState.CAMERA_INTERRUPTED,
}

ACCEPTANCE_PRE_SECONDS = 5.0
ACCEPTANCE_POST_SECONDS = 5.0


def _utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class AcceptanceThresholds:
    """All acceptance thresholds live here so deployments cannot hide magic values."""

    origin_stable_frames: int = 8
    destination_stable_frames: int = 8
    min_stable_seconds: float = 1.0
    stable_jitter_norm: float = 0.012
    stable_speed_norm_s: float = 0.06
    origin_capture_jitter_norm: float = 0.025
    calibration_jitter_multiplier: float = 2.5
    max_calibrated_jitter_norm: float = 0.03
    min_exit_frames: int = 2
    min_transit_frames: int = 2
    min_movement_distance_norm: float = 0.10
    min_trajectory_length_norm: float = 0.12
    occlusion_seconds: float = 0.50
    abort_lost_seconds: float = 4.0
    max_frame_gap_seconds: float = 1.0
    max_run_seconds: float = 120.0
    min_confidence: float = 0.70
    min_marker_size_px: float = 8.0
    max_trajectory_points: int = 600

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "AcceptanceThresholds":
        raw = dict(value or {})
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(f"未知验收阈值：{', '.join(unknown)}")
        defaults = cls()
        normalized: dict[str, int | float] = {}
        for key, value in raw.items():
            if isinstance(value, bool):
                raise ValueError(f"验收阈值 {key} 必须是数字")
            try:
                if isinstance(getattr(defaults, key), int):
                    number = int(value)
                    if float(value) != number:
                        raise ValueError
                    normalized[key] = number
                else:
                    normalized[key] = float(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"验收阈值 {key} 必须是数字") from exc
        parsed = cls(**normalized)
        integer_fields = {
            "origin_stable_frames": parsed.origin_stable_frames,
            "destination_stable_frames": parsed.destination_stable_frames,
            "min_exit_frames": parsed.min_exit_frames,
            "min_transit_frames": parsed.min_transit_frames,
            "max_trajectory_points": parsed.max_trajectory_points,
        }
        if any(isinstance(value, bool) or int(value) != value or int(value) < 1 for value in integer_fields.values()):
            raise ValueError("帧数和轨迹容量阈值必须是正整数")
        nonnegative = {
            key: float(getattr(parsed, key))
            for key in (
                "min_stable_seconds",
                "stable_jitter_norm",
                "stable_speed_norm_s",
                "origin_capture_jitter_norm",
                "max_calibrated_jitter_norm",
                "min_movement_distance_norm",
                "min_trajectory_length_norm",
                "occlusion_seconds",
                "abort_lost_seconds",
                "max_frame_gap_seconds",
                "max_run_seconds",
                "min_marker_size_px",
            )
        }
        if any(not math.isfinite(number) or number < 0 for number in nonnegative.values()):
            raise ValueError("验收阈值必须是有限的非负数")
        if not math.isfinite(float(parsed.calibration_jitter_multiplier)) or parsed.calibration_jitter_multiplier < 1:
            raise ValueError("calibration_jitter_multiplier 必须至少为 1")
        if not 0 <= parsed.min_confidence <= 1:
            raise ValueError("min_confidence 必须位于 0 到 1")
        if parsed.abort_lost_seconds <= parsed.occlusion_seconds:
            raise ValueError("abort_lost_seconds 必须大于 occlusion_seconds")
        if parsed.max_frame_gap_seconds <= 0 or parsed.max_run_seconds <= 0:
            raise ValueError("帧间隔与运行时限必须大于 0")
        return parsed


@dataclass(frozen=True, slots=True)
class AcceptanceZone:
    id: str
    name: str
    points: tuple[tuple[float, float], ...]

    @classmethod
    def from_dict(cls, value: dict[str, Any], label: str) -> "AcceptanceZone":
        if not isinstance(value, dict):
            raise ValueError(f"{label} 必须是包含归一化 points 的对象")
        zone_id = str(value.get("id") or value.get("zone_id") or "").strip()
        name = str(value.get("name") or "").strip()
        if not zone_id or not name:
            raise ValueError(f"{label} 必须包含 id 和 name")
        raw_points = value.get("points")
        if not isinstance(raw_points, (list, tuple)) or len(raw_points) < 3:
            raise ValueError(f"{label} 至少需要三个归一化坐标点")
        points: list[tuple[float, float]] = []
        for point in raw_points:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError(f"{label} 坐标必须是 [x,y]")
            x, y = float(point[0]), float(point[1])
            if not math.isfinite(x) or not math.isfinite(y) or not (0 <= x <= 1 and 0 <= y <= 1):
                raise ValueError(f"{label} 必须使用 0 到 1 的归一化坐标")
            points.append((x, y))
        area = abs(sum(
            points[index][0] * points[(index + 1) % len(points)][1]
            - points[(index + 1) % len(points)][0] * points[index][1]
            for index in range(len(points))
        )) / 2
        if area <= 1e-6:
            raise ValueError(f"{label} 多边形面积不能为零")
        return cls(zone_id, name, tuple(points))

    def contains(self, point: tuple[float, float]) -> bool:
        x, y = point
        inside = False
        for index, (x1, y1) in enumerate(self.points):
            x2, y2 = self.points[(index + 1) % len(self.points)]
            cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
            if abs(cross) <= 1e-9 and min(x1, x2) - 1e-9 <= x <= max(x1, x2) + 1e-9 and min(y1, y2) - 1e-9 <= y <= max(y1, y2) + 1e-9:
                return True
            if (y1 > y) != (y2 > y):
                intersection_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
                if x < intersection_x:
                    inside = not inside
        return inside

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "points": [list(point) for point in self.points]}


@dataclass(frozen=True, slots=True)
class AcceptanceConfig:
    validation_run_id: str
    item_id: str
    aruco_id: int
    camera_id: str
    source_session_id: str
    reconnect_epoch: int
    trial_kind: str
    minimum_duration_seconds: float
    scenario_index: int
    origin_zone: AcceptanceZone
    destination_zone: AcceptanceZone
    thresholds: AcceptanceThresholds

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AcceptanceConfig":
        if not isinstance(value, dict):
            raise ValueError("验收配置必须是对象")
        run_id = str(value.get("validation_run_id") or "").strip()
        item_id = str(value.get("item_id") or "").strip()
        camera_id = str(value.get("camera_id") or "").strip()
        source_session_id = str(value.get("source_session_id") or "").strip()
        if not run_id or not item_id or not camera_id or not source_session_id:
            raise ValueError("validation_run_id、item_id、camera_id、source_session_id 均不能为空")
        raw_aruco = value.get("aruco_id")
        if isinstance(raw_aruco, bool):
            raise ValueError("aruco_id 必须是整数")
        try:
            aruco_id = int(raw_aruco)
        except (TypeError, ValueError) as exc:
            raise ValueError("aruco_id 必须是整数") from exc
        if str(raw_aruco).strip() != str(aruco_id) or not 0 <= aruco_id < 50:
            raise ValueError("aruco_id 必须是当前 4x4_50 字典中的 0 到 49")
        reconnect_epoch = value.get("reconnect_epoch", 0)
        if isinstance(reconnect_epoch, bool):
            raise ValueError("reconnect_epoch 必须是非负整数")
        try:
            reconnect_epoch = int(reconnect_epoch)
        except (TypeError, ValueError) as exc:
            raise ValueError("reconnect_epoch 必须是非负整数") from exc
        if reconnect_epoch < 0:
            raise ValueError("reconnect_epoch 必须是非负整数")
        trial_kind = str(value.get("trial_kind") or "movement").strip().lower()
        duration_bounds = {
            "movement": (0.0, 3600.0),
            "stationary": (300.0, 3600.0),
            "minor_adjustment": (5.0, 300.0),
            "occlusion": (1.0, 300.0),
            "disconnect_reconnect": (0.0, 300.0),
        }
        if trial_kind not in duration_bounds:
            raise ValueError("trial_kind 必须是 movement/stationary/minor_adjustment/occlusion/disconnect_reconnect")
        minimum_duration = value.get("minimum_duration_seconds", duration_bounds[trial_kind][0])
        if isinstance(minimum_duration, bool):
            raise ValueError("minimum_duration_seconds 必须是数字")
        try:
            minimum_duration = float(minimum_duration)
        except (TypeError, ValueError) as exc:
            raise ValueError("minimum_duration_seconds 必须是数字") from exc
        lower, upper = duration_bounds[trial_kind]
        if not math.isfinite(minimum_duration) or not lower <= minimum_duration <= upper:
            raise ValueError(f"{trial_kind} 的 minimum_duration_seconds 必须位于 {lower:g} 到 {upper:g}")
        raw_scenario_index = value.get("scenario_index")
        if isinstance(raw_scenario_index, bool):
            raise ValueError("scenario_index 必须是 1 到 100 的整数")
        try:
            scenario_index = int(raw_scenario_index)
        except (TypeError, ValueError) as exc:
            raise ValueError("scenario_index 必须是 1 到 100 的整数") from exc
        if str(raw_scenario_index).strip() != str(scenario_index) or not 1 <= scenario_index <= 100:
            raise ValueError("scenario_index 必须是 1 到 100 的整数")
        origin = AcceptanceZone.from_dict(value.get("origin_zone"), "origin_zone")
        destination = AcceptanceZone.from_dict(value.get("destination_zone"), "destination_zone")
        if trial_kind == "movement" and (origin.id == destination.id or origin.points == destination.points):
            raise ValueError("起点区域与终点区域必须不同")
        return cls(
            validation_run_id=run_id,
            item_id=item_id,
            aruco_id=aruco_id,
            camera_id=camera_id,
            source_session_id=source_session_id,
            reconnect_epoch=reconnect_epoch,
            trial_kind=trial_kind,
            minimum_duration_seconds=minimum_duration,
            scenario_index=scenario_index,
            origin_zone=origin,
            destination_zone=destination,
            thresholds=AcceptanceThresholds.from_dict(value.get("thresholds")),
        )


class AcceptanceRun:
    """A fail-closed, single-marker movement acceptance state machine."""

    def __init__(self, config: AcceptanceConfig, *, armed_wall_time: float, armed_monotonic: float) -> None:
        self.config = config
        self.state = AcceptanceState.STABLE_AT_ORIGIN
        self.reason = "等待同一 ArUco 标签在起点区域多帧稳定"
        self.marker_status = "WAITING_FOR_ORIGIN"
        self.armed_wall_time = armed_wall_time
        self.armed_monotonic = armed_monotonic
        self.first_frame_monotonic: float | None = None
        self.updated_wall_time = armed_wall_time
        self.updated_monotonic = armed_monotonic
        self.last_frame_sequence: int | None = None
        self.last_frame_wall_time: float | None = None
        self.last_evidence: dict[str, Any] | None = None
        self.origin_samples: list[dict[str, Any]] = []
        self.destination_samples: list[dict[str, Any]] = []
        self.trajectory: list[dict[str, Any]] = []
        self.state_history: list[dict[str, Any]] = []
        self.baseline: dict[str, Any] | None = None
        self.calibration_snapshot: dict[str, Any] | None = None
        self.effective_thresholds = asdict(config.thresholds)
        self.effective_thresholds["max_run_seconds"] = max(
            config.thresholds.max_run_seconds,
            config.minimum_duration_seconds + config.thresholds.max_frame_gap_seconds,
        )
        self.exit_streak = 0
        self.exit_candidate: dict[str, Any] | None = None
        self.transit_frames = 0
        self.lost_frames = 0
        self.lost_since: float | None = None
        self.movement_started: dict[str, Any] | None = None
        self.entered_destination: dict[str, Any] | None = None
        self.confirmed_after: dict[str, Any] | None = None
        self.before_evidence: dict[str, Any] | None = None
        self.event_count = 0
        self.completed_without_event = False
        self.completion_outcome: str | None = None
        self.total_lost_frames = 0
        self.outside_origin_frames = 0
        self.destination_frames = 0
        self.detected_frames = 0
        self.continuous_detection_started: float | None = None
        self.observed_max_displacement = 0.0
        self.saw_occlusion = False
        self.before_frame_sequence: int | None = None
        self._record_transition(self.state, self.reason, armed_wall_time, armed_monotonic, None)

    @property
    def terminal(self) -> bool:
        return self.completed_without_event or self.state in TERMINAL_ACCEPTANCE_STATES

    def _record_transition(
        self,
        state: AcceptanceState,
        reason: str,
        wall_time: float,
        monotonic_time: float,
        frame_sequence: int | None,
    ) -> None:
        self.state = state
        self.reason = reason
        self.updated_wall_time = wall_time
        self.updated_monotonic = monotonic_time
        transition = {
            "state": state.value,
            "reason": reason,
            "frame_sequence": frame_sequence,
            "frame_timestamp": _utc_iso(wall_time),
            "wall_time": wall_time,
            "monotonic_time": monotonic_time,
        }
        if self.state_history and self.state_history[-1]["state"] == state.value and self.state_history[-1]["reason"] == reason:
            self.state_history[-1] = transition
        else:
            self.state_history.append(transition)

    def interrupt(self, reason: str, *, wall_time: float | None = None, monotonic_time: float | None = None) -> None:
        if self.terminal and not (self.state == AcceptanceState.MOVEMENT_CONFIRMED and self.event_count == 0):
            return
        wall = float(self.updated_wall_time if wall_time is None else wall_time)
        monotonic = float(self.updated_monotonic if monotonic_time is None else monotonic_time)
        self.marker_status = "CAMERA_INTERRUPTED"
        self._record_transition(AcceptanceState.CAMERA_INTERRUPTED, str(reason), wall, monotonic, self.last_frame_sequence)

    def cancel(self, reason: str, *, wall_time: float, monotonic_time: float) -> None:
        if self.terminal and not (self.state == AcceptanceState.MOVEMENT_CONFIRMED and self.event_count == 0):
            return
        self.marker_status = "CANCELLED"
        self._record_transition(AcceptanceState.ABORTED, str(reason), wall_time, monotonic_time, self.last_frame_sequence)

    def mark_event_emitted(self) -> None:
        if self.state == AcceptanceState.MOVEMENT_CONFIRMED and self.event_count == 0:
            self.event_count = 1
            self.marker_status = "CONFIRMED"

    def observe_postroll(self, packet: FramePacket) -> None:
        """Validate continuity while a confirmed run collects mandatory post-roll."""
        if self.state != AcceptanceState.MOVEMENT_CONFIRMED or self.event_count != 0:
            return
        if packet.source_session_id != self.config.source_session_id or packet.reconnect_epoch != self.config.reconnect_epoch:
            self.interrupt("post_roll_source_session_changed", wall_time=packet.wall_time, monotonic_time=packet.monotonic_time)
            return
        if self.last_frame_sequence is not None and packet.sequence <= self.last_frame_sequence:
            self.interrupt("post_roll_frame_sequence_not_increasing", wall_time=packet.wall_time, monotonic_time=packet.monotonic_time)
            return
        if packet.monotonic_time - self.updated_monotonic > self.config.thresholds.max_frame_gap_seconds:
            self.interrupt("post_roll_frame_gap_exceeded", wall_time=packet.wall_time, monotonic_time=packet.monotonic_time)
            return
        if self.last_frame_wall_time is not None and packet.wall_time < self.last_frame_wall_time:
            self.interrupt("post_roll_wall_clock_moved_backwards", wall_time=packet.wall_time, monotonic_time=packet.monotonic_time)
            return
        self.last_frame_sequence = packet.sequence
        self.last_frame_wall_time = packet.wall_time
        self.updated_wall_time = packet.wall_time
        self.updated_monotonic = packet.monotonic_time

    def _complete_negative_trial(self, evidence: dict[str, Any], outcome: str) -> None:
        self.completed_without_event = True
        self.completion_outcome = outcome
        self.reason = outcome
        self.updated_wall_time = evidence["wall_time"]
        self.updated_monotonic = evidence["monotonic_time"]
        self.marker_status = "NEGATIVE_SAMPLE_COMPLETE"

    def _maybe_complete_negative_trial(self, evidence: dict[str, Any]) -> bool:
        kind = self.config.trial_kind
        if kind == "movement" or self.config.minimum_duration_seconds <= 0:
            return False
        first = self.first_frame_monotonic
        if first is None or evidence["monotonic_time"] - first < self.config.minimum_duration_seconds:
            return False
        if kind == "stationary":
            eligible = (
                self.baseline is not None
                and self.total_lost_frames == 0
                and self.outside_origin_frames == 0
                and self.movement_started is None
            )
            if eligible:
                self._complete_negative_trial(evidence, "stationary_duration_complete_without_movement_event")
            return eligible
        if kind == "minor_adjustment":
            eligible = (
                self.baseline is not None
                and self.total_lost_frames == 0
                and self.outside_origin_frames == 0
                and self.movement_started is None
                and self.observed_max_displacement < float(self.effective_thresholds["min_movement_distance_norm"])
            )
            if eligible:
                self._complete_negative_trial(evidence, "minor_adjustment_window_complete_without_movement_event")
            return eligible
        if kind == "occlusion":
            eligible = (
                self.baseline is not None
                and self.saw_occlusion
                and self.destination_frames == 0
                and self.event_count == 0
            )
            if eligible:
                self._complete_negative_trial(evidence, "occlusion_window_complete_without_confirmed_placement")
            return eligible
        return False

    def _zone_for(self, center: tuple[float, float]) -> dict[str, str] | None:
        if self.config.origin_zone.contains(center):
            return {"id": self.config.origin_zone.id, "name": self.config.origin_zone.name}
        if self.config.destination_zone.contains(center):
            return {"id": self.config.destination_zone.id, "name": self.config.destination_zone.name}
        return None

    def _detection_evidence(
        self,
        packet: FramePacket,
        detection: Detection,
        frame_shape: tuple[int, ...],
    ) -> dict[str, Any] | None:
        if detection.detection_mode != "aruco" or detection.raw_id != self.config.aruco_id:
            return None
        if detection.confidence < self.config.thresholds.min_confidence or len(detection.corners) != 4:
            return None
        height, width = frame_shape[:2]
        bbox_width, bbox_height = float(detection.bbox[2]), float(detection.bbox[3])
        if min(bbox_width, bbox_height) < self.config.thresholds.min_marker_size_px:
            return None
        center = (detection.center[0] / max(width, 1), detection.center[1] / max(height, 1))
        if not (0 <= center[0] <= 1 and 0 <= center[1] <= 1):
            return None
        corners_px = [[float(x), float(y)] for x, y in detection.corners]
        corners_norm = [[float(x) / max(width, 1), float(y) / max(height, 1)] for x, y in detection.corners]
        return deepcopy({
            "frame_sequence": packet.sequence,
            "source_frame": packet.sequence,
            "frame_timestamp": _utc_iso(packet.wall_time),
            "source_timestamp": _utc_iso(packet.wall_time),
            "timestamp": _utc_iso(packet.wall_time),
            "wall_time": packet.wall_time,
            "monotonic_time": packet.monotonic_time,
            "source_session_id": packet.source_session_id,
            "reconnect_epoch": packet.reconnect_epoch,
            "center": [center[0], center[1]],
            "center_px": [float(detection.center[0]), float(detection.center[1])],
            "center_norm": [center[0], center[1]],
            "corners": corners_px,
            "corners_norm": corners_norm,
            "frame_size_px": [int(width), int(height)],
            "marker_size_px": [bbox_width, bbox_height],
            "bbox": [int(value) for value in detection.bbox],
            "confidence": float(detection.confidence),
            "aruco_id": int(detection.raw_id),
            "zone": self._zone_for(center),
            "zone_id": (self._zone_for(center) or {}).get("id"),
            "zone_name": (self._zone_for(center) or {}).get("name"),
            "lost": False,
        })

    @staticmethod
    def _sample_stats(samples: Iterable[dict[str, Any]]) -> tuple[tuple[float, float], float, float, float]:
        values = list(samples)
        centers = [(sample["center_norm"][0], sample["center_norm"][1]) for sample in values]
        mean = (
            sum(point[0] for point in centers) / len(centers),
            sum(point[1] for point in centers) / len(centers),
        )
        jitter = max((math.dist(point, mean) for point in centers), default=0.0)
        speeds = []
        for previous, current in zip(values, values[1:]):
            elapsed = current["monotonic_time"] - previous["monotonic_time"]
            if elapsed > 0:
                speeds.append(math.dist(previous["center_norm"], current["center_norm"]) / elapsed)
        duration = values[-1]["monotonic_time"] - values[0]["monotonic_time"] if len(values) > 1 else 0.0
        return mean, jitter, max(speeds, default=0.0), duration

    def _stable_window(
        self,
        samples: list[dict[str, Any]],
        minimum_frames: int,
    ) -> list[dict[str, Any]]:
        """Return the shortest recent window satisfying both frame and time gates.

        ``minimum_frames`` is a lower bound, not the window length.  Treating it
        as an exact slice made the default 10-frame gate impossible at 5 FPS:
        ten samples span only about 1.8 seconds, while the configured stability
        duration is three seconds.  Keep walking backwards until the duration
        gate is also covered, while retaining a bounded amount of metadata.
        """
        if len(samples) < minimum_frames:
            return []
        start = len(samples) - minimum_frames
        required_duration = float(self.config.thresholds.min_stable_seconds)
        while start > 0 and samples[-1]["monotonic_time"] - samples[start]["monotonic_time"] < required_duration:
            start -= 1
        window = samples[start:]
        if window[-1]["monotonic_time"] - window[0]["monotonic_time"] < required_duration:
            return []
        return window

    def _append_trajectory(self, evidence: dict[str, Any]) -> None:
        if len(self.trajectory) >= self.config.thresholds.max_trajectory_points:
            # Keep endpoints and recent evidence while bounding memory in a long run.
            del self.trajectory[1]
        self.trajectory.append(dict(evidence))

    def _trajectory_length(self) -> float:
        points = [point["center_norm"] for point in self.trajectory if not point.get("lost")]
        return sum(math.dist(previous, current) for previous, current in zip(points, points[1:]))

    def _calibrate_origin(self, evidence: dict[str, Any]) -> None:
        threshold = self.config.thresholds
        self.origin_samples.append(evidence)
        # 256 metadata-only samples cover more than three seconds even at the
        # supported maximum inference rate, without allowing an unbounded list.
        keep = max(threshold.origin_stable_frames * 4, 256)
        self.origin_samples = self.origin_samples[-keep:]
        window = self._stable_window(self.origin_samples, threshold.origin_stable_frames)
        if not window:
            return
        mean, jitter, speed, duration = self._sample_stats(window)
        if jitter > threshold.origin_capture_jitter_norm or speed > threshold.stable_speed_norm_s:
            return
        calibrated_jitter = min(
            threshold.max_calibrated_jitter_norm,
            max(threshold.stable_jitter_norm, jitter * threshold.calibration_jitter_multiplier),
        )
        movement_distance = max(threshold.min_movement_distance_norm, calibrated_jitter * 4)
        marker_widths = [sample["marker_size_px"][0] for sample in window]
        marker_heights = [sample["marker_size_px"][1] for sample in window]
        self.effective_thresholds.update({
            "stable_jitter_norm": calibrated_jitter,
            "min_movement_distance_norm": movement_distance,
        })
        self.baseline = {
            "center_norm": [mean[0], mean[1]],
            "first_frame_sequence": window[0]["frame_sequence"],
            "frame_sequence": window[-1]["frame_sequence"],
            "frame_timestamp": window[-1]["frame_timestamp"],
            "wall_time": window[-1]["wall_time"],
            "monotonic_time": window[-1]["monotonic_time"],
            "marker_size_px": [statistics.median(marker_widths), statistics.median(marker_heights)],
            "observed_jitter_norm": jitter,
            "observed_max_speed_norm_s": speed,
            "stable_duration_seconds": duration,
            "zone": self.config.origin_zone.as_dict(),
        }
        self.calibration_snapshot = {
            "sample_count": len(window),
            "observed_jitter_norm": jitter,
            "observed_max_speed_norm_s": speed,
            "marker_size_px_median": self.baseline["marker_size_px"],
            "configured_thresholds": asdict(threshold),
            "effective_thresholds": dict(self.effective_thresholds),
        }
        self.before_frame_sequence = window[-1]["frame_sequence"]
        self.before_evidence = dict(window[-1])
        self.marker_status = "STABLE_AT_ORIGIN"
        self._record_transition(
            AcceptanceState.STABLE_AT_ORIGIN,
            "起点稳定基线已建立",
            evidence["wall_time"],
            evidence["monotonic_time"],
            evidence["frame_sequence"],
        )

    def _destination_is_stable(self) -> bool:
        threshold = self.config.thresholds
        window = self._stable_window(self.destination_samples, threshold.destination_stable_frames)
        if not window:
            return False
        _mean, jitter, speed, duration = self._sample_stats(window)
        return (
            jitter <= float(self.effective_thresholds["stable_jitter_norm"])
            and speed <= threshold.stable_speed_norm_s
        )

    def observe(
        self,
        packet: FramePacket,
        detection: Detection | None,
        frame_shape: tuple[int, ...],
        *,
        ring_seconds: float = 0.0,
    ) -> bool:
        """Consume one real source frame; return True only on confirmation edge."""
        if self.terminal:
            return False
        if packet.source_session_id != self.config.source_session_id or packet.reconnect_epoch != self.config.reconnect_epoch:
            self.interrupt(
                "source_session_or_reconnect_epoch_changed",
                wall_time=packet.wall_time,
                monotonic_time=packet.monotonic_time,
            )
            return False
        if self.last_frame_sequence is not None:
            if packet.sequence <= self.last_frame_sequence:
                self.interrupt("frame_sequence_not_increasing", wall_time=packet.wall_time, monotonic_time=packet.monotonic_time)
                return False
            if packet.monotonic_time - self.updated_monotonic > self.config.thresholds.max_frame_gap_seconds:
                self.interrupt("frame_gap_exceeded", wall_time=packet.wall_time, monotonic_time=packet.monotonic_time)
                return False
            if self.last_frame_wall_time is not None and packet.wall_time < self.last_frame_wall_time:
                self.interrupt("frame_wall_clock_moved_backwards", wall_time=packet.wall_time, monotonic_time=packet.monotonic_time)
                return False
        if self.first_frame_monotonic is None:
            self.first_frame_monotonic = packet.monotonic_time
        effective_max_run = max(
            self.config.thresholds.max_run_seconds,
            self.config.minimum_duration_seconds + self.config.thresholds.max_frame_gap_seconds,
        )
        if packet.monotonic_time - self.first_frame_monotonic > effective_max_run:
            self.cancel("acceptance_run_timed_out", wall_time=packet.wall_time, monotonic_time=packet.monotonic_time)
            return False
        self.last_frame_sequence = packet.sequence
        self.last_frame_wall_time = packet.wall_time
        self.updated_wall_time = packet.wall_time
        self.updated_monotonic = packet.monotonic_time

        evidence = self._detection_evidence(packet, detection, frame_shape) if detection is not None else None
        if evidence is None:
            # Detection loss is not a stable observation. Never stitch samples
            # from before an occlusion to its first recovery frame and call
            # that a continuously stable origin or confirmed placement.
            self.origin_samples.clear()
            self.destination_samples.clear()
            self.exit_streak = 0
            self.exit_candidate = None
            self.continuous_detection_started = None
            if self.transit_frames < self.config.thresholds.min_transit_frames:
                self.transit_frames = 0
            lost = {
                "frame_sequence": packet.sequence,
                "source_frame": packet.sequence,
                "frame_timestamp": _utc_iso(packet.wall_time),
                "source_timestamp": _utc_iso(packet.wall_time),
                "timestamp": _utc_iso(packet.wall_time),
                "wall_time": packet.wall_time,
                "monotonic_time": packet.monotonic_time,
                "source_session_id": packet.source_session_id,
                "reconnect_epoch": packet.reconnect_epoch,
                "center": None,
                "center_px": None,
                "center_norm": None,
                "corners": None,
                "corners_norm": None,
                "frame_size_px": [int(frame_shape[1]), int(frame_shape[0])],
                "marker_size_px": None,
                "zone": None,
                "zone_id": None,
                "zone_name": None,
                "lost": True,
            }
            self.last_evidence = lost
            self.lost_frames += 1
            self.total_lost_frames += 1
            if self.lost_since is None:
                self.lost_since = packet.monotonic_time
            if self.baseline is not None:
                self._append_trajectory(lost)
            lost_for = packet.monotonic_time - self.lost_since
            self.marker_status = "LOST"
            if lost_for >= self.config.thresholds.occlusion_seconds:
                self.saw_occlusion = True
            if self.config.trial_kind == "occlusion" and self._maybe_complete_negative_trial(lost):
                return False
            if lost_for >= self.config.thresholds.abort_lost_seconds and self.config.trial_kind != "occlusion":
                self.cancel("aruco_marker_not_rediscovered", wall_time=packet.wall_time, monotonic_time=packet.monotonic_time)
            elif lost_for >= self.config.thresholds.occlusion_seconds and self.state != AcceptanceState.OCCLUDED:
                self._record_transition(AcceptanceState.OCCLUDED, "目标标签暂时遮挡；不会据此确认放置", packet.wall_time, packet.monotonic_time, packet.sequence)
            return False

        self.last_evidence = evidence
        self.detected_frames += 1
        if self.continuous_detection_started is None:
            self.continuous_detection_started = packet.monotonic_time
        self.marker_status = "DETECTED"
        self.lost_frames = 0
        self.lost_since = None
        zone = evidence["zone"]
        center = (evidence["center_norm"][0], evidence["center_norm"][1])

        if self.baseline is None:
            if zone and zone["id"] == self.config.origin_zone.id:
                self._calibrate_origin(evidence)
            else:
                self.origin_samples.clear()
                self.reason = "尚未在起点区域建立稳定基线"
            self._maybe_complete_negative_trial(evidence)
            return False

        self._append_trajectory(evidence)
        baseline_center = tuple(self.baseline["center_norm"])
        displacement = math.dist(center, baseline_center)
        self.observed_max_displacement = max(self.observed_max_displacement, displacement)
        outside_origin = not self.config.origin_zone.contains(center)
        in_destination = self.config.destination_zone.contains(center)
        if outside_origin:
            self.outside_origin_frames += 1
        if in_destination and self.config.destination_zone.id != self.config.origin_zone.id:
            self.destination_frames += 1
        movement_distance = float(self.effective_thresholds["min_movement_distance_norm"])

        if self.config.trial_kind in {"stationary", "minor_adjustment"} and (
            outside_origin or displacement >= movement_distance
        ):
            self.cancel("negative_trial_observed_meaningful_or_cross_zone_movement", wall_time=packet.wall_time, monotonic_time=packet.monotonic_time)
            return False

        if not outside_origin:
            self.exit_streak = 0
            self.exit_candidate = None
            self.destination_samples.clear()
            if self.movement_started is not None:
                returned = [sample for sample in self.trajectory[-self.config.thresholds.origin_stable_frames:] if not sample.get("lost")]
                if len(returned) >= self.config.thresholds.origin_stable_frames:
                    _mean, jitter, speed, duration = self._sample_stats(returned)
                    if duration >= self.config.thresholds.min_stable_seconds and jitter <= float(self.effective_thresholds["stable_jitter_norm"]) and speed <= self.config.thresholds.stable_speed_norm_s:
                        self.cancel("marker_returned_to_origin_without_destination_placement", wall_time=packet.wall_time, monotonic_time=packet.monotonic_time)
            else:
                # Keep the designated before frame close to the actual exit,
                # while retaining the calibrated (averaged) baseline center.
                self.before_frame_sequence = evidence["frame_sequence"]
                self.before_evidence = dict(evidence)
                self.baseline.update({
                    "frame_sequence": evidence["frame_sequence"],
                    "frame_timestamp": evidence["frame_timestamp"],
                    "wall_time": evidence["wall_time"],
                    "monotonic_time": evidence["monotonic_time"],
                    "marker_size_px": list(evidence["marker_size_px"]),
                })
                self._maybe_complete_negative_trial(evidence)
            return False

        if self.exit_streak == 0:
            self.exit_candidate = dict(evidence)
        self.exit_streak += 1
        if self.movement_started is None:
            if self.exit_streak < self.config.thresholds.min_exit_frames or displacement < movement_distance:
                return False
            if ring_seconds + 1e-6 < ACCEPTANCE_PRE_SECONDS:
                self.cancel("insufficient_five_second_pre_roll", wall_time=packet.wall_time, monotonic_time=packet.monotonic_time)
                return False
            self.movement_started = dict(self.exit_candidate or evidence)
            self._record_transition(AcceptanceState.MOVEMENT_STARTED, "标签已连续离开起点且位移超过校准阈值", packet.wall_time, packet.monotonic_time, packet.sequence)

        if not in_destination:
            self.transit_frames += 1
            self.destination_samples.clear()
            if self.transit_frames >= self.config.thresholds.min_transit_frames:
                self._record_transition(AcceptanceState.IN_TRANSIT, "已观察到起点与终点之外的连续真实轨迹", packet.wall_time, packet.monotonic_time, packet.sequence)
            return False

        if (
            self.transit_frames < self.config.thresholds.min_transit_frames
            or self._trajectory_length() < self.config.thresholds.min_trajectory_length_norm
        ):
            self.reason = "已看到终点标签，但缺少离区后的连续中途轨迹"
            return False

        if not self.destination_samples:
            self.entered_destination = dict(evidence)
            self._record_transition(AcceptanceState.ENTERED_DESTINATION, "目标标签进入终点区域", packet.wall_time, packet.monotonic_time, packet.sequence)
        self.destination_samples.append(evidence)
        self.destination_samples = self.destination_samples[-max(self.config.thresholds.destination_stable_frames * 4, 256):]
        if not self._destination_is_stable():
            self._record_transition(AcceptanceState.STABILIZING, "终点已检测，等待多帧再次稳定", packet.wall_time, packet.monotonic_time, packet.sequence)
            return False

        self.confirmed_after = dict(evidence)
        if self.config.trial_kind != "movement":
            self.cancel("negative_trial_reached_confirmed_destination", wall_time=packet.wall_time, monotonic_time=packet.monotonic_time)
            return False
        self.marker_status = "CONFIRMED_PENDING_POSTROLL"
        self._record_transition(AcceptanceState.MOVEMENT_CONFIRMED, "同一标签完成起点稳定、真实移动、终点再稳定", packet.wall_time, packet.monotonic_time, packet.sequence)
        return True

    def status(self) -> dict[str, Any]:
        evidence = self.last_evidence or {}
        displacement = None
        if self.baseline and evidence.get("center_norm"):
            displacement = math.dist(self.baseline["center_norm"], evidence["center_norm"])
        stable_remaining = 0.0
        samples: list[dict[str, Any]] = []
        if self.baseline is None:
            samples = self.origin_samples
        elif self.state in {AcceptanceState.ENTERED_DESTINATION, AcceptanceState.STABILIZING}:
            samples = self.destination_samples
        if samples:
            duration = samples[-1]["monotonic_time"] - samples[0]["monotonic_time"]
            stable_remaining = max(0.0, self.config.thresholds.min_stable_seconds - duration)
        observed_duration = (
            max(0.0, self.updated_monotonic - self.first_frame_monotonic)
            if self.first_frame_monotonic is not None else 0.0
        )
        negative_criteria_met = (
            self.event_count == 0
            and (
                self.completed_without_event
                or (
                    self.config.trial_kind == "disconnect_reconnect"
                    and self.state == AcceptanceState.CAMERA_INTERRUPTED
                )
            )
        )
        return deepcopy({
            "validation_run_id": self.config.validation_run_id,
            "item_id": self.config.item_id,
            "aruco_id": self.config.aruco_id,
            "camera_id": self.config.camera_id,
            "source_session_id": self.config.source_session_id,
            "reconnect_epoch": self.config.reconnect_epoch,
            "trial_kind": self.config.trial_kind,
            "minimum_duration_seconds": self.config.minimum_duration_seconds,
            "scenario_index": self.config.scenario_index,
            "state": self.state.value,
            "terminal": self.terminal,
            "marker_status": self.marker_status,
            "reason": self.reason,
            "frame_sequence": evidence.get("frame_sequence"),
            "frame_timestamp": evidence.get("frame_timestamp"),
            "frame_wall_time": evidence.get("wall_time"),
            "frame_monotonic_time": evidence.get("monotonic_time"),
            "center": evidence.get("center"),
            "center_norm": evidence.get("center_norm"),
            "corners": evidence.get("corners"),
            "corners_norm": evidence.get("corners_norm"),
            "marker_size_px": evidence.get("marker_size_px"),
            "frame_size_px": evidence.get("frame_size_px"),
            "current_zone": evidence.get("zone"),
            "stable_remaining_seconds": round(stable_remaining, 6),
            "observed_duration_seconds": observed_duration,
            "trial_remaining_seconds": max(0.0, self.config.minimum_duration_seconds - observed_duration),
            "displacement": displacement,
            "lost_frames": self.lost_frames,
            "event_count": self.event_count,
            "trial_complete": self.completed_without_event,
            "completion_outcome": self.completion_outcome,
            "negative_sample_eligible": negative_criteria_met,
            "negative_passed_criteria_met": negative_criteria_met,
            "total_lost_frames": self.total_lost_frames,
            "outside_origin_frames": self.outside_origin_frames,
            "destination_frames": self.destination_frames,
            "detected_frames": self.detected_frames,
            "observed_max_displacement": self.observed_max_displacement,
            "saw_occlusion": self.saw_occlusion,
            "required_pre_seconds": ACCEPTANCE_PRE_SECONDS,
            "required_post_seconds": ACCEPTANCE_POST_SECONDS,
            "origin_zone": self.config.origin_zone.as_dict(),
            "destination_zone": self.config.destination_zone.as_dict(),
            "thresholds": {
                "configured": asdict(self.config.thresholds),
                "effective": dict(self.effective_thresholds),
            },
            "calibration_snapshot": self.calibration_snapshot,
            "baseline": self.baseline,
            "before_frame": dict(self.before_evidence) if self.before_evidence is not None else None,
            "after_frame": dict(self.confirmed_after) if self.confirmed_after is not None else None,
            "trajectory": [dict(point) for point in self.trajectory],
            "state_history": [dict(item) for item in self.state_history],
            "armed_at": _utc_iso(self.armed_wall_time),
            "updated_at": _utc_iso(self.updated_wall_time),
        })
