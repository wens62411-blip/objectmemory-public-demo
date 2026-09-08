"""Actual TEST SQLite/media chronology, generated frames not physical evidence."""
from datetime import datetime, timedelta, timezone
from copy import deepcopy

import cv2
import numpy as np
import pytest

from apps.api.tests.test_authenticity_storage import client_for, add_item_camera, complete_event, record_event


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize('case,expected', [
    ('legacy_untimed', 'nearby'), ('legacy_same_time', 'nearby'),
    ('legacy_older_time', 'not_established'), ('legacy_prior_frame', 'not_established'),
    ('legacy_other_camera', 'not_established'), ('legacy_other_session', 'not_established'),
    ('legacy_bound_new_event', 'not_established'), ('untimed_co_moving', 'not_established'),
    ('current_co_moving', 'co_moving'),
])
def test_current_time_projection_preserves_only_unadvanced_legacy_proximity(case, expected):
    from apps.api.app.search import location_result
    timestamp = '2026-09-05T12:00:00Z'
    observed = {'holding_status': 'possibly_held', 'interaction': {'hand_near': True}}
    state = {'status': 'last_seen', 'last_seen_at': timestamp, 'current_zone': 'new desk',
             'current_camera': 'camera', 'source_session_id': 'session', 'last_observed': observed}
    events = []
    if case == 'legacy_same_time': observed['observed_at'] = timestamp
    if case == 'legacy_older_time': observed['observed_at'] = '2026-09-05T11:59:50Z'
    if case == 'legacy_prior_frame': observed['source_frame'] = 5
    if case == 'legacy_other_camera': observed['camera_id'] = 'earlier-camera'
    if case == 'legacy_other_session': observed['source_session_id'] = 'earlier-session'
    if case == 'legacy_bound_new_event':
        state['evidence_event_id'] = 'movement'
        events = [{'id': 'movement', 'event_id': 'movement', 'event_type': 'movement',
                   'camera_id': 'camera', 'source_session_id': 'session', 'timestamp_end': timestamp,
                   'source_frame_end': 90, 'final_status': 'position_changed'}]
    if case in {'untimed_co_moving', 'current_co_moving'}:
        observed.update({'holding_status': 'co_moving', 'interaction': {
            'holding_status': 'co_moving', 'hand_near': True, 'co_motion_confirmed': True}})
        if case == 'current_co_moving': observed['observed_at'] = timestamp
    original = deepcopy(state)
    result = location_result({'id': 'item', 'name': 'fixture'}, events, current_state=state)
    value = result['last_observed']
    assert value['holding_status'] == expected
    assert value['timestamp_start'] == timestamp and value['zone_name'] == 'new desk'
    if expected == 'nearby':
        assert value['interaction']['reason'] == 'legacy_proximity_only'
        assert value['interaction']['release_observed'] is False
    elif expected == 'not_established':
        assert value['interaction'] is None
    else:
        assert value['interaction']['co_motion_confirmed'] is True
    assert state == original  # Query projection must not migrate persisted state.


def observation(client, item, camera, second, *, state='VISIBLE_STATIC', zone='old desk'):
    ok, image = cv2.imencode('.jpg', np.full((48, 64, 3), 70, np.uint8))
    assert ok
    return client.app.state.runtime.event_service.observe({
        'item_id': item['id'], 'camera_id': camera['id'], 'source_session_id': 'source-session-a',
        'last_seen': (START + timedelta(seconds=second)).isoformat(), 'source_frame': second,
        'observation_source_frame': second, 'state': state, 'center': [.2, .3],
        'confidence': .9, 'zone_name': zone,
    }, observation_verified=True, observation_jpeg=image.tobytes())


