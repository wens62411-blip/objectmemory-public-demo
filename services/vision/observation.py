"""Detector-confirmed identity continuity, independent of action/stability.

Missing frames and tracker predictions never advance a real observation.
Only one bounded candidate and verified proof are retained per registered item.
"""
from copy import deepcopy
import math


class ObservationGate:
    def __init__(self, min_frames=3, max_gap=.75, max_speed=1.5, min_confidence=.55):
        self.min_frames = max(2, int(min_frames))
        self.max_gap, self.max_speed = float(max_gap), float(max_speed)
        self.min_confidence = float(min_confidence)
        self.candidates = {}
        self.verified = {}

    def reset(self):
        self.candidates.clear()
        self.verified.clear()

    def missing(self, item_id):
        self.candidates.pop(item_id, None)

    def observe(self, item_id, motion, monotonic_time, identity):
        if not identity.get('accepted') or motion.confidence < self.min_confidence:
            self.missing(item_id)
            return None
        prior = self.candidates.get(item_id)
        same = bool(prior and prior['motion'].source_session_id == motion.source_session_id)
        if same and motion.source_frame <= prior['motion'].source_frame:
            return None
        dt = monotonic_time - prior['time'] if same else 0
        continuous = (same and 0 < dt <= self.max_gap
                      and math.dist(motion.center_norm, prior['motion'].center_norm) <= self.max_speed * dt
                      and identity.get('profile_version') == prior['identity'].get('profile_version'))
        count = prior['count'] + 1 if continuous else 1
        candidate = {'motion': deepcopy(motion), 'time': monotonic_time,
                     'count': count, 'identity': deepcopy(identity)}
        self.candidates[item_id] = candidate
        if count >= self.min_frames:
            self.verified[item_id] = candidate
            return candidate
        return None
