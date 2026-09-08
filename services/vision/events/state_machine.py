from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from ..release_evidence import verified_release


class ItemState(StrEnum):
    UNKNOWN = "UNKNOWN"
    VISIBLE_STATIC = "VISIBLE_STATIC"
    HAND_NEAR = "HAND_NEAR"
    PICKUP_CANDIDATE = "PICKUP_CANDIDATE"
    CARRIED = "CARRIED"
    PLACED_CANDIDATE = "PLACED_CANDIDATE"
    PLACED_CONFIRMED = "PLACED_CONFIRMED"
    OCCLUDED = "OCCLUDED"
    EXITED_VIEW = "EXITED_VIEW"
    LOST = "LOST"


@dataclass(slots=True)
class MotionObservation:
    center_norm: tuple[float, float]
    speed_norm_s: float
    zone_id: str | None
    zone_name: str | None
    confidence: float
    velocity_norm_s: tuple[float, float] = (0.0, 0.0)
    hand_near: bool = False
    detection_mode: str = "aruco"
    source_frame: int | None = None
    source_timestamp: float | None = None
    source_session_id: str | None = None
    reconnect_epoch: int = 0
    hand_model_healthy: bool = False
    support_surface_confirmed: bool = False
    hand_interaction: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StateEvent:
    # The first eight fields intentionally retain the old constructor order so
    # downstream callers fail soft while migrating to movement-only events.
    event_type: str
    confidence: float
    evidence_type: str
    previous_zone_id: str | None
    previous_zone_name: str | None
    new_zone_id: str | None
    new_zone_name: str | None
    notes: str
    movement_session_id: str = ""
    source_session_id: str = ""
    source_frame_start: int = 0
    source_frame_end: int = 0
    source_timestamp_start: float = 0.0
    source_timestamp_end: float = 0.0
    from_position: tuple[float, float] | None = None
    to_position: tuple[float, float] | None = None
    pickup_evidence: dict[str, Any] = field(default_factory=dict)
    placement_evidence: dict[str, Any] = field(default_factory=dict)
    evidence_status: str = "confirmed"


@dataclass(slots=True)
class _StableBaseline:
    position: tuple[float, float]
    zone_id: str | None
    zone_name: str | None
    monotonic_timestamp: float
    source_timestamp: float
    source_frame: int
    confidence: float
    detection_mode: str


@dataclass(slots=True)
class _MovementEpisode:
    movement_session_id: str
    source_session_id: str
    reconnect_epoch: int
    from_baseline: _StableBaseline
    started_monotonic: float
    started_source_timestamp: float
    started_frame: int
    pickup_observation: MotionObservation
    hand_seen: bool
    min_confidence: float
    detection_frames: int = 1
    peak_displacement: float = 0.0
    destination_anchor: tuple[float, float] | None = None
    destination_zone_id: str | None = None
    destination_zone_name: str | None = None
    stable_since: float | None = None
    stable_frames: int = 0


