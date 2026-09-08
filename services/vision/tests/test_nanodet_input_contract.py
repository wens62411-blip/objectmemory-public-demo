"""Fixed input/output contract fixtures, not physical-phone accuracy tests.

The inference net is a recording fixture so tensor channels, letterbox padding,
and inverse geometry can be asserted independently of a model's predictions.
"""
from pathlib import Path

import cv2
import numpy as np
import pytest

from services.vision.detectors.nanodet import NanoDetDetectorBackend


class RecordingNet:
    def __init__(self, predictions):
        self.predictions = predictions
        self.blob = None

    def setInput(self, blob):
        self.blob = blob.copy()

    def getUnconnectedOutLayersNames(self):
        return ['fixture-class', 'fixture-box']

    def forward(self, names):
        return self.predictions


def detector_fixture(*, box=None, confidence=.9):
    detector = NanoDetDetectorBackend(Path(__file__).parent / 'never-a-model.onnx')
    # A single exact distribution anchor makes postprocess geometry deterministic.
    detector.strides = (1,)
    detector.reg_max = 208
    detector.project = np.arange(209)
    if box is None:
        box = [104, 104, 312, 312]
        confidence = 0.
    x1, y1, x2, y2 = box
    cx, cy = (x1+x2)/2, (y1+y2)/2
    detector.anchors = [np.asarray([[cx, cy]], dtype=np.float32)]
    distribution = np.full((4, 209), -100., dtype=np.float32)
    for index, distance in enumerate((cx-x1, cy-y1, x2-cx, y2-cy)):
        distribution[index, int(distance)] = 0
    classes = np.zeros((1, 80), dtype=np.float32)
    classes[0, 67] = confidence
    detector.net = RecordingNet([classes, distribution.reshape(1, -1)])
    return detector


@pytest.mark.parametrize('height,width,new_height,new_width,top,left', [
    (4, 8, 208, 416, 104, 0),
    (8, 4, 416, 208, 0, 104),
    (4, 4, 416, 416, 0, 0),
    (5, 7, 297, 416, 59, 0),  # bottom padding is one pixel larger
])
def test_tensor_is_rgb_area_letterbox_with_black_padding(height, width, new_height, new_width, top, left):
    frame = np.arange(height*width*3, dtype=np.uint8).reshape(height, width, 3)
    frame[:, :, 2] += 80  # distinct B and R even when testing a uniform region
    original = frame.copy()
    detector = detector_fixture()
    assert detector.detect(frame) == []
    expected = np.zeros((416, 416, 3), dtype=np.uint8)
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    expected[top:top+new_height, left:left+new_width] = cv2.resize(
        rgb, (new_width, new_height), interpolation=cv2.INTER_AREA)
    expected_tensor = ((expected.astype(np.float32) - np.asarray([103.53,116.28,123.675], np.float32))
                       / np.asarray([57.375,57.12,58.395], np.float32)).transpose(2,0,1)[None]
    np.testing.assert_allclose(detector.net.blob, expected_tensor, atol=1e-6, rtol=0)
    np.testing.assert_array_equal(frame, original)


@pytest.mark.parametrize('shape,box,expected', [
    ((100,200,3), [104,156,312,260], (50,25,100,50)),
    ((200,100,3), [156,104,260,312], (25,50,50,100)),
    ((100,100,3), [104,104,312,312], (25,25,50,50)),
    ((100,200,3), [104,52,312,364], (50,0,100,100)),
])
def test_model_boxes_are_unpadded_before_scaling_to_source(shape, box, expected):
    detector = detector_fixture(box=box)
    result = detector.detect(np.zeros(shape, dtype=np.uint8))
    assert len(result) == 1
    assert result[0].label == 'cell phone'
    assert result[0].bbox == expected
    x,y,w,h = expected
    assert result[0].center == (x+w/2,y+h/2)
    assert result[0].confidence == pytest.approx(.9)


