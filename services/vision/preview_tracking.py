"""Bounded, pixel-measured display tracking. Never produces identity/event proof.

A detector seeds features on its own original frame. Lucas-Kanade forward/back
checks transport those features to each new preview frame; no linear animation,
no stale box pasted onto a different frame. Lost texture/occlusion fails closed.
"""
from copy import deepcopy
from dataclasses import dataclass
import math
import threading

import cv2
import numpy as np


@dataclass(frozen=True)
class PreviewTrackingSettings:
    max_width: int = 640
    max_candidates: int = 12
    max_features: int = 48
    min_features: int = 6
    max_identity_age_seconds: float = .8
    max_frame_gap_seconds: float = .25
    forward_backward_pixels: float = 1.5
    max_patch_error: float = 22.0
    max_step_ratio: float = .25

    def __post_init__(self):
        bounds = {'max_width': (160, 1280), 'max_candidates': (1, 24),
                  'max_features': (8, 100), 'min_features': (4, 30),
                  'max_identity_age_seconds': (.1, 1.5), 'max_frame_gap_seconds': (.03, .5),
                  'forward_backward_pixels': (.1, 4), 'max_patch_error': (1, 40),
                  'max_step_ratio': (.01, .35)}
        for key, (low, high) in bounds.items():
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'Invalid preview tracking {key}')
        for key in ('max_width', 'max_candidates', 'max_features', 'min_features'):
            if type(getattr(self, key)) is not int:
                raise ValueError(f'{key} must be an integer')
        if self.min_features > self.max_features:
            raise ValueError('min_features exceeds max_features')


