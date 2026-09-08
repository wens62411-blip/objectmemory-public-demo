"""Read-only, offline NanoDet/DINO versus cross-image YOLOE/DINO evaluation.

Does not open a camera, call business APIs, or write SQLite. Private images
remain local. Visual-prompt candidate scores are never identity probabilities.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import socket
import sqlite3
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def local_data_path(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to((ROOT / 'data').resolve()):
        raise ValueError('All inputs and outputs must remain inside project data')
    return resolved


def atomic_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


@contextmanager
def offline_only():
    attempts = []

    def reject(*args, **kwargs):
        attempts.append('blocked_external_connection')
        raise OSError('Network disabled for local private-image evaluation')

    with patch.object(socket.socket, 'connect', reject), patch.object(socket.socket, 'connect_ex', reject), \
            patch.object(socket, 'create_connection', reject):
        yield attempts


def read_database(database: Path, item_id: str, reference_id: str | None):
    # Do NOT use Database(): its constructor migrates and writes the database.
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        items = [dict(row) for row in connection.execute('SELECT id,name,type FROM items')]
        for item in items:
            row = connection.execute('SELECT * FROM item_recognition_profiles WHERE id=?', (item['id'],)).fetchone()
            item['appearance_profile'] = dict(row) if row else None
            if row:
                item['appearance_profile']['embeddings'] = json.loads(row['embeddings'] or '[]')
        item = next((entry for entry in items if entry['id'] == item_id), None)
        if item is None:
            raise ValueError('Requested registered item does not exist')
        refs = [dict(row) for row in connection.execute(
            'SELECT * FROM item_reference_images WHERE item_id=? AND region_confirmed=1', (item_id,))]
        if reference_id:
            refs = [row for row in refs if row['id'] == reference_id]
        if len(refs) != 1:
            raise ValueError('Specify one existing confirmed reference with --reference-id')
        reference = refs[0]
        reference['region'] = json.loads(reference['region'])
        if not reference['path'].startswith('/media/registered-items/'):
            raise ValueError('Unexpected reference-image storage path')
        reference['local_path'] = local_data_path(ROOT / 'data' / reference['path'].removeprefix('/media/'))
        # Registration stores the uploaded ORIGINAL hash, while region geometry
        # is on its EXIF-normalized display copy. Do not confuse those files.
        original_url = reference.get('original_path') or ''
        if not original_url.startswith('/media/registered-items/'):
            raise ValueError('Unexpected original reference-image storage path')
        original_path = local_data_path(ROOT / 'data' / original_url.removeprefix('/media/'))
        if digest(original_path) != reference['sha256']:
            raise ValueError('Original reference no longer matches persisted SHA-256')
        reference['display_sha256'] = digest(reference['local_path'])
        settings = {}
        for row in connection.execute('SELECT value FROM settings'):
            value = json.loads(row[0] or '{}')
            if isinstance(value, dict):
                settings.update(value)
        counts = {table: connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                  for table in ('item_current_state', 'movement_events', 'event_media')}
    return items, item, reference, settings, counts


def read_image(path: Path):
    import cv2
    import numpy as np
    image = cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        raise ValueError(f'Cannot decode local image: {path.name}')
    return image


def video_frame(path: Path, seconds: float):
    import cv2
    capture = cv2.VideoCapture(str(path))  # Existing public FILE only; never camera index/URL.
    try:
        if not capture.isOpened() or not capture.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000):
            raise ValueError('Cannot seek pinned public video')
        ok, frame = capture.read()
        if not ok:
            raise ValueError('Cannot decode pinned public video')
        return frame, int(capture.get(cv2.CAP_PROP_POS_FRAMES)) - 1
    finally:
        capture.release()


def crop(frame, region):
    from services.vision.detectors.yoloe_visual import reference_xyxy
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = (round(v) for v in reference_xyxy(region, w, h))
    if min(x2 - x1, y2 - y1) < 32:
        raise ValueError('Reference crop must be at least 32 pixels per side')
    return frame[y1:y2, x1:x2]


def annotate(path: Path, frame, rows):
    import cv2
    image = frame.copy()
    for row in rows:
        x, y, w, h = (round(v) for v in row['bbox'])
        # Orange visual candidates do not masquerade as accepted real identities.
        accepted = row['appearance_match']['accepted']
        color = (40, 165, 235) if row['category_evidence'] == 'visual_reference_prompt' else (100, 195, 80)
        cv2.rectangle(image, (x, y), (x+w, y+h), color, 2)
        label = f"{row['raw_class_name']} {row['detector_score']:.3f} DINO={'pass' if accepted else 'reject'}"
        cv2.putText(image, label, (x, max(16, y-5)), cv2.FONT_HERSHEY_SIMPLEX, .45, color, 1, cv2.LINE_AA)
    ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not ok:
        raise RuntimeError('Annotation JPEG encoding failed')
    temporary = path.with_suffix('.part')
    temporary.write_bytes(encoded.tobytes())
    temporary.replace(path)
    return {'path': str(path), 'sha256': digest(path)}


def execute(args):
    import cv2
    from services.vision.detectors.appearance import AppearanceEncoder, ProfileMatcher
    from services.vision.detectors.nanodet import NanoDetDetectorBackend
    from services.vision.detectors.yoloe_visual import YOLOEVisualProbe

    output = local_data_path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    report = {'completed': False, 'production_integrated': False, 'business_writes': 0,
              'camera_opened_by_probe': False, 'external_image_upload': False,
              'reference_self_test': False, 'model_mock': False, 'errors': [], 'scenarios': []}
    if (output / 'result.json').is_file():
        previous = json.loads((output / 'result.json').read_text(encoding='utf-8'))
        report['previous_attempts'] = previous.get('previous_attempts', [])
        report['previous_attempts'].append({'completed': previous['completed'], 'errors': previous['errors']})
    encoder = None
    try:
        import psutil
        process = psutil.Process()
        report['memory_before_bytes'] = process.memory_info().rss
        report['packages'] = {name: importlib.metadata.version(name) for name in (
            'torch', 'torchvision', 'ultralytics', 'numpy', 'opencv-contrib-python', 'onnxruntime')}
        items, item, reference, settings, counts = read_database(local_data_path(args.database), args.item_id, args.reference_id)
        report['database_counts_read_only'] = counts
        threshold = float(settings.get('reference_match_threshold', .82))
        margin = float(settings.get('reference_match_margin', .06))
        confidence = float(settings.get('confidence_threshold', .35))
        if not .35 <= confidence < 1 or not .82 <= threshold <= 1 or not .06 <= margin <= 2:
            raise ValueError('Settings below reviewed evaluation baseline; probe will not silently lower gates')
        report['thresholds'] = {'detector': confidence, 'appearance': threshold, 'margin': margin, 'changed': False}
        reference_image = read_image(reference['local_path'])
        target_path = local_data_path(args.source)
        target = read_image(target_path)
        metadata = json.loads(local_data_path(args.provenance_json).read_text(encoding='utf-8'))['local_frame_evidence']
        if metadata['raw_sha256'] != digest(target_path):
            raise ValueError('Camera source file differs from root-provided source-frame proof')
        if metadata.get('source_type') != 'opencv_camera' or metadata.get('is_simulated') is not False:
            raise ValueError('Expected explicitly tagged physical-camera source evidence')
        encoder = AppearanceEncoder()
        if not encoder.health()['available']:
            raise RuntimeError(encoder.health()['error'])
        nano = NanoDetDetectorBackend(ROOT / 'data/models/object_detection_nanodet_2022nov.onnx', confidence=confidence)
        if not nano.health()['available']:
            raise RuntimeError(nano.health()['error'])
        yolo = YOLOEVisualProbe(local_data_path(args.weight), confidence=confidence)
        report['nanodet'] = {**nano.health(), 'sha256': digest(nano.model_path)}
        report['dino'] = encoder.health()
        cases = [{'name': 'user_reference_independent_camera_unknown', 'items': items, 'item': item,
                  'reference_id': reference['id'], 'reference': reference_image, 'region': reference['region'],
                  'reference_path': str(reference['local_path']), 'reference_file_sha256': reference['display_sha256'],
                  'original_upload_sha256': reference['sha256'],
                  'reference_profile_version': item['appearance_profile']['profile_version'],
                  'expected_identity': 'unknown_target_not_human_verified', 'runtime_mode': 'REAL',
                  'source_type': 'opencv_camera', 'is_simulated': False,
                  'sources': [(target, {key: metadata[key] for key in ('source_frame', 'source_session_id', 'source_timestamp')})]}]
        if args.include_public:
            public_meta = json.loads((ROOT / 'scripts/test-assets/public-phone-lotti.source.json').read_text(encoding='utf-8'))
            video = local_data_path(ROOT / public_meta['local_file'])
            if digest(video) != public_meta['sha256']:
                raise ValueError('Public video differs from pinned independent source')
            public_ref, reference_index = video_frame(video, public_meta['reference_seconds'])
            region = public_meta['reference_region_xywh_normalized']
            public_item = {'id': 'evaluation-public-phone', 'name': 'Public HTC phone', 'type': 'phone',
                           'appearance_profile': {'status': 'ready', 'profile_version': 1,
                            'model_id': encoder.model_id, 'model_version': encoder.model_version,
                            'dimension': encoder.dimension, 'embeddings': [encoder.encode(crop(public_ref, region))]}}
            public_sources = []
            for second in (41, 45, 50, 54):
                frame, index = video_frame(video, second)
                if second - public_meta['reference_seconds'] < 20 or index <= reference_index:
                    raise ValueError('Temporal holdout is not independent of reference sampling')
                public_sources.append((frame, {'source_frame': index, 'source_timestamp_seconds': second,
                                               'source_session_id': 'public-file-sha256:' + public_meta['sha256']}))
            common = {'runtime_mode': 'TEST', 'source_type': 'video_file', 'is_simulated': True,
                      'sources': public_sources, 'public_source': public_meta,
                      'reference_profile_version': 1}
            cases.append({**common, 'name': 'public_reference_temporal_holdout', 'items': [public_item], 'item': public_item,
                          'reference_id': 'public-20-second-reference', 'reference': public_ref, 'region': region,
                          'expected_identity': 'same_public_phone_temporal_holdout_not_user_phone',
                          'reference_frame': reference_index})
            cases.append({**common, 'name': 'user_reference_other_public_phone', 'items': items, 'item': item,
                          'reference_id': reference['id'], 'reference': reference_image, 'region': reference['region'],
                          'reference_profile_version': item['appearance_profile']['profile_version'],
                          'expected_identity': 'different_phone_must_not_be_user_identity'})
        for case in cases:
            ref_pixels = hashlib.sha256(case['reference'].tobytes()).hexdigest()
            crop_pixels = hashlib.sha256(crop(case['reference'], case['region']).tobytes()).hexdigest()
            case_report = {key: value for key, value in case.items() if key not in ('items', 'item', 'reference', 'sources')}
            case_report.update(item_id=case['item']['id'], reference_pixels_sha256=ref_pixels, samples=[])
            report['scenarios'].append(case_report)
            matcher = ProfileMatcher(case['items'], encoder, threshold=threshold, margin=margin)
            case_report['loaded_profile_versions'] = matcher.loaded_profile_versions
            if case['item']['id'] not in matcher.loaded_profile_versions:
                raise RuntimeError(f"Persisted requested profile rejected: {matcher.invalid_profiles}")
            yolo.set_reference(case['reference'], case['region'], reference_id=case['reference_id'], prompt_item_id=case['item']['id'])
            for index, (frame, provenance) in enumerate(case['sources']):
                pixel_hash = hashlib.sha256(frame.tobytes()).hexdigest()
                if pixel_hash in (ref_pixels, crop_pixels):
                    raise ValueError('Independent source duplicates reference pixels')
                sample = {**provenance, 'pixels_sha256': pixel_hash,
                          'source_dimensions': [frame.shape[1], frame.shape[0]], 'backends': {}}
                case_report['samples'].append(sample)
                for backend in ('nanodet', 'yoloe_visual'):
                    started = time.perf_counter()
                    if backend == 'nanodet':
                        rows = [{'raw_class_id': det.raw_id, 'raw_class_name': det.label,
                                 'category': det.label, 'category_evidence': 'semantic_model',
                                 'bbox': list(det.bbox), 'bbox_format': 'source_pixel_xywh',
                                 'detector_score': det.confidence, 'item_id': None,
                                 'proposal_backend': nano.name} for det in nano.detect(frame)]
                    else:
                        rows = yolo.detect(frame)
                    detection_ms = (time.perf_counter() - started) * 1000
                    for row in rows:
                        x, y, w, h = row['bbox']
                        x1, y1 = max(0, round(x)), max(0, round(y))
                        x2, y2 = min(frame.shape[1], round(x+w)), min(frame.shape[0], round(y+h))
                        if min(x2-x1, y2-y1) < 32:
                            matched = {'accepted': False, 'item_id': None, 'rejection': 'candidate_crop_too_small'}
                        else:
                            # A visual prompt has no independent semantic class. Keep
                            # that limitation visible; do NOT invent phone model.names.
                            matched = matcher.match(frame[y1:y2, x1:x2], category=row['category'])
                        row['appearance_match'] = matched
                        row['identity_for_production'] = None
                        row['identity_gate_scope'] = 'single_frame_similarity_only_not_continuous_confirmation'
                    sample['backends'][backend] = {'candidates': rows, 'detection_ms': round(detection_ms, 3),
                        'total_ms': round((time.perf_counter()-started)*1000, 3),
                        'annotation': annotate(output / f"{case['name']}-{index}-{backend}.jpg", frame, rows)}
            case_report['summary'] = {name: {'candidate_count': sum(len(s['backends'][name]['candidates']) for s in case_report['samples']),
                'appearance_accepts': sum(bool(r['appearance_match']['accepted']) for s in case_report['samples'] for r in s['backends'][name]['candidates'])}
                for name in ('nanodet', 'yoloe_visual')}
        report['yoloe'] = yolo.health()
        report['dino'] = encoder.health()
        report['memory_after_bytes'] = process.memory_info().rss
        report['completed'] = True
    except Exception as exc:
        report['errors'].append(f'{type(exc).__name__}: {exc}')
    finally:
        if encoder is not None:
            encoder.close()
        atomic_json(output / 'result.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=ROOT / 'data/database/objectmemory.sqlite')
    parser.add_argument('--item-id', required=True)
    parser.add_argument('--reference-id')
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--provenance-json', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'data/model-evaluation/results')
    parser.add_argument('--weight', type=Path, default=ROOT / 'data/model-evaluation/weights/yoloe-11s-seg.pt')
    parser.add_argument('--include-public', action='store_true')
    args = parser.parse_args()
    target = local_data_path(ROOT / 'data/model-evaluation/python-packages')
    sys.path.insert(0, str(target))
    os.environ.update(YOLO_AUTOINSTALL='false', YOLO_OFFLINE='true', YOLO_VERBOSE='false',
                      YOLO_CONFIG_DIR=str(ROOT / 'data/model-evaluation/settings'),
                      MPLCONFIGDIR=str(ROOT / 'data/model-evaluation/matplotlib'),
                      PYTHONDONTWRITEBYTECODE='1', OMP_NUM_THREADS='2', MKL_NUM_THREADS='2')
    with offline_only() as attempts:
        report = execute(args)
    report['network_attempts_blocked'] = len(attempts)
    report['offline_socket_guard'] = True
    atomic_json(local_data_path(args.output) / 'result.json', report)
    dependencies = []
    for distribution in importlib.metadata.distributions(path=[str(target)]):
        entry = {'name': distribution.metadata['Name'], 'version': distribution.version}
        direct_url = distribution.read_text('direct_url.json')
        if direct_url:
            entry['official_download'] = json.loads(direct_url)
        entry['installed_bytes'] = sum(distribution.locate_file(f).stat().st_size
                                      for f in (distribution.files or []) if distribution.locate_file(f).is_file())
        dependencies.append(entry)
    from services.vision.detectors.yoloe_visual import WEIGHT_URL, WEIGHT_SHA256, WEIGHT_BYTES
    atomic_json(ROOT / 'data/model-evaluation/manifest.json', {
        'purpose': 'isolated local model evaluation, not live integration',
        'dependencies': dependencies, 'package_target': str(target),
        'target_tree_bytes': sum(f.stat().st_size for f in target.rglob('*') if f.is_file()),
        'weight': {'url': WEIGHT_URL, 'sha256': WEIGHT_SHA256, 'bytes': WEIGHT_BYTES},
        'license_sources': ['https://github.com/ultralytics/ultralytics/blob/v8.3.235/LICENSE',
                            'https://huggingface.co/jameslahm/yoloe-11s-seg/blob/main/README.md',
                            'https://www.ultralytics.com/license'],
        'api_source': 'https://github.com/ultralytics/ultralytics/blob/v8.3.235/ultralytics/models/yolo/model.py',
        'base_environment_changed': False, 'existing_runtime_versions': report.get('packages'),
        'cuda_installed': False, 'private_images_uploaded': False,
        'disposal': 'Remove only this isolated target after model choice; never registered-items or existing models',
        'result': str(local_data_path(args.output) / 'result.json')})
    print(json.dumps({'completed': report['completed'], 'errors': report['errors'],
                      'scenarios': [{'name': row['name'], 'summary': row.get('summary')} for row in report['scenarios']]}, ensure_ascii=False))
    return 0 if report['completed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
