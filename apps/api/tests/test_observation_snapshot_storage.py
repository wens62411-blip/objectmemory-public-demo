"""Internal-proof storage contracts; generated JPEGs are not physical evidence.

Runtime proof admission is tested separately. These tests deliberately call the
internal service with its keyword proof, never create or start a camera stream.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import os

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from apps.api.app.search import location_result


START = datetime(2026, 9, 5, 10, tzinfo=timezone.utc)


@pytest.fixture
def context(tmp_path):
    app=create_app(tmp_path/'isolated-observation-store',testing=True)
    with TestClient(app) as client:
        client.get('/api/session')
        runtime=app.state.runtime
        item=runtime.db.save('items',{'name':'generated fixture','type':'phone'})
        camera=runtime.db.save('cameras',{'name':'never opened','source_type':'video',
            'source':str(tmp_path/'never-opened.avi'),'config':{},'room_name':'room','enabled':False})
        yield runtime,item,camera,client


def jpeg(value=80):
    ok,encoded=cv2.imencode('.jpg',np.full((96,128,3),value,np.uint8))
    assert ok
    return encoded.tobytes()


def observe(context,elapsed=0,*,frame=1,position=(.2,.3),state='VISIBLE_STATIC',content=None,**extra):
    runtime,item,camera,_=context
    track={'item_id':item['id'],'camera_id':camera['id'],'source_session_id':'observation-session',
        'source_frame':frame,'observation_source_frame':frame,'last_seen':(START+timedelta(seconds=elapsed)).isoformat(),
        'source_timestamp':(START+timedelta(seconds=elapsed)).isoformat(),'zone_name':'desk',
        'center':list(position),'confidence':.91,'state':state,'speed':0,'hand_near':False,
        'detection_mode':'experimental',**extra}
    return runtime.event_service.observe(track,observation_verified=True,observation_jpeg=content)


def test_first_verified_observation_persists_one_image_without_video_or_movement(context):
    runtime,item,camera,client=context
    data=jpeg()
    state=observe(context,content=data)
    observed=state['last_observed']
    path=runtime.event_service.media_path(observed['screenshot_path'])
    assert path.read_bytes()==data
    assert observed['screenshot_sha256']==hashlib.sha256(data).hexdigest()
    assert observed['source_frame']==observed['screenshot_source_frame']==1
    assert runtime.db.count('event_media')==1
    assert runtime.db.count('movement_events')==0
    assert not list((runtime.media/'event-clips').iterdir())
    result=client.get(f"/api/items/{item['id']}/last-location").json()
    assert result['evidence']['screenshot_path']==observed['screenshot_path']
    assert result['evidence']['evidence_status']=='observation_only'
    assert result['last_confirmed_placement'] is None
    assert result['location_hypotheses']==[]


def test_claimed_payload_proof_cannot_store_observation_image(context):
    runtime,item,camera,_=context
    assert runtime.event_service.observe({'item_id':item['id'],'camera_id':camera['id'],
        'observation_verified':True},observation_jpeg=jpeg()) is None
    assert runtime.db.count('item_current_state')==runtime.db.count('event_media')==0


@pytest.mark.parametrize('display_flags', [
    {'visual_only': True}, {'observation_evidence': False},
    {'state': 'PREDICTED'}, {'evidence_type': 'predicted'},
])
def test_display_prediction_cannot_advance_last_seen_even_with_internal_keyword(context, display_flags):
    runtime, item, _, client = context
    before = observe(context, content=jpeg())
    media_before = runtime.db.list('event_media')
    assert observe(context, 30, frame=100, position=(.9, .9), content=jpeg(220), **display_flags) is None
    assert runtime.db.get('item_current_state', f"TEST:{item['id']}") == before
    assert runtime.db.list('event_media') == media_before
    result = client.get(f"/api/items/{item['id']}/last-location").json()
    assert result['last_observed']['timestamp_start'] == before['last_seen_at']
    assert result['last_observed']['final_position'] == before['current_position']


def test_stationary_five_minutes_updates_state_without_replacing_snapshot(context):
    runtime,*_=context
    initial=observe(context,content=jpeg())['last_observed']
    updated=observe(context,1,frame=2,content=jpeg(120))['last_observed']
    assert updated['source_frame']==2 and updated['screenshot_source_frame']==1
    assert updated['screenshot_path']==initial['screenshot_path']
    assert updated['screenshot_observed_at']==START.isoformat()
    assert runtime.db.count('event_media')==1
    for second in range(2,301):
        replaced=observe(context,second,frame=second+1,content=jpeg(140))['last_observed']
        assert replaced['screenshot_source_frame']==1
        assert replaced['screenshot_path']==initial['screenshot_path']
    assert replaced['source_frame']==301
    assert runtime.event_service.media_path(initial['screenshot_path']).exists()
    assert runtime.db.count('event_media')==1
    assert runtime.db.count('movement_events')==0


def test_new_source_first_verified_observation_replaces_old_session_photo_without_hand_wait(context):
    runtime, item, _, _ = context
    previous = observe(context, content=jpeg())
    old_path = runtime.event_service.media_path(previous['last_observed']['screenshot_path'])
    new = observe(context, 30, frame=100, position=(.7, .6), content=jpeg(220),
                  source_session_id='fresh-source-session', state='CARRIED', hand_near=True, speed=.3)
    snapshot = new['last_observed']
    assert new['status'] == 'last_seen'
    assert snapshot['screenshot_source_session_id'] == 'fresh-source-session'
    assert snapshot['screenshot_source_frame'] == 100
    assert snapshot['screenshot_observed_at'] == new['last_seen_at']
    assert snapshot['screenshot_path'] != previous['last_observed']['screenshot_path']
    assert snapshot['image_status'] == 'available' and not old_path.exists()
    assert runtime.db.count('event_media') == 1 and runtime.db.count('movement_events') == 0
    # Subsequent carried frames in that session keep its one first screenshot;
    # movement does not create a new screenshot on every frame.
    next_state = observe(context, 31, frame=101, position=(.8, .6), content=jpeg(240),
                         source_session_id='fresh-source-session', state='CARRIED', hand_near=True, speed=.3)
    assert next_state['last_observed']['screenshot_path'] == snapshot['screenshot_path']
    assert runtime.db.count('event_media') == 1


def test_carried_updates_coordinates_but_saves_new_image_only_after_settled_hand_away(context):
    runtime,*_=context
    initial=observe(context,content=jpeg())['last_observed']
    moved=observe(context,1,frame=2,position=(.7,.3),state='CARRIED',speed=.4,content=jpeg(120))
    assert moved['status']=='last_seen'
    assert moved['current_position']==[.7,.3]
    assert moved['last_observed']['screenshot_path']==initial['screenshot_path']
    assert moved['last_observed']['image_status']=='previous_observation'
    observe(context,2,frame=3,position=(.7,.3),hand_near=True,content=jpeg(125))
    waiting=observe(context,3,frame=4,position=(.7,.3),content=jpeg(130))
    assert waiting['last_observed']['screenshot_path']==initial['screenshot_path']
    early=observe(context,7.9,frame=5,position=(.7,.3),content=jpeg(135))
    assert early['last_observed']['screenshot_path']==initial['screenshot_path']
    settled=observe(context,8,frame=6,position=(.7,.3),content=jpeg(140))
    assert settled['last_observed']['screenshot_path']!=initial['screenshot_path']
    assert settled['last_observed']['screenshot_source_frame']==6
    assert not runtime.event_service.media_path(initial['screenshot_path']).exists()
    assert runtime.db.count('movement_events')==0


@pytest.mark.parametrize('missing',['OCCLUDED','LOST'])
def test_missing_keeps_observation_time_image_and_coordinates(context,missing):
    initial=observe(context,content=jpeg())
    state=observe(context,40,frame=9,position=(.9,.9),state=missing,content=jpeg(220))
    assert state['last_seen_at']==initial['last_seen_at']
    assert state['current_position']==initial['current_position']
    assert state['last_observed']['observed_at']==initial['last_seen_at']
    assert state['last_observed']['screenshot_path']==initial['last_observed']['screenshot_path']
    assert state['status']==missing.lower()


def test_snapshot_write_failure_still_saves_last_seen(context,monkeypatch):
    runtime,*_=context
    def fail(_):raise OSError('controlled storage failure')
    monkeypatch.setattr(runtime.event_service,'_atomic_observation_jpeg',fail)
    state=observe(context,content=jpeg())
    assert state['status']=='last_seen' and state['last_seen_at']==START.isoformat()
    assert state['last_observed']['image_status']=='write_failed'
    assert not state['last_observed'].get('screenshot_path')
    assert runtime.db.count('movement_events')==runtime.db.count('event_media')==0


def test_older_callback_cannot_replace_snapshot(context):
    runtime,*_=context
    latest=observe(context,10,frame=10,content=jpeg())
    old=observe(context,0,frame=1,position=(.9,.9),content=jpeg(220))
    assert old['last_seen_at']==latest['last_seen_at']
    assert old['last_observed']['screenshot_path']==latest['last_observed']['screenshot_path']
    assert runtime.db.count('event_media')==1


def test_occlusion_resets_snapshot_stability_and_clears_deferred_reason_after_save(context):
    original=observe(context,content=jpeg())['last_observed']
    observe(context,1,frame=2,position=(.7,.3),state='CARRIED',content=jpeg(110))
    observe(context,2,frame=3,position=(.7,.3),content=jpeg(115))
    lost=observe(context,2.5,frame=4,position=(.7,.3),state='OCCLUDED',content=jpeg(120))
    assert lost['last_observed'].get('snapshot_stable_since') is None
    first=observe(context,40,frame=5,position=(.7,.3),content=jpeg(130))
    assert first['last_observed']['screenshot_path']==original['screenshot_path']
    stable=observe(context,45,frame=6,position=(.7,.3),content=jpeg(140))
    assert stable['last_observed']['screenshot_path']!=original['screenshot_path']
    assert 'snapshot_deferred_reason' not in stable['last_observed']


def test_profile_change_between_callback_validation_and_commit_rejects_new_coordinates_and_image(context,monkeypatch):
    runtime,item,_,_=context
    profile={'status':'ready','profile_version':1,'model_version':'unit-model-version'}
    runtime.db.save('item_recognition_profiles',profile,item['id'])
    identity={**profile,'accepted':True,'item_id':item['id']}
    original=observe(context,content=jpeg(),identity_evidence=identity)
    commit=runtime.db.save_current_state_if_newer
    def interleaved(*args,**kwargs):
        # Deterministic equivalent of a registration commit winning the SQLite
        # lock after Runtime validated the old profile, before this write.
        runtime.db.save('item_recognition_profiles',{'profile_version':2},item['id'])
        return commit(*args,**kwargs)
    monkeypatch.setattr(runtime.db,'save_current_state_if_newer',interleaved)
    assert observe(context,3,frame=4,position=(.7,.3),content=jpeg(220),identity_evidence=identity) is None
    current=runtime.db.get('item_current_state',f"TEST:{item['id']}")
    assert current==original
    assert runtime.db.count('event_media')==1
    assert len(list((runtime.media/'event-images').glob('observed-*.jpg')))==1


def test_retention_protects_current_image_and_reclaims_unreferenced_owner(context):
    runtime,item,_,_=context
    state=observe(context,content=jpeg())
    path=runtime.event_service.media_path(state['last_observed']['screenshot_path'])
    os.utime(path,(1,1))
    assert state['last_observed']['screenshot_path'] not in runtime.retention.preview()['orphan_media']
    runtime.retention.cleanup(trigger='test-current-image')
    runtime.retention._purge_mode_media('TEST',10**9)
    assert path.is_file()
    runtime.db.delete('item_current_state',f"TEST:{item['id']}")
    runtime.retention.cleanup(trigger='test-deleted-state')
    assert not path.exists()
    assert runtime.db.count('event_media')==0


@pytest.mark.parametrize('change',[{'center':[.8,.8]}, {'hand_near':True}, {'state':'CARRIED','speed':.3}, {'source_session_id':'new-source'}])
def test_new_observation_does_not_inherit_old_placement_by_zone_name(context,change):
    runtime,item,camera,_=context
    runtime.db.save('item_current_state',{'item_id':item['id'],'current_camera':camera['id'],
        'current_room':'room','current_zone':'desk','current_position':[.2,.3],
        'last_seen_at':START.isoformat(),'last_confirmed_placed_at':START.isoformat(),
        'evidence_event_id':'old-confirmation','runtime_mode':'TEST','source_type':'video_file',
        'is_simulated':True,'source_session_id':'observation-session','status':'confirmed_placed'},f"TEST:{item['id']}")
    state=observe(context,1,frame=2,**change)
    assert state['status']=='last_seen'
    assert state['evidence_event_id']=='old-confirmation'
    assert state['last_confirmed_placed_at']==START.isoformat()


@pytest.mark.parametrize('status',['occluded','lost','offline'])
def test_equal_last_seen_missing_wins_over_old_confirmation(status):
    event={'event_id':'prior','event_type':'movement','evidence_status':'confirmed','final_status':'confirmed_placed',
           'timestamp_end':START.isoformat(),'room_name':'room','zone_name':'desk','source_type':'video_file',
           'runtime_mode':'TEST','is_simulated':True}
    state={'last_seen_at':START.isoformat(),'status':status,'current_room':'room','current_zone':'desk',
           'source_type':'video_file','runtime_mode':'TEST','is_simulated':True,'evidence_event_id':'prior'}
    result=location_result({'id':'item','name':'fixture'},[event],'TEST',state)
    assert result['status']==status
    assert result['evidence']['event_type']==status
    assert result['last_confirmed']['event_id']=='prior'
    assert '未确认' in result['answer']
