"""Offline TEST-only measurement on reviewed public frames; no production edits.

NanoDet proposals and optical flow are real. Native OpenCV wrappers only count
and time calls; they return the exact native result. Repeated clips measure
cost, not additional accuracy trials or physical-camera/browser FPS.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import time

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.vision.detectors.appearance import normalize_category
from services.vision.detectors.nanodet import NanoDetDetectorBackend
from services.vision.person_tracking import AnonymousPersonTracker
from services.vision.preview_tracking import PreviewTracker

spec = importlib.util.spec_from_file_location('reviewed_public_assets', ROOT/'scripts/probe-public-phone-tiles.py')
assets_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assets_module)


def percentile(values, q):
    return float(np.percentile(values, q)) if values else 0.


def describe(values):
    return {'min': min(values, default=0), 'median': statistics.median(values) if values else 0,
            'p95': percentile(values, 95), 'max': max(values, default=0)}


def clip(path, second, length=21):
    capture = cv2.VideoCapture(str(path))
    try:
        assert capture.isOpened() and capture.set(cv2.CAP_PROP_POS_MSEC, second*1000)
        fps = capture.get(cv2.CAP_PROP_FPS)
        frames = []
        for _ in range(length):
            ok, frame = capture.read(); assert ok and frame is not None
            frames.append((int(capture.get(cv2.CAP_PROP_POS_FRAMES))-1, frame))
        assert fps > 0 and all(b[0] == a[0]+1 for a, b in zip(frames, frames[1:]))
        return frames, fps
    finally:
        capture.release()


def candidates(detections, frame, frame_id, timestamp, session):
    h, w = frame.shape[:2]; ordinary = []; people = []
    for detection in detections:
        x, y, bw, bh = detection.bbox
        row = {'category': detection.label, 'bbox': [x/w, y/h, bw/w, bh/h],
               'detector_score': detection.confidence, 'proposal_backend': detection.metadata['backend'],
               'accepted': False, 'item_id': None, 'best_item_id': None, 'best_score': None,
               'category_evidence': 'model', 'source_frame': frame_id, 'source_session_id': session}
        if detection.label == 'person':
            people.append(row)
        else:
            ordinary.append(row)
    tracker = AnonymousPersonTracker()
    people = tracker.update(sorted(people, key=lambda row: -row['detector_score'])[:2], session, frame_id, timestamp)
    for row in people:
        row.update(entity_type='person', visual_only=True, observation_evidence=False)
    return ordinary+people


def relevant(row, registered=('phone',)):
    return row.get('entity_type') == 'person' or normalize_category(row.get('category')) in {'phone', *registered}


def run_tracker(frames, fps, rows, session):
    tracker = PreviewTracker(); original_lk = cv2.calcOpticalFlowPyrLK; original_corners = cv2.goodFeaturesToTrack
    measurements = []; counters = {}

    def measured_lk(*args, **kwargs):
        started = time.perf_counter(); result = original_lk(*args, **kwargs)
        counters['lk_ms'] += (time.perf_counter()-started)*1000
        counters['lk_calls'] += 1; counters['lk_point_inputs'] += len(args[2])
        return result

    def measured_corners(*args, **kwargs):
        started = time.perf_counter(); result = original_corners(*args, **kwargs)
        counters['corner_ms'] += (time.perf_counter()-started)*1000
        counters['corner_calls'] += 1; counters['seed_features'] += 0 if result is None else len(result)
        return result

    cv2.calcOpticalFlowPyrLK, cv2.goodFeaturesToTrack = measured_lk, measured_corners
    try:
        started = time.perf_counter()
        tracker.offer(frames[0][1], rows, session, frames[0][0], frames[0][0]/fps, profile_generation=1)
        offer_ms = (time.perf_counter()-started)*1000
        for frame_id, frame in frames[1:]:
            counters = dict(lk_ms=0., lk_calls=0, lk_point_inputs=0, corner_ms=0., corner_calls=0, seed_features=0)
            started = time.perf_counter()
            output = tracker.update(frame, session, frame_id, frame_id/fps)
            elapsed = (time.perf_counter()-started)*1000
            measurements.append({'frame': frame_id, 'total_ms': elapsed, **counters,
                'live_tracks': len(tracker._tracks), 'live_features': sum(len(track['points']) for track in tracker._tracks),
                'output': [{'category': row['category'], 'entity_type': row.get('entity_type'), 'bbox': row['bbox'],
                            'visual_only': row['visual_only'], 'observation_evidence': row['observation_evidence']} for row in output]})
        return {'offer_ms': offer_ms, 'measurements': measurements}
    finally:
        cv2.calcOpticalFlowPyrLK, cv2.goodFeaturesToTrack = original_lk, original_corners


def raw_lk_comparison(frames, fps, rows, session):
    # Compute real point state at frame1, then compare two ways of transporting
    # exactly those same points into frame2. No tracker implementation changed.
    tracker = PreviewTracker()
    tracker.offer(frames[0][1], rows, session, frames[0][0], frames[0][0]/fps, profile_generation=1)
    tracker.update(frames[1][1], session, frames[1][0], frames[1][0]/fps)
    groups = [track['points'].copy() for track in tracker._tracks]
    if len(groups) < 2:
        return {'measured': False, 'reason': 'fewer_than_two_actual_seeded_tracks', 'tracks': len(groups)}
    previous, current = tracker._gray.copy(), tracker._gray_image(frames[2][1])
    options = {'winSize': (21, 21), 'maxLevel': 3}

    def sequential():
        results = []
        for points in groups:
            forward, status, error = cv2.calcOpticalFlowPyrLK(previous, current, points, None, **options)
            backward, back_status, _ = cv2.calcOpticalFlowPyrLK(current, previous, forward, None, **options)
            results.append((forward, status, error, backward, back_status))
        return tuple(np.concatenate([row[i] for row in results]) for i in range(5))

    points = np.concatenate(groups)
    def batched():
        forward, status, error = cv2.calcOpticalFlowPyrLK(previous, current, points, None, **options)
        backward, back_status, _ = cv2.calcOpticalFlowPyrLK(current, previous, forward, None, **options)
        return forward, status, error, backward, back_status

    seq, batch = sequential(), batched()
    valid = (seq[1].ravel() == 1) & (batch[1].ravel() == 1)
    max_delta = float(np.max(np.abs(seq[0][valid]-batch[0][valid]))) if valid.any() else None
    backward_valid = valid & (seq[4].ravel() == 1) & (batch[4].ravel() == 1)
    backward_delta = float(np.max(np.abs(seq[3][backward_valid]-batch[3][backward_valid]))) if backward_valid.any() else None
    error_delta = float(np.max(np.abs(seq[2][valid]-batch[2][valid]))) if valid.any() else None
    sequential_admitted = ((seq[1].ravel() == 1) & (seq[4].ravel() == 1)
        & (np.linalg.norm(points-seq[3], axis=2).ravel() <= tracker.config.forward_backward_pixels)
        & (seq[2].ravel() <= tracker.config.max_patch_error))
    batch_admitted = ((batch[1].ravel() == 1) & (batch[4].ravel() == 1)
        & (np.linalg.norm(points-batch[3], axis=2).ravel() <= tracker.config.forward_backward_pixels)
        & (batch[2].ravel() <= tracker.config.max_patch_error))
    elapsed = {'sequential': [], 'batched': []}
    for iteration in range(11):
        for name, function in ([('sequential', sequential), ('batched', batched)] if iteration % 2 == 0 else [('batched', batched), ('sequential', sequential)]):
            started = time.perf_counter(); function(); cost = (time.perf_counter()-started)*1000
            if iteration: elapsed[name].append(cost)
    return {'measured': True, 'tracks': len(groups), 'features': len(points),
            'sequential_lk_calls': 2*len(groups), 'batched_lk_calls': 2,
            'forward_status_equal': bool(np.array_equal(seq[1], batch[1])),
            'backward_status_equal': bool(np.array_equal(seq[4], batch[4])),
            'max_forward_delta_pixels_on_valid': max_delta,
            'max_backward_delta_pixels_on_valid': backward_delta,
            'max_lk_error_delta_on_valid': error_delta,
            'valid_forward_points': int(valid.sum()), 'valid_backward_points': int(backward_valid.sum()),
            'forward_backward_error_gate_mask_equal': bool(np.array_equal(sequential_admitted, batch_admitted)),
            'admitted_points': int(sequential_admitted.sum()),
            'sequential_ms': describe(elapsed['sequential']), 'batched_ms': describe(elapsed['batched'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'data/verification/core-vision/public-preview-cost.json')
    args = parser.parse_args(); output = args.output.resolve()
    if output.parent != (ROOT/'data/verification/core-vision').resolve() or output.suffix != '.json' or output.exists():
        parser.error('Use a new JSON path directly in data/verification/core-vision')
    report = {'completed': False, 'runtime_mode': 'TEST', 'source_type': 'video_file', 'is_simulated': True,
        'physical_camera': False, 'private_images_read': False, 'production_integrated': False, 'model_mock': False,
        'business_writes': False, 'started_at': datetime.now(timezone.utc).isoformat(), 'errors': [], 'windows': [],
        'limits': ['Native OpenCV call timing only; no browser/source FPS claim.', 'One actual model seed followed by 20 original video frames per window; not asynchronous model scheduling.',
                   'Five repeated paired runs measure timing, not independent recognition cases.', 'No DINO/hand/person-pose cost included; person boxes use actual NanoDet plus anonymous IoU role only.',
                   'No private empty-room case is reproduced. Concurrent service load can affect timings.'],
        'registered_categories_for_filter': ['phone'], 'detector_threshold_unchanged': .35,
        'repeated_measured_runs_per_variant': 5}
    try:
        public = assets_module.assets(); cv2.setNumThreads(2)
        detector = NanoDetDetectorBackend(ROOT/'data/models/object_detection_nanodet_2022nov.onnx', confidence=.35)
        assert detector.health()['available']; report['detector'] = detector.health()
        report['model_sha256'] = hashlib.sha256(detector.model_path.read_bytes()).hexdigest()
        for source, second in (('android', 4), ('android', 8), ('android', 20), ('lotti', 50), ('lotti', 54)):
            frames, fps = clip(public[source][0], second)
            session = 'public-file-sha256:'+public[source][1]['sha256']
            detections = detector.detect(frames[0][1])
            rows = candidates(detections, frames[0][1], frames[0][0], frames[0][0]/fps, session)
            original = deepcopy(rows); selected = [row for row in rows if relevant(row)]
            entry = {'source': source, 'source_page': public[source][1]['source_page'], 'source_session_id': session,
                'source_frame_start': frames[0][0], 'source_frame_end': frames[-1][0], 'source_fps': fps,
                'decoded_pixel_hashes': [hashlib.sha256(frame.tobytes()).hexdigest() for _, frame in frames],
                'raw_model_diagnostic_candidates': rows, 'filtered_candidate_count': len(selected),
                'filtered_categories': [row['category'] for row in selected], 'runs': {'all': [], 'filtered': []}}
            for iteration in range(6):
                for name, offered in ([('all', rows), ('filtered', selected)] if iteration % 2 == 0 else [('filtered', selected), ('all', rows)]):
                    result = run_tracker(frames, fps, offered, session)
                    if iteration: entry['runs'][name].append(result)
            assert rows == original, 'Filtering or flow mutated model diagnostics'
            entry['original_diagnostics_unchanged'] = True
            entry['summary'] = {}
            for name in ('all', 'filtered'):
                values = [measurement for run in entry['runs'][name] for measurement in run['measurements']]
                entry['summary'][name] = {'updates': len(values), 'offered_candidates': len(rows if name == 'all' else selected),
                    **{metric: describe([row[metric] for row in values]) for metric in ('total_ms', 'lk_ms', 'lk_calls', 'lk_point_inputs', 'live_tracks', 'live_features')},
                    'seed_features_per_run': [sum(row['seed_features'] for row in run['measurements']) for run in entry['runs'][name]],
                    'seed_cost_ms_per_run': [sum(row['corner_ms'] for row in run['measurements']) for run in entry['runs'][name]]}
            entry['retained_outputs_identical'] = all(
                [row for row in all_frame['output'] if relevant(row)] == selected_frame['output']
                for all_run, selected_run in zip(entry['runs']['all'], entry['runs']['filtered'])
                for all_frame, selected_frame in zip(all_run['measurements'], selected_run['measurements']))
            entry['native_batch_microbenchmark'] = raw_lk_comparison(frames, fps, rows, session)
            report['windows'].append(entry)
        report['completed'] = True
    except Exception as error:
        report['errors'].append(f'{type(error).__name__}: {error}')
    finally:
        report['ended_at'] = datetime.now(timezone.utc).isoformat()
        temporary = output.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8'); temporary.replace(output)
    print(json.dumps({'completed': report['completed'], 'errors': report['errors'], 'windows': [
        {'source': row['source'], 'frame': row['source_frame_start'], 'summary': row['summary'],
         'retained_outputs_identical': row['retained_outputs_identical'], 'batch': row['native_batch_microbenchmark']} for row in report['windows']]}, ensure_ascii=False))
    return 0 if report['completed'] else 1


if __name__ == '__main__': raise SystemExit(main())
