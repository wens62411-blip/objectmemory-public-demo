"""Person/foot guards plus opt-in real local MediaPipe/NanoDet file evaluation."""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from services.vision.detectors.person_pose import (
    MediaPipePersonPose, PersonPoseConfig, foot_evidence, normalized_box)


def standing_pose():
    points = [SimpleNamespace(x=.5, y=.15, visibility=.99, presence=.99) for _ in range(33)]
    for index, x, y in [(11, .45, .25), (12, .55, .25), (23, .45, .5), (24, .55, .5),
                         (25, .45, .7), (26, .55, .7), (27, .45, .85), (28, .55, .85),
                         (29, .44, .87), (30, .54, .87), (31, .47, .9), (32, .57, .9)]:
        points[index].x, points[index].y = x, y
    return points


def evidence(points):
    return foot_evidence(points, [.2, .1, .6, .85], 1280, 720, PersonPoseConfig())


def test_visible_straight_legs_only_yield_image_candidate_not_room_position():
    result = evidence(standing_pose())
    assert result['position_status'] == 'footpoint_observed'
    assert result['foot_point'] == pytest.approx([.505, .885])
    assert result['ground_contact_confirmed'] is False
    assert result['reason'] == 'requires_calibrated_floor_for_approximate_projection'
    assert 'world_landmarks' not in result
    assert all(row['index'] != 0 for row in result['landmarks'])  # No retained face landmark.


@pytest.mark.parametrize('index,attribute,value', [
    (27, 'visibility', .2), (28, 'presence', .1), (29, 'x', -1), (30, 'y', 1.2),
    (31, 'x', float('nan')), (32, 'y', .995), (0, 'visibility', .1), (23, 'presence', .2)])
def test_occlusion_partial_body_border_and_nonfinite_never_fabricate_foot(index, attribute, value):
    points = standing_pose()
    setattr(points[index], attribute, value)
    result = evidence(points)
    assert result['foot_point'] is None
    assert result['position_status'] == 'ground_unconfirmed'


def test_seated_bent_knee_rejected_even_with_visible_feet():
    points = standing_pose()
    points[25].x, points[25].y = .65, .52
    result = evidence(points)
    assert result['pose_status'] == 'non_upright_or_bent'
    assert result['foot_point'] is None


def test_straight_horizontal_legs_are_not_standing():
    points = standing_pose()
    for index, x in ((23, .3), (25, .45), (27, .6)):
        points[index].x, points[index].y = x, .5
    result = evidence(points)
    assert result['position_status'] == 'ground_unconfirmed'


def test_person_bbox_cannot_substitute_for_invisible_feet():
    assert foot_evidence([], [.1, .1, .8, .8], 1280, 720, PersonPoseConfig())['foot_point'] is None
    result = foot_evidence(standing_pose(), [.4, .1, .1, .85], 1280, 720, PersonPoseConfig())
    assert result['pose_status'] == 'pose_box_mismatch' and result['foot_point'] is None


@pytest.mark.parametrize('box', [[1, 2, 100, 200], [.1, .1, float('nan'), .2], [0, 0, 0, .5]])
def test_boxes_are_explicitly_normalized_not_auto_guessed(box):
    with pytest.raises(ValueError):
        normalized_box(box)


@pytest.mark.parametrize('config', [{'max_people': 0}, {'landmark_visibility': float('nan')},
                                   {'minimum_knee_angle': 90}, {'minimum_body_pixels': 0}])
def test_configuration_is_bounded(config):
    with pytest.raises(ValueError):
        PersonPoseConfig(**config)


def fake_detector(poses):
    detector = MediaPipePersonPose(config=PersonPoseConfig(enabled=False))
    detector.available = True
    detector._mp = SimpleNamespace(Image=lambda **kwargs: kwargs['data'], ImageFormat=SimpleNamespace(SRGB='rgb'))
    calls = []

    def detect(frame):
        calls.append(frame)
        return SimpleNamespace(pose_landmarks=poses)
    detector._model = SimpleNamespace(detect=detect, close=lambda: calls.append('closed'))
    return detector, calls


def test_overlapping_people_do_not_guess_pose_owner():
    detector, calls = fake_detector([standing_pose()])
    person = {'label': 'person', 'bbox': [.2, .1, .6, .85], 'confidence': .8}
    result = detector.detect(np.zeros((720, 1280, 3), np.uint8), [person, person.copy()])
    assert len(calls) == 1
    assert all(row['pose_status'] == 'ambiguous_person_pose' and row['foot_point'] is None for row in result)