@pytest.mark.parametrize('status', ['OCCLUDED', 'LOST', 'OFFLINE', 'EXITED_VIEW'])
def test_missing_after_new_confirmation_is_not_hidden_by_older_observation_snapshot(tmp_path, status):
    with client_for(tmp_path) as client:
        item, camera = add_item_camera(client)
        first = observation(client, item, camera, 5)
        confirmed = record_event(client, item, camera, 1)
        current = observation(client, item, camera, 13, state=status)
        assert current['last_seen_at'] == confirmed['timestamp_end']
        result = client.get(f"/api/items/{item['id']}/last-location").json()
        assert client.app.state.runtime.db.get('item_current_state', f"TEST:{item['id']}") == current
        assert result['status'] == status.lower()
        assert '未确认' in result['answer']
        assert confirmed['to_zone'] in result['answer']
        assert result['evidence']['event_type'] == status.lower()
        assert result['last_confirmed']['event_id'] == confirmed['event_id']
        # The older independent photograph is never relabelled as a new frame.
        assert result['last_observed']['screenshot_source_frame'] == 5
        assert result['last_observed']['screenshot_observed_at'] == first['last_seen_at']
        runtime = client.app.state.runtime
        runtime.retention.cleanup(trigger='current-evidence-chronology')
        assert runtime.event_service.media_path(first['last_observed']['screenshot_path']).is_file()
        assert runtime.db.get('events', confirmed['id']) is not None


def test_new_position_change_beats_older_sidecar_and_old_confirmed_placement(tmp_path):
    with client_for(tmp_path) as client:
        item, camera = add_item_camera(client)
        observation(client, item, camera, 5)
        prior = record_event(client, item, camera, 1)
        event = complete_event(client, item, camera, 2)
        event.update({'detection_mode': 'experimental', 'detector_backend': 'nanodet'})
        changed = record_event(client, item, camera, 2, event)
        assert changed['final_status'] == 'position_changed'
        result = client.get(f"/api/items/{item['id']}/last-location").json()
        assert result['status'] == 'last_seen'
        assert result['evidence']['zone_name'] == changed['to_zone']
        assert result['evidence']['timestamp_start'] == changed['timestamp_end']
        assert result['last_observed']['source_frame'] == changed['source_frame_end']
        assert result['last_confirmed']['event_id'] == prior['event_id']
        assert '尚未有新的确认放置证据' in result['answer']


def test_delayed_confirmation_does_not_replace_newer_observed_state(tmp_path):
    with client_for(tmp_path) as client:
        item, camera = add_item_camera(client)
        newest = observation(client, item, camera, 30, zone='new observed shelf')
        prior = record_event(client, item, camera, 1)
        result = client.get(f"/api/items/{item['id']}/last-location").json()
        assert result['status'] == 'last_seen'
        assert result['evidence']['zone_name'] == 'new observed shelf'
        assert result['evidence']['timestamp_start'] == newest['last_seen_at']
        assert result['evidence']['screenshot_source_frame'] == 30
        assert result['last_confirmed']['event_id'] == prior['event_id']


@pytest.mark.parametrize('failure', ['empty_result', 'io_error'])
def test_failed_clip_keeps_independent_observation_snapshot_and_no_half_history(tmp_path, monkeypatch, failure):
    from services.vision.events.media import ClipResult, EventMediaWriter
    with client_for(tmp_path) as client:
        item, camera = add_item_camera(client)
        runtime = client.app.state.runtime
        before = observation(client, item, camera, 13, zone='observed shelf')
        media_before = runtime.db.list('event_media')
        path = runtime.event_service.media_path(before['last_observed']['screenshot_path'])
        image_before = path.read_bytes()

        def fail_clip(*_args, **_kwargs):
            if failure == 'io_error':
                raise OSError('controlled clip failure')
            return ClipResult(None, 0, 0., None, 'controlled codec unavailable')

        monkeypatch.setattr(EventMediaWriter, 'write_clip', fail_clip)
        assert record_event(client, item, camera, 1) is False
        assert runtime.db.get('item_current_state', f"TEST:{item['id']}") == before
        assert runtime.db.count('movement_events') == 0
        assert runtime.db.list('event_media') == media_before
        assert len(list((runtime.media / 'event-images').iterdir())) == 1
        assert not list((runtime.media / 'event-clips').iterdir())
        runtime.retention.cleanup(trigger='failed-clip-current-snapshot')
        assert path.read_bytes() == image_before
        result = client.get(f"/api/items/{item['id']}/last-location").json()
        assert result['evidence']['screenshot_path'] == before['last_observed']['screenshot_path']
        assert result['evidence']['image_status'] == 'available'
        assert result['last_confirmed'] is None
