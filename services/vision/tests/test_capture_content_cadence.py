"""Exact-pixel duplicate read regression; controlled frames, not camera proof."""
from types import SimpleNamespace

import numpy as np
import pytest

from services.vision import capture as capture_module
from services.vision.capture import CaptureWorker


def run_reads(monkeypatch, reads, *, deduplicate=False):
    clock = SimpleNamespace(now=0.)
    packets, snapshots = [], []

    class Source:
        source_type = 'test_fixture'
        config = {}
        index = 0
        discontinuity = None
        closed = False

        def connect(self): return True

        def read_frame(self):
            snapshots.append(worker.health())
            if self.index == len(reads):
                worker.stop_event.set()
                return None
            at, frame, discontinuity = reads[self.index]
            self.index += 1
            clock.now = at
            self.discontinuity = (self.index, 'file_loop') if discontinuity else None
            return frame

        def consume_stream_discontinuity(self):
            value, self.discontinuity = self.discontinuity, None
            return value

        def health_check(self): return {'status': 'ended' if self.closed else 'online', 'fps': 30.}

        def disconnect(self): self.closed = True

    source = Source()
    worker = CaptureWorker(source, deduplicate_identical_frames=deduplicate, diagnostic_mode=True)
    worker._begin_source_session('start', increment_epoch=False)
    publish = worker.preview_frames.publish

    def collect(packet):
        packets.append(packet)
        publish(packet)

    monkeypatch.setattr(worker.preview_frames, 'publish', collect)
    monkeypatch.setattr(capture_module, 'time', SimpleNamespace(monotonic=lambda: clock.now,
        perf_counter=lambda: clock.now, time=lambda: 1_800_000_000+clock.now, sleep=lambda _: None))
    worker._run()
    return worker, packets, snapshots


def value(number):
    return np.full((12, 16, 3), number, np.uint8)


def test_exact_duplicate_reads_never_publish_or_advance_source_sequence(monkeypatch):
    one = value(12)
    changed = one.copy()
    changed[0, 0, 0] += 1  # Even one changed byte is a genuinely different frame.
    worker, packets, snapshots = run_reads(monkeypatch, [
        (1., one, False), (1.03, one.copy(), False), (1.06, changed, False),
        (1.09, one.copy(), False), (1.12, one.copy(), False),
    ], deduplicate=True)
    assert [packet.sequence for packet in packets] == [1, 2, 3]
    assert [packet.monotonic_time for packet in packets] == [1., 1.06, 1.09]
    assert len({packet.source_session_id for packet in packets}) == 1
    assert worker.health()['duplicate_source_reads'] == 2
    assert snapshots[-1]['raw_read_fps'] == 30
    assert snapshots[-1]['unique_pixel_fps'] == pytest.approx(2/.09)
    assert worker.frames.queue.empty() and worker.preview_frames.size == 0


def test_repeated_pixels_alone_never_stall_or_reset_valid_source_clock(monkeypatch):
    worker, packets, snapshots = run_reads(monkeypatch, [
        (1., value(10), False), (1.5, value(10), False), (3.1, value(10), False),
        (3.2, value(11), False), (3.3, value(12), False),
    ])
    unchanged = snapshots[3]
    assert unchanged['status'] == 'online'
    assert unchanged['fps'] == 30 and unchanged['unique_pixel_fps'] == 0
    assert unchanged['pixel_cadence_is_exposure_proof'] is False
    assert [packet.sequence for packet in packets] == [1, 2, 3, 4, 5]
    assert len({packet.source_session_id for packet in packets}) == 1
    assert packets[-1].reconnect_epoch == 0
    assert worker.session_reason == 'start'


def test_file_loop_same_first_pixels_are_a_new_session_not_a_duplicate(monkeypatch):
    worker, packets, _ = run_reads(monkeypatch, [
        (1., value(5), False), (1.03, value(5), True),
    ])
    assert len(packets) == 2
    assert packets[0].source_session_id != packets[1].source_session_id
    assert packets[1].sequence == 1
    assert worker.health()['duplicate_source_reads'] == 0


def test_noncontiguous_and_contiguous_equal_pixels_share_the_same_digest(monkeypatch):
    original = np.arange(12*16*3, dtype=np.uint8).reshape((12, 16, 3))
    view = original[:, ::-1]
    assert not view.flags.c_contiguous
    worker, packets, _ = run_reads(monkeypatch, [(1., view, False), (1.03, view.copy(), False)], deduplicate=True)
    assert len(packets) == 1
    assert worker.health()['duplicate_source_reads'] == 1


@pytest.mark.parametrize('value', [0, -1, float('nan'), float('inf'), 'true', None])
def test_content_filter_requires_explicit_boolean(value):
    with pytest.raises(ValueError):
        CaptureWorker(SimpleNamespace(config={}), deduplicate_identical_frames=value)
