"""Actual geometry proposals + DINO + owned TEST FastAPI; no proposal fixtures.

Ground-truth rectangles are read only by result evaluation, never supplied to
the candidate detector. Synthetic black/remote inputs are explicitly negatives.
Safety assertions do not claim that three similar phones can be identified.
The report separately records a failed positive identity goal when appropriate.
"""
from __future__ import annotations

import base64
from dataclasses import asdict
import gc
import json
import time

import cv2
import numpy as np
import pytest

from apps.api.tests.test_photo_registration_model_integration import running_backend
from apps.api.tests.test_public_phone_video_pipeline import ROOT, png, read_counts, sha256, source_frame
from services.vision.detectors.appearance import AppearanceEncoder, ProfileMatcher
from services.vision.detectors.nanodet import NanoDetDetectorBackend


ANDROID = ROOT/'data/test-assets/public-phone-android.webm'
LOTTI = ROOT/'data/test-assets/public-phone-lotti.webm'
# Evaluation/registration annotation ONLY. Neither detector receives this list.
ANDROID_BOXES = [(60,35,209,430),(318,6,222,460),(587,33,214,432)]
READY = all(path.is_file() for path in (ANDROID,LOTTI,ROOT/'data/models/dinov2-small.onnx',ROOT/'data/models/object_detection_nanodet_2022nov.onnx'))


def overlap(a,b):
    x,y,w,h=a; xx,yy,ww,hh=b
    area=max(0,min(x+w,xx+ww)-max(x,xx))*max(0,min(y+h,yy+hh)-max(y,yy))
    return area/max(1,w*h+ww*hh-area)


def phone_index(box):
    values=[overlap(box,expected) for expected in ANDROID_BOXES]
    return int(np.argmax(values)) if max(values)>=.5 else None


def synthetic_frame(kind):
    frame=np.full((480,854,3),220,np.uint8)
    cv2.rectangle(frame,(260,160),(560,285),(20,20,20),-1)
    if kind=='remote_shape':
        for x in (285,325,365,405,445,485,525):
            for y in (190,230,260):
                cv2.circle(frame,(x,y),8,(150,150,150),-1)
    return frame


def make_video(path,scenario):
    capture=cv2.VideoCapture(str(ANDROID)) if scenario.startswith('public_android') else None
    writer=None
    frame_hashes=set()
    try:
        if capture is not None:
            assert capture.isOpened() and capture.set(cv2.CAP_PROP_POS_MSEC,8000)
            fps=capture.get(cv2.CAP_PROP_FPS)
            count=round(12*fps)
        else:
            fps,count=10,70
        for _ in range(count):
            if capture is not None:
                ok,frame=capture.read()
                assert ok
                if scenario=='public_android_other_phone_single':
                    # Evaluation negative: remove the other two phones from the
                    # image. This is a public-video view crop, NOT a detector ROI.
                    frame=frame[:,560:].copy()
            else:
                frame=synthetic_frame(scenario)
            if writer is None:
                writer=cv2.VideoWriter(str(path),cv2.VideoWriter_fourcc(*'MJPG'),fps,(frame.shape[1],frame.shape[0]))
                assert writer.isOpened()
            frame_hashes.add(sha256(frame.tobytes()))
            writer.write(frame)
        return {'frames':count,'fps':fps,'source_seconds':[8,20] if capture is not None else None,
            'distinct_source_frames':len(frame_hashes),'synthetic_negative':capture is None,
            'width':frame.shape[1],'height':frame.shape[0],
            'source_crop_x':560 if scenario=='public_android_other_phone_single' else 0}
    finally:
        if capture is not None:capture.release()
        if writer is not None:writer.release()


