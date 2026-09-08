"""Bounded local physical-camera read comparison; no pixels or business writes.

Briefly releases the selected running source, reads it without inference using
the existing adapter/configuration, then restores its running state in finally.
Never changes exposure, gain, resolution or the persisted camera configuration.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

import cv2
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.vision.camera_sources.opencv_stream import WebcamSource


def distribution(values):
    ordered = sorted(values)
    return {'count': len(ordered), 'min': min(ordered), 'median': statistics.median(ordered),
            'p95': ordered[int((len(ordered)-1)*.95)], 'max': max(ordered)} if ordered else {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=10)
    parser.add_argument('--backend', choices=['DSHOW', 'MSMF'])
    parser.add_argument('--mjpeg', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not 2 <= args.seconds <= 10 or not output.is_relative_to((ROOT/'data/verification').resolve()) or output.exists():
        parser.error('Use 2..10 seconds and a new data/verification output')
    result = {'started_at': datetime.now(timezone.utc).isoformat(), 'runtime_mode': 'REAL',
              'source_type': 'opencv_camera', 'is_simulated': False, 'inference_enabled': False,
              'pixels_saved': 0, 'business_writes': 0, 'settings_changed': False,
              'transient_backend_override': args.backend,
              'transient_mjpeg_request': args.mjpeg,
              'exposure_writes': 0, 'restored': False, 'samples': [], 'errors': []}
    adapter = None
    with httpx.Client(base_url='http://127.0.0.1:8018', trust_env=False, timeout=30) as client:
        client.get('/api/session').raise_for_status()
        cameras = client.get('/api/cameras').json()
        camera = next(row for row in cameras if row.get('enabled') and row.get('source_type') == 'webcam')
        if not camera['health'].get('capture_thread_alive') or camera['health'].get('status') not in {'online', 'ready', 'running'}:
            raise SystemExit('Require an already running webcam to compare and restore')
        camera_path = f"/api/cameras/{camera['id']}"
        settings = dict(camera['config'])
        settings['backend'] = args.backend or camera['health']['backend']
        if args.mjpeg:
            settings['mjpeg'] = True
        before_events = len(client.get('/api/events').json())
        result['online_before'] = {key: camera['health'].get(key) for key in (
            'capture_fps', 'preview_fps', 'inference_fps', 'backend', 'source_session_id')}
        stopped = False
        try:
            response = client.post(camera_path+'/stop')
            response.raise_for_status()
            stopped = True
            stopped_health = client.get(camera_path).json().get('health', {})
            if stopped_health.get('capture_thread_alive') or stopped_health.get('status') not in {'stopped', 'error'}:
                raise RuntimeError('Original camera has not released its capture worker')
            adapter = WebcamSource(camera['source'], settings)
            if not adapter.connect():
                raise RuntimeError('Standalone capture failed to connect')
            result['driver_readonly'] = {key: float(adapter.capture.get(prop)) for key, prop in (
                ('fps', cv2.CAP_PROP_FPS), ('width', cv2.CAP_PROP_FRAME_WIDTH),
                ('height', cv2.CAP_PROP_FRAME_HEIGHT), ('exposure', cv2.CAP_PROP_EXPOSURE),
                ('auto_exposure', cv2.CAP_PROP_AUTO_EXPOSURE), ('gain', cv2.CAP_PROP_GAIN),
                ('fourcc', cv2.CAP_PROP_FOURCC))}
            fourcc = int(result['driver_readonly']['fourcc'])
            result['reported_fourcc_text'] = ''.join(chr((fourcc >> (8*offset)) & 255) for offset in range(4)).rstrip('\x00')
            result['mjpeg_reported_by_driver'] = result['reported_fourcc_text'] in {'MJPG', 'JPEG'}
            # Consume the adapter's primed warmup frame before timing reads.
            adapter.read_frame()
            started = previous = time.perf_counter()
            while time.perf_counter()-started < args.seconds:
                before = time.perf_counter()
                frame = adapter.read_frame()
                returned = time.perf_counter()
                if frame is None:
                    result['errors'].append('empty_frame')
                    continue
                # A small derived hash measures duplicate delivered images;
                # neither it nor brightness is image-recognition evidence.
                tiny = cv2.resize(frame, (64, 36), interpolation=cv2.INTER_AREA)
                result['samples'].append({'at': returned-started,
                    'read_ms': (returned-before)*1000, 'return_interval_ms': (returned-previous)*1000,
                    'mean_brightness': float(tiny.mean()),
                    'frame_sha256': hashlib.sha256(frame.tobytes()).hexdigest(),
                    'thumbnail_sha256': hashlib.sha256(tiny.tobytes()).hexdigest()})
                previous = returned
            result['elapsed_seconds'] = time.perf_counter()-started
        except Exception as error:
            result['errors'].append(type(error).__name__)
        finally:
            try:
                if adapter is not None:
                    adapter.disconnect()
            except Exception as error:
                result['errors'].append('release:'+type(error).__name__)
            finally:
                if stopped:
                    restored = client.post(camera_path+'/start')
                    result['restore_http_status'] = restored.status_code
                    current = client.get(camera_path).json()
                    result['restored'] = restored.is_success and current.get('health', {}).get('capture_thread_alive') is True
                    result['online_after'] = {key: current.get('health', {}).get(key) for key in (
                        'status', 'capture_fps', 'preview_fps', 'inference_fps', 'backend', 'source_session_id')}
            result['events_before'] = before_events
            result['events_after'] = len(client.get('/api/events').json())
            frames = result['samples']
            result['read_fps'] = (len(frames)-1)/(frames[-1]['at']-frames[0]['at']) if len(frames)>1 else 0
            result['interval_ms'] = distribution([row['return_interval_ms'] for row in frames])
            result['brightness'] = distribution([row['mean_brightness'] for row in frames])
            result['distinct_thumbnail_hashes'] = len({row['thumbnail_sha256'] for row in frames})
            result['distinct_full_frame_hashes'] = len({row['frame_sha256'] for row in frames})
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key != 'samples'}, ensure_ascii=False))
    return 0 if result['restored'] and not result['errors'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
