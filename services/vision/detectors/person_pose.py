"""Independent person pose / visible-foot candidates; never room coordinates.

Person boxes must be actual same-frame detector results in normalized xywh.
MediaPipe world landmarks are deliberately not exported: their hip-relative
coordinates are not a calibrated room coordinate system. This synchronous IMAGE
adapter retains neither frames nor tracks, so camera/session lifecycle is owned
by the caller. The model cannot prove ground contact; calibrated projection is
an explicitly approximate downstream step.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import threading
import time

import cv2
import numpy as np

MODEL_MANIFEST = json.loads(Path(__file__).with_name('person-pose-model.json').read_text(encoding='utf-8'))
MODEL_URL, MODEL_SHA256 = MODEL_MANIFEST['url'], MODEL_MANIFEST['sha256']
MODEL_BYTES, MODEL_CARD = MODEL_MANIFEST['bytes'], MODEL_MANIFEST['model_card']
FOOT_INDICES = (27, 28, 29, 30, 31, 32)
BODY_INDICES = (0, 11, 12, 23, 24, 25, 26)


@dataclass(frozen=True)
class PersonPoseConfig:
    enabled: bool = True
    max_people: int = 2
    detection_confidence: float = .6
    presence_confidence: float = .6
    landmark_visibility: float = .75
    landmark_presence: float = .75
    border_margin: float = .01
    minimum_knee_angle: float = 150.0
    minimum_vertical_leg_ratio: float = .15
    minimum_body_pixels: int = 80

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.max_people) is not int or not 1 <= self.max_people <= 4:
            raise ValueError('Invalid enabled/max_people person pose configuration')
        for name in ('detection_confidence', 'presence_confidence', 'landmark_visibility', 'landmark_presence'):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError(f'Invalid {name}')
        for name, low, high in (('border_margin', 0, .1), ('minimum_knee_angle', 120, 180),
                                ('minimum_vertical_leg_ratio', .05, .5)):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'Invalid {name}')
        if type(self.minimum_body_pixels) is not int or not 32 <= self.minimum_body_pixels <= 1000:
            raise ValueError('Invalid minimum_body_pixels')


def normalized_box(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError('Person bbox must be source normalized xywh')
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in value):
        raise ValueError('Person bbox contains invalid coordinates')
    x, y, w, h = value
    if min(x, y) < 0 or min(w, h) <= 0 or x+w > 1.00000001 or y+h > 1.00000001:
        raise ValueError('Person bbox outside normalized source frame')
    return [float(x), float(y), float(w), float(h)]


def _point(landmark):
    values = [getattr(landmark, name, None) for name in ('x', 'y', 'visibility', 'presence')]
    if any(type(v) not in (float, int) or not math.isfinite(v) for v in values):
        return None
    return dict(zip(('x', 'y', 'visibility', 'presence'), (float(v) for v in values)))


def _visible(point, config):
    return bool(point and config.border_margin <= point['x'] <= 1-config.border_margin
                and config.border_margin <= point['y'] <= 1-config.border_margin
                and config.landmark_visibility <= point['visibility'] <= 1
                and config.landmark_presence <= point['presence'] <= 1)


def _inside(point, bbox):
    x, y, w, h = bbox
    return x <= point['x'] <= x+w and y <= point['y'] <= y+h


def foot_evidence(landmarks, bbox, width: int, height: int, config: PersonPoseConfig):
    result = {'foot_point': None, 'foot_point_visible': False,
              'position_status': 'ground_unconfirmed', 'ground_contact_confirmed': False,
              'pose_status': 'no_pose', 'posture_state': 'unknown', 'reason': 'no_matching_pose', 'landmarks': []}
    if len(landmarks) != 33:
        return result
    points = [_point(landmark) for landmark in landmarks]
    result['landmarks'] = [{'index': i, **points[i]} for i in (*BODY_INDICES[1:], *FOOT_INDICES) if points[i]]
    valid_feet = [points[i] for i in FOOT_INDICES if points[i]]
    if len(valid_feet) == len(FOOT_INDICES):
        result['foot_min_visibility'] = min(point['visibility'] for point in valid_feet)
        result['foot_min_presence'] = min(point['presence'] for point in valid_feet)
    if not all(_visible(points[i], config) for i in FOOT_INDICES):
        return {**result, 'pose_status': 'feet_unconfirmed', 'reason': 'feet_occluded_outside_frame_or_low_visibility'}
    if not all(_visible(points[i], config) for i in BODY_INDICES):
        return {**result, 'pose_status': 'partial_body', 'reason': 'head_or_lower_body_not_reliably_visible'}
    if not all(_inside(points[i], bbox) for i in (11, 12, 23, 24, 25, 26, *FOOT_INDICES)):
        return {**result, 'pose_status': 'pose_box_mismatch', 'reason': 'pose_not_contained_by_detected_person'}
    if bbox[3] * height < config.minimum_body_pixels:
        return {**result, 'pose_status': 'person_too_small', 'reason': 'insufficient_person_pixels'}
    knee_angles = []
    for hip, knee, ankle in ((23, 25, 27), (24, 26, 28)):
        vectors = [np.asarray([points[a]['x']*width, points[a]['y']*height]) -
                   np.asarray([points[knee]['x']*width, points[knee]['y']*height]) for a in (hip, ankle)]
        denominator = float(np.linalg.norm(vectors[0]) * np.linalg.norm(vectors[1]))
        if denominator < 1e-9:
            return {**result, 'pose_status': 'degenerate_pose', 'reason': 'zero_length_leg'}
        angle = math.degrees(math.acos(float(np.clip(np.dot(*vectors) / denominator, -1, 1))))
        knee_angles.append(angle)
        leg_height = points[ankle]['y'] - points[hip]['y']
        if (angle < config.minimum_knee_angle or leg_height < config.minimum_vertical_leg_ratio*bbox[3]
                or not points[hip]['y'] < points[knee]['y'] < points[ankle]['y']):
            return {**result, 'pose_status': 'non_upright_or_bent', 'reason': 'seated_crouched_or_ground_contact_uncertain',
                    'posture_state': 'non_upright_or_bent', 'knee_angles_degrees': knee_angles}
    # Heel and toe midpoint estimates visible image contact, NOT world position.
    feet = [points[i] for i in (29, 30, 31, 32)]
    return {**result, 'foot_point': [sum(p['x'] for p in feet)/4, sum(p['y'] for p in feet)/4],
            'foot_point_visible': True, 'position_status': 'footpoint_observed',
            'posture_state': 'upright_candidate',
            'pose_status': 'feet_visible_upright_candidate', 'reason': 'requires_calibrated_floor_for_approximate_projection',
            'knee_angles_degrees': knee_angles,
            'footpoint_method': 'visible_bilateral_heel_toe_midpoint_source_image'}


class MediaPipePersonPose:
    def __init__(self, model_path: str | Path | None = None, config: PersonPoseConfig | None = None):
        self.config = config or PersonPoseConfig()
        self._lock = threading.RLock()
        self._model = None
        self._closed = False
        self.available = False
        self.error = None
        self._status = 'loading' if self.config.enabled else 'disabled'
        self._last_latency_ms = None
        self._frames_processed = 0
        self._model_sha256 = None
        self._package_version = None
        self.model_path = (Path(model_path) if model_path else Path(__file__).resolve().parents[3] / MODEL_MANIFEST['default_path']).resolve()
        if not self.config.enabled:
            return
        try:
            import mediapipe as mp
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision
            model_bytes = self.model_path.read_bytes()  # Unicode paths supported, no native-path conversion.
            self._model_sha256 = hashlib.sha256(model_bytes).hexdigest()
            if len(model_bytes) != MODEL_BYTES or self._model_sha256 != MODEL_SHA256:
                raise ValueError('Pose model differs from pinned official lite bundle')
            self._package_version = importlib.metadata.version('mediapipe')
            options = vision.PoseLandmarkerOptions(
                base_options=mp_python.BaseOptions(model_asset_buffer=model_bytes),
                running_mode=vision.RunningMode.IMAGE, num_poses=self.config.max_people,
                min_pose_detection_confidence=self.config.detection_confidence,
                min_pose_presence_confidence=self.config.presence_confidence,
                output_segmentation_masks=False)
            self._model = vision.PoseLandmarker.create_from_options(options)
            self._mp = mp
            self.available = True
            self._status = 'ready'
        except Exception as exc:
            self._status, self.error = 'unavailable', f'Local person pose unavailable: {exc}'

    def detect(self, frame: np.ndarray, person_boxes: list[dict]) -> list[dict]:
        with self._lock:
            if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3 or min(frame.shape[:2]) < 1 or frame.shape[0]*frame.shape[1] > 16_777_216:
                raise ValueError('Person pose input must be bounded uint8 BGR source image')
            if not isinstance(person_boxes, list) or len(person_boxes) > 32:
                raise ValueError('Person detections must be a bounded list')
            rows = []
            for index, person in enumerate(person_boxes):
                if not isinstance(person, dict) or person.get('label') != 'person':
                    raise ValueError('Pose input requires semantic person detections, never hand-derived boxes')
                bbox = normalized_box(person.get('bbox'))
                score = person.get('confidence')
                if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
                    raise ValueError('Person confidence is invalid')
                rows.append({'bbox': bbox, 'bbox_format': 'source_normalized_xywh', 'person_index': index,
                             'label': 'person', 'detector_confidence': float(score), 'pose_backend': 'mediapipe_pose_lite',
                             'pose_status': 'no_pose', 'posture_state': 'unknown', 'foot_point': None, 'foot_point_visible': False,
                             'position_status': 'ground_unconfirmed', 'ground_contact_confirmed': False,
                             'map_position': None, 'landmarks': [], 'reason': 'no_matching_pose'})
            if not rows:
                self._status = 'no_person' if self.available else self._status
                self._last_latency_ms = None  # This call did not run pose inference.
                return []
            if not self.available or self._model is None:
                self._last_latency_ms = None
                return [{**row, 'pose_status': self._status, 'reason': self.error or self._status} for row in rows]
            started = time.perf_counter()
            try:
                image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB,
                                       data=np.ascontiguousarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
                poses = self._model.detect(image).pose_landmarks
                assignments = {}
                for pose in poses[:self.config.max_people]:
                    if len(pose) != 33:
                        continue
                    torso = [_point(pose[i]) for i in (11, 12, 23, 24)]
                    if not all(_visible(point, self.config) for point in torso):
                        continue
                    matches = [i for i, row in enumerate(rows) if all(_inside(point, row['bbox']) for point in torso)]
                    if len(matches) != 1:
                        for index in matches:
                            assignments[index] = None  # Never guess between overlapping people.
                        continue
                    index = matches[0]
                    if index in assignments:
                        assignments[index] = None
                    else:
                        assignments[index] = pose
                for index, pose in assignments.items():
                    if pose is None:
                        rows[index].update(pose_status='ambiguous_person_pose', reason='pose_to_person_not_unique')
                    else:
                        rows[index].update(foot_evidence(pose, rows[index]['bbox'], frame.shape[1], frame.shape[0], self.config))
                self._frames_processed += 1
                self._status, self.error = ('ready' if poses else 'no_pose'), None
                return rows
            except Exception as exc:
                self._status, self.error = 'failed', f'Person pose inference failed: {exc}'
                return [{**row, 'pose_status': 'failed', 'reason': self.error, 'foot_point': None,
                         'foot_point_visible': False, 'position_status': 'ground_unconfirmed'} for row in rows]
            finally:
                self._last_latency_ms = round((time.perf_counter()-started)*1000, 3)

    def health(self):
        with self._lock:
            return {'name': 'mediapipe_pose_lite', 'enabled': self.config.enabled, 'available': self.available,
                    'status': self._status, 'error': self.error, 'closed': self._closed, 'running_mode': 'IMAGE',
                    'model_sha256': self._model_sha256, 'model_url': MODEL_URL, 'model_bytes': MODEL_BYTES,
                    'model_path': str(self.model_path), 'model_id': MODEL_MANIFEST['model_id'], 'model_version': MODEL_MANIFEST['model_version'],
                    'license': 'Apache-2.0', 'model_card': MODEL_CARD, 'mediapipe_version': self._package_version,
                    'config': asdict(self.config), 'last_latency_ms': self._last_latency_ms,
                    'frames_processed': self._frames_processed, 'world_coordinates_exposed': False,
                    'limitations': 'Visible image foot candidate only; not proof of ground contact, room position, or identity'}

    def close(self):
        with self._lock:
            model, self._model = self._model, None
            self.available, self._closed, self._status = False, True, 'disabled'
            if model is not None:
                try:
                    model.close()
                except Exception as exc:
                    self._status, self.error = 'failed', f'Person pose close failed: {exc}'
