"""Actual SQLite/image lifecycle failures; fixture vectors are not model accuracy."""
import sqlite3
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from apps.api.app.db import Database
from apps.api.app.registration import RegistrationError, RegistrationService


class Encoder:
    model_id = 'atomic-lifecycle-fixture'
    model_version = 'v1'
    dimension = 3

    def health(self):
        return {'available': True, 'model_id': self.model_id, 'model_version': self.model_version}

    def encode(self, crop):
        return [1., 0., 0.]


@pytest.fixture
def registration(tmp_path):
    db = Database(tmp_path / 'test.sqlite', 'TEST')
    service = RegistrationService(db, tmp_path / 'owned-data', tmp_path / 'no-models', Encoder())
    service._suggest = lambda _frame: ([], None)
    item = db.save('items', {'name': 'generated fixture', 'type': 'phone'}, 'fixture')
    for index in range(2):
        frame = np.random.default_rng(index).integers(0, 256, (96, 128, 3), dtype=np.uint8)
        ok, encoded = cv2.imencode('.png', frame)
        assert ok
        ref = service.add(item['id'], encoded.tobytes())
        service.confirm_region(item['id'], ref['id'], [0., 0., 1., 1.], True)
    return service, item['id']


def files(service):
    return {path.name: path.read_bytes() for path in service.folder.iterdir()}


