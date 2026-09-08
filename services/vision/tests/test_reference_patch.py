"""Synthetic descriptor/registration contracts; not phone-recognition proof."""
from copy import deepcopy
import hashlib
import io

import cv2
import numpy as np
import pytest
from PIL import Image

from services.vision.detectors.appearance import DIMENSION
from services.vision.detectors.reference_patch import ReferencePatchProposer, ReferencePatchSettings, _similarity


def basis(indices):
    vectors = np.zeros((len(indices), DIMENSION), np.float32)
    vectors[np.arange(len(indices)), indices] = 1
    return vectors


class PatchEncoder:
    model_id = 'test-descriptor-model'
    model_version = 'test-v1'
    dimension = DIMENSION
    available = True

    def __init__(self): self.calls = []

    def health(self): return {'available': self.available}

    def encode_patches(self, image):
        h, w = image.shape[:2]
        self.calls.append((h, w))
        yy, xx = np.mgrid[:4, :4]
        if h <= 128:
            features = basis(np.arange(16) % 8 if h == 64 else np.full(16, 20))
            size = (w/4, h/4)
            xy = np.column_stack(((xx.ravel()+.5)*size[0], (yy.ravel()+.5)*size[1]))
        else:
            features = basis(np.arange(16) % 8 if w == 512 else np.full(16, 20))
            size = (16, 16)
            xy = np.column_stack(((xx.ravel()+.5)*16+100, (yy.ravel()+.5)*16+80))
        return features, xy.astype(np.float32), size


def registration(tmp_path, item_id='one'):
    tmp_path.mkdir(parents=True, exist_ok=True)
    content = cv2.imencode('.png', np.full((128, 128, 3), 20, np.uint8))[1].tobytes()
    path = tmp_path/f'{item_id}.png'
    path.write_bytes(content)
    normalized = io.BytesIO()
    Image.open(io.BytesIO(content)).convert('RGB').save(normalized, format='JPEG', quality=95)
    display = tmp_path/f'{item_id}-display.jpg'
    display.write_bytes(normalized.getvalue())
    return {'id': item_id, 'name': 'Fixture phone', 'type': 'phone',
            'appearance_profile': {'status': 'ready', 'profile_version': 1,
                'model_id': PatchEncoder.model_id, 'model_version': PatchEncoder.model_version,
                'dimension': DIMENSION, 'embeddings': basis([0]).tolist(), 'reference_ids': ['ref-'+item_id]},
            'reference_images': [{'id': 'ref-'+item_id, 'item_id': item_id,
                'path': '/media/registered-items/'+display.name,
                'original_path': '/media/registered-items/'+path.name,
                'sha256': hashlib.sha256(content).hexdigest(),
                'region': [.25, .25, .5, .5], 'region_confirmed': True}]}


def test_reference_proposal_never_claims_identity_or_uses_query_roi(tmp_path):
    item = registration(tmp_path)
    encoder = PatchEncoder()
    proposer = ReferencePatchProposer([item], encoder, tmp_path)
    assert proposer.health()['loaded_profile_versions'] == {'one': 1}
    assert len(encoder.calls) == 2
    rows = proposer.detect(np.zeros((256, 512, 3), np.uint8))
    assert len(rows) == 1
    assert rows[0]['bbox'] == [100., 80., 64., 64.]
    assert rows[0]['best_item_id'] == 'one' and rows[0]['item_id'] is None
    assert rows[0]['accepted'] is False and rows[0]['proposal_only'] is True
    assert rows[0]['identity_confirmed'] is False and rows[0]['observation_evidence'] is False
    assert rows[0]['patch_count'] == 16 and rows[0]['unique_reference_matches'] == 8
    assert encoder.calls[2:] == [(256, 512), (256, 256), (256, 256), (256, 256)]
    proposer.detect(np.zeros((256, 512, 3), np.uint8))
    assert len(encoder.calls) == 10  # Reference features stay cached.


@pytest.mark.parametrize('path', ['/media/registered-items/../escape.png', '/media/registered-items/../../x',
                                '/media/registered-items/C:/escape.png', 'https://example.invalid/p.png',
                                '/media/demo-media/p.png', '/media/registered-items/..\\escape.png'])
