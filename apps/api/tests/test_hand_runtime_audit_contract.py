"""Pure sampler contracts; none of these fixtures are camera/action evidence."""
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('hand_runtime_audit_contract', ROOT/'scripts/audit-hand-actions-runtime.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def complete_report():
    return {'duration_requested_seconds': 120, 'elapsed_seconds': 120, 'errors': [],
            'samples': [{'elapsed': index*.5, 'fresh': True, 'frame': index+1,
                         'session': 'explicit-sampler-fixture'} for index in range(240)]}


def test_full_healthy_window_passes_both_sampled_and_continuous_checks():
    result = MODULE.metadata_checks(complete_report())
    assert result['passed_metadata_checks'] and result['continuous_fresh_pass']
    assert result['complete_window'] and result['fresh_fraction'] == 1
    assert result['observed_source_sessions'] == 1 and result['valid_frames_monotonic']
    assert result['max_sample_gap_seconds'] == result['unobserved_tail_seconds'] == .5


@pytest.mark.parametrize('missing,expected', [(12, True), (13, False)])
def test_sampled_95_percent_gate_never_claims_uninterrupted_freshness(missing, expected):
    report = complete_report()
    for row in report['samples'][5:5+missing]:
        row.update(fresh=False, frame=None, session=None)
    result = MODULE.metadata_checks(report)
    assert result['passed_metadata_checks'] is expected
    assert result['continuous_fresh_pass'] is False


@pytest.mark.parametrize('kind', ['one_sample', 'one_fresh', 'short_duration', 'unobserved_tail',
                                'long_polling_gap', 'reversed_time', 'read_error', 'two_sessions',
                                'regressing_frame', 'never_advancing_frame', 'missing_frame', 'boolean_frame'])
def test_any_fresh_does_not_hide_incomplete_or_invalid_evidence(kind):
    report = complete_report()
    if kind == 'one_sample':
        report['samples'] = report['samples'][:1]
    elif kind == 'one_fresh':
        for row in report['samples'][1:]: row.update(fresh=False, frame=None, session=None)
    elif kind == 'short_duration': report['elapsed_seconds'] = 119
    elif kind == 'unobserved_tail': report['samples'] = report['samples'][:-10]
    elif kind == 'long_polling_gap': report['samples'] = report['samples'][:50]+report['samples'][80:]
    elif kind == 'reversed_time': report['samples'][100]['elapsed'] = 1
    elif kind == 'read_error': report['errors'] = [{'error': 'explicit-read-failure'}]
    elif kind == 'two_sessions': report['samples'][100]['session'] = 'new-session'
    elif kind == 'regressing_frame': report['samples'][100]['frame'] = 1
    elif kind == 'never_advancing_frame':
        for row in report['samples']: row['frame'] = 1
    elif kind == 'missing_frame': report['samples'][100]['frame'] = None
    else: report['samples'][100]['frame'] = True
    result = MODULE.metadata_checks(report)
    assert result['passed_metadata_checks'] is False
    assert result['continuous_fresh_pass'] is False


def test_duplicate_fresh_poll_is_monotonic_but_not_new_physical_exposure_claim():
    report = complete_report()
    report['samples'][100]['frame'] = report['samples'][99]['frame']
    assert MODULE.metadata_checks(report)['passed_metadata_checks'] is True
    assert 'not camera exposure' in MODULE.metadata_checks(report)['metadata_check_scope']


def test_diagnostics_keep_only_bounded_allowlisted_numbers_and_no_secret_text():
    received = datetime(2026, 9, 7, 16, 30, tzinfo=timezone.utc)
    health = {'latency_ms': 321, 'frame_sequence': 42, 'preview_age_ms': 8,
              'inference_target_fps': 5, 'password': 'secret-fixture',
              'stage_timings_ms': {'object_detection': 17, 'hands': 12, 'tracking': True,
                                  'identity_matching': float('inf'), 'person_pose': -1,
                                  'private_command': 'secret-fixture'}}
    row = MODULE.diagnostic_fields({'timestamp': '2026-09-07T16:29:59.500000+00:00',
                                    'reason': 'stale_frame'}, health, received)
    assert row['reason'] == 'stale_frame' and row['action_age_at_receive_ms'] == 500
    assert row['health_latency_ms'] == 321 and row['health_frame_sequence'] == 42
    assert row['stage_timings_ms'] == {'object_detection': 17, 'hands': 12}
    assert 'secret-fixture' not in str(row) and 'password' not in row


@pytest.mark.parametrize('stamp', [None, 'private-secret-fixture', '2026-09-07T16:30:00', 'x'*10000])
def test_missing_invalid_naive_and_unbounded_timestamps_are_not_copied(stamp):
    row = MODULE.diagnostic_fields({'timestamp': stamp, 'reason': 'private-secret-fixture'}, {},
                                  datetime(2026, 9, 7, 16, 30, tzinfo=timezone.utc))
    assert row['source_timestamp'] is None and row['action_age_at_receive_ms'] is None
    assert row['reason'] == 'other_reason' and 'private-secret-fixture' not in str(row)


def test_check_does_not_mutate_the_original_receipt():
    report = complete_report()
    original = deepcopy(report)
    MODULE.metadata_checks(report)
    assert report == original


def test_rejected_frame_diagnostics_remain_measurable_without_reintroducing_old_actions():
    received = datetime(2026, 9, 8, 4, 30, tzinfo=timezone.utc)
    source = {'fresh': False, 'reason': 'stale_frame', 'timestamp': None, 'hands': [],
              'diagnostics': {'freshness_reason': 'source_frame_expired', 'source_age_ms': 1300.25,
                  'result_age_ms': 700, 'inference_duration_ms': 450, 'source_to_result_ms': 600.25,
                  'freshness_limit_ms': 1000, 'capture_status': 'online',
                  'profile_generation': 7, 'applied_profile_generation': 7, 'snapshot_profile_generation': 7,
                  'source_session_matches': True, 'reconnect_epoch_matches': True}}
    original = deepcopy(source)
    row = MODULE.diagnostic_fields(source, {}, received)
    diagnostic = row['action_diagnostics']
    assert diagnostic['freshness_reason'] == 'source_frame_expired'
    assert diagnostic['source_age_ms'] == 1300.25 and diagnostic['result_age_ms'] == 700
    assert diagnostic['profile_generation'] == diagnostic['snapshot_profile_generation'] == 7
    assert diagnostic['source_session_matches'] is True
    assert row['source_timestamp'] is None and row['action_age_at_receive_ms'] is None
    assert source == original


@pytest.mark.parametrize('payload', [None, [], 'secret-fixture',
    {'freshness_reason': ['secret-fixture'], 'capture_status': {'secret': 'fixture'},
     'source_age_ms': True, 'inference_duration_ms': float('nan'), 'result_age_ms': -1,
     'profile_generation': 10**100, 'source_session_matches': 1, 'reconnect_epoch_matches': 'true',
     'private_path': 'secret-fixture', 'error': 'secret-fixture'}])
def test_action_diagnostics_keep_only_bounded_typed_allowlisted_fields(payload):
    row = MODULE.diagnostic_fields({'reason': [], 'diagnostics': payload}, {},
        datetime(2026, 9, 8, 4, 30, tzinfo=timezone.utc))
    assert row['reason'] == 'other_reason'
    diagnostic = row['action_diagnostics']
    assert diagnostic['freshness_reason'] == 'not_reported'
    assert diagnostic['capture_status'] == 'unknown'
    assert diagnostic['source_age_ms'] is diagnostic['profile_generation'] is None
    assert diagnostic['source_session_matches'] is diagnostic['reconnect_epoch_matches'] is None
    assert 'secret' not in str(row)
