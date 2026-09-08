from pathlib import Path

import cv2
import numpy as np
import pytest

from services.vision.detectors import Detection, HsvReferenceMatcher, NanoDetDetectorBackend, compute_hsv_feature
from services.vision.detectors.experimental import OnnxYoloDetectorBackend
from services.vision.engine import VisionEngine


ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize('transposed', [False, True])
@pytest.mark.parametrize('shape', [(100, 200, 3), (200, 100, 3)])
def test_yolo_vectorized_decode_preserves_scalar_boxes_scores_and_order(monkeypatch, shape, transposed):
    rows = np.random.default_rng(17).uniform(0, .3, (8400, 84)).astype(np.float32)
    rows[3, 4] = np.float32(.35)  # Rounded below the Python threshold; must not shift indices.
    for index, category, score, box in [(4, 67, .91, [173.9, 183.5, 120.3, 70.7]),
                                       (82, 0, .8, [100.1, 140.9, 200.3, 110.2]),
                                       (5904, 67, .85, [400.9, 300.3, 60.1, 50.7])]:
        rows[index, :4], rows[index, category + 4] = box, score
    expected_boxes, expected_scores, expected_classes = [], [], []
    for row in rows:
        category = int(np.argmax(row[4:]))
        score = float(row[category + 4])
        if score >= .35:
            cx, cy, width, height = (float(value) * max(shape[:2]) / 640 for value in row[:4])
            expected_boxes.append([int(cx-width/2), int(cy-height/2), int(width), int(height)])
            expected_scores.append(score)
            expected_classes.append(category)

    class Net:
        def setInput(self, blob): pass
        def forward(self): return (rows.T if transposed else rows)[None]

    def nms(boxes, scores, confidence, iou):
        assert boxes == expected_boxes and scores == expected_scores
        assert confidence == .35 and iou == .45
        return np.asarray([2, 0])

    monkeypatch.setattr(cv2.dnn, 'NMSBoxes', nms)
    detector = OnnxYoloDetectorBackend('never-a-model.onnx')
    detector.net = Net()
    result = detector.detect(np.zeros(shape, np.uint8))
    # NMS inputs remain scalar-equivalent; wholly padded boxes are not objects.
    visible = [index for index in (2, 0)
               if expected_boxes[index][0] < shape[1] and expected_boxes[index][1] < shape[0]
               and sum(expected_boxes[index][::2]) > 0 and sum(expected_boxes[index][1::2]) > 0]
    assert [row.raw_id for row in result] == [expected_classes[index] for index in visible]
    assert [row.confidence for row in result] == [expected_scores[index] for index in visible]
    assert [row.identity for row in result] == [f'generic:cell phone:{index}' for index in visible]


@pytest.mark.parametrize('output_shape', [(1, 84, 8400), (1, 8400, 84), (1, 2, 4)])
def test_yolo_empty_or_short_predictions_produce_no_detections(output_shape):
    class Net:
        def setInput(self, blob): pass
        def forward(self): return np.zeros(output_shape, np.float32)

    detector = OnnxYoloDetectorBackend('never-a-model.onnx')
    detector.net = Net()
    assert detector.detect(np.zeros((100, 100, 3), np.uint8)) == []


@pytest.mark.parametrize('transposed', [False, True])
@pytest.mark.parametrize('row_count', [1, 3, 300])
def test_yolov5_uses_objectness_times_class_probability_without_shifting_phone(transposed, row_count):
    rows = np.zeros((row_count, 85), np.float32)
    rows[:, :4] = [320, 320, 100, 100]
    rows[:, 4] = .01  # High class probability alone is not evidence of an object.
    rows[:, 5 + 67] = .99
    rows[0, 4] = .8
    before = rows.copy()

    class Net:
        def setInput(self, blob): pass
        def forward(self): return (rows.T if transposed else rows)[None]

    detector = OnnxYoloDetectorBackend('never-a-model.onnx')
    detector.net = Net()
    result = detector.detect(np.zeros((640, 640, 3), np.uint8))
    assert len(result) == 1
    assert result[0].label == 'cell phone' and result[0].raw_id == 67
    assert result[0].confidence == pytest.approx(float(rows[0, 4]) * float(rows[0, 72]))
    np.testing.assert_array_equal(rows, before)


@pytest.mark.parametrize('columns', [84, 85])
def test_yolo_rejects_nonfinite_predictions_and_clips_to_actual_frame(columns):
    rows = np.zeros((5, columns), np.float32)
    offset = 5 if columns == 85 else 4
    if columns == 85:
        rows[:, 4] = 1
    rows[:, offset + 67] = .9
    rows[:, :4] = [0, 320, 200, 100]
    rows[1, 0] = np.nan
    rows[2, offset + 67] = np.nan
    rows[3, 0] = 900  # Entirely outside the original image.
    rows[4, 2] = -20

    class Net:
        def setInput(self, blob): pass
        def forward(self): return rows[None]

    detector = OnnxYoloDetectorBackend('never-a-model.onnx')
    detector.net = Net()
    result = detector.detect(np.zeros((640, 640, 3), np.uint8))
    assert len(result) == 1
    assert result[0].bbox == (0, 270, 100, 100)
    assert result[0].center == (50, 320)


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
