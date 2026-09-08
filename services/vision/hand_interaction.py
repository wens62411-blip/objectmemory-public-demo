"""Conservative hand/object interaction from verified, normalized detections.

This is geometric temporal evidence, not proof of physical grasp/contact. It
owns no detector, thread, frame buffer, database or event writer. One tracker is
used per camera; callers must never pass predicted boxes as detected objects.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import math


DEFAULTS = {
    "hand_interaction_near_distance": .04,
    "hand_interaction_separation_distance": .07,
    "hand_interaction_min_motion_frames": 4,
    "hand_interaction_min_motion_seconds": .45,
    "hand_interaction_min_object_step": .004,
    "hand_interaction_min_hand_step": .004,
    "hand_interaction_min_total_distance": .035,
    "hand_interaction_min_direction_cosine": .8,
    "hand_interaction_max_vector_error": .02,
    "hand_interaction_max_relative_drift": .04,
    "hand_interaction_release_stable_frames": 4,
    "hand_interaction_release_stable_seconds": 5.,
    "hand_interaction_separation_min_seconds": .6,
    "hand_interaction_release_max_object_speed": .025,
    "hand_interaction_release_position_jitter": .018,
    "hand_interaction_release_min_distance_increase": .01,
    "hand_interaction_release_valid_seconds": 3.,
    "hand_interaction_max_gap_seconds": .75,
    "hand_interaction_state_ttl_seconds": 20.,
    "hand_interaction_max_items": 128,
    "hand_interaction_max_hands": 8,
}


def _point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("normalized point required")
    if any(type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= 1 for v in value):
        raise ValueError("point must be finite normalized coordinates")
    return tuple(float(v) for v in value)


def _box(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("normalized xywh required")
    if any(type(v) not in (float, int) or not math.isfinite(v) for v in value):
        raise ValueError("box must be finite")
    x, y, width, height = map(float, value)
    if min(x, y) < 0 or min(width, height) <= 0 or x+width > 1.000001 or y+height > 1.000001:
        raise ValueError("box must be within source frame")
    return x, y, width, height


@dataclass
class _State:
    session: str
    timestamp: float
    frame: int
    object_center: tuple | None = None
    hand_id: str | None = None
    hand_center: tuple | None = None
    hand_distance: float | None = None
    near_armed: bool = False
    established: bool = False
    motion_frames: int = 0
    motion_started_at: float | None = None
    motion_ended_at: float | None = None
    motion_origin: tuple | None = None
    relative_origin: tuple | None = None
    co_start: int | None = None
    co_end: int | None = None
    release_frames: int = 0
    release_visible_frames: int = 0
    separation_frame: int | None = None
    separation_timestamp: float | None = None
    release_started_at: float | None = None
    release_anchor: tuple | None = None
    release_start: int | None = None
    release_end: int | None = None
    released_at: float | None = None
    release_completed: bool = False
    completed_release_at: float | None = None
    expired_release_frame: int | None = None
    last_result: dict | None = None


class HandObjectInteractionTracker:
    DEFAULTS = DEFAULTS

    def __init__(self, settings=None):
        settings = settings or {}
        self.settings = {key: settings.get(key, default) for key, default in DEFAULTS.items()}
        for key, value in self.settings.items():
            default = DEFAULTS[key]
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{key} must be positive and finite")
            if type(default) is int and (type(value) is not int or value > (1024 if key.endswith("max_items") else 64)):
                raise ValueError(f"{key} must be a bounded integer")
            if type(default) is float and value > (3600 if key.endswith("seconds") else 1):
                raise ValueError(f"{key} exceeds its safe bound")
        if self._get("separation_distance") <= self._get("near_distance"):
            raise ValueError("separation_distance must exceed near_distance")
        if self._get("min_motion_frames") < 2 or self._get("release_stable_frames") < 2:
            raise ValueError("interaction admission requires multiple frames")
        self.states: OrderedDict[str, _State] = OrderedDict()

    def _get(self, key):
        return self.settings[f"hand_interaction_{key}"]

    def reset(self):
        self.states.clear()

    def forget(self, item_id):
        self.states.pop(str(item_id), None)

    def expire(self, timestamp):
        if type(timestamp) not in (int, float) or not math.isfinite(timestamp):
            return 0
        expired = [key for key, state in self.states.items() if timestamp-state.timestamp > self._get("state_ttl_seconds")]
        for key in expired:
            self.states.pop(key, None)
        return len(expired)

    def _hands(self, hands, bbox):
        if not isinstance(hands, list) or len(hands) > self._get("max_hands"):
            raise ValueError("too many or malformed hands")
        result, ids = [], set()
        for hand in hands:
            if not isinstance(hand, dict) or type(hand.get("hand_id")) not in (str, int):
                raise ValueError("stable hand identity required")
            identifier = str(hand["hand_id"])
            if not identifier or len(identifier) > 128 or identifier in ids:
                raise ValueError("ambiguous hand identity")
            ids.add(identifier)
            center, hand_box = _point(hand.get("center")), _box(hand.get("bbox"))
            landmarks = hand.get("landmarks") or []
            if not isinstance(landmarks, list) or len(landmarks) > 21:
                raise ValueError("malformed landmarks")
            points = [_point(point[:2]) if isinstance(point, (list, tuple)) and len(point) in (2, 3) else _point(point) for point in landmarks]
            x, y, width, height = bbox
            if points:
                distance = min(math.hypot(max(x-px, 0., px-x-width), max(y-py, 0., py-y-height)) for px, py in points)
            else:
                hx, hy, hw, hh = hand_box
                distance = math.hypot(max(x-hx-hw, hx-x-width, 0.), max(y-hy-hh, hy-y-height, 0.))
            result.append({"hand_id": identifier, "center": center, "distance": distance,
                           "stable": hand.get("tracking_stable", hand.get("stable")) is True})
        return result

    @staticmethod
    def _clear_release(state):
        state.release_frames = 0
        state.release_visible_frames = 0
        state.separation_frame = state.separation_timestamp = None
        state.release_started_at = state.release_anchor = None
        state.release_start = state.release_end = state.released_at = None

    @staticmethod
    def _clear_motion(state):
        state.motion_frames = 0
        state.motion_started_at = state.motion_origin = state.relative_origin = None
        state.motion_ended_at = None
        state.co_start = state.co_end = None

    def _uncertain(self, state):
        state.hand_center = state.hand_distance = None
        state.near_armed = False
        self._clear_release(state)
        if not state.established:
            state.hand_id = None
            self._clear_motion(state)

    def _result(self, state, status, reason, detected, healthy, *, accepted=True):
        if state.expired_release_frame == state.frame:
            status, reason = "not_established", "previous_release_expired"
        result = {"hand_id": state.hand_id, "holding_status": status,
            "holding_established": state.established,
            "release_observed": status == "released" and state.release_end is not None,
            "co_motion_frames": state.motion_frames, "co_motion_start_frame": state.co_start,
            "co_motion_end_frame": state.co_end,
            "co_motion_start_timestamp": state.motion_started_at,
            "co_motion_end_timestamp": state.motion_ended_at,
            "release_start_frame": state.release_start,
            "release_end_frame": state.release_end, "release_timestamp": state.released_at,
            "release_start_timestamp": state.release_started_at,
            "release_stable_seconds": (max(0., state.timestamp - state.release_started_at)
                                       if state.release_started_at is not None else 0.),
            "release_required_seconds": self._get("release_stable_seconds"),
            "release_remaining_seconds": (max(0., self._get("release_stable_seconds")
                                               - (state.timestamp - state.release_started_at))
                                          if state.release_started_at is not None else self._get("release_stable_seconds")),
            "release_visible_frames": state.release_visible_frames,
            "separation_verified": state.separation_frame is not None,
            "separation_frame": state.separation_frame,
            "separation_timestamp": state.separation_timestamp,
            "reason": reason, "source_session_id": state.session, "source_frame": state.frame,
            "source_timestamp": state.timestamp, "item_detected": bool(detected),
            "hand_model_healthy": bool(healthy), "input_accepted": accepted}
        state.last_result = result
        return dict(result)

    def _advance_release(self, state, center, *, visible_hand):
        """Advance one actual, continuous, stable source frame, never a timer."""
        if not state.release_frames:
            state.release_started_at, state.release_start = state.timestamp, state.frame
            state.release_anchor = center
        state.release_frames += 1
        if visible_hand and state.separation_frame is None:
            state.release_visible_frames += 1
            if (state.release_visible_frames >= self._get("release_stable_frames")
                    and state.timestamp - state.release_started_at + 1e-9 >= self._get("separation_min_seconds")):
                state.separation_frame, state.separation_timestamp = state.frame, state.timestamp
        if (state.separation_frame is not None
                and state.timestamp - state.release_started_at + 1e-9 >= self._get("release_stable_seconds")):
            state.release_end = state.frame
            if state.released_at is None:
                state.released_at = state.completed_release_at = state.timestamp
                state.release_completed = True
            return self._result(state, "released", "witnessed_separation_and_continuous_object_stability", True, True)
        return self._result(state, "release_candidate", "waiting_for_five_second_stability_after_visible_separation", True, True)

    def update(self, item_id, bbox, hands, timestamp, source_session_id, source_frame,
               detected=True, hand_model_healthy=True):
        if (type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp < 0
                or type(source_frame) is not int or source_frame < 0
                or not isinstance(source_session_id, str) or not 1 <= len(source_session_id) <= 200
                or type(item_id) not in (str, int) or not str(item_id) or len(str(item_id)) > 200):
            invalid = _State(str(source_session_id)[:200], 0., 0)
            return self._result(invalid, "not_established", "invalid_source_metadata", False, False, accepted=False)
        self.expire(timestamp)
        item_id = str(item_id)
        state = self.states.get(item_id)
        if state and state.session == source_session_id and source_frame <= state.frame:
            # Replays neither advance counters nor erase a valid held memory.
            result = dict(state.last_result or {})
            result.update({"release_observed": False, "input_accepted": False, "reason": "replayed_source_frame"})
            return result
        reset_reason = None
        if state and (state.session != source_session_id or timestamp <= state.timestamp or timestamp-state.timestamp > self._get("max_gap_seconds")):
            reset_reason = "source_continuity_reset"
            state = None
        if state is None:
            state = _State(source_session_id, float(timestamp), source_frame)
            self.states[item_id] = state
            while len(self.states) > self._get("max_items"):
                self.states.popitem(last=False)
        previous_time, previous_frame = state.timestamp, state.frame
        previous_object, previous_hand = state.object_center, state.hand_center
        previous_distance = state.hand_distance
        state.timestamp, state.frame = float(timestamp), source_frame
        self.states.move_to_end(item_id)
        if (state.release_completed and state.completed_release_at is not None
                and timestamp-state.completed_release_at > self._get("release_valid_seconds") + 1e-9):
            # A witnessed release closed this hold even if the hand disappeared
            # afterwards. Expiry removes evidence eligibility, not that fact;
            # never resurrect the old hold as permanently uncertain.
            state.established = state.release_completed = False
            state.completed_release_at = None
            state.expired_release_frame = source_frame
            state.hand_id = state.hand_center = state.hand_distance = None
            state.near_armed = False
            self._clear_motion(state)
            self._clear_release(state)
            previous_hand = previous_distance = None
        if detected is not True:
            state.object_center = None
            self._uncertain(state)
            return self._result(state, "holding_uncertain" if state.established else "not_established", "object_not_detected", False, hand_model_healthy)
        try:
            bbox = _box(bbox)
        except ValueError:
            state.object_center = None
            self._uncertain(state)
            return self._result(state, "holding_uncertain" if state.established else "not_established", "invalid_object_detection", False, hand_model_healthy)
        center = (bbox[0]+bbox[2]/2, bbox[1]+bbox[3]/2)
        state.object_center = center
        if hand_model_healthy is not True:
            self._uncertain(state)
            return self._result(state, "holding_uncertain" if state.established else "not_established", "hand_model_unavailable", True, False)
        try:
            parsed = self._hands(hands, bbox)
        except (ValueError, TypeError, OverflowError):
            self._uncertain(state)
            return self._result(state, "holding_uncertain" if state.established else "not_established", "ambiguous_or_invalid_hands", True, True)
        near = [hand for hand in parsed if hand["distance"] <= self._get("near_distance")]
        if state.release_completed and len(near) == 1 and near[0]["stable"]:
            # A previously witnessed release closed that carrying episode. A
            # later pickup must prove fresh co-motion inside its own frame range.
            state.established = state.release_completed = False
            state.completed_release_at = None
            state.hand_id = None
            state.near_armed = False
            self._clear_motion(state)
            self._clear_release(state)
            previous_hand = previous_distance = None
        if len(near) > 1 or (state.established and any(hand["hand_id"] != state.hand_id for hand in near)):
            self._uncertain(state)
            return self._result(state, "holding_uncertain" if state.established else "not_established", "multiple_near_hands", True, True)
        if state.established:
            hand = next((hand for hand in parsed if hand["hand_id"] == state.hand_id and hand["stable"]), None)
        else:
            hand = near[0] if len(near) == 1 and near[0]["stable"] else None
        if hand is None:
            # Hand absence alone is not release. Only after the same tracked
            # hand was visibly separated in multiple actual frames may it leave
            # the view while an independently detected stationary item remains.
            stable_clear = (state.separation_frame is not None and previous_object is not None
                and timestamp > previous_time and state.release_anchor is not None
                and math.dist(previous_object, center) / (timestamp - previous_time) <= self._get("release_max_object_speed")
                and math.dist(state.release_anchor, center) <= self._get("release_position_jitter")
                and all(value["stable"] and value["distance"] >= self._get("separation_distance") for value in parsed))
            if stable_clear:
                state.hand_center = state.hand_distance = None
                return self._advance_release(state, center, visible_hand=False)
            self._uncertain(state)
            return self._result(state, "holding_uncertain" if state.established else "nearby" if near else "not_established", "bound_hand_not_visible_or_stable" if state.established else "no_stable_near_hand", True, True)
        if hand["hand_id"] != state.hand_id:
            self._clear_motion(state)
            previous_hand = previous_distance = None
            state.hand_id = hand["hand_id"]
        state.hand_center, state.hand_distance = hand["center"], hand["distance"]
        dt = timestamp-previous_time
        if hand["distance"] <= self._get("near_distance"):
            state.near_armed = True
            self._clear_release(state)
            coupled = False
            if previous_object is not None and previous_hand is not None and dt > 0:
                object_step = tuple(b-a for a, b in zip(previous_object, center))
                hand_step = tuple(b-a for a, b in zip(previous_hand, hand["center"]))
                od, hd = math.hypot(*object_step), math.hypot(*hand_step)
                coupled = (od >= self._get("min_object_step") and hd >= self._get("min_hand_step")
                    and math.dist(object_step, hand_step) <= self._get("max_vector_error")
                    and sum(a*b for a, b in zip(object_step, hand_step))/(od*hd) >= self._get("min_direction_cosine"))
                if coupled:
                    relative = tuple(a-b for a, b in zip(hand["center"], center))
                    if not state.motion_frames:
                        state.motion_started_at, state.motion_origin = previous_time, previous_object
                        state.relative_origin, state.co_start = relative, previous_frame
                    if math.dist(relative, state.relative_origin) > self._get("max_relative_drift"):
                        coupled = False
                    else:
                        state.motion_frames += 1
                        state.co_end = source_frame
                        state.motion_ended_at = timestamp
                        if (state.motion_frames >= self._get("min_motion_frames")
                                and timestamp-state.motion_started_at + 1e-9 >= self._get("min_motion_seconds")
                                and math.dist(state.motion_origin, center) >= self._get("min_total_distance")):
                            state.established = True
            if not coupled and not state.established:
                self._clear_motion(state)
            return self._result(state, "co_moving" if coupled and state.established else "holding_uncertain" if state.established else "nearby",
                "continuous_co_motion" if coupled and state.established else "held_memory_without_visible_release" if state.established else reset_reason or "proximity_is_not_holding", True, True)
        if not state.established:
            self._clear_motion(state)
            return self._result(state, "not_established", "separated_without_prior_co_motion", True, True)
        stable = (previous_object is not None and dt > 0
                  and math.dist(previous_object, center)/dt <= self._get("release_max_object_speed"))
        clearly_separated = hand["distance"] >= self._get("separation_distance")
        all_hands_clear = all(value["stable"] and value["distance"] >= self._get("separation_distance") for value in parsed)
        separation_visible = (state.near_armed and previous_hand is not None and previous_distance is not None
                              and hand["distance"]-previous_distance >= self._get("release_min_distance_increase"))
        if state.released_at is not None:
            if (stable and clearly_separated and all_hands_clear and timestamp-state.released_at <= self._get("release_valid_seconds") + 1e-9
                    and math.dist(state.release_anchor, center) <= self._get("release_position_jitter")):
                state.release_end = source_frame
                return self._result(state, "released", "visible_release_recently_observed", True, True)
            self._clear_release(state)
            state.near_armed = False
        if not clearly_separated or not all_hands_clear or not stable or (not state.release_frames and not separation_visible):
            self._clear_release(state)
            state.near_armed = False
            return self._result(state, "holding_uncertain", "release_not_visibly_established", True, True)
        if state.release_frames and math.dist(state.release_anchor, center) > self._get("release_position_jitter"):
            self._clear_release(state)
            state.near_armed = False
            return self._result(state, "holding_uncertain", "object_not_stable_after_separation", True, True)
        return self._advance_release(state, center, visible_hand=True)
