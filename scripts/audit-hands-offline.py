"""Read-only detector audit on the existing public photo. No camera, DB or downloads."""
from __future__ import annotations

import hashlib
import importlib.metadata
import itertools
import json
from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.vision.detectors.hands import MediaPipeHandDetector


def main() -> int:
    source = ROOT / "data/temporary/official-mediapipe-woman-hands.jpg"
    model = ROOT / "data/models/hand_landmarker.task"
    # Preserve the IMAGE-mode before audit; VIDEO measurements are a new result.
    report_path = ROOT / "data/verification/hands-video-audit.json"
    report = {"physical_camera_used": False, "private_images_uploaded": False, "database_used": False,
              "source_type": "existing_public_static_photo", "source_path": str(source),
              "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "model_path": str(model), "model_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
              "model_bytes": model.stat().st_size,
              "model_metadata": json.loads(model.with_suffix(".task.json").read_text()),
              "versions": {name: importlib.metadata.version(name) for name in ("mediapipe", "numpy", "opencv-contrib-python")},
              "variants": [], "errors": [], "started_at": time.time(),
              "confidence_semantics": "confidence is null because presence probability is not exposed; handedness_score separately describes Left/Right classification",
              "network_socket_blocked": True}
    detector = None
    try:
        image = cv2.imdecode(np.frombuffer(source.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError("Existing public photo could not be decoded")
        height, width = image.shape[:2]
        variants = [("original", image, False), ("horizontal_flip", cv2.flip(image, 1), True),
                    ("half_resolution", cv2.resize(image, (width // 2, height // 2), interpolation=cv2.INTER_AREA), False),
                    ("one_and_half_resolution", cv2.resize(image, (width * 3 // 2, height * 3 // 2), interpolation=cv2.INTER_LINEAR), False)]
        with patch("socket.socket", side_effect=AssertionError("Network forbidden during offline hand audit")), \
             patch("socket.create_connection", side_effect=AssertionError("Network forbidden during offline hand audit")):
            started = time.perf_counter()
            detector = MediaPipeHandDetector(True, 2, model)
            report["initialization_ms"] = (time.perf_counter() - started) * 1000
            report["detector_health"] = detector.health()
            if not detector.available:
                raise RuntimeError(detector.error)
            original_points = None
            for variant_index, (name, pixels, flipped) in enumerate(variants):
                durations = []
                submitted_timestamps = []
                base_timestamp = (variant_index + 1) * 1000
                input_timestamps = [base_timestamp + delta for delta in (0, 0, -10, 100, 200, 300)]
                for source_timestamp in input_timestamps:
                    started = time.perf_counter()
                    observations = detector.detect(pixels, timestamp_ms=source_timestamp)
                    durations.append((time.perf_counter() - started) * 1000)
                    submitted_timestamps.append(detector.health()["last_timestamp_ms"])
                normalized = [np.asarray(hand.landmarks, np.float64) for hand in observations]
                restored = [points.copy() for points in normalized]
                if flipped:
                    for points in restored:
                        points[:, 0] = 1 - points[:, 0]
                if original_points is None:
                    original_points = restored
                mapping_error = None
                if len(restored) == len(original_points) == 2:
                    mapping_error = min(float(np.mean([np.linalg.norm(restored[p[i]] - original_points[i], axis=1).mean() for i in range(2)]))
                                        for p in itertools.permutations(range(2)))
                result = {"name": name, "width": pixels.shape[1], "height": pixels.shape[0], "hand_count": len(observations),
                    "landmark_counts": [len(hand.landmarks) for hand in observations],
                    "first_ms": durations[0], "warm_median_ms": statistics.median(durations[1:]), "all_ms": durations,
                    "labels": [hand.handedness for hand in observations],
                    "handedness_scores": [hand.handedness_score for hand in observations],
                    "current_confidence_values": [hand.confidence for hand in observations],
                    "presence_confidence_honestly_null": all(hand.confidence is None for hand in observations),
                    "input_timestamps_ms": input_timestamps, "submitted_timestamps_ms": submitted_timestamps,
                    "timestamps_strictly_increasing": all(a < b for a, b in zip(submitted_timestamps, submitted_timestamps[1:])),
                    "normalized_backprojection_mean_error": mapping_error,
                    "hands": [{"bbox_pixels": list(hand.bbox), "center_pixels": list(hand.center), "landmarks_normalized": hand.landmarks} for hand in observations],
                    "all_coordinates_finite": all(np.isfinite(points).all() for points in normalized)}
                report["variants"].append(result)
                if len(observations) != 2 or any(len(hand.landmarks) != 21 for hand in observations):
                    report["errors"].append(name + ": expected two hands with 21 landmarks each")
            report["black_frame_hand_count"] = len(detector.detect(np.zeros_like(image)))
            report["final_health_before_close"] = detector.health()
            if report["black_frame_hand_count"] != 0:
                report["errors"].append("black frame unexpectedly detected hands")
    except Exception as exc:
        report["errors"].append(type(exc).__name__ + ": " + str(exc))
    finally:
        if detector is not None:
            detector.close()
            report["closed_available_false"] = detector.health()["available"] is False
        report["finished_at"] = time.time()
        report["success"] = not report["errors"]
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"success": report["success"], "errors": report["errors"], "report": str(report_path),
                     "initialization_ms": report.get("initialization_ms"),
                     "variants": [{key: variant[key] for key in ("name", "width", "height", "hand_count", "landmark_counts", "warm_median_ms", "labels", "handedness_scores", "normalized_backprojection_mean_error")} for variant in report["variants"]]}, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
