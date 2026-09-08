"""Actual file capture -> MediaPipe VIDEO -> hand actions -> HTTP -> SQLite.

The input is an existing public static photo encoded as a video, NOT a person
performing a physical action. No business API or detector response is mocked.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import cv2
import numpy as np
import pytest
from apps.api.tests.test_photo_registration_model_integration import running_backend

ROOT=Path(__file__).resolve().parents[3]
SOURCE=ROOT/'data/temporary/official-mediapipe-woman-hands.jpg'
MODEL=ROOT/'data/models/hand_landmarker.task'


def counts(path):
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
        return {name:db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                for name in ('movement_events','item_current_state','event_media')}


@pytest.mark.skipif(not SOURCE.is_file() or not MODEL.is_file(),reason='NOT_RUN: local public hand photo/model absent')
def test_actual_file_hand_actions_without_profiles_and_display_toggle(tmp_path):
    image=cv2.imdecode(np.frombuffer(SOURCE.read_bytes(),np.uint8),1)
    assert image is not None
    h,w=image.shape[:2]
    scale=min(1,800/w)
    image=cv2.resize(image,(round(w*scale)//2*2,round(h*scale)//2*2))
    h,w=image.shape[:2]
    video=tmp_path/'public-static-hand-input.avi'
    writer=cv2.VideoWriter(str(video),cv2.VideoWriter_fourcc(*'MJPG'),10,(w,h))
    assert writer.isOpened()
    try:
        for _ in range(120):
            writer.write(image)
    finally:
        writer.release()
    lifecycle=[]
    report={'passed':False,'physical_camera':False,'physical_grasp_accuracy':'NOT_TESTED',
            'source_kind':'public_static_photo_encoded_to_video','business_mock':False,'model_mock':False,
            'model_sha256':hashlib.sha256(MODEL.read_bytes()).hexdigest(),
            'source_sha256':hashlib.sha256(SOURCE.read_bytes()).hexdigest(),'lifecycle':lifecycle}
    try:
        with running_backend(tmp_path,1,lifecycle) as (client,database):
            before=counts(database)
            assert before=={'movement_events':0,'item_current_state':0,'event_media':0}
            assert client.patch('/api/settings',json={'show_hands':False,'hand_detection_enabled':True}).status_code==200
            upload=client.post('/api/cameras/upload-video',files={'file':('public-hand.avi',video.read_bytes(),'video/x-msvideo')})
            assert upload.status_code==200,upload.text
            response=client.post('/api/cameras',json={'name':'Public static photo video test','source_type':'video',
                'source':upload.json()['source'],'config':{'loop':True},'inference_fps':5})
            assert response.status_code==200,response.text
            camera=response.json()['id']
            assert client.post(f'/api/cameras/{camera}/start').status_code==200
            endpoint=f'/api/cameras/{camera}/actions'
            deadline=time.monotonic()+20
            samples=[]
            while time.monotonic()<deadline:
                data=client.get(endpoint).json()
                if data.get('fresh'):
                    samples.append(data)
                if len(data.get('hands',[]))==2 and all(hand.get('tracking_stable') for hand in data['hands']):
                    break
                time.sleep(.2)
            else:
                pytest.fail(f'Actual MediaPipe hand actions not stable: {samples[-1:]!r}')
            assert data['hand_status']['enabled'] is True and data['hand_status']['available'] is True
            assert data['runtime_mode']=='TEST' and data['source_type']=='video_file' and data['is_simulated'] is True
            assert data['profile_count']==0 and data['interactions']==[]
            assert all(hand['source_frame']==data['source_frame'] and hand['source_session_id']==data['source_session_id'] for hand in data['hands'])
            assert all(hand['grasp_established'] is False for hand in data['hands'])
            session=data['source_session_id']
            assert client.patch('/api/settings',json={'show_hands':True}).status_code==200
            after=client.get(endpoint).json()
            assert after['source_session_id']==session,'Display-only change must not reopen the video'
            assert after['hand_status']['enabled'] is True
            report.update({'hand_count':len(data['hands']),'sample_count':len(samples),
                'actions':[{key:hand[key] for key in ('posture','motion','tracking_stable','bbox')} for hand in data['hands']],
                'display_toggle_preserved_session':True,'profile_count':0,'database_counts':counts(database)})
            assert counts(database)==before
            assert client.post(f'/api/cameras/{camera}/stop').status_code==200
            stopped=client.get(endpoint).json()
            assert not stopped['fresh'] and stopped['hands']==[] and stopped['interactions']==[]
            assert counts(database)==before
        report['passed']=True
    finally:
        (tmp_path/'hand-actions-http-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
