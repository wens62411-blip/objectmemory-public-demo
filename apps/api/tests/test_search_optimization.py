"""Search ordering, evidence provenance and exact/fuzzy selection contracts."""
from copy import deepcopy

import pytest

from apps.api.app import search


ITEM = {'id': 'phone', 'name': '手机'}


def event(event_id, timestamp='2026-09-09T01:00:00Z', **changes):
    return {
        'id': event_id, 'event_id': event_id, 'event_type': 'movement',
        'timestamp_start': timestamp, 'timestamp_end': timestamp,
        'evidence_status': 'confirmed', 'final_status': 'confirmed_placed',
        'runtime_mode': 'REAL', 'is_simulated': False, 'source_type': 'opencv_camera',
        'room_name': '客厅', 'zone_name': event_id, **changes,
    }


@pytest.mark.parametrize('timestamps,winner', [
    (['2026-09-09T08:59:00+08:00', '2026-09-09T01:00:00Z', 'invalid'], 1),
    (['2026-09-09T09:00:00+08:00', '2026-09-09T01:00:00Z'], 0),
    (['2026-09-09T01:00:00', '2026-09-09T01:00:00Z'], 0),
    (['invalid', None, ''], 0),
    (['invalid', '2026-09-09T01:00:00Z'], 1),
])
def test_event_order_uses_utc_instants_and_stable_ties(timestamps, winner):
    rows = [event(str(index), timestamp) for index, timestamp in enumerate(timestamps)]
    original = deepcopy(rows)
    result = search.location_result(ITEM, rows)
    assert result['last_confirmed'] is rows[winner]
    assert result['evidence'] is rows[winner]
    assert rows == original


@pytest.mark.parametrize('changes,accepted', [
    ({}, True),
    ({'source_type': 'authorized_screen_capture'}, True),
    ({'source_type': 'esp32_real', 'source_attestation_id': 'attested'}, True),
    ({'source_type': 'esp32_real'}, False),
    ({'source_type': 'esp32_real', 'source_attestation_id': ''}, False),
    ({'source_type': 'rtsp'}, False),
    ({'source_type': 'browser_camera'}, False),
    ({'runtime_mode': 'TEST'}, False),
    ({'is_simulated': True}, False),
    ({'is_simulated': None}, False),
    ({'is_simulated': 0}, False),
    ({'evidence_status': 'observed'}, False),
    ({'final_status': 'picked_up'}, False),
])
def test_real_confirmation_keeps_source_requirements(changes, accepted):
    row = event('candidate', **changes)
    result = search.location_result(ITEM, [row])
    assert result['last_confirmed'] is (row if accepted else None)


def test_test_mode_accepts_simulated_confirmations():
    row = event('simulated', runtime_mode='TEST', is_simulated=True, source_type='video_file')
    assert search.location_result(ITEM, [row], 'TEST')['last_confirmed'] is row


@pytest.mark.parametrize('id_field', ['id', 'event_id'])
def test_current_evidence_id_precedes_later_source_timestamps(id_field):
    old = event('older', '2026-09-09T00:00:00Z', manually_corrected=True)
    old[id_field] = 'accepted'
    later = event('later')
    state = {'evidence_event_id': 'accepted', 'last_seen_at': '2026-09-09T00:00:00Z'}
    result = search.location_result(ITEM, [later, old], current_state=state)
    assert result['last_confirmed'] is old
    assert result['evidence'] is old
    assert result['authenticity_label'] == '人工纠正'


def test_current_id_uses_newest_matching_row_before_confirmation_filter():
    old = event('current', '2026-09-09T00:00:00Z')
    rejected = event('current', '2026-09-09T02:00:00Z', evidence_status='observed')
    accepted = event('accepted')
    state = {'evidence_event_id': 'current'}
    result = search.location_result(ITEM, [old, rejected, accepted], current_state=state)
    assert result['last_confirmed'] is accepted


@pytest.mark.parametrize('status', ['occluded', 'exited_view'])
def test_unconfirmed_missing_event_keeps_available_confirmation(status):
    confirmed = event('桌面')
    missing = event('missing', '2026-09-09T02:00:00Z', event_type=status, evidence_status='observed')
    result = search.location_result(ITEM, [confirmed, missing])
    assert result['status'] == status
    assert result['evidence'] is confirmed
    assert result['last_confirmed'] is confirmed
    assert result['answer'] == '手机最后一次确认放在客厅 · 桌面。'
    assert result['last_picked_up'] is result['last_occluded'] is result['last_exited'] is None


@pytest.mark.parametrize('status', ['occluded', 'exited_view', 'offline', 'lost', 'last_seen'])
@pytest.mark.parametrize('timestamp', ['2026-09-09T01:00:00Z', 'invalid'])
def test_only_missing_observations_override_confirmation_at_equal_time(status, timestamp):
    confirmed = event('桌面', timestamp)
    state = {'status': status, 'last_seen_at': timestamp, 'current_room': '客厅', 'current_zone': '沙发'}
    original = deepcopy(state)
    result = search.location_result(ITEM, [confirmed], current_state=state)
    assert result['evidence'] is (confirmed if status == 'last_seen' else result['last_observed'])
    assert result['last_confirmed'] is confirmed
    assert state == original


def test_evidence_provenance_preserves_all_nullable_fields():
    confirmed = event('桌面', validation_run_id='run', clip_path='/clip.mp4', trajectory=[1, 2])
    result = search.location_result(ITEM, [confirmed])
    assert result['evidence_provenance'] == {
        'validation_run_id': 'run', 'detection_mode': None, 'aruco_id': None,
        'source_session_id': None, 'trajectory': [1, 2], 'before_screenshot': None,
        'before_screenshot_sha256': None, 'after_screenshot': None,
        'after_screenshot_sha256': None, 'clip_path': '/clip.mp4', 'clip_sha256': None,
    }
    assert search.location_result(ITEM, [])['evidence_provenance'] is None


def test_exact_alias_skips_all_fuzzy_work_even_when_last(monkeypatch):
    def unexpected_matcher(*args, **kwargs):
        pytest.fail('an exact match must not run fuzzy comparisons')

    monkeypatch.setattr(search, 'SequenceMatcher', unexpected_matcher)
    items = [{'name': 'pencil'}, {'name': 'notebook'}, {'name': 'case', 'aliases': ['手机壳']}]
    assert search.match_items('帮我找手机壳？', items) == [items[-1]]


def test_exact_results_keep_input_order_and_five_item_limit():
    items = [{'name': f'phone {index}', 'type': 'phone'} for index in range(8)]
    assert search.match_items('我的手机在哪里', iter(items)) == items[:5]


def test_fuzzy_top_five_keep_tie_order_and_exclude_low_scores():
    items = [{'id': index, 'name': 'abcd'} for index in range(8)]
    items.insert(0, {'name': 'unrelated'})
    assert search.match_items('abce', items) == items[1:6]


def test_fuzzy_scores_are_descending_and_empty_names_do_not_match():
    items = [{'name': 'qqqq'}, {'name': 'abzz'}, {'name': 'abcd'}, {'name': 'abcf'}, {'name': '我的'}]
    assert search.match_items('abce', items) == [items[2], items[3], items[1]]
    assert search.match_items('我的？', items) == []
