"""Bounded local runtime probe; optional explicitly requested local frame evidence."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import base64
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

import httpx


def summarize_candidates(candidates):
    """Count neural output separately from shape labels and unknown provenance.

    PhoneShapeProposer also emits `cell phone` labels, even for window panes.
    Those labels cannot establish a phone detection or an accepted identity.
    """
    result = {key: Counter() for key in ('categories','reference_categories','geometry_categories','unverified_categories','all_candidate_labels','accepted')}
    result.update(model_candidates=0,geometry_only_candidates=0,unverified_candidates=0,invalid_identity_claims=0)
    for candidate in candidates:
        category = candidate.get('category') or 'unknown'
        result['all_candidate_labels'][category] += 1
        shape = candidate.get('category_evidence') == 'geometry_only' or candidate.get('proposal_backend') == 'phone_shape_proposal'
        model = not shape and candidate.get('category_evidence') == 'model'
        reference = (not shape and candidate.get('category_evidence') == 'registered_reference_patch'
                     and candidate.get('proposal_backend') == 'dinov2_reference_patches')
        if shape:
            result['geometry_categories'][category] += 1
            result['geometry_only_candidates'] += 1
        elif model:
            result['categories'][category] += 1
            result['model_candidates'] += 1
        elif reference:
            result['reference_categories'][category] += 1
        else:
            result['unverified_categories'][category] += 1
            result['unverified_candidates'] += 1
        if candidate.get('accepted') is True:
            item_id = candidate.get('item_id')
            if (model or reference) and isinstance(item_id,str) and item_id:
                result['accepted'][item_id] += 1
            else:
                result['invalid_identity_claims'] += 1
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8018')
    parser.add_argument('--seconds', type=float, default=30)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--save-frame-dir', type=Path, help='Save one actual source JPEG and backend-drawn candidate image locally; no business writes')
    args = parser.parse_args()
    if urlsplit(args.url).hostname not in {'127.0.0.1', 'localhost', '::1'} or not 1 <= args.seconds <= 60:
        parser.error('Only localhost and a 1..60 second window are supported')
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'read_only': True,
              'report_schema_version': 3, 'categories_semantics': 'semantic model labels only; registered-reference, geometry and unknown provenance counted separately',
              'household_images_exported': False, 'physical_target_presence_human_verified': False,
              'samples': [], 'errors': [], 'duplicate_snapshots_ignored': 0}
    seen_frames = set()
    last_image = None
    evidence_dir = None
    if args.save_frame_dir:
        allowed = (Path(__file__).resolve().parents[1] / 'data/verification').resolve()
        evidence_dir = args.save_frame_dir.resolve()
        if not evidence_dir.is_relative_to(allowed):
            parser.error('Local frame evidence must remain inside data/verification')
    with httpx.Client(base_url=args.url, trust_env=False, timeout=4) as client:
        response = client.get('/api/session'); response.raise_for_status()
        # Never serialize the session cookie or pairing code.
        report['runtime_mode'] = response.json().get('runtime_mode')
        response = client.get('/api/items'); response.raise_for_status()
        items = response.json()
        report['items'] = [{'id': i['id'], 'name': i['name'], 'references': len(i.get('reference_images', [])),
            'profile': {k: i.get('recognition_profile', {}).get(k) for k in
                ['registration_status', 'profile_version', 'loaded_profile_version', 'loaded_camera_ids', 'quality_warnings']}}
            for i in items]
        cameras = client.get('/api/cameras').json()
        camera_ids = [c['id'] for c in cameras if c.get('enabled')]
        report['locations_before'] = [client.get(f"/api/items/{i['id']}/last-location").json().get('last_observed') for i in items]
        started = time.monotonic()
        while time.monotonic() - started < args.seconds:
            for camera_id in camera_ids:
                try:
                    response = client.get(f'/api/cameras/{camera_id}/vision-snapshot'); response.raise_for_status()
                    value = response.json()
                    frame_key = (camera_id, value.get('source_session_id'), value.get('source_frame'))
                    if frame_key in seen_frames:
                        report['duplicate_snapshots_ignored'] += 1
                        continue
                    seen_frames.add(frame_key)
                    sample = {k: value.get(k) for k in ['source_frame', 'source_session_id', 'source_timestamp',
                        'source_type', 'is_simulated', 'loaded_profiles', 'stage_timings_ms']}
                    sample['camera_id'] = camera_id
                    sample['candidates'] = [{k: c.get(k) for k in ['category', 'accepted', 'item_id', 'best_score',
                        'second_score', 'rejection_reason', 'detector_score', 'proposal_backend',
                        'category_evidence', 'geometry_score', 'appearance_match_attempted', 'bbox',
                        'best_item_id', 'best_item_name', 'profile_version']} for c in value.get('candidates', [])]
                    if evidence_dir:
                        last_image = (value, sample)
                    report['samples'].append(sample)
                except (httpx.HTTPError, ValueError) as exc:
                    report['errors'].append({'camera_id': camera_id, 'kind': type(exc).__name__})
            time.sleep(.5)
        report['seconds'] = round(time.monotonic() - started, 3)
        report['locations_after'] = [client.get(f"/api/items/{i['id']}/last-location").json().get('last_observed') for i in items]
        report['camera_health'] = [{k: c.get('health', {}).get(k) for k in ['status', 'capture_fps', 'preview_fps',
            'inference_fps', 'frames_read', 'processed_frames', 'loaded_profile_versions', 'recognition_error']}
            for c in client.get('/api/cameras').json()]
    if evidence_dir and last_image:
        import cv2
        import numpy as np
        value, sample = last_image
        encoded = base64.b64decode(value['image_data_url'].split(',', 1)[1], validate=True)
        frame = cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError('Fresh source image failed to decode')
        evidence_dir.mkdir(parents=True, exist_ok=True)
        raw_path = evidence_dir / 'actual-source.jpg'
        annotated_path = evidence_dir / 'backend-candidates.jpg'
        raw_path.write_bytes(encoded)
        h, w = frame.shape[:2]
        for index, candidate in enumerate(value.get('candidates') or []):
            box = candidate.get('bbox')
            if not isinstance(box, list) or len(box) != 4 or not all(isinstance(v, (int, float)) and np.isfinite(v) for v in box):
                continue
            x, y, bw, bh = box
            if min(x, y) < 0 or min(bw, bh) <= 0 or x+bw > 1.0001 or y+bh > 1.0001:
                continue
            p1, p2 = (round(x*w), round(y*h)), (round((x+bw)*w), round((y+bh)*h))
            color = (60, 190, 40) if candidate.get('accepted') else (0, 180, 240)
            cv2.rectangle(frame, p1, p2, color, 2)
            text = f"{index+1} {candidate.get('category')} accepted={candidate.get('accepted')} score={candidate.get('best_score')}"
            cv2.putText(frame, text, (p1[0], max(18, p1[1]-5)), cv2.FONT_HERSHEY_SIMPLEX, .5, color, 1, cv2.LINE_AA)
        ok, annotated = cv2.imencode('.jpg', frame)
        if not ok:
            raise ValueError('Backend annotation failed to encode')
        annotated_path.write_bytes(annotated.tobytes())
        report['local_frame_evidence'] = {**sample, 'raw_path': str(raw_path), 'annotated_path': str(annotated_path),
            'raw_sha256': hashlib.sha256(encoded).hexdigest(), 'annotation_sha256': hashlib.sha256(annotated).hexdigest(),
            'model': value.get('model'), 'geometry': value.get('geometry'), 'width': w, 'height': h,
            'business_writes': 0, 'reference_is_test_input': False, 'external_upload': False}
    report.update(summarize_candidates(candidate for sample in report['samples'] for candidate in sample['candidates']))
    report['distinct_source_frames'] = len({(s['camera_id'], s['source_session_id'], s['source_frame']) for s in report['samples']})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ['runtime_mode', 'seconds', 'distinct_source_frames', 'categories', 'geometry_categories', 'unverified_categories', 'accepted', 'errors']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
