"""Public independently filmed phone -> actual models -> HTTP/SQLite/search.

No candidate boxes, appearance embeddings, business APIs or source frames are
mocked. The manually annotated rectangle is ONLY the registration photo crop.
Held-out frames are decoded from a separate source time range, never composited.
This establishes a public-video workflow, NOT the user's physical phone accuracy.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

import cv2
import numpy as np
import pytest

from apps.api.tests.test_photo_registration_model_integration import running_backend
from services.vision.detectors.appearance import AppearanceEncoder, ProfileMatcher
from services.vision.detectors.nanodet import NanoDetDetectorBackend

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT/'data/test-assets/public-phone-lotti.webm'
METADATA = ROOT/'scripts/test-assets/public-phone-lotti.source.json'
CANDIDATE = ROOT/'data/models/object_detection_nanodet_2022nov.onnx'
APPEARANCE = ROOT/'data/models/dinov2-small.onnx'
ASSETS_READY = all(path.is_file() for path in (SOURCE, METADATA, CANDIDATE, APPEARANCE))


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source_frame(second: float, source: Path = SOURCE):
    capture = cv2.VideoCapture(str(source))
    try:
        assert capture.isOpened()
        assert capture.set(cv2.CAP_PROP_POS_MSEC, second*1000)
        ok, frame = capture.read()
        assert ok and frame is not None
        return frame, int(capture.get(cv2.CAP_PROP_POS_FRAMES))-1
    finally:
        capture.release()


def png(frame):
    ok, encoded = cv2.imencode('.png', frame)
    assert ok
    return encoded.tobytes()


def read_counts(database):
    with sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True) as db:
        return {table: db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                for table in ('movement_events', 'item_current_state', 'event_media')}


def create_heldout_video(path: Path, start: float, end: float):
    capture = cv2.VideoCapture(str(SOURCE))
    writer = None
    hashes = set()
    try:
        assert capture.isOpened()
        fps = capture.get(cv2.CAP_PROP_FPS)
        assert fps > 0
        assert capture.set(cv2.CAP_PROP_POS_MSEC, start*1000)
        count = round((end-start)*fps)
        for _ in range(count):
            ok, frame = capture.read()
            assert ok
            if writer is None:
                height, width = frame.shape[:2]
                writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), fps, (width, height))
                assert writer.isOpened()
            # This is real decoded footage; no transformation other than codec.
            hashes.add(sha256(frame.tobytes()))
            writer.write(frame)
        return {'source_start_frame': round(start*fps), 'source_end_frame_exclusive': round(end*fps),
                'fps': fps, 'frames': count, 'distinct_decoded_frames': len(hashes)}, hashes
    finally:
        capture.release()
        if writer:
            writer.release()


@pytest.mark.skipif(not ASSETS_READY, reason='NOT_RUN: pinned public footage or real model weights absent')
@pytest.mark.parametrize('reference_mode', ['single_view', 'two_views', 'different_phone_unconfirmed'])
def test_public_phone_real_models_and_heldout_http_pipeline(tmp_path, reference_mode):
    metadata = json.loads(METADATA.read_text(encoding='utf-8'))
    assert sha256(SOURCE.read_bytes()) == metadata['sha256']
    reference, reference_index = source_frame(metadata['reference_seconds'])
    region = metadata['reference_region_xywh_normalized']
    reference_source = SOURCE
    if reference_mode == 'different_phone_unconfirmed':
        other_metadata = json.loads((ROOT/'scripts/test-assets/public-phone-android.source.json').read_text(encoding='utf-8'))
        reference_source = ROOT/other_metadata['local_file']
        assert sha256(reference_source.read_bytes()) == other_metadata['sha256']
        reference, reference_index = source_frame(1, reference_source)
        # Manual annotation ONLY for registration of a different physical phone.
        # The candidate detector still sees the untouched HTC held-out video.
        region = [60/854, 35/480, 209/854, 430/480]
    references = [(reference, reference_index, region)]
    for extra in metadata.get('additional_references', []) if reference_mode == 'two_views' else []:
        extra_frame, extra_index = source_frame(extra['seconds'])
        references.append((extra_frame, extra_index, extra['region']))
    encoder = AppearanceEncoder()
    assert encoder.health()['available'] is True
    assert encoder.model_version.endswith(('-l2-v1', '-l2-v2')), 'Review new model-version acceptance expectations explicitly'
    expect_observation = reference_mode == 'two_views' or (reference_mode == 'single_view' and encoder.model_version.endswith('-l2-v2'))
    detector = NanoDetDetectorBackend(CANDIDATE)
    assert detector.health()['available'] is True
    vectors = []
    for ref_frame, _, ref_region in references:
        rh, rw = ref_frame.shape[:2]
        rx, ry, rbw, rbh = [round(value*extent) for value, extent in zip(ref_region, [rw, rh, rw, rh])]
        vectors.append(encoder.encode(ref_frame[ry:ry+rbh, rx:rx+rbw]))
    matcher = ProfileMatcher([{'id': 'public-phone', 'type': 'phone', 'appearance_profile': {
        'status': 'ready', 'profile_version': 1, 'dimension': encoder.dimension,
        'model_id': encoder.model_id, 'model_version': encoder.model_version,
        'embeddings': vectors,
    }}], encoder, threshold=.82, margin=.06)
    lifecycle = []
    report = {'passed': False, 'runtime_mode': 'TEST', 'source_type': 'video_file',
        'is_simulated': True, 'public_filmed_video': True, 'physical_camera': False,
        'user_phone_accuracy': 'NOT_TESTED', 'physical_pickup_release': 'NOT_TESTED',
        'candidate_mock': False, 'encoder_mock': False, 'api_mock': False,
        'reference_is_test_input': False, 'source_metadata': metadata,
        'reference_frames': [entry[1] for entry in references],
        'reference_source_sha256': sha256(reference_source.read_bytes()),
        'reference_source': str(reference_source),
        'registration_condition': reference_mode,
        'expected_continuous_observation': expect_observation,
        'scope_limit': 'Public-video same-unit temporal holdout only. Not general multi-view accuracy or user-phone success.',
        'candidate_model': detector.health(),
        'candidate_model_sha256': sha256(CANDIDATE.read_bytes()),
        'appearance_model': encoder.health(), 'model_only_samples': [], 'snapshots': [],
        'lifecycle': lifecycle}
    try:
        for second in (41, 45, 50, 54):
            frame, index = source_frame(second)
            candidates = detector.detect(frame)
            results = []
            for candidate in candidates:
                cx, cy, cw, ch = candidate.bbox
                results.append({'candidate': asdict(candidate), 'identity': matcher.match(
                    frame[cy:cy+ch, cx:cx+cw], category=candidate.label)})
            report['model_only_samples'].append({'seconds': second, 'source_frame': index,
                'pixels_sha256': sha256(frame.tobytes()), 'results': results})
        assert any(result['candidate']['label']=='cell phone' for sample in report['model_only_samples'] for result in sample['results']), 'Actual model did not detect any phone'
        model_accepted = any(result['identity']['accepted'] for sample in report['model_only_samples'] for result in sample['results'])
        if expect_observation:
            assert model_accepted, 'Actual DINO did not match held-out phone'
        else:
            assert not model_accepted, 'Previously rejected baseline changed; investigate actual identity behavior rather than hiding it'

        video = tmp_path/'public-heldout-phone.avi'
        video_info, test_hashes = create_heldout_video(video, *metadata['heldout_seconds'])
        assert all(entry[1] < video_info['source_start_frame'] for entry in references)
        assert all(sha256(entry[0].tobytes()) not in test_hashes for entry in references)
        assert video_info['distinct_decoded_frames'] > 20
        report['test_video'] = {**video_info, 'sha256': sha256(video.read_bytes())}
        with running_backend(tmp_path, 1, lifecycle) as (client, database):
            report['before'] = read_counts(database)
            assert report['before'] == dict(movement_events=0, item_current_state=0, event_media=0)
            response = client.patch('/api/settings', json={'detection_mode':'experimental', 'hand_detection_enabled':False})
            assert response.status_code == 200, response.text
            upload = client.post('/api/cameras/upload-video', files={'file':('public-heldout-phone.avi', video.read_bytes(), 'video/x-msvideo')})
            assert upload.status_code == 200, upload.text
            created = client.post('/api/cameras', json={'name':'Public held-out phone TEST', 'source_type':'video',
                'source':upload.json()['source'], 'config':{'loop':False}, 'inference_fps':5, 'enabled':True})
            assert created.status_code == 200, created.text
            camera = created.json()['id']
            started = client.post(f'/api/cameras/{camera}/start')
            assert started.status_code == 200, started.text
            # Generic category detection must not invent a registered identity.
            deadline = time.monotonic()+12
            while time.monotonic() < deadline:
                response = client.get(f'/api/cameras/{camera}/vision-snapshot')
                if response.status_code == 200:
                    generic = response.json()
                    if any(c['category']=='cell phone' for c in generic.get('candidates',[])):
                        break
                time.sleep(.2)
            else:
                pytest.fail('Actual video stream produced no generic phone candidate')
            assert generic['loaded_profiles'] == []
            assert all(c.get('accepted') is not True and not c.get('item_id') for c in generic['candidates'])
            assert read_counts(database) == report['before']
            report['unregistered_generic_candidates'] = generic['candidates']
            assert client.post(f'/api/cameras/{camera}/stop').status_code == 200

            item_name = '公开视频中的另一台 Galaxy 手机' if reference_mode == 'different_phone_unconfirmed' else '公开视频中的 HTC 手机'
            created = client.post('/api/items', json={'name':item_name, 'type':'phone'})
            assert created.status_code == 200, created.text
            item_id = created.json()['id']
            endpoint = f'/api/items/{item_id}'
            uploaded = client.post(endpoint+'/reference-images', files=[('files', (f'reference-frame-{ref_index}.png', png(ref_frame), 'image/png')) for ref_frame, ref_index, _ in references])
            assert uploaded.status_code == 200, uploaded.text
            for ref, (_, _, ref_region) in zip(uploaded.json(), references):
                assert client.patch(endpoint+'/reference-images/'+ref['id'], json={'region':ref_region, 'confirmed':True}).status_code == 200
            built = client.post(endpoint+'/profile/build')
            assert built.status_code == 200 and built.json()['registration_status']=='ready', built.text
            assert read_counts(database)['item_current_state'] == 0
            if expect_observation and os.environ.get('OM_PUBLIC_PHONE_BROWSER') == '1':
                browser_output = tmp_path/'browser'
                completed = subprocess.run(['node', str(ROOT/'scripts/public-phone-browser.mjs'),
                    '--api', str(client.base_url), '--item', item_id, '--camera', camera,
                    '--output', str(browser_output)], cwd=ROOT, capture_output=True,
                    text=True, encoding='utf-8', timeout=50,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                report['browser'] = json.loads((browser_output/'public-phone-browser.json').read_text(encoding='utf-8')) if (browser_output/'public-phone-browser.json').is_file() else {'stdout':completed.stdout,'stderr':completed.stderr}
                assert completed.returncode == 0 and report['browser'].get('passed'), report['browser']
            assert client.post(f'/api/cameras/{camera}/start').status_code == 200
            previous_seen = None
            observed_updates = []
            deadline = time.monotonic()+13
            while time.monotonic() < deadline:
                location = client.get(endpoint+'/last-location').json()
                state = location.get('current_state')
                if state and state.get('last_seen_at') != previous_seen:
                    previous_seen = state.get('last_seen_at')
                    observed_updates.append({'last_seen_at':previous_seen, 'state':state})
                response = client.get(f'/api/cameras/{camera}/vision-snapshot')
                if response.status_code == 200:
                    snapshot = response.json()
                    snapshot.pop('image_data_url',None)
                    if not report['snapshots'] or report['snapshots'][-1]['source_frame']!=snapshot['source_frame']:
                        report['snapshots'].append(snapshot)
                if expect_observation and len(observed_updates) >= 3:
                    break
                time.sleep(.25)
            report['observed_updates'] = observed_updates
            report['after'] = read_counts(database)
            if not expect_observation:
                phone_candidates = [candidate for snapshot in report['snapshots'] for candidate in snapshot.get('candidates',[])
                                    if candidate['category']=='cell phone']
                report['phone_candidate_count'] = len(phone_candidates)
                report['identity_accepted_count'] = sum(candidate.get('accepted') is True for candidate in phone_candidates)
                report['negative_search'] = location
                assert len(phone_candidates) >= 3, 'Negative identity test needs actual phone candidates, not an absent source'
                # The full stream can contain isolated above-threshold matches
                # even though four sampled frames rejected. Do not overwrite
                # that fact with an 'all frames rejected' claim: continuity
                # must still prevent an unconfirmed current-state update.
                assert observed_updates == [] and report['after'] == report['before']
                assert location['evidence'] is None and location['last_observed'] is None
                assert location['observation_hint']['code'] in {'identity_uncertain','no_target_candidate'}
                searched = client.post('/api/search', json={'query':item_name+'在哪里'})
                assert searched.status_code == 200
                report['search'] = searched.json()
                assert client.post(f'/api/cameras/{camera}/stop').status_code == 200
                assert read_counts(database) == report['before']
                assert client.post('/api/storage/clear-test').status_code == 200
                report['verified_claim'] = 'no continuous accepted identity; no invented current state or placement'
                report['passed'] = True
                return
            assert len(observed_updates) >= 3, 'Held-out footage did not produce continuous current-state updates'
            assert report['after']['item_current_state'] == 1
            assert location['status']=='last_seen' and location['last_confirmed_placement'] is None
            assert location['runtime_mode']=='TEST' and location['is_simulated'] is True
            assert location['source_type']=='video_file'
            observed = location['last_observed']
            image_response = client.get(observed['screenshot_path'])
            assert image_response.status_code == 200
            assert sha256(image_response.content) == observed['screenshot_sha256']
            saved_frame = cv2.imdecode(np.frombuffer(image_response.content,np.uint8),1)
            assert saved_frame is not None
            assert observed['screenshot_source_session_id']==observed['source_session_id']
            assert 0 < observed['screenshot_source_frame'] <= observed['source_frame'] <= video_info['frames']
            replay = cv2.VideoCapture(str(video))
            try:
                assert replay.isOpened()
                # CaptureWorker numbers the first decoded source packet as 1.
                assert replay.set(cv2.CAP_PROP_POS_FRAMES, observed['screenshot_source_frame']-1)
                readable, expected_frame = replay.read()
                assert readable and expected_frame.shape == saved_frame.shape
                source_mae = float(np.abs(expected_frame.astype(np.float32)-saved_frame.astype(np.float32)).mean())
                # Lossy JPEG allows small codec error, not unrelated old media.
                assert source_mae < 12, f'Saved observation pixels do not match its declared source frame: MAE={source_mae}'
                report['screenshot_source_pixel_check'] = {'source_frame':observed['screenshot_source_frame'],
                    'mean_absolute_error':source_mae, 'max_allowed':12, 'shape':list(saved_frame.shape)}
            finally:
                replay.release()
            report['evidence'] = {key: observed.get(key) for key in ('screenshot_path','screenshot_sha256','source_frame','source_session_id','screenshot_source_frame')}
            # Retain one bounded public evidence JPEG outside the temporary DB.
            (tmp_path/'public-observed.jpg').write_bytes(image_response.content)
            searched = client.post('/api/search', json={'query':item_name+'在哪里'})
            assert searched.status_code == 200, searched.text
            search_data = searched.json()
            report['search'] = search_data
            serialized = json.dumps(search_data, ensure_ascii=False)
            assert item_id in serialized and observed['screenshot_sha256'] in serialized
            assert report['after']['movement_events']==0, 'Do not infer a confirmed movement from a static held-out interval'
            assert client.post(f'/api/cameras/{camera}/stop').status_code == 200
            offline = client.get(endpoint+'/last-location').json()
            assert offline['current_state']['last_seen_at']==previous_seen
            report['stopped_search'] = offline
            assert client.post('/api/storage/clear-test').status_code == 200
            assert read_counts(database)==report['before']
        report['passed'] = True
    finally:
        (tmp_path/'public-phone-http-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
