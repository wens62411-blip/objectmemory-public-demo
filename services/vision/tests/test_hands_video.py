from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import threading

import numpy as np
import pytest

from services.vision.detectors.hands import MediaPipeHandDetector


@pytest.fixture
def video_detector(tmp_path, monkeypatch):
    pytest.importorskip("mediapipe")
    from mediapipe.tasks.python import vision
    native = SimpleNamespace(timestamps=[], closed=0, error=None, invalid_points=False, no_hands=False)
    def infer(_image, timestamp):
        native.timestamps.append(timestamp)
        if native.error:
            raise RuntimeError(native.error)
        points = [SimpleNamespace(x=.1 + i * .01, y=.2 + i * .015) for i in range(21)]
        if native.invalid_points:
            points[0].x = float("nan")
        return SimpleNamespace(hand_landmarks=[] if native.no_hands else [points],
                               handedness=[] if native.no_hands else [[SimpleNamespace(category_name="Left", score=.97)]])
    native.detect_for_video = infer
    def close():
        native.closed += 1
    native.close = close
    def create(options):
        native.options = options
        return native
    monkeypatch.setattr(vision.HandLandmarker, "create_from_options", staticmethod(create))
    model = tmp_path / "中文模型.task"
    model.write_bytes(b"controlled_test_model_native_runtime_is_doubled")
    detector = MediaPipeHandDetector(True, 2, model)
    yield detector, native
    detector.close()


def test_video_mode_two_hands_and_no_fabricated_presence(video_detector):
    detector, native = video_detector
    assert native.options.running_mode.name == "VIDEO" and native.options.num_hands == 2
    hands = detector.detect(np.zeros((100, 200, 3), np.uint8), timestamp_ms=100)
    assert len(hands) == 1 and len(hands[0].landmarks) == 21
    assert hands[0].confidence is None
    assert hands[0].handedness == "Left" and hands[0].handedness_score == .97
    assert hands[0].source_timestamp_ms == hands[0].timestamp_ms == 100
    assert detector.health()["confidence_kind"] == "presence_not_exposed"


def test_duplicate_backward_timestamps_are_adjusted_and_explicit(video_detector):
    detector, native = video_detector
    frame = np.zeros((100, 200, 3), np.uint8)
    for value in (100, 100, 90, 150):
        detector.detect(frame, timestamp_ms=value)
    assert native.timestamps == [100, 101, 102, 150]
    health = detector.health()
    assert health["timestamp_adjustments"] == 2
    assert health["last_input_timestamp_ms"] == health["last_timestamp_ms"] == 150
    assert health["frames_processed"] == 4 and health["last_latency_ms"] >= 0


def test_default_detect_remains_compatible_and_state_no_hands(video_detector):
    detector, native = video_detector
    native.no_hands = True
    assert detector.detect(np.zeros((100, 200, 3), np.uint8)) == []
    assert detector.health()["status"] == "no_hands"
    assert detector.health()["last_hand_count"] == 0
    assert detector.health()["last_input_timestamp_ms"] is None


def test_native_failure_is_recorded_and_next_frame_can_recover(video_detector):
    detector, native = video_detector
    native.error = "controlled native failure"
    assert detector.detect(np.zeros((100, 200, 3), np.uint8), timestamp_ms=1) == []
    assert detector.health()["status"] == "failed"
    assert "controlled native failure" in detector.health()["error"]
    native.error = None
    assert len(detector.detect(np.zeros((100, 200, 3), np.uint8), timestamp_ms=2)) == 1
    assert detector.health()["status"] == "ready" and detector.health()["error"] is None


def test_nonfinite_landmarks_rejected_not_drawn_or_used_for_nearby(video_detector):
    detector, native = video_detector
    native.invalid_points = True
    assert detector.detect(np.zeros((100, 200, 3), np.uint8), timestamp_ms=1) == []
    assert detector.health()["status"] == "failed"


@pytest.mark.parametrize("timestamp", [True, -1, float("nan"), "100", 1.2])
def test_invalid_timestamp_is_not_sent_to_native(video_detector, timestamp):
    detector, native = video_detector
    assert detector.detect(np.zeros((100, 200, 3), np.uint8), timestamp_ms=timestamp) == []
    assert native.timestamps == [] and detector.health()["status"] == "failed"


def test_close_is_idempotent_and_prevents_native_reuse(video_detector):
    detector, native = video_detector
    detector.close()
    detector.close()
    assert native.closed == 1
    assert detector.detect(np.zeros((100, 200, 3), np.uint8), timestamp_ms=5) == []
    assert native.timestamps == []
    assert not detector.health()["available"] and detector.health()["status"] == "disabled"


def test_disabled_does_not_load_a_model():
    detector = MediaPipeHandDetector(False, model_path=Path("does-not-exist"))
    assert detector.health()["status"] == "disabled" and not detector.available
    assert detector.detect(np.zeros((10, 10, 3), np.uint8)) == []
    detector.close()


def test_close_waits_for_active_detection_and_releases_once(video_detector):
    detector, native = video_detector
    entered, proceed = threading.Event(), threading.Event()
    original = native.detect_for_video
    def slow_inference(image, timestamp):
        entered.set()
        assert proceed.wait(2)
        return original(image, timestamp)
    native.detect_for_video = slow_inference
    with ThreadPoolExecutor(max_workers=2) as pool:
        infer_future = pool.submit(detector.detect, np.zeros((100, 200, 3), np.uint8), 1)
        assert entered.wait(2)
        close_future = pool.submit(detector.close)
        try:
            assert native.closed == 0
        finally:
            proceed.set()
        assert len(infer_future.result(timeout=3)) == 1
        close_future.result(timeout=3)
    assert native.closed == 1 and detector.health()["status"] == "disabled"


@pytest.mark.parametrize("kwargs", [{"max_hands": 3}, {"max_hands": True},
    {"min_presence_confidence": float("nan")}, {"min_tracking_confidence": 1.2}, {"min_detection_confidence": -1}])
def test_invalid_model_configuration_fails_before_native_load(kwargs):
    detector = MediaPipeHandDetector(**kwargs)
    assert not detector.available and detector.health()["status"] == "failed"
    detector.close()
