"""Bounded, no-network candidate audit on an independently filmed public video.

Only reads the declared video/model; never opens hardware or changes REAL data.
Outputs unmodified decoded keyframes and actual candidate model results. The
public-source frames are NOT the user's phone and do not establish identity.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2
from services.vision.detectors.nanodet import NanoDetDetectorBackend


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sample_video(video: Path, output: Path, seconds: list[float], confidence=.35):
    output.mkdir(parents=True, exist_ok=True)
    model = ROOT / 'data/models/object_detection_nanodet_2022nov.onnx'
    backend = NanoDetDetectorBackend(model, confidence=confidence)
    metadata_path = ROOT / 'scripts/test-assets' / f'{video.stem}.source.json'
    metadata = json.loads(metadata_path.read_text(encoding='utf-8')) if metadata_path.is_file() else None
    source_hash = digest(video)
    verified_source = bool(metadata and metadata.get('sha256') == source_hash)
    report = {'source': str(video), 'source_sha256': digest(video),
              'physical_camera': False, 'public_filmed_video': verified_source,
              'source_authenticity': 'pinned_public_filmed_video' if verified_source else 'unverified_input',
              'source_metadata': metadata if verified_source else None,
              'identity_accuracy': 'NOT_TESTED', 'candidate_mock': False,
              'model': backend.health(), 'model_sha256': digest(model),
              'confidence_threshold': confidence, 'samples': [], 'errors': [], 'completed': False}
    if not backend.health()['available']:
        raise RuntimeError(backend.health()['error'])
    capture = cv2.VideoCapture(str(video))
    try:
        if not capture.isOpened():
            raise RuntimeError('Public source file could not be decoded')
        fps = capture.get(cv2.CAP_PROP_FPS)
        report.update(fps=fps, frame_count=int(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
        for second in seconds:
            if not capture.set(cv2.CAP_PROP_POS_MSEC, second * 1000):
                raise RuntimeError(f'Failed to seek to {second}')
            ok, frame = capture.read()
            if not ok:
                report['errors'].append(f'Could not decode second {second}')
                continue
            source_frame = int(capture.get(cv2.CAP_PROP_POS_FRAMES)) - 1
            started = time.perf_counter()
            detections = backend.detect(frame)
            elapsed_ms = (time.perf_counter() - started) * 1000
            destination = output / f'frame-{source_frame:06d}.jpg'
            encoded_ok, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
            if not encoded_ok:
                raise RuntimeError('Keyframe encode failed')
            destination.write_bytes(encoded.tobytes())
            report['samples'].append({'requested_seconds': second,
                'source_frame_zero_based': source_frame,
                'decoded_timestamp_seconds': capture.get(cv2.CAP_PROP_POS_MSEC) / 1000,
                'shape': list(frame.shape), 'keyframe': str(destination),
                'keyframe_sha256': digest(destination),
                'decoded_pixels_sha256': hashlib.sha256(frame.tobytes()).hexdigest(),
                'latency_ms': round(elapsed_ms, 3),
                'detections': [asdict(detection) for detection in detections]})
        report['completed'] = not report['errors']
    except Exception as exc:
        report['errors'].append(f'{type(exc).__name__}: {exc}')
        raise
    finally:
        capture.release()
        report['camera_released'] = not capture.isOpened()
        report['phone_sample_count'] = sum(any(d['label'] == 'cell phone' for d in sample['detections']) for sample in report['samples'])
        (output / 'model-only.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--video', type=Path, default=ROOT/'data/test-assets/public-phone-android.webm')
    parser.add_argument('--output', type=Path, default=ROOT/'data/verification/public-phone-baseline')
    parser.add_argument('--seconds', nargs='+', type=float, default=[1, 4, 8, 12, 16, 20])
    parser.add_argument('--confidence', type=float, default=.35)
    args = parser.parse_args()
    if not 0 < args.confidence < 1 or not 1 <= len(args.seconds) <= 20 or any(t < 0 for t in args.seconds):
        parser.error('Use 1..20 nonnegative timestamps and a confidence in (0,1)')
    report = sample_video(args.video.resolve(), args.output.resolve(), args.seconds, args.confidence)
    print(json.dumps({'phone_sample_count': report['phone_sample_count'], 'samples': len(report['samples']), 'errors': report['errors']}, ensure_ascii=False))
    return int(bool(report['errors']))


if __name__ == '__main__':
    raise SystemExit(main())
