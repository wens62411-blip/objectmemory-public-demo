"""Offline diagnostic of the SAME VisionEngine on a reference and captured frame.

Never opens a camera, starts a worker, mutates SQLite, downloads a model, or
substitutes a detector. A reference self-check is not recognition acceptance;
replaying one captured frame is not live tracking or pickup/placement evidence.
All image evidence stays under ignored data/verification.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def confined(path, boundary, *, file=True):
    value = Path(path).resolve()
    if value == boundary.resolve() or not value.is_relative_to(boundary.resolve()):
        raise ValueError('Diagnostic path is outside the permitted local data directory')
    if file and not value.is_file():
        raise ValueError('Required local diagnostic input is missing')
    return value


def atomic_bytes(path, content):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.diagnostic-', suffix='.tmp', delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, value):
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8'))


def load_reader():
    spec = importlib.util.spec_from_file_location('readonly_frame_registration', ROOT/'scripts/verify-physical-phone-frame.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_image(path):
    import cv2
    import numpy as np
    if not 0 < path.stat().st_size <= 15*1024*1024:
        raise ValueError('Image exceeds the bounded diagnostic byte limit')
    value = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    if value is None or value.shape[0]*value.shape[1] > 16_777_216:
        raise ValueError('Not a bounded decodable input image')
    return value


def validate_source(frame_path, metadata):
    if metadata.get('raw_sha256') != sha256(frame_path):
        raise ValueError('Captured JPEG does not match its source provenance hash')
    physical = (metadata.get('runtime_mode') == 'REAL' and metadata.get('is_simulated') is False
                and metadata.get('source_type') in {'opencv_camera','browser_camera','esp32_real'})
    video = (metadata.get('runtime_mode') == 'TEST' and metadata.get('is_simulated') is True
             and metadata.get('source_type') == 'video_file')
    if not (physical or video):
        raise ValueError('Expected physical provenance or explicitly isolated TEST video provenance')
    if (not isinstance(metadata.get('source_session_id'), str) or not metadata['source_session_id']
            or type(metadata.get('source_frame')) is not int or metadata['source_frame'] < 1):
        raise ValueError('Captured source session/frame identifiers are missing')
    if video:
        seconds = metadata.get('source_timestamp_seconds')
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError('Video relative timestamp is missing')
        return float(seconds)  # TEST-only clock; never a camera wall-clock claim.
    timestamp = datetime.fromisoformat(metadata['source_timestamp'])
    if timestamp.tzinfo is None:
        raise ValueError('Captured source timestamp must have timezone information')
    return timestamp.timestamp()


def capture_provenance(document, source_path=None, manifest_hash=None):
    if isinstance(document, list):
        selected = [row for row in document if isinstance(row, dict)
                    and Path(row.get('path') or '').resolve() == source_path]
        if len(selected) != 1 or not manifest_hash:
            raise ValueError('Frame must have one hash-bound entry in the extraction manifest')
        row = selected[0]
        return {**row, 'raw_sha256': row.get('sha256'), 'source_frame': row.get('frame_index'),
                'source_session_id': 'test-file-extraction:'+manifest_hash,
                'source_timestamp': None, 'source_timestamp_seconds': row.get('seconds')}
    if document.get('contract_version') == 1 and isinstance(document.get('positive'), dict):
        # Existing pinned physical-frame cases are source records, not proof
        # that this replay or current registration will identify the object.
        return {**document['positive'], 'raw_sha256': document['positive'].get('sha256')}
    return document


def annotation(path, frame, rows):
    import cv2
    image = frame.copy()
    height, width = image.shape[:2]
    for row in rows:
        x, y, w, h = row['bbox']
        x, y, w, h = round(x*width), round(y*height), round(w*width), round(h*height)
        color = (60, 180, 75) if row.get('accepted') is True else (40, 165, 235)
        cv2.rectangle(image, (x, y), (x+w, y+h), color, 2)
        reason = row.get('rejection_reason') or row.get('rejection') or ('identity accepted' if row.get('accepted') else 'raw category only')
        label = f"{row.get('category','object')}: {reason}"
        cv2.putText(image, label[:110], (max(0,x), max(16,y-6)), cv2.FONT_HERSHEY_SIMPLEX, .43, color, 1, cv2.LINE_AA)
    ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise RuntimeError('Diagnostic annotation JPEG encoding failed')
    atomic_bytes(path, encoded)
    return {'path': str(path), 'sha256': sha256(path)}


@contextmanager
def no_capture_or_network():
    import cv2
    def forbidden(*_args, **_kwargs):
        raise RuntimeError('Offline diagnostic attempted to open a camera or network connection')
    with patch.object(cv2, 'VideoCapture', forbidden), patch.object(socket.socket, 'connect', forbidden), \
            patch.object(socket.socket, 'connect_ex', forbidden), patch.object(socket, 'create_connection', forbidden):
        yield


def run(args):
    from apps.api.app.schemas import SettingsInput
    from services.vision.capture import FramePacket
    from services.vision.engine import VisionEngine
    from services.vision.detectors.appearance import AppearanceEncoder

    boundary = ROOT/'data/verification'
    source_path = confined(args.source, boundary)
    metadata_path = confined(args.provenance_json, boundary)
    output = confined(args.output, boundary, file=False)
    output.mkdir(parents=True, exist_ok=True)
    metadata = capture_provenance(json.loads(metadata_path.read_text(encoding='utf-8')), source_path, sha256(metadata_path))
    source_time = validate_source(source_path, metadata)
    reader = load_reader()
    items, saved_settings, protected = reader.read_registration(ROOT, args.item_id)
    reader.protect_detection_inputs([source_path], protected)
    item = next(row for row in items if row['id'] == args.item_id)
    references = [row for row in item.get('reference_images', []) if row.get('region_confirmed')]
    if args.reference_id:
        references = [row for row in references if row['id'] == args.reference_id]
    if len(references) != 1:
        raise ValueError('Select one confirmed reference with --reference-id')
    reference = references[0]
    reference_path = confined(ROOT/'data'/reference['path'].removeprefix('/media/'), ROOT/'data/registered-items')
    settings = {**SettingsInput().model_dump(), **saved_settings}
    reader.current_nanodet_path(ROOT, settings)
    frames = [('reference-self-check', reference_path, read_image(reference_path), 'reference-self-check', 1, source_time),
              ('independent-capture', source_path, read_image(source_path), metadata['source_session_id'], metadata['source_frame'], source_time)]
    if hashlib.sha256(frames[0][2].tobytes()).digest() == hashlib.sha256(frames[1][2].tobytes()).digest():
        raise ValueError('Independent input duplicates reference image pixels')
    report = {'completed': False, 'pipeline': 'VisionEngine._process, unchanged installed models and gates',
        'mode': 'TEST_OFFLINE_REPLAY', 'camera_opened': False, 'database_writes': 0, 'network_access': False,
        'identity_human_verified': False, 'movement_acceptance': False,
        'source_label': args.source_label, 'reference_self_check_is_acceptance': False,
        'original_capture_provenance': {key: metadata.get(key) for key in ('runtime_mode','source_type','is_simulated',
            'source_session_id','source_frame','source_timestamp','source_timestamp_seconds','raw_sha256')},
        'thresholds_changed': False, 'settings': {key: settings.get(key) for key in ('detection_mode',
            'experimental_confidence','reference_match_threshold','reference_match_margin',
            'reference_patch_enabled','reference_patch','identity_min_crop_pixels','hand_detection_enabled')},
        'cases': [], 'errors': []}
    encoder = AppearanceEncoder()
    engine = None
    observations, events = [], []
    try:
        if not encoder.health().get('available'):
            raise RuntimeError(encoder.health().get('error') or 'Installed appearance model unavailable')
        with tempfile.TemporaryDirectory(prefix='engine-replay-', dir=output) as temporary, no_capture_or_network():
            engine = VisionEngine({'id': 'offline-recognition-diagnostic', 'source_type': 'video',
                'source': str(Path(temporary)/'never-opened.avi'), 'enabled': False}, items, [],
                {**settings, 'runtime_mode': 'TEST', 'diagnostic_mode': True,
                 'media_root': temporary, 'record_events': False, 'save_clips': False},
                on_event=lambda *values: events.append(values), on_track=lambda row: observations.append(row),
                appearance_encoder=encoder)
            if not engine.experimental or not engine.experimental.health().get('available'):
                raise RuntimeError('Configured NanoDet model is unavailable; no substitution permitted')
            report['models'] = {'object': {**engine.experimental.health(), 'sha256': sha256(engine.experimental.model_path)},
                'identity': encoder.health(), 'hands': engine.hands.health(),
                'reference_patches': engine.reference_patches.health() if engine.reference_patches else None}
            try:
                for name, path, frame, session, sequence, wall_time in frames:
                    before_callbacks = len(observations)
                    started = time.perf_counter()
                    engine._process(FramePacket(frame, time.monotonic(), wall_time, sequence, session, 0))
                    snapshot = engine.recognition_snapshot()
                    if not snapshot:
                        raise RuntimeError('No fresh same-frame result was published')
                    facts = snapshot['pipeline_diagnostics']
                    candidates = snapshot['candidates']
                    identities = [row for row in candidates if row.get('accepted') is True]
                    objects = [row for row in candidates if row.get('category') != 'person']
                    reason = ('identity_accepted_single_frame_only' if identities else
                              'identity_gate_rejected_candidates' if objects else
                              'no_raw_object_or_registered_patch_candidate' if facts['raw_object_count'] == 0 else
                              'raw_objects_not_admitted')
                    report['cases'].append({'name': name, 'input': {'path': str(path), 'sha256': sha256(path)},
                        'source_session_id': session, 'source_frame': sequence,
                        'elapsed_ms': round((time.perf_counter()-started)*1000, 3), 'result_reason': reason,
                        'pipeline_diagnostics': facts, 'loaded_profiles': snapshot['loaded_profiles'],
                        'candidates': candidates, 'hand_count': snapshot['hand_count'],
                        'observation_callback_count': len(observations)-before_callbacks,
                        'verified_observation_count': len(engine.observation_gate.verified),
                        'artifacts': {'source': annotation(output/f'{name}-source.jpg', frame, []),
                            'raw_model': annotation(output/f'{name}-raw-model.jpg', frame, facts['raw_detections']),
                            'candidates': annotation(output/f'{name}-candidates.jpg', frame, candidates),
                            'identities': annotation(output/f'{name}-identities.jpg', frame, identities)}})
                assert engine.capture.thread is None and engine._thread is None
                assert not events, 'Diagnostic unexpectedly emitted an event'
            finally:
                engine.stop()
                engine = None
        if any(sha256(path) != digest for path, digest in protected.items()):
            raise RuntimeError('Protected source/reference file changed during diagnostic')
        report['protected_inputs_unchanged'] = True
        report['completed'] = True
    except Exception as exc:
        report['errors'].append(f'{type(exc).__name__}: {exc}')
    finally:
        if engine is not None: engine.stop()
        encoder.close()
        write_json(output/'result.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--item-id', required=True)
    parser.add_argument('--reference-id')
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--provenance-json', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-label', choices=['unreviewed','no_target_visible','screen_recording_negative',
        'target_visible_identity_unverified'], default='unreviewed')
    report = run(parser.parse_args())
    print(json.dumps({'completed': report['completed'], 'errors': report['errors'],
        'cases': [{'name': row['name'], 'reason': row['result_reason'], 'facts': row['pipeline_diagnostics']}
                  for row in report['cases']]}, ensure_ascii=False))
    return 0 if report['completed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