@pytest.mark.parametrize('shape,box', [
    ((100,200,3), [104,10,312,30]),  # entirely inside top letterbox padding
    ((200,100,3), [10,104,30,312]),  # entirely inside left padding
])
def test_padding_only_prediction_does_not_invent_a_source_pixel_box(shape, box):
    assert detector_fixture(box=box).detect(np.zeros(shape, dtype=np.uint8)) == []


def test_default_confidence_is_not_lowered_to_make_a_fixture_pass():
    detector = detector_fixture(box=[104,104,312,312], confidence=.1)
    assert detector.confidence == .35
    assert detector.detect(np.zeros((100,100,3), dtype=np.uint8)) == []


def test_unknown_runtime_is_not_silently_selected():
    with pytest.raises(ValueError, match='runtime'):
        NanoDetDetectorBackend('absent.onnx', runtime='network-magic')


def test_actual_onnxruntime_matches_opencv_heads_and_detections():
    pytest.importorskip('onnxruntime')
    root = Path(__file__).resolve().parents[3]
    model = root / 'data/models/object_detection_nanodet_2022nov.onnx'
    footage = root / 'data/test-assets/public-phone-lotti.webm'
    if not model.is_file() or not footage.is_file():
        pytest.skip('Actual pinned model and independently filmed public video required')
    old = NanoDetDetectorBackend(model, runtime='opencv')
    new = NanoDetDetectorBackend(model, runtime='onnxruntime')
    assert old.health()['available'] and new.health()['available']
    assert new.health()['inference_runtime'] == 'onnxruntime_cpu'
    assert new.health()['runtime_threads'] == 2
    assert old.confidence == new.confidence == .35
    capture = cv2.VideoCapture(str(footage))
    phone_frames = 0
    try:
        for second in (20, 41, 45, 50, 54):
            capture.set(cv2.CAP_PROP_POS_MSEC, second * 1000)
            ok, pixels = capture.read()
            assert ok
            first, second_results = old.detect(pixels), new.detect(pixels)
            assert [row.label for row in first] == [row.label for row in second_results]
            phone_frames += any(row.label == 'cell phone' for row in first)
            for a, b in zip(first, second_results):
                assert a.confidence == pytest.approx(b.confidence, abs=1e-5)
                np.testing.assert_allclose(a.bbox, b.bbox, atol=1, rtol=0)
    finally:
        capture.release()
    assert phone_frames >= 4  # Parity includes actual phone-positive frames, not just empty output.


def test_auto_runtime_keeps_opencv_when_optional_runtime_is_absent(monkeypatch, tmp_path):
    import sys
    monkeypatch.setitem(sys.modules, 'onnxruntime', None)
    model = tmp_path / 'unit-model.onnx'
    model.write_bytes(b'unit fixture; parser is explicitly mocked')
    monkeypatch.setattr(cv2.cuda, 'getCudaEnabledDeviceCount', lambda: 0)
    monkeypatch.setattr(cv2.dnn, 'readNetFromONNX', lambda _bytes: RecordingNet([]))
    detector = NanoDetDetectorBackend(model)
    assert detector.health()['available']
    assert detector.health()['inference_runtime'] == 'opencv_dnn'
    requested = NanoDetDetectorBackend(model, runtime='onnxruntime')
    assert not requested.health()['available']
    assert requested.health()['error']


def test_invalid_runtime_model_contract_fails_closed_without_another_parser(monkeypatch, tmp_path):
    ort = pytest.importorskip('onnxruntime')
    class WrongSession:
        def get_inputs(self): return []
        def get_outputs(self): return []
    model = tmp_path / 'unit-model.onnx'
    model.write_bytes(b'unit fixture; session is explicitly mocked')
    monkeypatch.setattr(ort, 'InferenceSession', lambda *args, **kwargs: WrongSession())
    monkeypatch.setattr(cv2.dnn, 'readNetFromONNX', lambda _bytes: pytest.fail('Invalid contract must not silently fall back'))
    detector = NanoDetDetectorBackend(model, runtime='onnxruntime')
    assert not detector.health()['available']
    assert 'contract' in detector.health()['error']
    assert detector.detect(np.zeros((80, 80, 3), np.uint8)) == []
