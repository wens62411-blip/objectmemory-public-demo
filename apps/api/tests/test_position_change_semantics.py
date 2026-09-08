"""Real TEST SQLite/media recording with synthetic source frames.

This proves final-status/retention contracts, not physical hand/surface detection.
"""
from datetime import datetime,timedelta

import pytest

from apps.api.tests.test_authenticity_storage import client_for,add_item_camera,complete_event,record_event
from services.vision.hand_interaction import HandObjectInteractionTracker


def release_for(client, event):
    """Actual interaction state machine with synthetic input, not physical proof."""
    tracker=HandObjectInteractionTracker({})
    start=event['source_frame_start']
    session=event['source_session_id']
    # Use actual 5 Hz temporal updates over 6.2 seconds, not ten frames falsely
    # labelled as a five-second release. All data stays in the TEST database.
    for offset in range(32):
        x=.15+min(offset,4)*.025
        hand_x=x+(.1 if offset<5 else .2)
        hands=[{'hand_id':'generated-hand-track','center':[hand_x,.44],
                'bbox':[hand_x-.03,.4,.06,.08], 'stable':True,
                'landmarks':[[hand_x,.44] for _ in range(21)]}]
        result=tracker.update('generated-item',[x,.4,.08,.08],hands,offset*.2,
                              session,start+offset)
    assert result['release_observed'] is True
    ended_at = datetime.fromisoformat(event['source_timestamp_start']) + timedelta(seconds=6.2)
    event['source_frame_end'] = start + 31
    event['source_timestamp_end'] = ended_at.isoformat()
    event['placement_evidence'].update({'source_frame': start + 31, 'source_timestamp': ended_at.timestamp()})
    runtime = client.app.state.runtime
    source = runtime.db.get('source_sessions', session)
    source.update({'last_frame': start + 31, 'last_frame_at': ended_at.isoformat()})
    runtime.db.save('source_sessions', source, session)
    assert result['source_frame']==event['source_frame_end']
    return result


@pytest.mark.parametrize('support',[
    {}, {'support_surface_confirmed':True},
    {'support_surface_confirmed':True,'hand_model_healthy':True},
    {'support_surface_confirmed':True,'hand_model_healthy':True,'hand_near':True},
    {'support_surface_confirmed':True,'hand_model_healthy':False,'hand_near':False},
])
def test_photo_motion_without_complete_support_is_not_confirmed_placement(tmp_path,support):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client)
        runtime=client.app.state.runtime
        event=complete_event(client,item,camera)
        event.update({'detection_mode':'experimental','detector_backend':'nanodet',
                      'placement_evidence':{**event['placement_evidence'],**support}})
        saved=record_event(client,item,camera,event=event)
        assert saved and saved['evidence_status']=='confirmed'
        assert saved['final_status']=='position_changed'
        current=runtime.db.get('item_current_state',f"TEST:{item['id']}")
        assert current['status']=='last_seen'
        assert current['last_confirmed_placed_at'] is None
        assert current['last_confirmed_placement'] is None
        result=client.get(f"/api/items/{item['id']}/last-location").json()
        assert result['last_confirmed'] is None
        assert result['evidence']['event_type']=='last_seen'
        assert '确认放在' not in result['answer']
        if runtime.retention._cleanup_thread:runtime.retention._cleanup_thread.join(10)
        assert runtime.retention.preview()['event_ids']==[]


def test_healthy_hand_model_and_surface_without_temporal_release_is_only_position_change(tmp_path):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client)
        event=complete_event(client,item,camera)
        event.update({'detection_mode':'experimental','detector_backend':'nanodet',
                      'placement_evidence':{**event['placement_evidence'],
                        'support_surface_confirmed':True,'hand_model_healthy':True,'hand_near':False}})
        saved=record_event(client,item,camera,event=event)
        assert saved['final_status']=='position_changed'
        assert client.get(f"/api/items/{item['id']}/last-location").json()['last_confirmed'] is None


