"""Actual offline hand model + geometry on an existing public photo, not a camera audit."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
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
from services.vision.hand_actions import HandActionAnalyzer


def main():
    source = ROOT / "data/temporary/official-mediapipe-woman-hands.jpg"
    model = ROOT / "data/models/hand_landmarker.task"
    output = ROOT / "data/verification/hand-postures.json"
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "physical_camera_used": False,
              "physical_grasp_verified": False, "private_images_uploaded": False, "database_used": False,
              "input_kind": "existing_public_photo_repeated_and_transformed_not_physical_video",
              "source_path": str(source), "model_path": str(model), "sequence_fps": 5,
              "network_blocked": True, "camera_constructor_blocked": True,
              "detector_reinitialized_for_each_independent_source": True,
              "synthetic_translation_pixels_per_frame": 8,
              "posture_ground_truth": "not_labeled_no_gesture_accuracy_claim", "variants": [], "errors": []}
    detector = None
    try:
        raw = source.read_bytes()
        report["source_sha256"] = hashlib.sha256(raw).hexdigest()
        report["model_sha256"] = hashlib.sha256(model.read_bytes()).hexdigest()
        original = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if original is None:
            raise ValueError("Existing public image cannot be decoded")
        height, width = original.shape[:2]
        variants = [
            ("static_original", [original] * 6),
            ("static_horizontal_flip", [cv2.flip(original, 1)] * 6),
            ("static_half_size", [cv2.resize(original, (width//2, height//2), interpolation=cv2.INTER_AREA)] * 6),
            ("synthetic_image_translation_right", [cv2.warpAffine(original, np.float32([[1, 0, i*8], [0, 1, 0]]),
                 (width, height), borderMode=cv2.BORDER_REPLICATE) for i in range(6)]),
        ]
        with patch("socket.socket", side_effect=AssertionError("Network forbidden in offline public-photo audit")), \
             patch("socket.create_connection", side_effect=AssertionError("Network forbidden in offline public-photo audit")), \
             patch("cv2.VideoCapture", side_effect=AssertionError("Camera forbidden in public-photo audit")):
            for variant_index, (name, frames) in enumerate(variants):
                # Each transform is an independent source, not a continuous
                # VIDEO stream. Do not reuse native tracking across a cut.
                if detector is not None:
                    detector.close()
                detector = MediaPipeHandDetector(True, 2, model)
                if not detector.available:
                    raise RuntimeError(detector.error)
                analyzer = HandActionAnalyzer()
                variant = {"name": name, "width": frames[0].shape[1], "height": frames[0].shape[0],
                           "physical_motion": False, "frames": []}
                detector_times, analyzer_times = [], []
                for index, pixels in enumerate(frames, 1):
                    seconds = (variant_index+1)*10 + index/5
                    before = time.perf_counter()
                    hands = detector.detect(pixels, timestamp_ms=round(seconds*1000))
                    detector_times.append((time.perf_counter()-before)*1000)
                    before = time.perf_counter()
                    actions = analyzer.update(hands, pixels.shape, f"public-photo-{name}", index, seconds)
                    analyzer_times.append((time.perf_counter()-before)*1000)
                    variant["frames"].append({"source_frame": index, "source_timestamp": seconds,
                        "detected_hands": len(hands), "landmark_counts": [len(hand.landmarks) for hand in hands],
                        "detector_bboxes_xywh_pixels": [list(hand.bbox) for hand in hands], "actions": actions})
                    if len(hands) != 2 or len(actions) != 2 or any(len(hand.landmarks) != 21 for hand in hands):
                        report["errors"].append(f"{name} frame {index}: expected two actual model hands and two actions")
                    for hand, action in zip(hands, actions):
                        expected_bbox = [hand.bbox[0]/pixels.shape[1], hand.bbox[1]/pixels.shape[0],
                                         hand.bbox[2]/pixels.shape[1], hand.bbox[3]/pixels.shape[0]]
                        if action["bbox"] != expected_bbox or action["bbox_format"] != "xywh":
                            report["errors"].append(f"{name} frame {index}: detector/action XYWH mismatch")
                        if action["confidence"] is not None or action["grasp_established"] is not False:
                            report["errors"].append(f"{name} frame {index}: fabricated grasp or confidence")
                variant["detector_median_ms"] = statistics.median(detector_times)
                variant["analyzer_median_ms"] = statistics.median(analyzer_times)
                variant["analyzer_health"] = analyzer.health()
                variant["final_postures"] = [action["posture"] for action in actions]
                variant["final_motions"] = [action["motion"] for action in actions]
                variant["final_tracking_stable"] = [action["tracking_stable"] for action in actions]
                if not all(variant["final_tracking_stable"]):
                    report["errors"].append(f"{name}: hand association did not become stable")
                report["variants"].append(variant)
            report["detector_health"] = detector.health()
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        if detector is not None:
            detector.close()
            report["detector_closed"] = detector.health()["closed"]
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["success"] = not report["errors"]
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        os.replace(temporary, output)
    print(json.dumps({"success": report["success"], "errors": report["errors"], "report": str(output),
        "variants": [{key: item[key] for key in ("name", "final_postures", "final_motions", "final_tracking_stable",
                       "detector_median_ms", "analyzer_median_ms")} for item in report["variants"]]}, ensure_ascii=False, indent=2))
    return int(not report["success"])


if __name__ == "__main__":
    raise SystemExit(main())
