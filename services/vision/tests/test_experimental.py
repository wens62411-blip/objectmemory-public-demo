from pathlib import Path

import cv2
import numpy as np
import pytest

from services.vision.detectors import Detection, HsvReferenceMatcher, NanoDetDetectorBackend, compute_hsv_feature
from services.vision.engine import VisionEngine


ROOT = Path(__file__).resolve().parents[3]


def test_hsv_reference_matcher_consumes_api_feature_contract():
    blue = np.full((80, 100, 3), (220, 80, 25), dtype=np.uint8)
    red = np.full((80, 100, 3), (25, 40, 220), dtype=np.uint8)
    items = [{
        "id": "wallet", "reference_images": [
            {"features": {"backend": "hsv-histogram-v1", "vector": compute_hsv_feature(blue)}}
        ],
    }]
    matcher = HsvReferenceMatcher(items, threshold=.8)
    assert matcher.match(blue)[0] == "wallet"
    assert matcher.match(red)[0] is None


def test_official_nanodet_model_executes_real_cpu_inference_when_downloaded():
    model = ROOT / "data/models/object_detection_nanodet_2022nov.onnx"
    if not model.is_file():
        pytest.skip("optional model not downloaded")
    capture = cv2.VideoCapture(str(ROOT / "demo/sample-videos/object-memory-demo.avi"))
    ok, frame = capture.read()
    capture.release()
    assert ok
    detector = NanoDetDetectorBackend(model, confidence=.2)
    assert detector.health()["available"]
    result = detector.detect(frame)
    assert isinstance(result, list)
    assert detector.health()["device"] in {"cpu", "cuda"}


def test_legacy_color_alone_cannot_assign_registered_photo_identity(tmp_path):
    blue = np.full((100, 100, 3), (220, 80, 25), dtype=np.uint8)
    items = [{
        "id": "wallet", "name": "蓝色钱包", "aruco_id": None,
        "reference_images": [{"features": {"backend": "hsv-histogram-v1", "vector": compute_hsv_feature(blue)}}],
    }]
    engine = VisionEngine(
        {"id": "cam", "source_type": "video", "source": str(ROOT / "demo/sample-videos/object-memory-demo.avi")},
        items, [], {"data_dir": str(tmp_path), "detection_mode": "experimental", "reference_match_threshold": .8, "show_hands": False},
        lambda *_args: None,
    )
    detection = Detection("generic:handbag:0", "handbag", (0, 0, 100, 100), (50, 50), .91, "experimental")
    engine._assign_reference_identities(blue, [detection])
    assert detection.identity.startswith('generic:')
    assert not engine._recognition_candidates[0]['accepted']
    engine.stop()


def test_missing_experimental_model_never_falls_back_to_aruco(tmp_path):
    engine = VisionEngine(
        {"id": "cam", "source_type": "video", "source": str(ROOT / "demo/sample-videos/object-memory-demo.avi")},
        [{"id": "phone", "name": "我的手机", "aruco_id": 1}], [],
        {"data_dir": str(tmp_path), "detection_mode": "experimental", "model_path": str(tmp_path / "missing.onnx"), "show_hands": False},
        lambda *_args: None,
    )
    health = engine.health()
    assert health["detection_mode"] == "experimental"
    assert health["fallback_mode"] is None
    assert health['detector']['available'] is False
    assert health['detector']['error']
    engine.stop()
