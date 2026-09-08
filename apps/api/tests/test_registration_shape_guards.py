"""Registration suggestion/wording guards using stubs, not model accuracy.

Plan: an auxiliary geometry proposal must neither certify a neural category
nor erase successful primary proposals when auxiliary processing fails.
No HTTP server, model session, camera, or real database is opened by these tests.
"""
from types import SimpleNamespace

import numpy as np
import pytest

from apps.api.app.registration import RegistrationService
from services.vision.detectors.base import Detection


class RegistrationDBStub:
    def __init__(self, proposals=()):
        self.proposals = list(proposals)

    def get(self, table, row_id):
        if table == 'settings':
            return {'value': {'phone_shape_enabled': True}}
        if table == 'items':
            return {'id': row_id, 'type': 'phone'}
        if table == 'item_recognition_profiles':
            return {'item_id': row_id, 'status': 'ready', 'profile_version': 1,
                    'reference_ids': ['ref'], 'embeddings': []}
        pytest.fail(f'Unexpected table read: {table}')

    def list(self, table, filters, limit=1000):
        if table == 'item_recognition_profiles':
            return [{'id': row_id, **self.get(table, row_id)} for row_id in filters['id']][:limit]
        assert table == 'item_reference_images'
        item_ids = filters['item_id'] if isinstance(filters['item_id'], list) else [filters['item_id']]
        return [{'id': 'ref', 'item_id': item_id,
                 'region_confirmed': True, 'suggested_regions': self.proposals} for item_id in item_ids][:limit]


@pytest.mark.parametrize('category_evidence,expects_warning', [
    ('geometry_only', True),
    ('model', False),
])
def test_only_model_phone_proposal_can_clear_missing_category_warning(
    tmp_path, category_evidence, expects_warning,
):
    db = RegistrationDBStub([{'label': 'cell phone', 'category_evidence': category_evidence}])
    service = RegistrationService(db, tmp_path / 'data', tmp_path / 'absent-models')

    profile = service.profile('registered-phone')

    assert bool(profile['quality_warnings']) is expects_warning
    assert profile['registration_status'] == 'ready'
    assert profile['profile_version'] == 1
    assert service._encoder is None


def test_auxiliary_suggestion_failure_preserves_primary_model_proposals(tmp_path, monkeypatch):
    service = RegistrationService(RegistrationDBStub(), tmp_path / 'data', tmp_path / 'absent-models')
    primary = Detection('generic:book:1', 'book', (10, 20, 60, 40), (40., 40.), .8,
                        'experimental', metadata={'backend': 'primary-detector-stub'})
    service._suggestion_detector = SimpleNamespace(
        health=lambda: {'available': True}, detect=lambda frame: [primary],
    )

    def fail_auxiliary(*_args, **_kwargs):
        raise ValueError('auxiliary pixel limit exceeded')

    monkeypatch.setattr('services.vision.detectors.phone_shape.PhoneShapeProposer.supplement', fail_auxiliary)
    suggestions, error = service._suggest(np.zeros((100, 200, 3), dtype=np.uint8))

    assert len(suggestions) == 1
    assert suggestions[0]['label'] == 'book'
    assert suggestions[0]['bbox'] == [.05, .2, .3, .4]
    assert suggestions[0]['category_evidence'] == 'model'
    assert suggestions[0]['score'] == .8
    assert error and 'auxiliary pixel limit exceeded' in error
    assert service._encoder is None
