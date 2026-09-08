"""Real isolated SQLite/media retention; JPEGs are generated test fixtures.

No physical camera, detector accuracy, or confirmed placement is claimed.
"""
import hashlib

import pytest

from apps.api.tests.test_observation_snapshot_storage import context, observe, jpeg


BINDING = ('screenshot_path','screenshot_sha256','screenshot_source_frame','screenshot_observed_at',
           'screenshot_source_session_id','screenshot_camera_id','screenshot_position')


def failed_input(runtime,monkeypatch,failure):
    if failure == 'io_error':
        def fail(_content):
            raise OSError('explicit isolated disk-write failure')
        monkeypatch.setattr(runtime.event_service,'_atomic_observation_jpeg',fail)
        return jpeg(210)
    return b'invalid-jpeg-fixture' if failure == 'invalid_jpeg' else None


@pytest.mark.parametrize('failure',['io_error','invalid_jpeg','missing_jpeg'])
def test_failed_replacement_preserves_old_binding_through_retention_then_success_replaces(context,monkeypatch,failure):
    runtime,item,_,client = context
    original = observe(context,content=jpeg(80))['last_observed']
    path = runtime.event_service.media_path(original['screenshot_path'])
    original_bytes = path.read_bytes()
    observe(context,1,frame=2,position=(.7,.3),state='CARRIED',speed=.4,content=jpeg(110))
    observe(context,2,frame=3,position=(.7,.3),content=jpeg(130))
    # Production now waits for five seconds of settled observations before
    # attempting a replacement; the failure must hit that write, not a deferred
    # candidate. These controlled timestamps are not physical release evidence.
    for second in range(3,7):
        waiting = observe(context,second,frame=second+1,position=(.7,.3),content=jpeg(140))
        assert waiting['last_observed']['screenshot_path'] == original['screenshot_path']
    with monkeypatch.context() as failed:
        content = failed_input(runtime,failed,failure)
        replacement = observe(context,7,frame=8,position=(.7,.3),content=content)
    preserved = replacement['last_observed']
    assert replacement['status'] == 'last_seen' and replacement['current_position'] == [.7,.3]
    assert {key:preserved.get(key) for key in BINDING} == {key:original.get(key) for key in BINDING}
    assert preserved['source_frame'] == 8 and preserved['screenshot_source_frame'] == 1
    assert preserved['image_status'] == 'previous_observation' and preserved['image_error']
    assert '较早' in preserved['snapshot_deferred_reason']
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original['screenshot_sha256']
    assert path.read_bytes() == original_bytes
    plan = runtime.retention.preview()
    assert not plan['media_ids'] and original['screenshot_path'] not in plan['orphan_media']
    runtime.retention.cleanup(trigger='isolated-write-failure')
    assert path.is_file() and runtime.db.count('event_media') == 1
    displayed = client.get(f'/api/items/{item["id"]}/last-location').json()['evidence']
    assert displayed['screenshot_path'] == original['screenshot_path']
    assert displayed['source_frame'] == 8 and displayed['screenshot_source_frame'] == 1
    assert displayed['image_status'] == 'previous_observation'
    recovered = observe(context,8,frame=9,position=(.7,.3),content=jpeg(220))['last_observed']
    assert recovered['image_status'] == 'available' and recovered['image_error'] is None
    assert recovered['screenshot_source_frame'] == 9 and recovered['screenshot_path'] != original['screenshot_path']
    assert 'snapshot_deferred_reason' not in recovered
    assert not path.exists()  # Only after new image/current-state commit succeeds.
    assert runtime.db.count('event_media') == 1 and runtime.db.count('movement_events') == 0


@pytest.mark.parametrize('failure',['io_error','invalid_jpeg','missing_jpeg'])
def test_first_failed_observation_does_not_invent_retained_image(context,monkeypatch,failure):
    runtime,*_ = context
    content = failed_input(runtime,monkeypatch,failure)
    result = observe(context,content=content)
    observed = result['last_observed']
    assert observed['image_status'] in {'not_available','write_failed'}
    assert not observed.get('screenshot_path') and not observed.get('screenshot_sha256')
    assert runtime.db.count('event_media') == runtime.db.count('movement_events') == 0
