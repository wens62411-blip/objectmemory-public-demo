"""Actual latest-slot/JPEG with controlled burst times, not physical FPS proof."""
import math

import pytest

from services.vision.tests.test_preview_pacing import run_cadence


def test_fresh_early_burst_frame_is_kept_until_deadline_instead_of_discarded(monkeypatch, tmp_path):
    result = run_cadence(monkeypatch, tmp_path,
        [(0, 1, 0), (.008, 2, 0), (.100, 3, 0), (.108, 4, 0), (.200, 5, 0), (.208, 6, 0)])
    assert result['source_frames'] == [1, 2, 3, 4, 5, 6], result
    assert result['published_times'] == pytest.approx([0, 1/30, .1, .1+1/30, .2, .2+1/30])


def test_newest_early_frame_replaces_single_pending(monkeypatch, tmp_path):
    result = run_cadence(monkeypatch, tmp_path, [(0, 1, 0), (.005, 2, 0), (.010, 3, 0)])
    assert result['source_frames'] == [1, 3], result
    assert result['max_pending_frames'] == 1
    assert result['published_times'] == pytest.approx([0, 1/30])


@pytest.mark.parametrize('source_fps', [60, 120])
def test_qpc_deadlines_keep_fast_sources_bounded_without_old_image_replay(monkeypatch, tmp_path, source_fps):
    result = run_cadence(monkeypatch, tmp_path, [(index/source_fps, index+1, 0) for index in range(source_fps*3)])
    assert 89 <= result['output_frames'] <= 91
    assert result['max_pending_frames'] == 1
    for index, stamp in enumerate(result['published_times']):
        assert index+1 <= 1+math.floor((stamp+1e-7)*30)
        assert sum(stamp <= value < stamp+1-1e-7 for value in result['published_times'][index:]) <= 30


def test_qpc_deadline_is_not_mixed_with_quantized_source_age_clock(monkeypatch, tmp_path):
    result = run_cadence(monkeypatch, tmp_path, [(0, 1, 0), (.008, 2, 0), (.1, 3, 0)], source_clock_step=.015625)
    assert result['source_frames'] == [1, 2, 3]
    assert result['published_times'] == pytest.approx([0, 1/30, .1])


def test_source_session_change_replaces_pending_without_carrying_old_frame(monkeypatch, tmp_path):
    result = run_cadence(monkeypatch, tmp_path, [(0, 1, 0, 'a'), (.008, 2, 0, 'a'), (.015, 1, 0, 'b')])
    assert list(zip(result['source_sessions'], result['source_frames'])) == [('a', 1), ('b', 1)]
    assert result['published_times'] == pytest.approx([0, 1/30])


def test_session_reset_without_new_image_invalidates_pending(monkeypatch, tmp_path):
    result = run_cadence(monkeypatch, tmp_path, [(0, 1, 0, 'a'), (.008, 2, 0, 'a'), (.02, None, 0, 'b')])
    assert list(zip(result['source_sessions'], result['source_frames'])) == [('a', 1)]


def test_profile_generation_change_invalidates_waiting_pending(monkeypatch, tmp_path):
    def change(engine, entry):
        if entry[1] is None:
            engine._profile_revision += 1
            engine._applied_profile_revision = engine._profile_revision
    result = run_cadence(monkeypatch, tmp_path, [(0, 1, 0), (.008, 2, 0), (.02, None, 0)], on_arrival=change)
    assert result['source_frames'] == [1]


def test_source_failure_drops_held_pending_without_replaying_last_image(monkeypatch, tmp_path):
    def stop_capture(engine, entry):
        if entry[1] is None:
            engine.capture.stop_event.set()
    result = run_cadence(monkeypatch, tmp_path, [(0, 1, 0), (.008, 2, 0), (.02, None, 0)], on_arrival=stop_capture)
    assert result['source_frames'] == [1]


def test_repeated_source_frame_revision_does_not_publish_same_frame_twice(monkeypatch, tmp_path):
    result = run_cadence(monkeypatch, tmp_path, [(0, 1, 0), (.1, 1, 0), (.2, 2, 0)])
    assert result['source_frames'] == [1, 2]


def test_slow_processing_coalesces_producer_bursts_without_unbounded_queue(monkeypatch, tmp_path):
    result = run_cadence(monkeypatch, tmp_path, [(index/120, index+1, 0) for index in range(120)], jpeg_delay=.09)
    assert result['max_pending_frames'] <= 1
    assert result['output_frames'] <= 13
    assert result['output_frames'] == result['distinct_jpegs']
    assert all(right-left >= .09-1e-8 for left, right in zip(result['published_times'], result['published_times'][1:]))


def test_long_gap_has_no_old_frame_fill_or_catchup_burst(monkeypatch, tmp_path):
    result = run_cadence(monkeypatch, tmp_path, [(0, 1, 0), (.008, 2, 0), (5, 3, 0), (5.008, 4, 0), (8, None, 0)])
    assert result['source_frames'] == [1, 2, 3, 4]
    assert result['published_times'] == pytest.approx([0, 1/30, 5, 5+1/30])
