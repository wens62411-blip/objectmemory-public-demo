"""Bounded diagnostic contracts plus actual JPEG/file preview, never camera proof."""
import json
import time

import cv2
import numpy as np
import pytest

from services.vision.capture import CaptureWorker, LatestFrameSlot, PipelineDiagnostics
from services.vision import engine as engine_module
from services.vision.engine import VisionEngine
from services.vision.tests.test_preview_decoupling import make_video, wait_until
from services.vision.tests.test_preview_pacing import run_cadence


def test_numeric_diagnostics_are_bounded_allowlisted_and_aggregated_without_raw_rows():
    diagnostics = PipelineDiagnostics(True, ('work_ms',), ('published',), ('overwritten',))
    for value in range(200):
        diagnostics.record('published', {'work_ms': value, 'password': 'secret', 'jpeg': b'private', 'nan': float('nan')}, {'overwritten': 1, 'token': 123})
    report = diagnostics.snapshot()
    assert len(diagnostics._samples) == report['samples_retained'] == report['capacity'] == 120
    assert report['cumulative_counts'] == {'published': 200, 'overwritten': 200}
    assert report['milliseconds'] == {'work_ms': {'count': 120, 'min': 80., 'p50': 139.5, 'p95': 193., 'max': 199.}}
    assert not any(word in json.dumps(report) for word in ('secret', 'password', 'jpeg', 'token', 'source_session', 'raw'))
    diagnostics.record('private-camera-name', {'work_ms': 100})
    assert diagnostics.snapshot() == report
    diagnostics.clear()
    assert diagnostics.snapshot()['samples_retained'] == 0
    assert diagnostics.snapshot()['cumulative_counts'] == {}


def test_disabled_diagnostics_do_not_accumulate_even_when_called():
    diagnostics = PipelineDiagnostics(False, ('work_ms',), ('published',))
    for _ in range(200):
        diagnostics.record('published', {'work_ms': 12})
    assert diagnostics.snapshot() == {'enabled': False}
    assert len(diagnostics._samples) == 0 and diagnostics._counts == {}


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1, True, '1', b'private'])
def test_nonfinite_negative_or_nonnumeric_diagnostic_measurements_are_rejected(value):
    diagnostics = PipelineDiagnostics(True, ('work_ms',), ('published',))
    diagnostics.record('published', {'work_ms': value})
    assert diagnostics.snapshot()['milliseconds'] == {}


def attach_diagnostics(monkeypatch, *, enabled=True):
    diagnostics = PipelineDiagnostics(enabled,
        ('slot_wait_ms', 'arrival_interval_ms', 'source_age_at_read_ms', 'processing_ms', 'tracker_ms', 'jpeg_ms'),
        ('published', 'pending_replaced', 'source_invalidated', 'profile_invalidated', 'duplicate_source_frame',
         'stale_input', 'stale_output', 'encoding_error', 'stopped'),
        ('source_revisions_observed', 'source_revisions_overwritten', 'source_revisions_deferred'))
    original = VisionEngine._run_preview
    def run(engine):
        if not hasattr(engine_module.time, 'perf_counter'):
            monkeypatch.setattr(engine_module.time, 'perf_counter', time.perf_counter, raising=False)
        engine._preview_diagnostics = diagnostics
        original(engine)
    monkeypatch.setattr(VisionEngine, '_run_preview', run)
    return diagnostics


def test_preview_pending_replacement_is_not_reported_as_producer_overwrite(monkeypatch, tmp_path):
    diagnostics = attach_diagnostics(monkeypatch)
    result = run_cadence(monkeypatch, tmp_path, [(index/120, index+1, 0) for index in range(1200)])
    counts = diagnostics.snapshot()['cumulative_counts']
    assert counts['source_revisions_observed'] == 1200
    assert counts['source_revisions_overwritten'] == 0
    assert counts['published'] == result['output_frames']
    assert counts['pending_replaced'] + counts['published'] == 1200
    assert counts['source_revisions_deferred'] > counts['pending_replaced']
    assert 'pacing_rejected' not in counts
    assert diagnostics.snapshot()['samples_retained'] == 120


def test_preview_counts_unread_slot_revisions_separately_from_pacing(monkeypatch, tmp_path):
    diagnostics = attach_diagnostics(monkeypatch)
    publish = LatestFrameSlot.publish
    def burst(slot, packet):
        # Two producer publications before the consumer wakes. The first
        # revision is genuinely overwritten, not rejected by preview pacing.
        publish(slot, packet)
        publish(slot, packet)
    monkeypatch.setattr(LatestFrameSlot, 'publish', burst)
    result = run_cadence(monkeypatch, tmp_path, [(index/30, index+1, 0) for index in range(120)])
    counts = diagnostics.snapshot()['cumulative_counts']
    assert counts == {'published': 120, 'source_revisions_observed': 120, 'source_revisions_overwritten': 120, 'source_revisions_deferred': 0}
    assert result['output_frames'] == 120


