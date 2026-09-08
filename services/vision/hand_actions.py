"""Bounded, conservative geometry of observed hands, never proof of grasping.

Coordinates and direction refer to the original source image (right/down are
positive), not a mirrored display or world axes. ``timestamp`` is source seconds.
Only adjacent unambiguous observations share a hand ID; missing hands are not
predicted or associated across an absence. No images, models or I/O are retained.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
import math
import threading
from uuid import uuid4


DEFAULT_SETTINGS = {
    "hand_action_max_hands": 2,
    "hand_action_max_gap_seconds": .75,
    "hand_action_min_stable_frames": 3,
    "hand_action_min_stable_seconds": .2,
    "hand_action_tracking_min_frames": 3,
    "hand_action_tracking_min_seconds": .2,
    "hand_action_history_size": 12,
    "hand_action_motion_window_seconds": .75,
    "hand_action_motion_min_frames": 3,
    "hand_action_motion_min_seconds": .2,
    "hand_action_motion_min_distance": .025,
    "hand_action_still_distance": .012,
    "hand_action_axis_dominance": 1.5,
    "hand_action_motion_directness": .75,
    "hand_action_match_max_distance": .15,
    "hand_action_match_max_speed": 1.5,
    "hand_action_match_jitter": .02,
    "hand_action_match_margin": .025,
    "hand_action_match_scale_ratio": 2.,
    "hand_action_handedness_min_score": .7,
    "hand_action_min_palm_diagonal_ratio": .015,
    "hand_action_extended_angle": 150.,
    "hand_action_curled_angle": 105.,
    "hand_action_extension_ratio": .15,
    "hand_action_curl_distance_ratio": .15,
    "hand_action_pinch_ratio": .28,
    "hand_action_pinch_other_extended": 2,
    "hand_action_thumb_open_ratio": .7,
    "hand_action_thumb_folded_ratio": 1.15,
}

POSTURE_LABELS = {"open_palm": "张手（几何推断）", "closed_fist": "握拳（几何推断）",
                  "pinch": "捏合（几何推断）", "pointing": "指向（几何推断）",
                  "fingers_extended": "四指展开（几何推断）", "unknown": "手势未确认"}
MOTION_LABELS = {"still": "画面内基本静止", "left": "向画面左侧", "right": "向画面右侧",
                 "up": "向画面上方", "down": "向画面下方", "unknown": "运动方向未确认"}
_INTEGER_BOUNDS = {"max_hands": (1, 2), "min_stable_frames": (2, 30), "history_size": (3, 60),
                   "motion_min_frames": (2, 30), "tracking_min_frames": (2, 30), "pinch_other_extended": (1, 3)}
_FLOAT_BOUNDS = {
    "max_gap_seconds": (.01, 5), "min_stable_seconds": (.01, 5),
    "tracking_min_seconds": (.01, 5),
    "motion_window_seconds": (.05, 5), "motion_min_seconds": (.01, 5),
    "motion_min_distance": (.001, 1), "still_distance": (.0001, .5),
    "axis_dominance": (1.01, 10), "motion_directness": (.5, 1),
    "match_max_distance": (.001, 1), "match_max_speed": (.01, 10), "match_jitter": (0, .25),
    "match_margin": (.001, .5), "match_scale_ratio": (1.01, 4), "handedness_min_score": (.5, 1),
    "min_palm_diagonal_ratio": (.0001, .5), "extended_angle": (120, 179), "curled_angle": (30, 119),
    "extension_ratio": (.01, 1), "curl_distance_ratio": (0, 1), "pinch_ratio": (.01, .5),
    "thumb_open_ratio": (.1, 2), "thumb_folded_ratio": (.1, 3),
}


def _finite(value):
    return type(value) in (float, int) and math.isfinite(value)


def _angle(a, b, c):
    first = (a[0]-b[0], a[1]-b[1])
    second = (c[0]-b[0], c[1]-b[1])
    product = math.hypot(*first) * math.hypot(*second)
    if product <= 1e-9:
        return None
    cosine = (first[0]*second[0] + first[1]*second[1]) / product
    return math.degrees(math.acos(max(-1., min(1., cosine))))


@dataclass
class _Track:
    hand_id: str
    observation: dict
    history: deque
    candidate: str
    candidate_since: float
    streak: int
    tracking_since: float
    tracking_frames: int


class HandActionAnalyzer:
    def __init__(self, settings: dict | None = None):
        supplied = settings or {}
        self.settings = {key: supplied.get(key, default) for key, default in DEFAULT_SETTINGS.items()}
        for suffix, (low, high) in _INTEGER_BOUNDS.items():
            value = self.settings[f"hand_action_{suffix}"]
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"hand_action_{suffix} must be an integer in [{low}, {high}]")
        for suffix, (low, high) in _FLOAT_BOUNDS.items():
            value = self.settings[f"hand_action_{suffix}"]
            if not _finite(value) or not low <= value <= high:
                raise ValueError(f"hand_action_{suffix} must be finite in [{low}, {high}]")
        if self._get("history_size") < self._get("motion_min_frames"):
            raise ValueError("hand_action_history_size must cover motion_min_frames")
        if self._get("motion_window_seconds") < self._get("motion_min_seconds"):
            raise ValueError("hand_action_motion_window_seconds must cover motion_min_seconds")
        if self._get("still_distance") >= self._get("motion_min_distance"):
            raise ValueError("hand_action_still_distance must be below motion_min_distance")
        self._lock = threading.RLock()
        self._tracks: dict[str, _Track] = {}
        self._context = None
        self._last_frame = None
        self._last_timestamp = None
        self._last_reset = "not_started"
        self._rejected = 0

    def _get(self, suffix):
        return self.settings[f"hand_action_{suffix}"]

    def reset(self, reason="manual"):
        with self._lock:
            self._tracks.clear()
            self._context = self._last_frame = self._last_timestamp = None
            self._last_reset = str(reason)[:120]

    def health(self):
        with self._lock:
            return {"backend": "conservative_landmark_geometry_v1", "active_tracks": len(self._tracks),
                    "history_samples": sum(len(track.history) for track in self._tracks.values()),
                    "last_reset_reason": self._last_reset, "rejected_observations": self._rejected,
                    "thresholds": dict(self.settings), "grasp_established": False}

    def _observation(self, hand, width, height):
        points = getattr(hand, "landmarks", None)
        bbox = getattr(hand, "bbox", None)
        if not isinstance(points, (list, tuple)) or len(points) != 21:
            return None
        if any(not isinstance(point, (list, tuple)) or len(point) != 2
               or any(not _finite(v) or not 0 <= v <= 1 for v in point) for point in points):
            return None
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or not all(_finite(v) for v in bbox):
            return None
        x, y, box_width, box_height = bbox
        if not (0 <= x < width and 0 <= y < height and box_width > 0 and box_height > 0
                and x+box_width <= width and y+box_height <= height):
            return None
        pixels = [(float(x)*width, float(y)*height) for x, y in points]
        palm = max(math.dist(pixels[0], pixels[9]), math.dist(pixels[5], pixels[17]))
        if palm < math.hypot(width, height) * self._get("min_palm_diagonal_ratio"):
            return None
        side = getattr(hand, "handedness", None)
        side = side if side in {"Left", "Right"} else None
        score = getattr(hand, "handedness_score", None)
        score = float(score) if _finite(score) and 0 <= score <= 1 else None
        trusted_side = side if score is not None and score >= self._get("handedness_min_score") else None
        center = [sum(points[index][axis] for index in (0, 5, 9, 13, 17))/5 for axis in (0, 1)]
        return {"landmarks": [list(point) for point in points], "pixels": pixels, "palm": palm,
                "center": center, "bbox": [x/width, y/height, box_width/width, box_height/height],
                "handedness": side, "handedness_score": score, "trusted_side": trusted_side}

    def _posture(self, observation):
        points, palm = observation["pixels"], observation["palm"]
        finger_states = []
        angles = []
        for mcp in (5, 9, 13, 17):
            pip, dip, tip = mcp+1, mcp+2, mcp+3
            pip_angle = _angle(points[mcp], points[pip], points[dip])
            dip_angle = _angle(points[pip], points[dip], points[tip])
            angles.append([pip_angle, dip_angle])
            extension = (math.dist(points[0], points[tip])-math.dist(points[0], points[pip]))/palm
            if pip_angle is None or dip_angle is None:
                finger_states.append("unknown")
            elif min(pip_angle, dip_angle) >= self._get("extended_angle") and extension >= self._get("extension_ratio"):
                finger_states.append("extended")
            elif min(pip_angle, dip_angle) <= self._get("curled_angle") and extension <= self._get("curl_distance_ratio"):
                finger_states.append("curled")
            else:
                finger_states.append("unknown")
        pinch_ratio = math.dist(points[4], points[8])/palm
        thumb_angle = _angle(points[2], points[3], points[4])
        thumb_open = (thumb_angle is not None and thumb_angle >= self._get("extended_angle")
                      and math.dist(points[4], points[5])/palm >= self._get("thumb_open_ratio"))
        thumb_folded = math.dist(points[4], points[9])/palm <= self._get("thumb_folded_ratio")
        if pinch_ratio <= self._get("pinch_ratio") and finger_states[1:].count("extended") >= self._get("pinch_other_extended"):
            posture = "pinch"
        elif finger_states == ["extended"]*4 and thumb_open:
            posture = "open_palm"
        elif finger_states == ["curled"]*4 and thumb_folded:
            posture = "closed_fist"
        elif finger_states == ["extended", "curled", "curled", "curled"]:
            posture = "pointing"
        elif finger_states == ["extended"]*4:
            # Observable four-finger geometry remains useful when the thumb
            # projection cannot justify the more specific open-palm label.
            posture = "fingers_extended"
        else:
            posture = "unknown"
        return posture, {"finger_states": finger_states, "pip_dip_angles_degrees": angles,
                         "thumb_index_distance_palm_ratio": pinch_ratio, "thumb_angle_degrees": thumb_angle,
                         "palm_scale_pixels": palm, "method": "projected_2d_geometry_not_grasp_classifier"}

    def _associate(self, observations, width, height, dt):
        tracks = list(self._tracks.values())
        diagonal = math.hypot(width, height)
        max_distance = min(self._get("match_max_distance"), self._get("match_jitter")+self._get("match_max_speed")*dt)
        costs = {}
        for i, observation in enumerate(observations):
            for j, track in enumerate(tracks):
                old = track.observation
                if old["trusted_side"] != observation["trusted_side"]:
                    continue
                ratio = observation["palm"]/old["palm"]
                if not 1/self._get("match_scale_ratio") <= ratio <= self._get("match_scale_ratio"):
                    continue
                dx, dy = (observation["center"][axis]-old["center"][axis] for axis in (0, 1))
                distance = math.hypot(dx*width, dy*height)/diagonal
                if distance <= max_distance:
                    costs[i, j] = distance
        blocked_observations, blocked_tracks = set(), set()
        margin = self._get("match_margin")
        for i in range(len(observations)):
            candidates = sorted((cost, j) for (row, j), cost in costs.items() if row == i)
            if len(candidates) > 1 and candidates[1][0]-candidates[0][0] < margin:
                blocked_observations.add(i)
                blocked_tracks.update(j for _cost, j in candidates)
        for j in range(len(tracks)):
            candidates = sorted((cost, i) for (i, column), cost in costs.items() if column == j)
            if len(candidates) > 1 and candidates[1][0]-candidates[0][0] < margin:
                blocked_tracks.add(j)
                blocked_observations.update(i for _cost, i in candidates)
        matches = {}
        used = set()
        for (i, j), _cost in sorted(costs.items(), key=lambda pair: pair[1]):
            if i not in matches and j not in used and i not in blocked_observations and j not in blocked_tracks:
                matches[i] = tracks[j]
                used.add(j)
        ambiguous = {i for i, j in costs if i in blocked_observations or j in blocked_tracks}
        return matches, ambiguous

    def _motion(self, history, timestamp):
        samples = [(stamp, center) for stamp, center in history if timestamp-stamp <= self._get("motion_window_seconds")+1e-9]
        span = samples[-1][0]-samples[0][0]
        dx, dy = (samples[-1][1][axis]-samples[0][1][axis] for axis in (0, 1))
        distance = math.hypot(dx, dy)
        path_distance = sum(math.dist(a[1], b[1]) for a, b in zip(samples, samples[1:]))
        extent = max(math.dist(samples[0][1], sample[1]) for sample in samples)
        evidence = {"sample_count": len(samples), "duration_seconds": span, "delta": [dx, dy],
                    "displacement": distance, "path_distance": path_distance, "coordinate_space": "source_normalized"}
        motion = "unknown"
        if len(samples) >= self._get("motion_min_frames") and span+1e-9 >= self._get("motion_min_seconds"):
            if extent <= self._get("still_distance"):
                motion = "still"
            elif distance >= self._get("motion_min_distance") and distance/max(path_distance, 1e-9) >= self._get("motion_directness"):
                if abs(dx) >= abs(dy)*self._get("axis_dominance"):
                    motion = "right" if dx > 0 else "left"
                elif abs(dy) >= abs(dx)*self._get("axis_dominance"):
                    motion = "down" if dy > 0 else "up"
        return motion, evidence

    def update(self, hands, frame_shape, source_session_id, source_frame, timestamp):
        """Analyze one actual source frame; duplicate/out-of-order frames return []."""
        with self._lock:
            valid_shape = (isinstance(frame_shape, (tuple, list)) and len(frame_shape) >= 2
                           and all(type(v) is int and 1 <= v <= 16384 for v in frame_shape[:2]))
            if (not valid_shape or not isinstance(source_session_id, str) or not source_session_id or len(source_session_id) > 200
                    or type(source_frame) is not int or source_frame < 1
                    or not _finite(timestamp) or not 0 <= timestamp < 2**53):
                self.reset("invalid_source_metadata")
                raise ValueError("Hand action source requires valid dimensions, session, positive frame and finite seconds")
            height, width = frame_shape[:2]
            context = (source_session_id, width, height)
            if self._context is not None and self._context != context:
                self.reset("source_or_geometry_changed")
            elif self._last_frame is not None and (source_frame <= self._last_frame or timestamp <= self._last_timestamp):
                self._tracks.clear()
                # Keep the watermark. Old replayed frames must not establish a
                # new stable identity merely because the previous one was reset.
                self._last_reset = "non_monotonic_source"
                return []
            elif self._last_timestamp is not None and timestamp-self._last_timestamp > self._get("max_gap_seconds"):
                self.reset("source_frame_gap")
            dt = timestamp-self._last_timestamp if self._last_timestamp is not None else 0.
            self._context, self._last_frame, self._last_timestamp = context, source_frame, float(timestamp)
            if not isinstance(hands, (list, tuple)) or len(hands) > self._get("max_hands"):
                self._tracks.clear()
                self._rejected += 1
                return []
            observations = []
            for hand in hands:
                observation = self._observation(hand, width, height)
                if observation is not None:
                    observations.append(observation)
                else:
                    self._rejected += 1
            matches, ambiguous = self._associate(observations, width, height, dt)
            next_tracks, results = {}, []
            for i, observation in enumerate(observations):
                posture, posture_evidence = self._posture(observation)
                track = matches.get(i)
                association = "continuous" if track is not None else "ambiguous_new_identity" if i in ambiguous else "new_identity"
                if track is None:
                    track = _Track(f"hand-{uuid4().hex}", observation,
                                   deque(maxlen=self._get("history_size")), posture, timestamp, 0, timestamp, 0)
                if posture != track.candidate:
                    track.candidate, track.candidate_since, track.streak = posture, timestamp, 0
                track.streak += 1
                track.tracking_frames += 1
                track.observation = observation
                track.history.append((timestamp, tuple(observation["center"])))
                stable = (posture != "unknown" and track.streak >= self._get("min_stable_frames")
                          and timestamp-track.candidate_since+1e-9 >= self._get("min_stable_seconds"))
                motion, motion_evidence = self._motion(track.history, timestamp)
                displayed_posture = posture if stable else "unknown"
                results.append({"hand_id": track.hand_id, "posture": displayed_posture,
                    "posture_label": POSTURE_LABELS[displayed_posture], "posture_candidate": posture,
                    "posture_stable_frames": track.streak, "posture_duration_seconds": timestamp-track.candidate_since,
                    "motion": motion, "motion_label": MOTION_LABELS[motion], "motion_stable": motion != "unknown",
                    "stable": stable, "center": list(observation["center"]), "center_basis": "palm_landmarks",
                    "tracking_stable": (track.tracking_frames >= self._get("tracking_min_frames")
                                        and timestamp-track.tracking_since+1e-9 >= self._get("tracking_min_seconds")),
                    "tracking_frames": track.tracking_frames, "tracking_duration_seconds": timestamp-track.tracking_since,
                    "bbox": list(observation["bbox"]), "bbox_format": "xywh", "landmarks": deepcopy(observation["landmarks"]),
                    "coordinate_space": "source_normalized", "handedness": observation["handedness"],
                    "handedness_score": observation["handedness_score"], "association": association,
                    "source_session_id": source_session_id, "source_frame": source_frame, "source_timestamp": timestamp,
                    "posture_evidence": posture_evidence, "motion_evidence": motion_evidence,
                    "evidence_type": "landmark_geometry_only", "grasp_established": False, "confidence": None})
                next_tracks[track.hand_id] = track
            # An absent/invalid/ambiguous hand cannot lend its ID to a later one.
            self._tracks = next_tracks
            return results