def test_reference_path_escape_is_rejected(tmp_path, path):
    item = registration(tmp_path)
    item['reference_images'][0]['original_path'] = path
    proposer = ReferencePatchProposer([item], PatchEncoder(), tmp_path)
    assert not proposer.health()['available']
    assert proposer.detect(np.zeros((256, 512, 3), np.uint8)) == []


@pytest.mark.parametrize('invalid', ['original_hash', 'unconfirmed', 'roi', 'model_version', 'profile_version', 'reference_ids'])
def test_registration_profile_integrity_fails_closed(tmp_path, invalid):
    item = registration(tmp_path)
    if invalid == 'original_hash': item['reference_images'][0]['sha256'] = '0'*64
    elif invalid == 'unconfirmed': item['reference_images'][0]['region_confirmed'] = False
    elif invalid == 'roi': item['reference_images'][0]['region'] = [0, 0, 1.1, .5]
    elif invalid == 'model_version': item['appearance_profile']['model_version'] = 'wrong'
    elif invalid == 'profile_version': item['appearance_profile']['profile_version'] = True
    elif invalid == 'reference_ids': item['appearance_profile']['reference_ids'] = ['absent']
    proposer = ReferencePatchProposer([item], PatchEncoder(), tmp_path)
    assert not proposer.health()['available']
    assert 'one' in proposer.health()['invalid_profiles']


def test_missing_or_changed_model_cannot_reuse_cached_features(tmp_path):
    item = registration(tmp_path)
    encoder = PatchEncoder()
    encoder.available = False
    assert not ReferencePatchProposer([item], encoder, tmp_path).health()['available']
    encoder.available = True
    proposer = ReferencePatchProposer([item], encoder, tmp_path)
    encoder.model_version = 'changed'
    assert proposer.detect(np.zeros((256, 512, 3), np.uint8)) == []
    assert proposer.health()['error'] == 'encoder_version_changed'


def test_arbitrary_same_directory_display_cannot_replace_verified_original(tmp_path):
    item = registration(tmp_path)
    swapped = cv2.imencode('.jpg', np.full((128, 128, 3), 100, np.uint8))[1].tobytes()
    (tmp_path/'one-display.jpg').write_bytes(swapped)
    proposer = ReferencePatchProposer([item], PatchEncoder(), tmp_path)
    assert not proposer.health()['available']
    assert proposer.health()['invalid_profiles']['one'] == 'reference_display_not_bound_to_original'


def test_same_location_two_registered_identities_are_not_both_eligible(tmp_path):
    items = [registration(tmp_path, 'one'), registration(tmp_path, 'two')]
    encoder = PatchEncoder()
    proposer = ReferencePatchProposer(items, encoder, tmp_path)
    rows = proposer.detect(np.zeros((256, 512, 3), np.uint8))
    assert len(rows) == 2
    assert all(not row['eligible'] and row['rejection'] == 'ambiguous_registered_item' for row in rows)
    assert len(encoder.calls) == 8  # 2x2 reference encodes + exactly 4 shared query encodes.


def test_multiple_spatial_matches_cannot_be_promoted(tmp_path, monkeypatch):
    item = registration(tmp_path)
    proposer = ReferencePatchProposer([item], PatchEncoder(), tmp_path)
    cluster = proposer._clusters

    def two_places(*args):
        values = cluster(*args)
        if not values: return []
        other = deepcopy(values[0])
        other['bbox'][0] += 200
        return values+[other]

    monkeypatch.setattr(proposer, '_clusters', two_places)
    rows = proposer.detect(np.zeros((256, 512, 3), np.uint8))
    assert len(rows) == 2
    assert all(not row['eligible'] and row['rejection'] == 'multiple_spatial_matches' for row in rows)


def test_nms_merges_overlapping_fixed_window_proposals(tmp_path, monkeypatch):
    item = registration(tmp_path)
    proposer = ReferencePatchProposer([item], PatchEncoder(), tmp_path)
    cluster = proposer._clusters

    def overlap(*args):
        values = cluster(*args)
        if not values: return []
        other = deepcopy(values[0])
        other['bbox'][0] += 4
        return values+[other]

    monkeypatch.setattr(proposer, '_clusters', overlap)
    rows = proposer.detect(np.zeros((256, 512, 3), np.uint8))
    assert len(rows) == 1 and rows[0]['eligible']