def test_next_frame_without_pose_has_no_stale_foot_and_close_releases():
    poses = [standing_pose()]
    detector, calls = fake_detector(poses)
    frame = np.zeros((720, 1280, 3), np.uint8)
    person = {'label': 'person', 'bbox': [.2, .1, .6, .85], 'confidence': .8}
    assert detector.detect(frame, [person])[0]['foot_point'] is not None
    poses.clear()
    assert detector.detect(frame, [person])[0]['foot_point'] is None
    assert detector.detect(frame, []) == [] and len(calls) == 2
    assert detector.health()['last_latency_ms'] is None
    detector.close()
    detector.close()
    assert calls[-1] == 'closed' and len(calls) == 3
    assert detector.health()['closed'] is True


def test_failed_model_keeps_actual_person_bbox_but_not_foot(tmp_path):
    detector = MediaPipePersonPose(tmp_path / 'missing.task')
    person = {'label': 'person', 'bbox': [.2, .1, .6, .85], 'confidence': .8}
    result = detector.detect(np.zeros((720, 1280, 3), np.uint8), [person])[0]
    assert result['bbox'] == person['bbox']
    assert result['foot_point'] is None and result['map_position'] is None
    assert result['pose_status'] == 'unavailable'
    assert detector.health()['error']


def test_hand_box_cannot_be_claimed_as_person():
    detector, _ = fake_detector([])
    with pytest.raises(ValueError, match='semantic person'):
        detector.detect(np.zeros((720, 1280, 3), np.uint8), [{'label': 'hand', 'bbox': [.2, .1, .6, .85], 'confidence': .8}])


@pytest.mark.skipif(os.environ.get('OM_RUN_PERSON_POSE_MODEL') != '1', reason='Opt-in actual local model/file test, not Mock')
def test_real_local_pose_file_evaluation():
    from services.vision.detectors.nanodet import NanoDetDetectorBackend
    root = Path(__file__).resolve().parents[3]
    model = root / 'data/models/pose_landmarker_lite.task'
    sample = root / 'data/model-evaluation/pose-official-sample.jpg'
    camera = root / 'data/verification/core-vision/probe-before/actual-source.jpg'
    assert model.is_file() and sample.is_file() and camera.is_file()
    assert hashlib.sha256(sample.read_bytes()).hexdigest() == 'c8a830ed683c0276d713dd5aeda28f415f10cd6291972084a40d0d8b934ed62b'
    read = lambda path: cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    public = read(sample)
    detector = MediaPipePersonPose(model)
    nano = NanoDetDetectorBackend(root / 'data/models/object_detection_nanodet_2022nov.onnx')
    report = {'passed': False, 'model_mock': False, 'camera_opened': False, 'database_writes': 0,
              'human_room_mapping_verified': False, 'samples': [],
              'sample_attribution': {'url': 'https://storage.googleapis.com/mediapipe-assets/pose.jpg',
                'upstream_usage': 'https://github.com/google-ai-edge/mediapipe/blob/master/mediapipe/tasks/python/test/vision/pose_landmarker_test.py',
                'sha256': hashlib.sha256(sample.read_bytes()).hexdigest()}, 'model': detector.health()}
    try:
        assert detector.available and nano.health()['available']
        for name, frame, source_type in (
            ('official_visible_person_bent_yoga_pose', public, 'official_public_photo'),
            ('official_photo_upper_body_crop', public[:int(public.shape[0]*.65)], 'derived_crop_not_physical_trial'),
            ('actual_camera_saved_unknown', read(camera), 'saved_real_camera_frame')):
            h, w = frame.shape[:2]
            people = [{'label': row.label, 'bbox': [row.bbox[0]/w, row.bbox[1]/h, row.bbox[2]/w, row.bbox[3]/h],
                       'confidence': row.confidence} for row in nano.detect(frame) if row.label == 'person']
            rows = detector.detect(frame, people)
            report['samples'].append({'name': name, 'source_type': source_type, 'pixels_sha256': hashlib.sha256(frame.tobytes()).hexdigest(),
                'person_detections': people, 'results': rows, 'health_after': detector.health()})
            if name == 'official_visible_person_bent_yoga_pose':
                assert people, 'Actual NanoDet failed to detect the official sample person'
                assert detector.health()['frames_processed'] > 0
                assert any(row['landmarks'] for row in rows), 'Actual pose not associated with actual person'
            # Bent yoga / clipped legs / unknown room must not produce asserted ground position.
            assert all(row['ground_contact_confirmed'] is False and row['map_position'] is None for row in rows)
            assert all(row['foot_point'] is None for row in rows)
        report['passed'] = True
    finally:
        detector.close()
        report['closed'] = detector.health()['closed']
        output = Path(os.environ.get('OM_CORE_VERIFICATION_DIR') or root / 'data/verification/core-vision')
        destination = output / 'person-pose-model.json'
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
