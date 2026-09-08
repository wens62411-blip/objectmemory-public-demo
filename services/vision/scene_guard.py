"""Low-frequency, conservative camera-view change detection (not 3-D mapping)."""
import cv2
import numpy as np


class SceneGuard:
    def __init__(self, interval_seconds=3, shift_threshold=.04):
        self.interval = float(interval_seconds)
        self.threshold = float(shift_threshold)
        self.orb = cv2.ORB_create(nfeatures=450)
        self.reference = None
        self.last_check = float('-inf')
        self.last_result = {'status': 'not_initialized'}

    def _features(self, frame):
        height, width = frame.shape[:2]
        small = cv2.resize(frame, (400, max(1, round(height*400/width))))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        keypoints, descriptors = self.orb.detectAndCompute(gray, None)
        return gray.shape, keypoints, descriptors

    def establish(self, frame):
        self.reference = None
        self.last_check = float('-inf')
        try:
            self.reference = self._features(frame)
            self.last_result = {'status': 'baseline', 'notice': '图像稳定性检查不是精确标定'}
            return True
        except Exception as exc:
            # An old reference must not survive a failed attempt to establish
            # the newly selected scene. Object detection can continue safely.
            self.last_result = {'status': 'failed', 'stage': 'baseline',
                                'error': f'{type(exc).__name__}: {str(exc)[:240]}'}
            return False

    def check(self, frame, timestamp):
        try:
            return self._check(frame, timestamp)
        except Exception as exc:
            # A throttled next frame may reuse this result, but never the old
            # stable result. Insufficient/failed checks cannot prove a surface.
            self.last_result = {'status': 'failed', 'stage': 'comparison',
                                'error': f'{type(exc).__name__}: {str(exc)[:240]}'}
            return False

    def _check(self, frame, timestamp):
        if self.reference is None:
            self.establish(frame)
            return False
        if timestamp-self.last_check < self.interval:
            return False
        self.last_check = timestamp
        shape, points, descriptors = self._features(frame)
        reference_shape, reference_points, reference_descriptors = self.reference
        if shape != reference_shape:
            self.last_result = {'status': 'changed', 'reason': 'aspect_ratio_changed'}
            return True
        if descriptors is None or reference_descriptors is None:
            self.last_result = {'status': 'insufficient_detail'}
            return False
        pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(reference_descriptors, descriptors, k=2)
        matches = [pair[0] for pair in pairs if len(pair)==2 and pair[0].distance < .7*pair[1].distance]
        if len(matches)<25:
            self.last_result = {'status': 'insufficient_matches', 'matches': len(matches)}
            return False
        before = np.float32([reference_points[m.queryIdx].pt for m in matches])
        after = np.float32([points[m.trainIdx].pt for m in matches])
        matrix, inliers = cv2.estimateAffinePartial2D(before, after, method=cv2.RANSAC, ransacReprojThreshold=2)
        if matrix is None or inliers is None:
            self.last_result = {'status': 'insufficient_consensus', 'matches': len(matches)}
            return False
        accepted = inliers.ravel().astype(bool)
        coverage = set((int(x>=shape[1]/2), int(y>=shape[0]/2)) for x,y in before[accepted])
        shift = float(np.median(np.linalg.norm(after[accepted]-before[accepted], axis=1)) / np.hypot(*shape)) if accepted.any() else None
        consensus = bool(accepted.sum()>=25 and accepted.mean()>=.65 and len(coverage)>=3)
        changed = bool(consensus and shift is not None and shift>=self.threshold)
        self.last_result = {'status': 'changed' if changed else 'stable' if consensus else 'insufficient_consensus', 'matches': len(matches),
                            'inliers': int(accepted.sum()), 'quadrants': len(coverage), 'normalized_shift': shift}
        return changed