def test_item_budget_is_explicit_not_unbounded(tmp_path):
    items = [registration(tmp_path, 'one'), registration(tmp_path, 'two')]
    proposer = ReferencePatchProposer(items, PatchEncoder(), tmp_path, {'max_items': 1})
    assert proposer.health()['loaded_profile_versions'] == {'one': 1}
    assert proposer.health()['invalid_profiles'] == {'two': 'item_limit_exceeded'}


@pytest.mark.parametrize('settings', [{'max_items': 33}, {'max_items': True}, {'max_references': 13},
                                   {'foreground_threshold': .79}, {'background_margin': .05},
                                   {'min_patches': 7}, {'min_reference_matches': 5}, {'top_patch_threshold': .81}])
def test_thresholds_and_resource_bounds_cannot_silently_weaken(settings):
    with pytest.raises(ValueError): ReferencePatchSettings(**settings)


def test_local_similarity_matches_blas_without_loosening_threshold_boundaries():
    generator = np.random.default_rng(41)
    a = generator.normal(size=(256, DIMENSION)).astype(np.float32)
    b = generator.normal(size=(196, DIMENSION)).astype(np.float32)
    a /= np.linalg.norm(a, axis=1, keepdims=True)
    b /= np.linalg.norm(b, axis=1, keepdims=True)
    expected = a @ b.T
    np.testing.assert_allclose(_similarity(a, b), expected, atol=1e-6, rtol=0)
    target = basis([0])
    for threshold in (.06, .80, .82, .83):
        values = np.array([threshold-1e-5, threshold, threshold+1e-5], np.float32)
        query = np.zeros((3, DIMENSION), np.float32)
        query[:, 0] = values
        query[:, 1] = np.sqrt(1-values**2)
        local = _similarity(query, target)
        np.testing.assert_array_equal(local >= threshold, (query @ target.T) >= threshold)
        assert not (local[0, 0] >= threshold) and local[-1, 0] >= threshold


def test_batch_encoder_shares_one_call_without_dropping_any_query_window(tmp_path):
    class Batched(PatchEncoder):
        batch_calls = 0

        def encode_patches_batch(self, images):
            self.batch_calls += 1
            assert len(images) == 4
            return [self.encode_patches(image) for image in images]

    encoder = Batched()
    proposer = ReferencePatchProposer([registration(tmp_path)], encoder, tmp_path)
    output = proposer.detect(np.zeros((256, 512, 3), np.uint8))
    assert len(output) == 1 and output[0]['bbox'] == [100., 80., 64., 64.]
    assert encoder.batch_calls == 1
    assert proposer.health()['last_query_model_calls'] == 1
    assert proposer.health()['last_query_encodes'] == 4


def test_partial_batch_result_is_never_silently_accepted(tmp_path):
    class BrokenBatch(PatchEncoder):
        def encode_patches_batch(self, images):
            return [self.encode_patches(images[0])]

    proposer = ReferencePatchProposer([registration(tmp_path)], BrokenBatch(), tmp_path)
    assert proposer.detect(np.zeros((256, 512, 3), np.uint8)) == []
    assert proposer.health()['error'] == 'query_encoding_failed:ValueError'


@pytest.mark.parametrize('failure', ['nan_features', 'outside_coordinates', 'raises'])
def test_batch_failure_cannot_publish_proposals_from_earlier_valid_windows(tmp_path, failure):
    class BrokenBatch(PatchEncoder):
        def encode_patches_batch(self, images):
            if failure == 'raises':
                raise RuntimeError('isolated encoder failure')
            output = [self.encode_patches(image) for image in images]
            features, xy, size = output[2]
            features, xy = features.copy(), xy.copy()
            if failure == 'nan_features':
                features[0, 0] = np.nan
            else:
                xy[0, 0] = images[2].shape[1]+1
            output[2] = (features, xy, size)
            return output

    proposer = ReferencePatchProposer([registration(tmp_path)], BrokenBatch(), tmp_path)
    assert proposer.detect(np.zeros((256, 512, 3), np.uint8)) == []
    assert proposer.health()['error'].startswith('query_encoding_failed:')