def test_confirm_region_and_profile_invalidation_commit_together(registration):
    service, item_id = registration
    ref = service.db.list('item_reference_images')[0]
    prior = service.db.get('item_recognition_profiles', item_id)
    with service.db.connect() as conn:
        conn.execute("""CREATE TRIGGER reject_profile_update BEFORE UPDATE ON item_recognition_profiles
            BEGIN SELECT RAISE(ABORT, 'controlled profile failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match='controlled profile failure'):
        service.confirm_region(item_id, ref['id'], [.1, .1, .8, .8], True)
    assert service.db.get('item_reference_images', ref['id']) == ref
    assert service.db.get('item_recognition_profiles', item_id) == prior


def test_candidate_validation_precedes_ready_visibility_and_all_feature_writes(registration, monkeypatch):
    service, item_id = registration
    from services.vision.detectors import appearance
    original = appearance.ProfileMatcher
    observed = []

    def validate(items, encoder):
        observed.append(service.db.get('item_recognition_profiles', item_id)['status'])
        assert observed[-1] == 'building'
        assert all(row['status'] == 'region_confirmed' for row in service.db.list('item_reference_images'))
        return original(items, encoder)

    monkeypatch.setattr(appearance, 'ProfileMatcher', validate)
    result = service.build(item_id)
    assert result['registration_status'] == 'ready' and observed == ['building']
    assert result['ready_reference_count'] == 2
    assert all(row['status'] == 'featured' for row in service.db.list('item_reference_images'))


def test_second_encoding_failure_leaves_no_partial_generation(registration, monkeypatch):
    service, item_id = registration
    refs = service.db.list('item_reference_images')
    before = files(service)
    calls = 0

    def fail_second(_crop):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError('controlled second encode failure')
        return [1., 0., 0.]

    monkeypatch.setattr(service.encoder, 'encode', fail_second)
    with pytest.raises(RegistrationError, match='controlled second encode failure'):
        service.build(item_id)
    assert service.db.list('item_reference_images') == refs
    assert files(service) == before
    profile = service.profile(item_id)
    assert profile['registration_status'] == 'failed'
    assert profile['ready_reference_count'] == 0
    assert profile['loaded_profile_version'] is None


def test_ready_database_failure_rolls_back_generated_crops_and_feature_rows(registration):
    service, item_id = registration
    refs = service.db.list('item_reference_images')
    before = files(service)
    with service.db.connect() as conn:
        conn.execute("""CREATE TRIGGER reject_ready BEFORE UPDATE ON item_recognition_profiles
            WHEN NEW.status='ready' BEGIN SELECT RAISE(ABORT, 'controlled ready failure'); END""")
    with pytest.raises(RegistrationError, match='controlled ready failure'):
        service.build(item_id)
    assert service.db.list('item_reference_images') == refs
    assert files(service) == before
    assert service.profile(item_id)['registration_status'] == 'failed'


def test_local_matcher_validation_does_not_claim_formal_camera_loading(registration):
    service, item_id = registration
    result = service.build(item_id)
    assert result['registration_status'] == 'ready'
    assert result['loaded_profile_version'] is None and result['loaded_camera_ids'] == []
    assert result['validated_profile_version'] == result['profile_version']
    pending = SimpleNamespace(loaded_profile_versions={item_id: result['profile_version']},
                              _profile_revision=2, _applied_profile_revision=1)
    assert service.profile(item_id, {'camera': pending})['loaded_profile_version'] is None
    pending._applied_profile_revision = 2
    loaded = service.profile(item_id, {'camera': pending})
    assert loaded['loaded_profile_version'] == result['profile_version']
    assert loaded['loaded_camera_ids'] == ['camera']


def test_cancelled_build_clears_staged_files_and_never_leaves_ready_generation(registration, monkeypatch):
    service, item_id = registration
    before = files(service)
    class ControlledCancel(BaseException):
        pass
    calls = 0
    def cancelled(_crop):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ControlledCancel('controlled build cancellation')
        return [1., 0., 0.]
    monkeypatch.setattr(service.encoder, 'encode', cancelled)
    with pytest.raises(ControlledCancel):
        service.build(item_id)
    assert files(service) == before
    assert service.profile(item_id)['registration_status'] == 'failed'
    assert service.profile(item_id)['loaded_profile_version'] is None


@pytest.mark.parametrize('fails', [False, True])
def test_build_route_invalidates_formal_profiles_before_encoding_and_on_failure(tmp_path, monkeypatch, fails):
    from apps.api.tests.test_authenticity_storage import client_for
    with client_for(tmp_path) as client:
        runtime = client.app.state.runtime
        item = client.post('/api/items', json={'name': 'route lifecycle fixture', 'type': 'phone'}).json()
        service = runtime.registration
        service._encoder = Encoder()
        service._suggest = lambda _frame: ([], None)
        ok, encoded = cv2.imencode('.png', np.random.default_rng(19).integers(0, 256, (96, 128, 3), np.uint8))
        assert ok
        ref = service.add(item['id'], encoded.tobytes())
        service.confirm_region(item['id'], ref['id'], [0., 0., 1., 1.], True)
        service.build(item['id'])
        reload_states = []
        engine = SimpleNamespace(loaded_profile_versions={item['id']: service.profile(item['id'])['profile_version']},
                                 _profile_revision=1, _applied_profile_revision=1)
        def reload_profiles(items, _encoder=None, enable=False):
            profile = next(row['appearance_profile'] for row in items if row['id'] == item['id'])
            reload_states.append(profile['status'])
            engine.loaded_profile_versions = {item['id']: profile['profile_version']} if profile['status'] == 'ready' else {}
        engine.reload_profiles = reload_profiles
        runtime.engines['lifecycle-callback-fixture'] = engine
        def encode(_crop):
            assert reload_states == ['building']
            assert engine.loaded_profile_versions == {}
            if fails:
                raise ValueError('controlled route encoding failure')
            return [1., 0., 0.]
        monkeypatch.setattr(service.encoder, 'encode', encode)
        try:
            response = client.post(f"/api/items/{item['id']}/profile/build")
            assert response.status_code == (422 if fails else 200)
            assert reload_states == ['building', 'failed' if fails else 'ready']
            if fails:
                assert engine.loaded_profile_versions == {}
                assert service.profile(item['id'], runtime.engines)['loaded_profile_version'] is None
        finally:
            runtime.engines.pop('lifecycle-callback-fixture', None)