class ItemMotionStateMachine:
    """Evidence-gated state machine that emits one row per complete movement.

    A normal observation only updates current state. There are no ``seen``,
    ``picked_up`` or intermediate ``moved`` history events. A ``movement`` is
    emitted only after a stable baseline, continuous detections, meaningful
    displacement and a second stable position in the same source session.
    """

    def __init__(
        self,
        *,
        min_detection_frames: int = 3,
        min_stable_frames: int = 5,
        max_frame_gap_seconds: float = 0.75,
        min_move_distance: float = 0.04,
        same_zone_move_distance: float = 0.10,
        min_confidence: float = 0.55,
        stable_speed: float = 0.025,
        stable_position_jitter: float = 0.018,
        min_stable_seconds: float | None = None,
        placement_release_seconds: float = 5.,
        static_seconds: float = 1.5,
        movement_speed: float = 0.035,
        occluded_seconds: float = 0.6,
        lost_seconds: float = 8.0,
        edge_margin: float = 0.045,
        # Deprecated knobs remain accepted so old configuration does not crash.
        seen_seconds: float | None = None,
        min_static_before_pickup: float | None = None,
        pickup_confirm_seconds: float | None = None,
        movement_step: float | None = None,
    ) -> None:
        self.min_detection_frames = max(2, int(min_detection_frames))
        self.min_stable_frames = max(2, int(min_stable_frames))
        self.max_frame_gap_seconds = max(0.05, float(max_frame_gap_seconds))
        self.min_move_distance = max(0.001, float(min_move_distance))
        self.same_zone_move_distance = max(self.min_move_distance, float(same_zone_move_distance))
        self.min_confidence = min(1.0, max(0.0, float(min_confidence)))
        self.stable_speed = max(0.0, float(stable_speed))
        self.stable_position_jitter = max(0.001, float(stable_position_jitter))
        self.min_stable_seconds = max(0.0, float(static_seconds if min_stable_seconds is None else min_stable_seconds))
        self.placement_release_seconds = max(0.0, float(placement_release_seconds))
        self.movement_speed = max(0.0, float(movement_speed))
        self.occluded_seconds = max(0.0, float(occluded_seconds))
        self.lost_seconds = max(self.occluded_seconds, float(lost_seconds))
        self.edge_margin = min(0.25, max(0.0, float(edge_margin)))

        self.state = ItemState.UNKNOWN
        self.baseline: _StableBaseline | None = None
        self.episode: _MovementEpisode | None = None
        self.last_observation: MotionObservation | None = None
        self.last_seen_at: float | None = None
        # Raw candidates remain diagnostic-only until a continuous stable
        # baseline has been established in this source session.
        self.last_verified_observation: MotionObservation | None = None
        self.last_frame_at: float | None = None
        self.missing_since: float | None = None
        self.source_session_id: str | None = None
        self.reconnect_epoch = 0
        self.last_rejection: str | None = None
        self.rejection_counts: dict[str, int] = {}

        self._last_frame: int | None = None
        self._synthetic_frame = 0
        self._baseline_anchor: tuple[float, float] | None = None
        self._baseline_since: float | None = None
        self._baseline_stable_frames = 0
        self._consecutive_detections = 0
        self._last_emitted_movement_session_id: str | None = None

    def _reject(self, reason: str) -> None:
        self.last_rejection = reason
        self.rejection_counts[reason] = self.rejection_counts.get(reason, 0) + 1

    @staticmethod
    def _source_time(observation: MotionObservation, timestamp: float) -> float:
        return float(observation.source_timestamp if observation.source_timestamp is not None else timestamp)

    def _frame_number(self, observation: MotionObservation | None, frame_sequence: int | None) -> int:
        value = frame_sequence if frame_sequence is not None else (observation.source_frame if observation else None)
        if value is not None:
            return int(value)
        self._synthetic_frame += 1
        return self._synthetic_frame

    def reset_source_session(self, source_session_id: str, reconnect_epoch: int = 0, reason: str = "source_reset") -> None:
        """Invalidate every temporal premise after disconnect, seek or restart."""
        if self.episode is not None:
            self._reject(f"episode_rejected:{reason}")
        self.source_session_id = str(source_session_id)
        self.reconnect_epoch = int(reconnect_epoch)
        self.state = ItemState.UNKNOWN
        self.baseline = None
        self.episode = None
        self.last_observation = None
        self.last_seen_at = None
        self.last_verified_observation = None
        self.last_frame_at = None
        self.missing_since = None
        self._last_frame = None
        self._baseline_anchor = None
        self._baseline_since = None
        self._baseline_stable_frames = 0
        self._consecutive_detections = 0

    def _invalidate_episode(self, reason: str) -> None:
        if self.episode is not None:
            self._reject(f"episode_rejected:{reason}")
        self.episode = None
        self.baseline = None
        self._baseline_anchor = None
        self._baseline_since = None
        self._baseline_stable_frames = 0
        self._consecutive_detections = 0

    def _handle_missing(self, timestamp: float, reason: str) -> None:
        if self.missing_since is None:
            self.missing_since = self.last_seen_at if self.last_seen_at is not None else timestamp
            self._invalidate_episode(reason)
        missing_for = timestamp - self.missing_since
        if missing_for >= self.lost_seconds:
            self.state = ItemState.LOST
        elif missing_for >= self.occluded_seconds:
            self.state = ItemState.OCCLUDED
        else:
            self.state = ItemState.UNKNOWN
        self._reject(reason)

    @property
    def observation_verified(self) -> bool:
        return (
            self.baseline is not None
            and self.missing_since is None
            and self.last_verified_observation is not None
            and self.last_verified_observation.source_frame == self._last_frame
        )

    def _is_stable(self, observation: MotionObservation, anchor: tuple[float, float] | None) -> bool:
        if observation.speed_norm_s > self.stable_speed:
            return False
        return anchor is None or math.dist(anchor, observation.center_norm) <= self.stable_position_jitter

    def _set_baseline(self, observation: MotionObservation, timestamp: float, frame: int) -> None:
        self.baseline = _StableBaseline(
            observation.center_norm,
            observation.zone_id,
            observation.zone_name,
            timestamp,
            self._source_time(observation, timestamp),
            frame,
            observation.confidence,
            observation.detection_mode,
        )
        self.state = ItemState.HAND_NEAR if observation.hand_near else ItemState.VISIBLE_STATIC
        self._baseline_anchor = observation.center_norm
        self._baseline_since = timestamp
        self._baseline_stable_frames = self.min_stable_frames
        self.last_verified_observation = observation

    def _accumulate_baseline(self, observation: MotionObservation, timestamp: float, frame: int) -> None:
        if not self._is_stable(observation, self._baseline_anchor):
            self._baseline_anchor = observation.center_norm
            self._baseline_since = timestamp
            self._baseline_stable_frames = 1 if observation.speed_norm_s <= self.stable_speed else 0
            self.state = ItemState.UNKNOWN
            return
        if self._baseline_anchor is None:
            self._baseline_anchor = observation.center_norm
            self._baseline_since = timestamp
            self._baseline_stable_frames = 1
        else:
            self._baseline_stable_frames += 1
        stable_for = timestamp - (self._baseline_since if self._baseline_since is not None else timestamp)
        if (
            self._consecutive_detections >= self.min_detection_frames
            and self._baseline_stable_frames >= self.min_stable_frames
            and stable_for >= self.min_stable_seconds
        ):
            self._set_baseline(observation, timestamp, frame)

    def _start_episode(self, observation: MotionObservation, timestamp: float, frame: int) -> None:
        assert self.baseline is not None
        session_id = self.source_session_id or observation.source_session_id or "legacy"
        self.episode = _MovementEpisode(
            movement_session_id=str(uuid.uuid4()),
            source_session_id=session_id,
            reconnect_epoch=self.reconnect_epoch,
            from_baseline=self.baseline,
            started_monotonic=timestamp,
            started_source_timestamp=self._source_time(observation, timestamp),
            started_frame=frame,
            pickup_observation=observation,
            hand_seen=observation.hand_near,
            min_confidence=min(self.baseline.confidence, observation.confidence),
            peak_displacement=math.dist(self.baseline.position, observation.center_norm),
        )
        self.state = ItemState.PICKUP_CANDIDATE
        self._baseline_anchor = None
        self._baseline_since = None
        self._baseline_stable_frames = 0

    def _evidence_type(self, observation: MotionObservation, hand_seen: bool) -> str:
        return f"{observation.detection_mode}+hand" if hand_seen else f"{observation.detection_mode}_motion"

    def _finish_episode(self, observation: MotionObservation, timestamp: float, frame: int) -> StateEvent | None:
        episode = self.episode
        if episode is None:
            return None
        from_baseline = episode.from_baseline
        distance = math.dist(from_baseline.position, observation.center_norm)
        crossed_zone = (
            from_baseline.zone_id is not None
            and observation.zone_id is not None
            and from_baseline.zone_id != observation.zone_id
        )
        required_distance = self.min_move_distance if crossed_zone else self.same_zone_move_distance
        enough_frames = episode.detection_frames >= self.min_detection_frames
        if distance < required_distance or not enough_frames:
            reason = "insufficient_displacement" if distance < required_distance else "insufficient_detection_frames"
            self._reject(f"episode_rejected:{reason}")
            self.episode = None
            self._set_baseline(observation, timestamp, frame)
            return None
        if episode.movement_session_id == self._last_emitted_movement_session_id:
            self._reject("duplicate_movement_session")
            self.episode = None
            self._set_baseline(observation, timestamp, frame)
            return None

        confidence = round(min(0.99, max(self.min_confidence, episode.min_confidence * 0.96)), 3)
        pickup_evidence = {
            "source_frame": from_baseline.source_frame,
            "source_timestamp": from_baseline.source_timestamp,
            "position": list(from_baseline.position),
            "zone_id": from_baseline.zone_id,
            "zone_name": from_baseline.zone_name,
            "hand_near": episode.hand_seen,
            "movement_detected_frame": episode.started_frame,
            "movement_detected_timestamp": episode.started_source_timestamp,
        }
        placement_evidence = {
            "source_frame": frame,
            "source_timestamp": self._source_time(observation, timestamp),
            "position": list(observation.center_norm),
            "zone_id": observation.zone_id,
            "zone_name": observation.zone_name,
            "stable_frames": episode.stable_frames,
            "hand_near": observation.hand_near,
            "hand_model_healthy": observation.hand_model_healthy,
            "support_surface_confirmed": observation.support_surface_confirmed,
            "hand_interaction": dict(observation.hand_interaction),
            "release_stable_seconds": observation.hand_interaction.get("release_stable_seconds", 0.),
            "release_required_seconds": self.placement_release_seconds,
        }
        event = StateEvent(
            event_type="movement",
            confidence=confidence,
            evidence_type=self._evidence_type(observation, episode.hand_seen),
            previous_zone_id=from_baseline.zone_id,
            previous_zone_name=from_baseline.zone_name,
            new_zone_id=observation.zone_id,
            new_zone_name=observation.zone_name,
            notes=(
                f"连续证据确认移动，位移 {distance:.3f}；"
                f"新位置稳定 {episode.stable_frames} 帧"
            ),
            movement_session_id=episode.movement_session_id,
            source_session_id=episode.source_session_id,
            source_frame_start=from_baseline.source_frame,
            source_frame_end=frame,
            source_timestamp_start=from_baseline.source_timestamp,
            source_timestamp_end=self._source_time(observation, timestamp),
            from_position=from_baseline.position,
            to_position=observation.center_norm,
            pickup_evidence=pickup_evidence,
            placement_evidence=placement_evidence,
            evidence_status="confirmed",
        )
        self._last_emitted_movement_session_id = episode.movement_session_id
        self.episode = None
        self._set_baseline(observation, timestamp, frame)
        self.state = (ItemState.VISIBLE_STATIC if observation.detection_mode == 'experimental'
                      and not (observation.support_surface_confirmed and observation.hand_model_healthy
                               and verified_release(observation.hand_interaction, episode.source_session_id,
                                                    from_baseline.source_frame, frame,
                                                    min_stable_seconds=self.placement_release_seconds))
                      else ItemState.PLACED_CONFIRMED)
        self.last_rejection = None
        return event

    def update(
        self,
        observation: MotionObservation | None,
        timestamp: float,
        *,
        source_session_id: str | None = None,
        reconnect_epoch: int | None = None,
        frame_sequence: int | None = None,
        source_timestamp: float | None = None,
    ) -> list[StateEvent]:
        session_id = str(
            source_session_id
            or (observation.source_session_id if observation else None)
            or self.source_session_id
            or "legacy"
        )
        epoch = int(
            reconnect_epoch
            if reconnect_epoch is not None
            else (observation.reconnect_epoch if observation else self.reconnect_epoch)
        )
        frame = self._frame_number(observation, frame_sequence)

        if self.source_session_id is None:
            self.source_session_id = session_id
            self.reconnect_epoch = epoch
        elif session_id != self.source_session_id or epoch != self.reconnect_epoch:
            self.reset_source_session(session_id, epoch, "source_session_changed")

        if self._last_frame is not None and frame <= self._last_frame:
            self._reject("duplicate_or_out_of_order_frame")
            return []
        # A stream that continues supplying frames with no detection is not a
        # disconnected camera. Keep the verified last sighting through loss.
        if self.last_frame_at is not None and timestamp - self.last_frame_at > self.max_frame_gap_seconds:
            self.reset_source_session(session_id, epoch, "frame_gap")
            self._reject("frame_gap")

        self._last_frame = frame
        self.last_frame_at = timestamp
        if observation is None:
            self._handle_missing(timestamp, "detection_missing")
            return []
        if source_timestamp is not None:
            observation.source_timestamp = float(source_timestamp)
        observation.source_frame = frame
        observation.source_session_id = session_id
        observation.reconnect_epoch = epoch
        if observation.confidence < self.min_confidence:
            self._handle_missing(timestamp, "low_confidence")
            return []

        if self.missing_since is not None:
            self.missing_since = None
            self._invalidate_episode("reappeared_after_occlusion")
            self.state = ItemState.UNKNOWN

        previous_observation = self.last_observation
        self._consecutive_detections += 1
        self.last_observation = observation
        self.last_seen_at = timestamp

        if self.baseline is None:
            self._accumulate_baseline(observation, timestamp, frame)
            return []

        self.last_verified_observation = observation

        if self.episode is None:
            displacement = math.dist(self.baseline.position, observation.center_norm)
            if displacement >= self.min_move_distance:
                self._start_episode(observation, timestamp, frame)
                return []

            # Re-anchor small, genuinely stable adjustments so box jitter cannot
            # accumulate forever into a false movement episode.
            if self._is_stable(observation, self._baseline_anchor):
                if self._baseline_since is None:
                    self._baseline_since = timestamp
                    self._baseline_stable_frames = 1
                else:
                    self._baseline_stable_frames += 1
                if (
                    self._baseline_stable_frames >= self.min_stable_frames
                    and timestamp - self._baseline_since >= self.min_stable_seconds
                ):
                    self._set_baseline(observation, timestamp, frame)
            else:
                self._baseline_anchor = observation.center_norm
                self._baseline_since = timestamp
                self._baseline_stable_frames = 1 if observation.speed_norm_s <= self.stable_speed else 0
            self.state = ItemState.HAND_NEAR if observation.hand_near else ItemState.VISIBLE_STATIC
            return []

        episode = self.episode
        episode.detection_frames += 1
        episode.hand_seen = episode.hand_seen or observation.hand_near
        episode.min_confidence = min(episode.min_confidence, observation.confidence)
        displacement = math.dist(episode.from_baseline.position, observation.center_norm)
        episode.peak_displacement = max(episode.peak_displacement, displacement)
        step = math.dist(previous_observation.center_norm, observation.center_norm) if previous_observation else displacement

        stable_here = (observation.speed_norm_s <= self.stable_speed and step <= self.stable_position_jitter
                       and not observation.hand_near
                       and observation.hand_interaction.get('holding_status') not in
                       {'co_moving', 'holding_uncertain'})
        same_destination = (
            episode.destination_anchor is not None
            and observation.zone_id == episode.destination_zone_id
            and math.dist(episode.destination_anchor, observation.center_norm) <= self.stable_position_jitter
        )
        if stable_here and (episode.destination_anchor is None or same_destination):
            if episode.destination_anchor is None:
                episode.destination_anchor = observation.center_norm
                episode.destination_zone_id = observation.zone_id
                episode.destination_zone_name = observation.zone_name
                episode.stable_since = timestamp
                episode.stable_frames = 1
            else:
                episode.stable_frames += 1
            self.state = ItemState.PLACED_CANDIDATE
        else:
            episode.destination_anchor = None
            episode.destination_zone_id = None
            episode.destination_zone_name = None
            episode.stable_since = None
            episode.stable_frames = 0
            self.state = ItemState.CARRIED

        stable_for = timestamp - (episode.stable_since if episode.stable_since is not None else timestamp)
        if episode.stable_frames >= self.min_stable_frames and stable_for >= self.min_stable_seconds:
            # A photo identity has no tactile placement proof. Keep current
            # observations fresh, but do not write an episode while an observed
            # hand separation is still settling or is entirely unverified.
            if observation.detection_mode == 'experimental' and not verified_release(
                observation.hand_interaction, episode.source_session_id,
                episode.from_baseline.source_frame, frame, min_stable_seconds=self.placement_release_seconds,
            ):
                self._reject("waiting_for_verified_release")
                return []
            event = self._finish_episode(observation, timestamp, frame)
            return [event] if event is not None else []
        return []

    def diagnostics(self) -> dict[str, Any]:
        episode = self.episode
        return {
            "state": self.state.value,
            "source_session_id": self.source_session_id,
            "reconnect_epoch": self.reconnect_epoch,
            "last_frame": self._last_frame,
            "last_seen_at": self.last_seen_at,
            "last_frame_at": self.last_frame_at,
            "observation_verified": self.observation_verified,
            "verified_source_frame": self.last_verified_observation.source_frame if self.last_verified_observation else None,
            "baseline": (
                {
                    "position": list(self.baseline.position),
                    "zone_id": self.baseline.zone_id,
                    "source_frame": self.baseline.source_frame,
                }
                if self.baseline
                else None
            ),
            "candidate": (
                {
                    "movement_session_id": episode.movement_session_id,
                    "source_frame_start": episode.started_frame,
                    "from_zone_id": episode.from_baseline.zone_id,
                    "peak_displacement": round(episode.peak_displacement, 5),
                    "detection_frames": episode.detection_frames,
                    "placement_stable_frames": episode.stable_frames,
                    "release_remaining_seconds": (self.last_observation.hand_interaction.get("release_remaining_seconds")
                                                  if self.last_observation else None),
                    "release_required_seconds": self.placement_release_seconds,
                }
                if episode
                else None
            ),
            "last_rejection": self.last_rejection,
            "rejection_counts": dict(self.rejection_counts),
        }
