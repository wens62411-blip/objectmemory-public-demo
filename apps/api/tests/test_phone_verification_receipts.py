"""A completed diagnostic process must not fabricate continuous source coverage."""
from copy import deepcopy
import runpy
from pathlib import Path


assess = runpy.run_path(str(Path(__file__).resolve().parents[3] / 'scripts/collect-phone-verification.py'))['assess_runtime_coverage']


def good_receipt():
    return {'completed': True, 'requested_seconds': 1800, 'duration_seconds': 1800,
            'sample_interval': 5, 'after': {'stats': {'online_cameras': 1}},
            'samples': [{'elapsed_seconds': t, 'cameras': [
                {'health': {'capture_fps': 30, 'preview_fps': 29, 'error': ''},
                 'snapshot': {'source_session_id': 'session-a', 'source_frame': t + 1,
                              'image_decodes': True, 'accepted': 0}}]}
                for t in range(0, 1800, 5)]}


def test_complete_execution_with_unsampled_tail_is_not_continuous_pass():
    report = good_receipt()
    report['samples'] = report['samples'][:315]
    report['after']['stats']['online_cameras'] = 0
    result = assess(report)
    assert not result['continuous_requested_window_verified']
    assert 'unsampled_tail' in result['rejection_reasons']
    assert 'no_online_camera_at_final_health' in result['rejection_reasons']


def test_interior_gap_or_changed_session_is_not_continuous_pass():
    report = good_receipt()
    del report['samples'][100:110]
    report['samples'][-1]['cameras'][0]['snapshot']['source_session_id'] = 'reconnected'
    result = assess(report)
    assert not result['continuous_requested_window_verified']
    assert 'sampling_gap_or_clock_order' in result['rejection_reasons']
    assert 'missing_or_changed_source_session' in result['rejection_reasons']


def test_sampled_window_and_fps_dips_remain_separate_claims():
    report = good_receipt()
    report['samples'][100]['cameras'][0]['health']['preview_fps'] = 14
    result = assess(report)
    assert result['continuous_requested_window_verified']
    assert result['warm_preview_below_20_fps_samples'] == 1
    assert result['physical_phone_acceptance'] == 'NOT_PERFORMED'


def test_empty_receipt_never_passes():
    assert not assess({})['continuous_requested_window_verified']


def test_repeated_source_frame_and_empty_camera_sample_never_pass():
    report = good_receipt()
    report['samples'][20]['cameras'][0]['snapshot']['source_frame'] = 1
    assert 'missing_or_non_increasing_source_frames' in assess(report)['rejection_reasons']
    report = good_receipt()
    report['samples'][20]['cameras'] = []
    assert 'missing_camera_sample' in assess(report)['rejection_reasons']


def test_undecodable_or_error_snapshot_never_passes():
    report = good_receipt()
    failed = deepcopy(report)
    failed['samples'][20]['cameras'][0]['snapshot_error'] = 'HTTP 503'
    assert not assess(failed)['continuous_requested_window_verified']
    report['samples'][20]['cameras'][0]['snapshot']['image_decodes'] = False
    assert not assess(report)['continuous_requested_window_verified']


def candidate_summary(rows):
    # Only a statistics contract; these explicit fixtures are not camera proof.
    module = runpy.run_path(str(Path(__file__).resolve().parents[3] / 'scripts/audit-phone-recognition.py'))
    return module['summarize_candidates'](rows)


def test_geometry_phone_labels_do_not_count_as_neural_phone_detections():
    result = candidate_summary([
        {'category':'cell phone','category_evidence':'geometry_only','proposal_backend':'phone_shape_proposal'},
        {'category':'person','category_evidence':'model','accepted':False},
    ])
    assert result['categories'] == {'person':1}
    assert result['geometry_categories'] == {'cell phone':1}
    assert result['all_candidate_labels'] == {'cell phone':1,'person':1}
    assert result['model_candidates'] == 1
    assert result['geometry_only_candidates'] == 1


def test_either_shape_marker_prevents_a_claimed_model_identity_being_counted():
    result = candidate_summary([
        {'category':'cell phone','category_evidence':'model','proposal_backend':'phone_shape_proposal','accepted':True,'item_id':'fixture'},
        {'category':'cell phone','category_evidence':'geometry_only','proposal_backend':'nanodet','accepted':True,'item_id':'fixture'},
    ])
    assert not result['categories']
    assert not result['accepted']
    assert result['invalid_identity_claims'] == 2


def test_missing_provenance_is_not_counted_as_a_model_detection():
    result = candidate_summary([{'category':'cell phone','accepted':True,'item_id':'fixture'}])
    assert not result['categories']
    assert result['unverified_categories'] == {'cell phone':1}
    assert not result['accepted']


def test_real_model_category_and_identity_counts_are_kept_distinct():
    result = candidate_summary([
        {'category':'cell phone','category_evidence':'model','accepted':False},
        {'category':'cell phone','category_evidence':'model','accepted':True,'item_id':'fixture'},
    ])
    assert result['categories'] == {'cell phone':2}
    assert result['accepted'] == {'fixture':1}
    assert result['invalid_identity_claims'] == 0


def test_registered_reference_route_is_counted_separately_from_generic_model():
    result = candidate_summary([
        {'category': 'cell phone', 'category_evidence': 'registered_reference_patch',
         'proposal_backend': 'dinov2_reference_patches', 'accepted': True, 'item_id': 'fixture'},
        {'category': 'cell phone', 'category_evidence': 'registered_reference_patch',
         'proposal_backend': 'phone_shape_proposal', 'accepted': True, 'item_id': 'not-proof'},
        {'category': 'cell phone', 'category_evidence': 'registered_reference_patch',
         'proposal_backend': 'unknown', 'accepted': True, 'item_id': 'not-proof'},
    ])
    assert result['reference_categories'] == {'cell phone': 1}
    assert result['accepted'] == {'fixture': 1}
    assert not result['categories']
    assert result['invalid_identity_claims'] == 2