def test_following_observation_does_not_reclassify_position_change_as_confirmed_placement(tmp_path):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client)
        runtime=client.app.state.runtime
        event=complete_event(client,item,camera)
        event.update({'detection_mode':'experimental','detector_backend':'nanodet'})
        saved=record_event(client,item,camera,event=event)
        assert saved['final_status']=='position_changed'
        stamp=(datetime.fromisoformat(saved['timestamp_end'])+timedelta(seconds=2)).isoformat()
        current=runtime.event_service.observe({'item_id':item['id'],'camera_id':camera['id'],
            'source_session_id':saved['source_session_id'],'source_frame':100,'last_seen':stamp,
            'state':'VISIBLE_STATIC','center':[.7,.5],'zone_name':saved['to_zone'], 'confidence':.9},
            observation_verified=True)
        assert current['last_confirmed_placement'] is None
        assert current['last_confirmed_placed_at'] is None


def test_actual_temporal_interaction_with_visible_release_allows_photo_placement(tmp_path):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client)
        event=complete_event(client,item,camera)
        interaction = release_for(client, event)
        event.update({'detection_mode':'experimental','detector_backend':'nanodet',
                      'placement_evidence':{**event['placement_evidence'],
                        'support_surface_confirmed':True,'hand_model_healthy':True,'hand_near':False,
                        'hand_interaction':interaction}})
        saved=record_event(client,item,camera,event=event)
        assert saved['final_status']=='confirmed_placed'
        assert saved['placement_evidence']['hand_interaction']['release_end_frame']==saved['source_frame_end']
        assert saved['placement_evidence']['source_frame'] == saved['source_frame_end']
        assert saved['placement_evidence']['hand_interaction']['release_stable_seconds'] >= 5


@pytest.mark.parametrize('change',[
    {'release_observed':False}, {'holding_status':'nearby'}, {'hand_id':''},
    {'hand_model_healthy':False}, {'item_detected':False}, {'source_session_id':'previous-session'},
    {'source_frame':18}, {'release_end_frame':18}, {'release_start_frame':19},
    {'co_motion_start_frame':9}, {'co_motion_end_frame':16}, {'co_motion_start_frame':True},
    {'co_motion_frames':0}, {'co_motion_frames':True},
])
def test_incomplete_or_conflicting_release_payload_cannot_confirm_photo_placement(tmp_path,change):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client)
        event=complete_event(client,item,camera)
        interaction={**release_for(client, event),**change}
        event.update({'detection_mode':'experimental','detector_backend':'nanodet',
                      'placement_evidence':{**event['placement_evidence'],
                        'support_surface_confirmed':True,'hand_model_healthy':True,'hand_near':False,
                        'hand_interaction':interaction}})
        saved=record_event(client,item,camera,event=event)
        assert saved['evidence_status']=='confirmed'  # actual movement evidence still valid
        assert saved['final_status']=='position_changed'
        current=client.app.state.runtime.db.get('item_current_state',f"TEST:{item['id']}")
        assert current['last_confirmed_placed_at'] is None
        assert current['last_confirmed_placement'] is None
        assert client.get(f"/api/items/{item['id']}/last-location").json()['last_confirmed'] is None


def test_recent_two_changes_preserve_older_confirmed_placement_dependency(tmp_path):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client)
        runtime=client.app.state.runtime
        old=record_event(client,item,camera,index=1)
        changes=[]
        for index in (2,3,4):
            event=complete_event(client,item,camera,index)
            event.update({'detection_mode':'experimental','detector_backend':'nanodet'})
            changes.append(record_event(client,item,camera,index,event))
            if runtime.retention._cleanup_thread:runtime.retention._cleanup_thread.join(10)
        runtime.retention.cleanup(trigger='test-position-change-limit')
        assert runtime.db.get('events',changes[0]['id']) is None
        assert runtime.db.get('events',changes[1]['id']) is not None
        assert runtime.db.get('events',changes[2]['id']) is not None
        current=runtime.db.get('item_current_state',f"TEST:{item['id']}")
        assert current['evidence_event_id']==changes[-1]['event_id']
        assert current['last_confirmed_placed_at']==old['timestamp_end']
        assert current['last_confirmed_placement']['event_id']==old['event_id']
        assert runtime.db.get('events',old['id']) is not None
        result=client.get(f"/api/items/{item['id']}/last-location").json()
        assert result['last_confirmed']['event_id']==old['event_id']
        assert result['evidence']['event_type']=='last_seen'
        assert result['evidence']['zone_name']==changes[-1]['to_zone']
