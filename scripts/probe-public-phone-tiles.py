"""Bounded offline TEST experiment. Never imported by the production engine.

Compare original frames against fixed 2x2 overlapping NanoDet crops. All boxes
are actual model outputs mapped into original pixels and class-aware NMS. DINO
uses photographed public references, not generated images or user photographs.
Only the JSON report is written; no database, video, image or model is changed.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.vision.detectors.appearance import AppearanceEncoder, ProfileMatcher
from services.vision.detectors.nanodet import NanoDetDetectorBackend

ANDROID_ROIS = {'android-left': (60, 35, 209, 430), 'android-center': (318, 6, 222, 460),
                'android-right': (587, 33, 214, 432)}
DETECTOR_THRESHOLD, IDENTITY_THRESHOLD, IDENTITY_MARGIN, NMS_IOU = .35, .82, .06, .6


def iou(a, b):
    ax, ay, aw, ah = a; bx, by, bw, bh = b
    overlap = max(0, min(ax+aw, bx+bw)-max(ax, bx))*max(0, min(ay+ah, by+bh)-max(ay, by))
    return overlap/max(1, aw*ah+bw*bh-overlap)


def nms(rows):
    kept = []
    for row in sorted(rows, key=lambda value: -value['detector_score']):
        if all(row['category'] != other['category'] or iou(row['bbox'], other['bbox']) <= NMS_IOU for other in kept):
            kept.append(row)
    return kept


def assets():
    result = {}
    for name in ('android', 'lotti'):
        metadata = json.loads((ROOT/f'scripts/test-assets/public-phone-{name}.source.json').read_text(encoding='utf-8'))
        path = (ROOT/metadata['local_file']).resolve(strict=True)
        if path.parent != (ROOT/'data/test-assets').resolve():
            raise ValueError('Only existing reviewed public test assets are allowed')
        if hashlib.sha256(path.read_bytes()).hexdigest() != metadata['sha256']:
            raise ValueError('Public asset hash mismatch')
        result[name] = (path, metadata)
    return result


def read_frame(path, second):
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened() or not capture.set(cv2.CAP_PROP_POS_MSEC, second*1000):
            raise ValueError('Public video could not be opened or sought')
        ok, frame = capture.read()
        if not ok or frame is None:
            raise ValueError('Public frame could not be decoded')
        return frame, int(capture.get(cv2.CAP_PROP_POS_FRAMES))-1
    finally:
        capture.release()


def tiles(width, height):
    # Fixed .75-length tiles give 50% full-image overlap along both axes.
    # These settings are not tuned on the detector result.
    tw, th = round(width*.75), round(height*.75)
    return [(x, y, tw, th) for y in (0, height-th) for x in (0, width-tw)]


def predictions(detector, frame, windows, label):
    started = time.perf_counter(); rows = []
    height, width = frame.shape[:2]
    for index, (tx, ty, tw, th) in enumerate(windows):
        for detection in detector.detect(frame[ty:ty+th, tx:tx+tw]):
            x, y, w, h = detection.bbox
            box = [x+tx, y+ty, w, h]
            assert 0 <= box[0] < box[0]+w <= width and 0 <= box[1] < box[1]+h <= height
            rows.append({'category': detection.label, 'detector_score': detection.confidence,
                         'bbox': box, 'origin': label, 'tile_index': index, 'tile_xywh': [tx, ty, tw, th]})
    return rows, (time.perf_counter()-started)*1000


def assess(rows, frame, matchers, positive):
    started = time.perf_counter(); evaluated = []
    for original in rows:
        row = dict(original); x, y, w, h = row['bbox']
        row['matches'] = {}
        for name, matcher in matchers.items():
            row['matches'][name] = matcher.match(frame[y:y+h, x:x+w], category=row['category']) if min(w, h) >= 32 else {'accepted': False, 'rejection': 'insufficient_detail', 'item_id': None}
        if positive:
            overlap, expected = max((iou(row['bbox'], roi), item) for item, roi in ANDROID_ROIS.items())
            row['reviewed_roi_iou'], row['expected_item_by_reviewed_roi'] = overlap, expected if overlap >= .5 else None
        else:
            row['expected_item_by_reviewed_roi'] = None
        evaluated.append(row)
    phone_rows = [row for row in evaluated if row['category'] == 'cell phone']
    identity = {}
    for name in matchers:
        accepted = [row for row in evaluated if row['matches'][name]['accepted']]
        incorrect = [row for row in accepted if not positive or (row.get('expected_item_by_reviewed_roi') is not None
                     and row['matches'][name]['item_id'] != row['expected_item_by_reviewed_roi'])]
        unlocalized = [row for row in accepted if positive and row.get('expected_item_by_reviewed_roi') is None]
        identity[name] = {'accepted': len(accepted), 'incorrect_identity': len(incorrect),
            'accepted_without_labelled_roi_match': len(unlocalized),
            'ambiguous': sum(row['matches'][name].get('rejection') == 'ambiguous_identity' for row in evaluated),
            'below_threshold': sum(row['matches'][name].get('rejection') == 'below_threshold' for row in evaluated)}
    return {'candidates': evaluated, 'candidate_count': len(evaluated), 'phone_candidates': len(phone_rows),
            'phone_rois_detected_iou_05': [item for item, roi in ANDROID_ROIS.items() if any(iou(row['bbox'], roi) >= .5 for row in phone_rows)] if positive else None,
            'identity': identity, 'identity_ms': (time.perf_counter()-started)*1000}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'data/verification/core-vision/public-phone-tiles.json')
    args = parser.parse_args()
    output = args.output.resolve()
    if output.parent != (ROOT/'data/verification/core-vision').resolve() or output.suffix != '.json':
        parser.error('Report must be a JSON directly in data/verification/core-vision')
    if output.exists():
        parser.error('Refusing to overwrite an existing experiment report')
    report = {'completed': False, 'runtime_mode': 'TEST', 'source_type': 'video_file', 'is_simulated': True,
              'physical_camera': False, 'user_phone_acceptance': False, 'production_integrated': False,
              'business_writes': False, 'external_image_upload': False, 'model_mock': False,
              'started_at': datetime.now(timezone.utc).isoformat(), 'errors': [], 'samples': [],
              'thresholds': {'detector': DETECTOR_THRESHOLD, 'identity': IDENTITY_THRESHOLD, 'margin': IDENTITY_MARGIN, 'nms_iou': NMS_IOU, 'lowered': False},
              'limits': ['Five frames of the same three stationary public phones, not five independent physical trials.',
                         'Android phone ROIs occupy most image height; this is NOT a small-phone benchmark.',
                         'Tiles can cut phones. Per-frame ROI coverage is reported; no missing parts are synthesized.',
                         'No registered-photo self-test. Reference 1s is excluded from held-out 4/8/12/16/20s.',
                         'Concurrent local service load may affect timings. No production setting is changed.']}
    encoder = None
    try:
        public = assets(); cv2.setNumThreads(2)
        detector = NanoDetDetectorBackend(ROOT/'data/models/object_detection_nanodet_2022nov.onnx', confidence=DETECTOR_THRESHOLD, iou_threshold=NMS_IOU)
        assert detector.health()['available'], detector.health()
        encoder = AppearanceEncoder(); assert encoder.health()['available'], encoder.health()
        reference, reference_index = read_frame(public['android'][0], 1)
        assert reference.shape[:2] == (480, 854)
        reference_hash = hashlib.sha256(reference.tobytes()).hexdigest()
        items = []
        for item, (x, y, w, h) in ANDROID_ROIS.items():
            items.append({'id': item, 'type': 'phone', 'appearance_profile': {'status': 'ready', 'profile_version': 1,
                'model_id': encoder.model_id, 'model_version': encoder.model_version, 'dimension': encoder.dimension,
                'embeddings': [encoder.encode(reference[y:y+h, x:x+w])]}})
        matchers = {'center_only': ProfileMatcher([items[1]], encoder, IDENTITY_THRESHOLD, IDENTITY_MARGIN),
                    'three_registered_phones': ProfileMatcher(items, encoder, IDENTITY_THRESHOLD, IDENTITY_MARGIN)}
        report.update(assets={key: value[1] for key, value in public.items()}, detector=detector.health(), appearance=encoder.health(),
            reference={'frame': reference_index, 'seconds': 1, 'pixels_sha256': reference_hash, 'rois': ANDROID_ROIS,
                       'roi_basis': 'Existing reviewed public phone ROIs in services/vision/tests/test_appearance_public_phone.py'})
        # Actual model warm-up on the photographed reference, never counted as a test.
        detector.detect(reference)
        for source, seconds in (('android', (4, 8, 12, 16, 20)), ('lotti', (41, 45, 50))):
            for second in seconds:
                frame, frame_index = read_frame(public[source][0], second)
                pixel_hash = hashlib.sha256(frame.tobytes()).hexdigest(); assert pixel_hash != reference_hash
                height, width = frame.shape[:2]; windows = tiles(width, height)
                sample = {'source': source, 'source_sha256': public[source][1]['sha256'], 'source_frame': frame_index,
                    'source_timestamp_seconds': second, 'pixels_sha256': pixel_hash, 'dimensions': [width, height],
                    'identity_expectation': 'three_reviewed_android_phone_rois' if source == 'android' else 'different_public_htc_phone_and_background_must_not_match_android_identities',
                    'tile_windows': windows, 'variants': {}}
                if source == 'android':
                    sample['roi_max_tile_coverage'] = {key: max(max(0, min(x+w, tx+tw)-max(x, tx))*max(0, min(y+h, ty+th)-max(y, ty))/(w*h)
                        for tx, ty, tw, th in windows) for key, (x, y, w, h) in ANDROID_ROIS.items()}
                full, full_ms = predictions(detector, frame, [(0, 0, width, height)], 'full_frame')
                tiled, tiled_ms = predictions(detector, frame, windows, 'tile')
                for name, rows, detector_ms in (('full_frame', full, full_ms), ('tiles_only', tiled, tiled_ms), ('full_plus_tiles', [*full, *tiled], full_ms+tiled_ms)):
                    details = assess(nms(rows), frame, matchers, source == 'android')
                    details.update(raw_candidate_count=len(rows), detector_ms=detector_ms,
                                   total_ms=detector_ms+details['identity_ms'])
                    sample['variants'][name] = details
                report['samples'].append(sample)
        report['summary'] = {}
        for source in ('android', 'lotti'):
            selected = [sample for sample in report['samples'] if sample['source'] == source]
            report['summary'][source] = {name: {'frames': len(selected),
                'phone_candidates': sum(sample['variants'][name]['phone_candidates'] for sample in selected),
                'labelled_phone_instances_detected': sum(len(sample['variants'][name]['phone_rois_detected_iou_05'] or []) for sample in selected) if source == 'android' else None,
                'median_total_ms': statistics.median(sample['variants'][name]['total_ms'] for sample in selected),
                'identity': {matcher: {metric: sum(sample['variants'][name]['identity'][matcher][metric] for sample in selected)
                            for metric in ('accepted', 'incorrect_identity', 'accepted_without_labelled_roi_match', 'ambiguous', 'below_threshold')}
                            for matcher in matchers}} for name in ('full_frame', 'tiles_only', 'full_plus_tiles')}
        report['completed'] = True
    except Exception as error:
        report['errors'].append(f'{type(error).__name__}: {error}')
    finally:
        if encoder is not None: encoder.close()
        report['ended_at'] = datetime.now(timezone.utc).isoformat()
        temporary = output.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(output)
    print(json.dumps({'completed': report['completed'], 'errors': report['errors'], 'summary': report.get('summary')}, ensure_ascii=False))
    return 0 if report['completed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
