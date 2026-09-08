"""Real read-only API/SQLite contract tests; snapshots are explicit fixtures.

No detector, camera, physical-phone accuracy, or location is simulated as real.
The cached engine fixtures only exercise diagnostic selection and wording.
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app


@pytest.fixture
def context(tmp_path):
    app = create_app(tmp_path / 'hint-contract', testing=True)
    runtime = app.state.runtime
    with TestClient(app) as client:
        client.get('/api/session')
        item = runtime.db.save('items', {'name': 'diagnostic fixture', 'type': 'phone'}, 'target')
        runtime.db.save('item_recognition_profiles', {
            'item_id': item['id'], 'status': 'ready', 'profile_version': 6,
            'model_id': 'fixture-only', 'model_version': 'fixture-v1',
            'dimension': 3, 'embeddings': [[1., 0., 0.]],
        }, item['id'])
        yield SimpleNamespace(client=client, runtime=runtime, item=item)
        runtime.engines.clear()


def engine(context, camera_id='loaded', *, loaded=True, snapshot=True,
           candidates=(), model_error=None, snapshot_version=6):
    runtime = context.runtime
    runtime.db.save('cameras', {'name': camera_id, 'source_type': 'video',
        'source': 'never-opened-fixture.avi', 'enabled': False, 'runtime_mode': 'TEST'}, camera_id)
    model = {'available': not bool(model_error), 'status': 'failed' if model_error else 'ready',
             'error': model_error, 'model_id': 'fixture-only'}
    cached = {'jpeg': b'private-frame-must-not-leak', 'created_at': 123,
        'source_session_id': f'session-{camera_id}', 'source_frame': 31,
        'source_timestamp': '2026-09-06T00:00:00+00:00', 'runtime_mode': 'TEST',
        'model': model, 'loaded_profiles': [{'item_id': 'target', 'profile_version': snapshot_version}],
        'candidates': list(candidates)} if snapshot else None
    value = SimpleNamespace(
        loaded_profile_versions={'target': 6} if loaded else {},
        recognition_snapshot=lambda: deepcopy(cached),
        health=lambda: {'status': 'ready' if snapshot else 'offline',
                        'detection_mode': 'experimental', 'recognition_error': model_error,
                        'appearance': model, 'detector': {'available': True}},
    )
    runtime.engines[camera_id] = value
    return value


def query(context):
    response = context.client.get('/api/items/target/last-location')
    assert response.status_code == 200, response.text
    value = response.json()
    assert value.get('evidence') is None
    assert value.get('current_state') is None
    assert context.runtime.db.count('item_current_state') == 0
    assert context.runtime.db.count('movement_events') == 0
    assert context.runtime.db.count('event_media') == 0
    assert 'jpeg' not in value['observation_hint']
    assert 'private-frame' not in response.text
    return value['observation_hint']['code']


def test_offline_is_not_a_negative_phone_detection(context):
    assert query(context) == 'no_live_source'


def test_unbuilt_profile_is_not_a_negative_phone_detection(context):
    context.runtime.db.save('item_recognition_profiles', {'status': 'images_saved'}, 'target')
    engine(context)
    assert query(context) == 'profile_not_ready'


def test_unloaded_current_profile_is_explicit(context):
    engine(context, loaded=False)
    assert query(context) == 'profile_not_loaded'


@pytest.mark.parametrize('reason', ['insufficient_detail', 'crop_too_small'])
def test_small_phone_candidate_without_best_identity_is_explained(context, reason):
    engine(context, candidates=[{'category': 'cell phone', 'accepted': False, 'rejection_reason': reason}])
    assert query(context) == 'object_too_small'


@pytest.mark.parametrize('candidates', [[], [{'category': 'bed', 'accepted': False,
    'rejection_reason': 'no_ready_profiles_for_category'}]])
def test_no_target_category_reports_candidate_gap_not_identity_failure(context, candidates):
    engine(context, candidates=candidates)
    assert query(context) == 'no_target_candidate'


def test_same_category_wrong_registered_identity_is_uncertain(context):
    engine(context, candidates=[{'category': 'cell phone', 'accepted': True,
        'best_item_id': 'another-phone', 'item_id': 'another-phone'}])
    assert query(context) == 'identity_uncertain'


def test_geometry_hint_does_not_claim_neural_category_or_identity(context):
    engine(context, candidates=[{'category': 'cell phone', 'accepted': False,
        'category_evidence': 'geometry_only', 'rejection_reason': 'shape_requires_category_confirmation'}])
    assert query(context) == 'shape_candidate_only'


def test_accepted_target_without_state_waits_for_verified_continuity(context):
    engine(context, candidates=[{'category': 'cell phone', 'accepted': True,
        'best_item_id': 'target', 'item_id': 'target'}])
    assert query(context) == 'waiting_continuity'


def test_other_unloaded_camera_cannot_supply_accepted_target(context):
    engine(context)
    engine(context, 'unloaded-other', loaded=False, candidates=[{
        'category': 'cell phone', 'accepted': True, 'item_id': 'target', 'best_item_id': 'target'}])
    assert query(context) == 'no_target_candidate'


def test_stale_snapshot_profile_cannot_supply_accepted_target(context):
    engine(context, snapshot_version=5, candidates=[{
        'category': 'cell phone', 'accepted': True, 'item_id': 'target', 'best_item_id': 'target'}])
    assert query(context) == 'profile_not_loaded'


def test_loaded_camera_offline_cannot_borrow_unloaded_camera_frame(context):
    engine(context, snapshot=False)
    engine(context, 'unloaded-other', loaded=False)
    assert query(context) == 'no_live_source'


def test_model_failure_is_not_no_phone_candidate(context):
    engine(context, model_error='fixture failed, no private path allowed')
    assert query(context) == 'model_unavailable'


def test_read_requests_never_build_profile_or_start_inference(context, monkeypatch):
    engine(context)
    def forbidden(*args, **kwargs):
        pytest.fail('Read-only diagnostic must not activate, build, or open a camera')
    monkeypatch.setattr(context.runtime, 'refresh_recognition', forbidden)
    monkeypatch.setattr(context.runtime, 'start', forbidden)
    monkeypatch.setattr(context.runtime.registration, 'build', forbidden)
    before = context.runtime.db.get('item_recognition_profiles', 'target')
    for _ in range(3):
        assert query(context) == 'no_target_candidate'
    assert context.runtime.db.get('item_recognition_profiles', 'target') == before
