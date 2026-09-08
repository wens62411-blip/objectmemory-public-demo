from pathlib import Path

import cv2
import numpy as np

from services.vision.detectors import ArucoDetectorBackend
from services.vision.trackers import StableIdentityTracker


ROOT = Path(__file__).resolve().parents[3]


def test_generated_marker_is_really_detected():
    image = cv2.imdecode(np.frombuffer((ROOT / "demo" / "markers" / "aruco-001.png").read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    detector = ArucoDetectorBackend({1: {"id": "phone", "name": "我的手机"}})
    detections = detector.detect(image)
    assert len(detections) == 1
    assert detections[0].raw_id == 1
    assert detections[0].identity == "phone"
    assert detections[0].confidence >= 0.92


def test_every_demo_video_frame_contains_real_marker():
    video = cv2.VideoCapture(str(ROOT / "demo" / "sample-videos" / "object-memory-demo.avi"))
    detector = ArucoDetectorBackend()
    frames = detected = 0
    centers = []
    while True:
        ok, frame = video.read()
        if not ok:
            break
        frames += 1
        detections = detector.detect(frame)
        if detections:
            detected += 1
            centers.append(detections[0].center[0] / frame.shape[1])
    video.release()
    assert frames == 140
    assert detected == frames
    assert min(centers) < 0.3
    assert max(centers) > 0.7


def test_tracker_recovers_stable_track_id_after_short_occlusion():
    detector = ArucoDetectorBackend({1: {"id": "phone", "name": "手机"}})
    image = cv2.imdecode(np.frombuffer((ROOT / "demo" / "markers" / "aruco-001.png").read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    detection = detector.detect(image)[0]
    tracker = StableIdentityTracker(recovery_seconds=3)
    first, missing = tracker.update([detection], image.shape, 0.0)
    assert not missing
    _none, missing = tracker.update([], image.shape, 0.5)
    assert "phone" in missing
    recovered, _missing = tracker.update([detection], image.shape, 1.0)
    assert recovered[0].track_id == first[0].track_id
    assert recovered[0].recovered


def test_tracker_deduplicates_same_registered_identity_per_frame():
    detector = ArucoDetectorBackend({1: {"id": "phone", "name": "手机"}})
    image = cv2.imdecode(np.frombuffer((ROOT / "demo/markers/aruco-001.png").read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    first = detector.detect(image)[0]
    second = type(first)(first.identity, first.label, (20, 20, 50, 50), (45, 45), first.confidence - .1, first.detection_mode, first.raw_id)
    tracker = StableIdentityTracker()
    observations, _ = tracker.update([first, second], image.shape, 1.0)
    assert len(observations) == 1
    assert observations[0].bbox == first.bbox