class PreviewTracker:
    def __init__(self, settings=None):
        self.config = PreviewTrackingSettings(**(settings or {}))
        self._lock = threading.Lock()
        self._pending = None
        self._revision = 0
        self._applied_revision = 0
        self.reset()

    def reset(self):
        self._gray = None
        self._tracks = []
        self._session = None
        self._sequence = -1
        self._time = None

    def invalidate(self):
        with self._lock:
            self._revision += 1
            self._pending = None

    def _gray_image(self, frame):
        h, w = frame.shape[:2]
        scale = min(1.0, self.config.max_width / w)
        small = cv2.resize(frame, (round(w*scale), round(h*scale)), interpolation=cv2.INTER_AREA) if scale < 1 else frame
        return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

    def offer(self, frame, candidates, session, sequence, timestamp, *, profile_generation=0):
        # One pending seed, replacing rather than accumulating detector frames.
        gray = self._gray_image(frame)
        with self._lock:
            self._revision += 1
            stamped=[{**deepcopy(candidate), 'profile_generation':profile_generation} for candidate in candidates[:self.config.max_candidates]]
            self._pending = (gray, stamped, session, sequence, timestamp)

    def _seed(self, gray, candidates, session, sequence, timestamp):
        self.reset()
        self._gray, self._session, self._sequence, self._time = gray, session, sequence, timestamp
        h, w = gray.shape
        valid_candidates = []
        for candidate in candidates:
            box = candidate.get('bbox')
            if (not isinstance(box, (list, tuple)) or len(box) != 4
                    or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in box)):
                continue
            x, y, bw, bh = box
            if min(x, y) < 0 or min(bw, bh) <= 0 or x+bw > 1.0001 or y+bh > 1.0001:
                continue
            valid_candidates.append(candidate)
            mask = np.zeros_like(gray)
            # Inner ROI limits features on the background around a loose box.
            left, top = round((x+bw*.08)*w), round((y+bh*.08)*h)
            right, bottom = round((x+bw*.92)*w), round((y+bh*.92)*h)
            mask[top:bottom, left:right] = 255
            points = cv2.goodFeaturesToTrack(gray, self.config.max_features, .02, 4, mask=mask, blockSize=5)
            if points is None or len(points) < self.config.min_features:
                continue
            self._tracks.append({'candidate': candidate, 'points': points,
                'box': np.array([x*w, y*h, bw*w, bh*h], np.float32),
                'detection_frame': sequence, 'identity_at': timestamp})
        return valid_candidates

    def update(self, frame, session, sequence, timestamp):
        with self._lock:
            revision = self._revision
            pending, self._pending = self._pending, None
        just_detected = None
        if revision != self._applied_revision:
            self._applied_revision = revision
            if pending and pending[2] == session and pending[3] <= sequence and 0 <= timestamp-pending[4] <= self.config.max_identity_age_seconds:
                just_detected = self._seed(*pending)
            else:
                self.reset()
        if just_detected is None and not self._tracks:
            # Idle preview still publishes the actual source frame. It needs
            # no grayscale allocation when no boxes can be transported.
            self.reset()
            return []
        gray = self._gray_image(frame)
        if session != self._session or self._gray is None or gray.shape != self._gray.shape:
            self.reset()
            return []
        if just_detected is not None and sequence == self._sequence and timestamp == self._time and np.array_equal(gray, self._gray):
            # The model's own source frame needs no optical-flow bridge. In
            # particular, a featureless detected object can be shown once but
            # must NOT acquire a persistent box on later unmeasured frames.
            return [{**candidate, 'source_frame': sequence, 'source_session_id': session,
                     'identity_verified_at_frame': sequence, 'identity_age_ms': 0.,
                     'tracking_status': 'detector_same_frame', 'tracker_backend': 'detector_same_frame',
                     'visual_only': True, 'observation_evidence': False}
                    for candidate in just_detected]
        if sequence <= self._sequence or timestamp < self._time:
            return []
        # A newly delivered delayed model frame may be bridged once, but never
        # a discontinuity between successive preview frames.
        if timestamp-self._time > (self.config.max_identity_age_seconds if pending else self.config.max_frame_gap_seconds):
            self.reset()
            return []
        h, w = gray.shape
        kept, output = [], []
        active = [track for track in self._tracks
                  if timestamp-track['identity_at'] <= self.config.max_identity_age_seconds]
        if not active:
            self.reset()
            return []
        # LK builds the same image pyramids for every invocation. Transport
        # all bounded point groups together, then apply each item's unchanged
        # agreement gates independently; objects never share motion estimates.
        all_old = np.concatenate([track['points'] for track in active])
        all_new, all_forward, all_error = cv2.calcOpticalFlowPyrLK(
            self._gray, gray, all_old, None, winSize=(21, 21), maxLevel=3)
        if all_new is None or all_forward is None or all_error is None:
            self.reset()
            return []
        all_back, all_backward, _ = cv2.calcOpticalFlowPyrLK(
            gray, self._gray, all_new, None, winSize=(21, 21), maxLevel=3)
        if all_back is None or all_backward is None:
            self.reset()
            return []
        offset = 0
        for track in active:
            old = track['points']
            end = offset + len(old)
            new, forward, error = all_new[offset:end], all_forward[offset:end], all_error[offset:end]
            back, backward = all_back[offset:end], all_backward[offset:end]
            offset = end
            valid = ((forward.ravel() == 1) & (backward.ravel() == 1)
                     & (np.linalg.norm(old-back, axis=2).ravel() <= self.config.forward_backward_pixels)
                     & (error.ravel() <= self.config.max_patch_error))
            if np.count_nonzero(valid) < self.config.min_features:
                continue
            a, b = old[valid].reshape(-1, 2), new[valid].reshape(-1, 2)
            shift = np.median(b-a, axis=0)
            # Robust feature agreement prevents a passing hand dominating a box.
            inliers = np.linalg.norm(b-a-shift, axis=1) <= 2.5
            if np.count_nonzero(inliers) < self.config.min_features or np.mean(inliers) < .65 or np.linalg.norm(shift) > min(w, h)*self.config.max_step_ratio:
                continue
            box = track['box'].copy()
            box[:2] += shift
            x, y, bw, bh = box
            if min(x, y) < 0 or x+bw > w or y+bh > h:
                continue
            track.update(points=b[inliers].reshape(-1, 1, 2), box=box)
            kept.append(track)
            output.append({**track['candidate'], 'bbox': [float(x/w), float(y/h), float(bw/w), float(bh/h)],
                'source_frame': sequence, 'source_session_id': session,
                'identity_verified_at_frame': track['detection_frame'],
                'identity_age_ms': round((timestamp-track['identity_at'])*1000, 2),
                'tracking_status': 'optical_flow', 'tracker_backend': 'lk_forward_backward',
                'visual_only': True, 'observation_evidence': False})
        self._gray, self._sequence, self._time, self._tracks = gray, sequence, timestamp, kept
        return output