def test_stale_frames_and_idle_reads_do_not_inflate_publications(monkeypatch, tmp_path):
    diagnostics = attach_diagnostics(monkeypatch)
    result = run_cadence(monkeypatch, tmp_path, [(0, 1, 0), (.1, None, 0), (5, 2, 3), (5.1, 3, 0)])
    counts = diagnostics.snapshot()['cumulative_counts']
    assert result['source_frames'] == [1, 3]
    assert counts == {'published': 2, 'stale_input': 1, 'source_revisions_observed': 3, 'source_revisions_overwritten': 0, 'source_revisions_deferred': 0}


def test_preview_disabled_path_never_calls_diagnostic_record(monkeypatch, tmp_path):
    diagnostics = attach_diagnostics(monkeypatch, enabled=False)
    def forbidden(*args, **kwargs):
        raise AssertionError('Disabled preview must not collect diagnostic records')
    monkeypatch.setattr(diagnostics, 'record', forbidden)
    result = run_cadence(monkeypatch, tmp_path, [(index/30, index+1, 0) for index in range(10)])
    assert result['output_frames'] == 10 and diagnostics.snapshot() == {'enabled': False}


@pytest.mark.parametrize('enabled', [True, False])
def test_capture_diagnostic_bounds_and_no_frame_content(enabled, monkeypatch):
    class Source:
        source_type = 'test_fixture'
        config = {}
        index = 0
        closed = False
        def connect(self): return True
        def consume_stream_discontinuity(self): return None
        def read_frame(self):
            self.index += 1
            if self.index > 150:
                worker.stop_event.set()
                return None
            # This verifies 150 distinct publications, not replayed black pixels.
            return np.full((12, 12, 3), self.index, np.uint8)
        def health_check(self): return {'status': 'ended' if self.index > 150 else 'running'}
        def disconnect(self): self.closed = True
    source = Source()
    worker = CaptureWorker(source, diagnostic_mode=enabled)
    if not enabled:
        monkeypatch.setattr(worker.diagnostics, 'record', lambda *args, **kwargs: pytest.fail('Disabled capture collected timings'))
    worker._run()
    report = worker.health()['capture_pipeline_diagnostics']
    assert source.closed and worker.preview_frames.size == 0
    if not enabled:
        assert report == {'enabled': False}
    else:
        assert report['samples_retained'] == 120
        assert report['cumulative_counts'] == {'published': 150, 'read_empty': 1}
        assert set(report['milliseconds']) == {'read_ms', 'read_return_interval_ms', 'publish_interval_ms', 'queue_publish_ms', 'slot_publish_ms', 'read_to_publish_ms', 'fingerprint_ms'}
        assert all(row['min'] >= 0 for row in report['milliseconds'].values())
        assert 'frame' not in json.dumps(report) and 'source' not in json.dumps(report)


def test_real_file_pipeline_exposes_segment_times_only_when_enabled(tmp_path):
    path = tmp_path/'diagnostic-file.avi'
    make_video(path, frames=60)
    events = []
    engine = VisionEngine({'id': 'diagnostic-file', 'source_type': 'video', 'source': str(path),
        'config': {'loop': False, 'realtime': True}}, [], [],
        {'runtime_mode': 'TEST', 'source_type': 'video_file', 'diagnostic_mode': True,
         'detection_mode': 'aruco', 'hand_detection_enabled': False, 'media_root': str(tmp_path/'media')},
        lambda *args: events.append(args))
    assert engine.start()
    try:
        wait_until(lambda: engine.health()['preview_pipeline_diagnostics']['cumulative_counts'].get('published', 0) >= 12)
        health = engine.health()
        preview = health['preview_pipeline_diagnostics']
        assert health['capture_pipeline_diagnostics']['cumulative_counts']['published'] >= 12
        for key in ('slot_wait_ms', 'tracker_ms', 'copy_ms', 'draw_ms', 'resize_ms', 'banner_ms', 'jpeg_ms', 'latest_lock_wait_ms', 'latest_lock_hold_ms', 'processing_ms', 'source_to_publish_ms'):
            assert preview['milliseconds'][key]['count'] > 0 and preview['milliseconds'][key]['max'] >= 0
        assert preview['milliseconds']['jpeg_ms']['max'] > 0
        snapshot = engine.get_preview_snapshot()
        assert cv2.imdecode(np.frombuffer(snapshot['jpeg'], np.uint8), cv2.IMREAD_COLOR) is not None
        assert events == []
        assert all(not list((tmp_path/'media').rglob(pattern)) for pattern in ('*.jpg', '*.mp4'))
    finally:
        engine.stop()
    assert not engine.health()['preview_thread_alive'] and not engine.health()['capture_thread_alive']