@pytest.mark.skipif(not READY,reason='NOT_RUN: pinned public input/model files absent')
@pytest.mark.parametrize('scenario',['public_android','public_android_other_phone_single','filled_black_rectangle','remote_shape'])
def test_actual_shape_video_identity_and_safety(tmp_path,scenario):
    from services.vision.detectors.phone_shape import PhoneShapeProposer
    for name,path in (('android',ANDROID),('lotti',LOTTI)):
        metadata=json.loads((ROOT/f'scripts/test-assets/public-phone-{name}.source.json').read_text(encoding='utf-8'))
        assert sha256(path.read_bytes())==metadata['sha256']
    is_android=scenario.startswith('public_android')
    if is_android:
        reference,ref_index=source_frame(1,ANDROID)
        region=[60/854,35/480,209/854,430/480]
    else:
        metadata=json.loads((ROOT/'scripts/test-assets/public-phone-lotti.source.json').read_text(encoding='utf-8'))
        reference,ref_index=source_frame(20,LOTTI)
        region=metadata['reference_region_xywh_normalized']
    h,w=reference.shape[:2]
    x,y,bw,bh=[round(v*e) for v,e in zip(region,[w,h,w,h])]
    encoder=None
    matcher=None
    generic=None
    lifecycle=[]
    report={'scenario':scenario,'runtime_mode':'TEST','source_type':'video_file','is_simulated':True,
        'physical_camera':False,'source_kind':'public_video_cropped_view' if scenario=='public_android_other_phone_single' else 'public_filmed_phones' if is_android else 'synthetic_negative',
        'api_mock':False,'candidate_mock':False,'encoder_mock':False,'gt_used_for_proposals':False,
        'reference_source_frame':ref_index,'reference_crop_xywh':region,'identity_threshold':.82,'identity_margin':.06,
        'shape_model':None,'appearance_model':None,'direct_samples':[],
        'snapshots':[],'observed_states':[],'source_keyframes':[],'lifecycle':lifecycle,
        'safety_passed':False,'positive_identity_goal_passed':False,'overall_goal_passed':False,
        'runtime_policy':'geometry_only_display_candidate_not_eligible_for_identity_or_state',
        'direct_samples_scope':'offline_actual_DINO_diagnostic_not_runtime_identity_acceptance',
        'pytest_contract':'UI_only_safety_not_a_positive_identity_accuracy_claim'}
    camera=None
    try:
        encoder=AppearanceEncoder()
        report['appearance_model']=encoder.health()
        assert report['appearance_model']['available']
        matcher=ProfileMatcher([{'id':'registered-phone','type':'phone','appearance_profile':{
            'status':'ready','profile_version':1,'dimension':encoder.dimension,
            'model_id':encoder.model_id,'model_version':encoder.model_version,
            'embeddings':[encoder.encode(reference[y:y+bh,x:x+bw])],
        }}],encoder,threshold=.82,margin=.06)
        shape=PhoneShapeProposer()
        report['shape_model']=shape.health()
        generic=NanoDetDetectorBackend(ROOT/'data/models/object_detection_nanodet_2022nov.onnx')
        report['generic_model']=generic.health()
        assert report['generic_model']['available']
        samples=[(second,source_frame(second,ANDROID)[0]) for second in (8,12,16,20)] if is_android else [(None,synthetic_frame(scenario))]
        for second,frame in samples:
            if scenario=='public_android_other_phone_single':
                frame=frame[:,560:].copy()
            primary=generic.detect(frame)
            raw=shape.detect(frame)
            candidates=shape.supplement(frame,primary)
            results=[]
            # Evaluate raw geometry independently, including geometry correctly
            # suppressed by an overlapping non-phone model detection. Neither
            # these diagnostics nor the annotations are fed to the TEST API.
            for candidate in [*primary,*raw]:
                if candidate.label!='cell phone':continue
                cx,cy,cw,ch=candidate.bbox
                decision=matcher.match(frame[cy:cy+ch,cx:cx+cw],category=candidate.label)
                source_box=(cx+(560 if scenario=='public_android_other_phone_single' else 0),cy,cw,ch)
                results.append({'proposal':asdict(candidate),'offline_identity_without_category_gate':decision,
                    'evaluation_phone_index':phone_index(source_box) if is_android else None})
            report['direct_samples'].append({'seconds':second,'source_pixels_sha256':sha256(frame.tobytes()),
                'primary_detections':[asdict(value) for value in primary],
                'supplemented_detections':[asdict(value) for value in candidates],
                'results':results})
        raw_shapes=[value for sample in report['direct_samples'] for value in sample['results'] if value['proposal']['metadata'].get('backend')=='phone_shape_proposal']
        assert raw_shapes,'A real shape proposal is required; a silent empty source cannot pass this test'
        assert all(row['proposal']['metadata']['category_evidence']=='geometry_only' for row in raw_shapes)
        report['raw_other_phone_accepts']=sum(row['evaluation_phone_index'] in (1,2) and row['offline_identity_without_category_gate']['accepted'] for row in raw_shapes)
        if not is_android:
            assert not any(row['offline_identity_without_category_gate']['accepted'] for row in raw_shapes)
        # Only one model-owning process at a time: release offline models before
        # the owned TEST backend starts its real encoder/detector sessions.
        encoder.close()
        encoder=matcher=generic=None
        gc.collect()
        video=tmp_path/f'{scenario}.avi'
        report['video']=make_video(video,scenario)
        report['video']['sha256']=sha256(video.read_bytes())
        with running_backend(tmp_path,1,lifecycle) as (client,database):
            before=read_counts(database)
            assert before==dict(movement_events=0,item_current_state=0,event_media=0)
            configured=client.patch('/api/settings',json={'detection_mode':'experimental','hand_detection_enabled':False,'phone_shape_enabled':True})
            assert configured.status_code==200,configured.text
            settings=client.get('/api/settings').json()
            assert settings['phone_shape_enabled'] is True
            assert settings['reference_match_threshold']==.82 and settings['reference_match_margin']==.06
            report['runtime_settings']={key:settings[key] for key in ('phone_shape_enabled','phone_shape','reference_match_threshold','reference_match_margin','observation_min_frames')}
            item=client.post('/api/items',json={'name':'外形测试的已注册手机','type':'phone'}).json()
            endpoint=f"/api/items/{item['id']}"
            uploaded=client.post(endpoint+'/reference-images',files=[('files',('public-phone-reference.png',png(reference),'image/png'))])
            assert uploaded.status_code==200,uploaded.text
            ref=uploaded.json()[0]
            assert client.patch(endpoint+'/reference-images/'+ref['id'],json={'region':region,'confirmed':True}).status_code==200
            built=client.post(endpoint+'/profile/build')
            assert built.status_code==200 and built.json()['registration_status']=='ready',built.text
            upload=client.post('/api/cameras/upload-video',files={'file':(video.name,video.read_bytes(),'video/x-msvideo')})
            assert upload.status_code==200,upload.text
            created=client.post('/api/cameras',json={'name':scenario+' TEST','room_name':'公开测试场景（未校准）',
                'source_type':'video','source':upload.json()['source'],'config':{'loop':False},'inference_fps':5})
            assert created.status_code==200,created.text
            camera=created.json()['id']
            started=client.post(f'/api/cameras/{camera}/start')
            assert started.status_code==200,started.text
            try:
                deadline=time.monotonic()+(13 if is_android else 7.5)
                seen_frames=set()
                seen_states=set()
                while time.monotonic()<deadline:
                    response=client.get(f'/api/cameras/{camera}/vision-snapshot')
                    if response.status_code==200:
                        snapshot=response.json()
                        image_url=snapshot.pop('image_data_url',None)
                        key=(snapshot['source_session_id'],snapshot['source_frame'])
                        if key not in seen_frames:
                            seen_frames.add(key)
                            for candidate in snapshot.get('candidates',[]):
                                box=candidate['bbox']
                                dims=report['video']
                                candidate['evaluation_phone_index']=phone_index([box[0]*dims['width']+dims['source_crop_x'],box[1]*dims['height'],box[2]*dims['width'],box[3]*dims['height']]) if is_android else None
                            report['snapshots'].append(snapshot)
                            if image_url and len(report['source_keyframes'])<3:
                                body=base64.b64decode(image_url.split(',',1)[1],validate=True)
                                assert cv2.imdecode(np.frombuffer(body,np.uint8),1) is not None
                                path=tmp_path/f'actual-source-{snapshot["source_frame"]}.jpg'
                                path.write_bytes(body)
                                report['source_keyframes'].append({'path':str(path),'sha256':sha256(body),'source_frame':snapshot['source_frame'],'source_session_id':snapshot['source_session_id']})
                    location=client.get(endpoint+'/last-location').json()
                    state=location.get('current_state')
                    if state and state['last_seen_at'] not in seen_states:
                        seen_states.add(state['last_seen_at'])
                        report['observed_states'].append(state)
                    time.sleep(.2)
                report['after']=read_counts(database)
                report['search']=client.post('/api/search',json={'query':'外形测试的已注册手机在哪里'}).json()
                candidates=[candidate for snapshot in report['snapshots'] for candidate in snapshot.get('candidates',[])]
                runtime_shapes=[candidate for candidate in candidates if candidate.get('proposal_backend')=='phone_shape_proposal']
                if scenario=='remote_shape':
                    # The real NanoDet model identifies this synthetic keypad
                    # as a keyboard, so overlap suppression may remove its raw
                    # phone-shaped outline. Require nonempty real inference;
                    # absence of every candidate must not vacuously pass.
                    assert len(report['snapshots'])>=3
                    assert runtime_shapes or any(value.get('category')=='keyboard' for value in candidates)
                    report['overlap_suppression_observed']=not bool(runtime_shapes)
                else:
                    assert runtime_shapes,'Actual HTTP stream did not expose any shape proposal'
                assert all(candidate.get('category_evidence')=='geometry_only' and candidate.get('detector_score') is None and isinstance(candidate.get('geometry_score'),(int,float)) for candidate in runtime_shapes)
                assert all(candidate.get('accepted') is False and candidate.get('item_id') is None
                    and candidate.get('best_score') is None and candidate.get('appearance_match_attempted') is False
                    and candidate.get('rejection_reason')=='shape_requires_category_confirmation'
                    for candidate in runtime_shapes),'Display-only geometry must not run identity matching or be accepted'
                report['runtime_shape_candidate_count']=len(runtime_shapes)
                report['spatial_ambiguity_rejections']=sum(candidate.get('rejection_reason')=='multiple_spatial_candidates' for candidate in candidates)
                report['runtime_other_phone_accepts']=sum(candidate.get('accepted') is True and candidate.get('evaluation_phone_index') in (1,2) for candidate in candidates)
                assert report['runtime_other_phone_accepts']==0,'Another physical phone was assigned to the registered left phone'
                assert report['after']['movement_events']==0,'Geometry cannot manufacture confirmed pickup/placement or movement history'
                assert report['after']==before and not report['observed_states']
                assert not any(candidate.get('accepted') is True for candidate in candidates)
                if scenario=='public_android':
                    report['failed_goals']=['Single-photo identity continuity on three similar phones was NOT achieved. Geometry is display-only after offline DINO misaccepted other phones; no automatic identity/state claim.']
                else:
                    report['positive_identity_goal_status']='NOT_APPLICABLE_negative_input'
                report['safety_passed']=True
                report['overall_goal_passed']=report['safety_passed'] and (scenario!='public_android' or report['positive_identity_goal_passed'])
            finally:
                assert client.post(f'/api/cameras/{camera}/stop').status_code==200
                assert client.post('/api/storage/clear-test').status_code==200
                assert read_counts(database)==before
    except Exception as exc:
        report['error']=f'{type(exc).__name__}: {exc}'
        raise
    finally:
        if encoder is not None:
            encoder.close()
        matcher=generic=None
        gc.collect()
        (tmp_path/'phone-shape-http-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
