"""Synthetic authorization contracts, not camera or recognition accuracy tests."""
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pytest

from services.vision.detectors.base import Detection
from services.vision.engine import VisionEngine


@pytest.fixture
def route():
    # The narrow routing methods require no capture, model, filesystem or DB.
    engine = object.__new__(VisionEngine)
    engine.settings = {}
    engine.item_by_id = {'item': {'name': 'registered fixture', 'type': 'phone'}}
    engine.loaded_profile_versions = {'item': 7}
    engine._issued_reference_patch_detections = {}
    engine._appearance_encoder = SimpleNamespace(health=lambda: {'available': True})
    engine.reference_matcher = SimpleNamespace(match=lambda *_args, **_kwargs: {'accepted': False})
    proposal = {'best_item_id': 'item', 'profile_version': 7, 'category': 'cell phone',
                'bbox': (20, 30, 50, 90), 'score': .86, 'best_score': .86, 'eligible': True}
    rows = [proposal]
    engine.reference_patches = SimpleNamespace(backend='dinov2_reference_patches',
        config=SimpleNamespace(top_patch_threshold=.82, identity_margin=.06),
        health=lambda: {'available': True}, detect=lambda _frame: deepcopy(rows))
    return engine, np.zeros((180, 240, 3), np.uint8), rows


def test_only_internal_same_frame_proof_assigns_identity_once(route):
    engine, frame, _ = route
    detections = engine._supplement_reference_patches(frame, [])
    engine._assign_reference_identities(frame, detections)
    assert detections[0].identity == 'item'
    assert engine._recognition_candidates[0]['accepted'] is True
    assert not engine._issued_reference_patch_detections
    # Reusing the mutated Detection must clear the old registered identity too.
    engine._assign_reference_identities(frame, detections)
    assert not engine._recognition_candidates[0]['accepted']
    assert detections[0].identity.startswith('generic:unverified-reference:')
    assert 'identity_evidence' not in detections[0].metadata


@pytest.mark.parametrize('change', ['copied_detection', 'different_frame', 'bbox', 'metadata', 'profile'])
def test_changed_or_unissued_patch_proof_cannot_assign_identity(route, change):
    engine, frame, _ = route
    detections = engine._supplement_reference_patches(frame, [])
    if change == 'copied_detection':
        detections = deepcopy(detections)
    elif change == 'different_frame':
        frame = frame.copy()
    elif change == 'bbox':
        detections[0].bbox = (100, 20, 50, 90)
    elif change == 'metadata':
        detections[0].metadata['reference_patch_proof']['best_score'] = .999
    else:
        engine.loaded_profile_versions['item'] = 8
    engine._assign_reference_identities(frame, detections)
    candidate = engine._recognition_candidates[0]
    assert not candidate['accepted'] and candidate['rejection_reason'] == 'unbound_reference_patch_proof'
    assert detections[0].identity != 'item'


def test_two_separate_matches_are_ambiguous_in_both_candidate_and_track_proof(route):
    engine, frame, rows = route
    rows.append({**rows[0], 'bbox': (130, 30, 50, 90)})
    detections = engine._supplement_reference_patches(frame, [])
    engine._assign_reference_identities(frame, detections)
    assert len(engine._recognition_candidates) == 2
    for detection, candidate in zip(detections, engine._recognition_candidates):
        assert not candidate['accepted'] and detection.identity.startswith('generic:ambiguous:')
        assert not detection.metadata['identity_evidence']['accepted']


@pytest.mark.parametrize('label, expected_count', [('cell phone', 1), ('remote', 2)])
def test_fusion_removes_only_overlapping_same_category_model_box(route, label, expected_count):
    engine, frame, _ = route
    generic = Detection('generic:model', label, (20, 30, 50, 90), (45, 75), .9,
                        'experimental', metadata={'backend': 'nanodet'})
    detections = engine._supplement_reference_patches(frame, [generic])
    assert len(detections) == expected_count
    if label == 'remote':
        assert generic in detections and generic.label == 'remote'


def test_rejected_local_proposal_cannot_be_upgraded_by_metadata_edit(route):
    engine, frame, rows = route
    rows[0].update(eligible=False, rejection='ambiguous_identity')
    detections = engine._supplement_reference_patches(frame, [])
    detections[0].metadata['reference_patch_proof'].update(accepted=True, item_id='item')
    engine._assign_reference_identities(frame, detections)
    assert not engine._recognition_candidates[0]['accepted']


def test_contained_local_region_and_single_full_phone_box_are_one_object(route):
    engine, frame, rows = route
    rows[0]['bbox'] = (40, 50, 35, 55)  # less than .5 IoU, but same physical region
    semantic = Detection('generic:model', 'cell phone', (20, 30, 90, 100), (65, 80), .9,
                         'experimental', metadata={'backend': 'nanodet'})
    detections = engine._supplement_reference_patches(frame, [semantic])
    assert len(detections) == 1 and detections[0].bbox == semantic.bbox
    engine._assign_reference_identities(frame, detections)
    assert engine._recognition_candidates[0]['accepted']
    assert engine._recognition_candidates[0]['localization_method'] == 'semantic_box_with_reference_patch_region'


def test_local_match_inside_two_semantic_objects_cannot_silently_fuse_them(route):
    engine, frame, _ = route
    same = [Detection(f'generic:{i}', 'cell phone', (20+i, 30, 60, 100), (50, 80), .9,
                      'experimental', metadata={'backend': 'nanodet'}) for i in (0, 1)]
    detections = engine._supplement_reference_patches(frame, same)
    assert len(detections) == 3  # all remain for ordinary ambiguity checks
    engine._assign_reference_identities(frame, detections)
    assert all(not row['accepted'] for row in engine._recognition_candidates)


def test_tiny_region_cannot_take_over_an_unrelated_large_semantic_box(route):
    engine, frame, rows = route
    rows[0]['bbox'] = (40, 50, 32, 32)
    whole = Detection('generic:large', 'cell phone', (0, 0, 230, 170), (115, 85), .9,
                      'experimental', metadata={'backend': 'nanodet'})
    assert len(engine._supplement_reference_patches(frame, [whole])) == 2


def test_one_semantic_box_cannot_be_consumed_by_first_of_two_distinct_items(route):
    engine, frame, rows = route
    engine.item_by_id['second'] = {'name': 'other fixture', 'type': 'phone'}
    engine.loaded_profile_versions['second'] = 7
    rows.append({**rows[0], 'best_item_id': 'second', 'bbox': (100, 30, 50, 90)})
    shared = Detection('generic:merged', 'cell phone', (10, 20, 150, 110), (85, 75), .9,
                       'experimental', metadata={'backend': 'nanodet'})
    detections = engine._supplement_reference_patches(frame, [shared])
    assert len(detections) == 3
    assert [det.bbox for det in detections[1:]] == [(20, 30, 50, 90), (100, 30, 50, 90)]
    for detection in detections[1:]:
        assert detection.metadata['reference_patch_proof']['fusion_rejection'] == 'semantic_contains_multiple_reference_regions'
