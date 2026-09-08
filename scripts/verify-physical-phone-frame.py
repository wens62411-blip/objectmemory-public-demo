"""Strict local REAL-camera-frame replay gate, not live or movement acceptance.

Exit 0: this bounded detection/identity case passed; 1: recognition failed;
2: incomplete/untrusted inputs or unavailable actual models. No business writes.
Private case files/images belong only in ignored data/verification, never fixtures.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import time
import tempfile
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class InvalidCase(ValueError):
    pass


def confined(root: Path, value: str | Path, allowed: Path, *, exists=True) -> Path:
    candidate = Path(value)
    path = (candidate if candidate.is_absolute() else root/candidate).resolve()
    boundary = allowed.resolve()
    if path == boundary or not path.is_relative_to(boundary):
        raise InvalidCase('Path must remain inside the permitted local data directory')
    if exists and not path.is_file():
        raise InvalidCase('Required local file is missing')
    return path


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def current_nanodet_path(root,settings):
    expected=(root/'data/models/object_detection_nanodet_2022nov.onnx').resolve()
    configured=Path(settings.get('model_path') or 'data/models/object_detection_nanodet_2022nov.onnx')
    configured=(configured if configured.is_absolute() else root/configured).resolve()
    if configured != expected:
        raise InvalidCase('Current detector model_path differs from the supported NanoDet gate; not silently substituting a model')
    return expected


def protect_detection_inputs(paths,protected):
    reference_hashes=set(protected.values())
    sources={path:file_hash(path) for path in paths}
    if any(digest in reference_hashes for digest in sources.values()):
        raise InvalidCase('A reference image or copied reference cannot be the detection input')
    protected.update(sources)


def load_frame(root, record):
    import cv2
    import numpy as np
    path = confined(root, record['path'], root/'data/verification')
    expected = record.get('sha256')
    if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
        raise InvalidCase('A pinned lowercase SHA-256 is required')
    if not 0 < path.stat().st_size <= 15*1024*1024:
        raise InvalidCase('Frame size exceeds the bounded local input contract')
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected:
        raise InvalidCase('Source frame hash mismatch')
    frame = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)
    if frame is None or frame.shape[0]*frame.shape[1] > 16_777_216:
        raise InvalidCase('Source frame is not a bounded decodable image')
    return path, frame


def validate_case(root, case):
    if case.get('contract_version') != 1 or not isinstance(case.get('target_item_id'), str) or not case['target_item_id']:
        raise InvalidCase('Target identity and case contract version are required')
    source = case['positive']
    if source.get('runtime_mode') != 'REAL' or source.get('is_simulated') is not False or source.get('source_type') not in {'opencv_camera', 'browser_camera', 'esp32_real'}:
        raise InvalidCase('A non-simulated physical camera source is required')
    UUID(source['source_session_id'])
    if type(source.get('source_frame')) is not int or source['source_frame'] < 1:
        raise InvalidCase('Source frame sequence is required')
    timestamp = datetime.fromisoformat(source['source_timestamp'])
    if timestamp.tzinfo is None:
        raise InvalidCase('Source timestamp must have timezone information')
    if source.get('reviewed_by') != 'assistant_visual_review' or source.get('reviewed_phone_present') is not True:
        raise InvalidCase('Explicit assistant-reviewed physical phone annotation is required')
    positive_path, frame = load_frame(root, source)
    roi = source.get('phone_roi_xywh')
    if not isinstance(roi, list) or len(roi) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in roi):
        raise InvalidCase('Finite reviewed pixel-edge xywh annotation is required')
    x,y,w,h = roi
    if min(x,y) < 0 or min(w,h) <= 0 or x+w > frame.shape[1] or y+h > frame.shape[0]:
        raise InvalidCase('Reviewed annotation lies outside the source image')
    negatives = case.get('negative_controls')
    if not isinstance(negatives, list) or len(negatives) != 2:
        raise InvalidCase('Exactly two independently reviewed no-phone controls are required')
    loaded = []
    for row in negatives:
        if row.get('reviewed_by') != 'assistant_visual_review' or row.get('reviewed_phone_present') is not False:
            raise InvalidCase('No-phone control review is missing')
        loaded.append(load_frame(root,row))
    hashes = [source['sha256'],*[row['sha256'] for row in negatives]]
    if len(set(hashes)) != 3:
        raise InvalidCase('Positive and controls must be distinct images')
    return (positive_path,frame), loaded


def read_registration(root, target_id):
    database = confined(root, 'data/database/objectmemory.sqlite', root/'data/database')
    # Do not import Database: its constructor performs schema migrations.
    connection = sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)
    connection.row_factory = sqlite3.Row
    def decoded(row, json_fields):
        value = dict(row)
        for key in json_fields:
            if isinstance(value.get(key), str):
                value[key] = json.loads(value[key])
        return value
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        items = [decoded(row, ('aliases',)) for row in connection.execute('SELECT * FROM items')]
        refs = [decoded(row, ('features', 'region', 'suggested_regions', 'capture_source', 'quality'))
                for row in connection.execute('SELECT * FROM item_reference_images')]
        for item in items:
            item['reference_images'] = [row for row in refs if row.get('item_id') == item['id']]
            row = connection.execute('SELECT * FROM item_recognition_profiles WHERE id=?',(item['id'],)).fetchone()
            if row:
                profile = decoded(row, ('embeddings', 'reference_ids'))
                item['appearance_profile'] = profile
        target = next((item for item in items if item['id'] == target_id),None)
        if not target or target.get('appearance_profile',{}).get('status') != 'ready':
            raise InvalidCase('Current REAL target profile is not ready')
        row = connection.execute("SELECT value FROM settings WHERE id='main'").fetchone()
        settings = json.loads(row['value']) if row else {}
        if settings.get('detection_mode') != 'experimental':
            raise InvalidCase('Current REAL settings do not select photo recognition')
    finally:
        connection.close()
    protected = {}
    for ref in refs:
        for key in ('path','original_path','crop_path'):
            value = ref.get(key)
            if not value: continue
            if not value.startswith('/media/registered-items/'):
                raise InvalidCase('Unexpected registration media path')
            path = confined(root,'data/'+value[len('/media/'):],root/'data/registered-items')
            protected[path] = file_hash(path)
            if key == 'original_path' and ref.get('sha256') and protected[path] != ref['sha256']:
                raise InvalidCase('Registered original image hash mismatch')
    if not protected:
        raise InvalidCase('No protected registered reference images')
    return items, settings, protected


def predict(frame, detector, matcher, minimum_crop_pixels):
    """Only full input image and actual model boxes; no reviewed ROI parameter."""
    rows = []
    height,width = frame.shape[:2]
    for detection in detector.detect(frame):
        if detection.label == 'person': continue
        x,y,w,h = detection.bbox
        if min(x,y) < 0 or min(w,h) <= 0 or x+w > width or y+h > height:
            raise InvalidCase('Detector returned an invalid source-space bounding box')
        if min(w,h) < minimum_crop_pixels:
            match = {'accepted':False,'item_id':None,'rejection':'insufficient_detail'}
        else:
            match = matcher.match(frame[y:y+h,x:x+w],category=detection.label)
        rows.append({'category':detection.label,'bbox':list(detection.bbox),
                     'detector_score':float(detection.confidence),'identity':match})
    # Same fail-closed spatial ambiguity rule as the production engine.
    counts = Counter(row['identity'].get('item_id') for row in rows if row['identity'].get('accepted'))
    for row in rows:
        match = row['identity']
        if match.get('accepted') and counts[match.get('item_id')] > 1:
            row['raw_identity_accepted_before_ambiguity'] = match['item_id']
            row['identity'] = {**match,'accepted':False,'item_id':None,'rejection':'multiple_spatial_candidates'}
    return rows


def production_predict(frame, engine):
    """Execute production recognition only, with no capture/track/event loop.

    The reviewer annotation is deliberately not in this function's contract.
    All source-image windows/boxes come from the current production algorithms.
    """
    detections = engine.experimental.detect(frame)
    detections = engine._supplement_reference_patches(frame, detections)
    engine._assign_reference_identities(frame, detections)
    height, width = frame.shape[:2]
    rows = []
    for candidate in engine._recognition_candidates:
        x, y, w, h = candidate['bbox']
        bbox = [round(x * width), round(y * height), round(w * width), round(h * height)]
        if min(bbox[:2]) < 0 or min(bbox[2:]) <= 0 or bbox[0] + bbox[2] > width or bbox[1] + bbox[3] > height:
            raise InvalidCase('Production recognition returned an invalid source-space bounding box')
        row = {'category': candidate['category'], 'bbox': bbox,
               'detector_score': candidate.get('detector_score'),
               'proposal_backend': candidate.get('proposal_backend'),
               'category_evidence': candidate.get('category_evidence'),
               'identity': {key: value for key, value in candidate.items() if key not in ('bbox', 'category', 'detector_score')}}
        if candidate.get('rejection') == 'multiple_spatial_candidates':
            row['raw_identity_accepted_before_ambiguity'] = candidate.get('best_item_id')
        rows.append(row)
    return rows


def annotated_prediction(output, frame, predictions, source, target_id):
    """Diagnostic plot of actual model boxes on the immutable captured pixels."""
    import cv2
    plotted = frame.copy()
    for row in predictions:
        if row['identity'].get('accepted') and row['identity'].get('item_id') == target_id:
            x, y, w, h = row['bbox']
            cv2.rectangle(plotted, (x, y), (x + w, y + h), (45, 205, 100), 3)
            cv2.putText(plotted, 'registered item / local-reference match', (x, max(24, y - 10)),
                        cv2.FONT_HERSHEY_SIMPLEX, .65, (45, 205, 100), 2, cv2.LINE_AA)
    # This is an offline replay receipt, never a screenshot of the current UI.
    cv2.rectangle(plotted, (0, 0), (plotted.shape[1], 66), (25, 28, 28), -1)
    cv2.putText(plotted, 'OFFLINE REPLAY / local-reference match / not live or placement proof',
                (12, 26), cv2.FONT_HERSHEY_SIMPLEX, .65, (255, 255, 255), 1, cv2.LINE_AA)
    cv2.putText(plotted, f"Captured: {source['source_timestamp']}  frame: {source['source_frame']}",
                (12, 52), cv2.FONT_HERSHEY_SIMPLEX, .55, (255, 255, 255), 1, cv2.LINE_AA)
    success, encoded = cv2.imencode('.jpg', plotted, [cv2.IMWRITE_JPEG_QUALITY, 93])
    if not success:
        raise InvalidCase('Could not encode offline model-box diagnostic')
    path = output.with_name(output.stem + '-annotated.jpg')
    with path.open('xb') as stream:
        stream.write(encoded.tobytes())
    return {'path': str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
            'sha256': file_hash(path), 'annotation_source': 'actual_production_predictions',
            'source_image_sha256': source['sha256'], 'current_live': False}


def overlap(a,b):
    x,y,w,h = a; u,v,p,q = b
    intersection = max(0,min(x+w,u+p)-max(x,u))*max(0,min(y+h,v+q)-max(y,v))
    return intersection/(w*h+p*q-intersection)


def evaluate_predictions(positive, negatives, reviewed_roi, target_id):
    """Review annotation is used only AFTER predictions, never as model input."""
    good = [row for row in positive if overlap(row['bbox'],reviewed_roi) >= .5
            and row['identity'].get('accepted') and row['identity'].get('item_id') == target_id]
    false_accepts = [row for batch in negatives for row in batch if
                    (row['identity'].get('accepted') and row['identity'].get('item_id') == target_id)
                    or row.get('raw_identity_accepted_before_ambiguity') == target_id]
    passed = len(good) == 1 and not false_accepts
    return {'passed':passed,'iou_threshold':.5,'positive_target_matches':len(good),
            'negative_target_acceptances':len(false_accepts),'positive_max_iou':max((overlap(row['bbox'],reviewed_roi) for row in positive),default=0),
            'reason':'bounded_detection_identity_pass' if passed else 'physical_phone_detection_or_identity_failed'}


def run(case_path, output_path, *, root=ROOT):
    report = {'started_at':datetime.now(timezone.utc).isoformat(),
              'scope':'offline replay of REAL captured frame; not current live or movement acceptance',
              'business_database_mode':'ro/query_only','business_writes':0,'external_upload':False,
              'reviewed_roi_used_for_inference':False,'reference_is_detection_input':False,
              'display_only_shape_proposals':'omitted: geometry-only proposals cannot establish identity in production',
              'current_live_verified':False,'placement_verified':False,'closed_loop_verified':False}
    protected = {}; encoder = None; engine = None; media_workspace = None; output = None
    try:
        manifest = confined(root,case_path,root/'data/verification')
        if manifest.stat().st_size > 65536: raise InvalidCase('Case manifest exceeds size limit')
        case = json.loads(manifest.read_text(encoding='utf-8'))
        (positive_path,positive), negatives = validate_case(root,case)
        requested_output = confined(root,output_path,root/'data/verification',exists=False)
        if requested_output.exists() or requested_output.suffix != '.json' or requested_output in {manifest,positive_path,*[p for p,_ in negatives]}:
            raise InvalidCase('Report cannot overwrite a case or source image')
        output = requested_output
        items,settings,protected = read_registration(root,case['target_item_id'])
        protect_detection_inputs([positive_path,*[p for p,_ in negatives]],protected)
        from services.vision.detectors.appearance import AppearanceEncoder
        from services.vision.engine import VisionEngine
        thresholds = {key:settings.get(key,default) for key,default in (
            ('experimental_confidence',.35),('reference_match_threshold',.82),('reference_match_margin',.06),('identity_min_crop_pixels',32))}
        model_path = current_nanodet_path(root, settings)
        encoder = AppearanceEncoder(root/'data/models/dinov2-small.onnx')
        media_workspace = tempfile.TemporaryDirectory(prefix='om-physical-replay-')
        def forbidden_event(*_args):
            raise InvalidCase('Offline recognition must never invoke an event callback')
        # Construct actual production recognizers, but never start capture or
        # call _process. No application/database service is instantiated.
        engine = VisionEngine({'id': 'offline-real-frame-replay', 'source_type': 'webcam', 'source': '0'},
            items, [], {**settings, 'runtime_mode': 'TEST', 'source_type': 'test_fixture',
                'media_root': media_workspace.name, 'reference_root': str(root/'data/registered-items'),
                'model_path': str(model_path), 'record_events': False, 'save_clips': False,
                'hand_detection_enabled': False, 'person_detection_enabled': False},
            forbidden_event, appearance_encoder=encoder)
        detector, matcher = engine.experimental, engine.reference_matcher
        if not detector or not matcher or not detector.health().get('available') or not encoder.health().get('available') or case['target_item_id'] not in matcher.loaded_profile_versions:
            raise InvalidCase('Actual detector/encoder/current target profile unavailable')
        started = time.perf_counter()
        positive_predictions = production_predict(positive, engine)
        negative_predictions = [production_predict(frame, engine) for _, frame in negatives]
        result = evaluate_predictions(positive_predictions,negative_predictions,case['positive']['phone_roi_xywh'],case['target_item_id'])
        report.update(status='PASS' if result['passed'] else 'FAIL',exit_code=0 if result['passed'] else 1,
            evaluation=result,case=case,thresholds=thresholds,loaded_profile_versions=matcher.loaded_profile_versions,
            detector_health=detector.health(),encoder_health=encoder.health(),
            recognition_route='current VisionEngine: NanoDet + registered reference patches + production identity admission',
            reference_patch_health=engine.reference_patches.health() if engine.reference_patches else None,
            capture_started=False,engine_process_called=False,temporary_media_cleaned=False,
            predictions={'positive':positive_predictions,'negative_controls':negative_predictions},
            elapsed_ms=round((time.perf_counter()-started)*1000,3))
        output.parent.mkdir(parents=True, exist_ok=True)
        report['annotated_prediction'] = annotated_prediction(output, positive, positive_predictions,
                                                             case['positive'], case['target_item_id'])
    except Exception as exc:
        report.update(status='INCOMPLETE',exit_code=2,error=f'{type(exc).__name__}: {exc}')
    finally:
        if engine is not None: engine.stop()
        if encoder is not None: encoder.close()
        if media_workspace is not None:
            temporary_media_path = Path(media_workspace.name)
            media_workspace.cleanup()
            report['temporary_media_cleaned'] = not temporary_media_path.exists()
        unchanged = all(path.is_file() and file_hash(path)==digest for path,digest in protected.items())
        report.update(protected_files_unchanged=unchanged,protected_hashes={str(p.relative_to(root)):h for p,h in protected.items()})
        if not unchanged: report.update(status='INCOMPLETE',exit_code=2,error='A protected source/reference file changed during verification')
    if output is not None and report.get('status') in {'PASS','FAIL','INCOMPLETE'}:
        try:
            output.parent.mkdir(parents=True,exist_ok=True)
            with output.open('x',encoding='utf-8') as stream:
                stream.write(json.dumps(report,ensure_ascii=False,indent=2))
        except OSError as exc:
            report.update(status='INCOMPLETE',exit_code=2,error=f'Report write failed without overwrite: {type(exc).__name__}')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',type=Path,required=True)
    parser.add_argument('--output',type=Path)
    args = parser.parse_args()
    output = args.output or args.case.parent/'physical-frame-result.json'
    report = run(args.case,output)
    print(json.dumps({key:report.get(key) for key in ('status','exit_code','scope','evaluation','error','protected_files_unchanged')},ensure_ascii=False))
    return report['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
