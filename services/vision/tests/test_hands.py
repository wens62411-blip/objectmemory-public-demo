from pathlib import Path

import cv2
import numpy as np
import pytest

from services.vision.detectors import MediaPipeHandDetector


ROOT = Path(__file__).resolve().parents[3]


def test_mediapipe_tasks_backend_loads_model_from_chinese_path_buffer():
    model = ROOT / "data/models/hand_landmarker.task"
    if not model.is_file():
        pytest.skip("optional hand model not downloaded")
    detector = MediaPipeHandDetector(True, 2, model)
    try:
        assert detector.health()["available"], detector.health()
        assert detector.health()["backend"] == "tasks"
        result = detector.detect(np.zeros((256, 256, 3), dtype=np.uint8))
        assert isinstance(result, list)
    finally:
        detector.close()


def test_official_mediapipe_hand_image_detects_21_landmarks_when_present():
    model = ROOT / "data/models/hand_landmarker.task"
    image_path = ROOT / "data/temporary/official-mediapipe-woman-hands.jpg"
    if not model.is_file() or not image_path.is_file():
        pytest.skip("optional official hand test assets not downloaded")
    image = cv2.imdecode(np.frombuffer(image_path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    detector = MediaPipeHandDetector(True, 2, model)
    try:
        hands = detector.detect(image)
        assert len(hands) == 2
        assert all(len(hand.landmarks) == 21 for hand in hands)
    finally:
        detector.close()

