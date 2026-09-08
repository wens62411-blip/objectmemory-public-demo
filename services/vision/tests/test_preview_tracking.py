import cv2
import numpy as np
import pytest
from services.vision.preview_tracking import PreviewTracker, PreviewTrackingSettings


def frame(dx=0):
    value = np.zeros((240, 320, 3), np.uint8)
    # Textured rigid synthetic target; actual optical flow, not mocked offsets.
    texture = np.random.default_rng(7).integers(40, 245, (80, 60, 3), dtype=np.uint8)
    value[70:150, 80+dx:140+dx] = texture
    return value


def candidate():
    return {'bbox': [80/320, 70/240, 60/320, 80/240], 'accepted': True,
            'item_id': 'registered', 'best_item_id': 'registered', 'category': 'cell phone'}


def test_actual_flow_tracks_target_on_current_frame_without_identity_proof():
    tracker = PreviewTracker()
    tracker.offer(frame(), [candidate()], 's', 1, 1.)
    for i in range(1, 16):
        results = tracker.update(frame(i), 's', i+1, 1+i/30)
        assert len(results) == 1
        assert abs(results[0]['bbox'][0] - (80+i)/320) < .01
        assert results[0]['source_frame'] == i+1
        assert results[0]['identity_verified_at_frame'] == 1
        assert results[0]['visual_only'] and not results[0]['observation_evidence']


@pytest.mark.parametrize('mode', ['occluded', 'expired', 'session', 'gap', 'profile'])
def test_invalid_flow_never_reuses_old_box(mode):
    tracker = PreviewTracker()
    tracker.offer(frame(), [candidate()], 's', 1, 1.)
    assert tracker.update(frame(2), 's', 2, 1.03)
    if mode == 'profile': tracker.invalidate()
    output = tracker.update(np.zeros_like(frame()) if mode == 'occluded' else frame(3),
                            'new' if mode == 'session' else 's', 3,
                            2 if mode == 'expired' else 1.5 if mode == 'gap' else 1.06)
    assert not output


def test_delayed_detector_seed_is_measured_not_pasted():
    tracker = PreviewTracker()
    tracker.offer(frame(), [candidate()], 's', 1, 1.)
    result = tracker.update(frame(12), 's', 10, 1.3)
    assert result and abs(result[0]['bbox'][0] - 92/320) < .01


@pytest.mark.parametrize('textured', [True, False])
def test_exact_detector_frame_displays_immediately_even_without_trackable_texture(textured):
    image = frame() if textured else np.zeros_like(frame())
    tracker = PreviewTracker()
    tracker.offer(image, [candidate()], 's', 1, 1.)
    result = tracker.update(image.copy(), 's', 1, 1.)
    assert len(result) == 1
    assert result[0]['bbox'] == candidate()['bbox']
    assert result[0]['source_frame'] == result[0]['identity_verified_at_frame'] == 1
    assert result[0]['tracker_backend'] == 'detector_same_frame'
    assert result[0]['visual_only'] and not result[0]['observation_evidence']
    assert not tracker.update(image, 's', 1, 1.)  # No repeated delivery.
    if not textured:
        assert not tracker.update(image, 's', 2, 1.03)  # No texture, no propagated box.


@pytest.mark.parametrize('mismatch', ['pixels', 'timestamp', 'session'])
def test_same_number_alone_does_not_prove_same_detector_frame(mismatch):
    tracker = PreviewTracker()
    tracker.offer(frame(), [candidate()], 's', 1, 1.)
    result = tracker.update(frame(2) if mismatch == 'pixels' else frame(),
                            'other' if mismatch == 'session' else 's', 1,
                            1.02 if mismatch == 'timestamp' else 1.)
    assert not result


@pytest.mark.parametrize('settings', [{'max_width': float('nan')}, {'max_candidates': True}, {'max_identity_age_seconds': 100}, {'max_features': 8, 'min_features': 20}])
def test_configuration_is_bounded(settings):
    with pytest.raises(ValueError): PreviewTrackingSettings(**settings)


def two_targets(dx=0, dy=0):
    image = frame(dx)
    texture = np.random.default_rng(19).integers(40, 245, (60, 65, 3), dtype=np.uint8)
    image[50+dy:110+dy, 205:270] = texture
    return image


def test_multiple_tracks_share_one_forward_backward_measurement(monkeypatch):
    calls = []
    native = cv2.calcOpticalFlowPyrLK

    def measured(*args, **kwargs):
        calls.append(len(args[2]))
        return native(*args, **kwargs)

    monkeypatch.setattr(cv2, 'calcOpticalFlowPyrLK', measured)
    rows = [candidate(), {**candidate(), 'item_id': 'second', 'best_item_id': 'second',
                         'bbox': [205/320, 50/240, 65/320, 60/240]}]
    tracker = PreviewTracker()
    tracker.offer(two_targets(), rows, 's', 1, 1.)
    output = tracker.update(two_targets(2, 3), 's', 2, 1.03)
    assert len(output) == 2
    assert len(calls) == 2  # One forward/backward pair, not one pair per item.
    assert calls[0] == calls[1] == 96
    assert abs(output[0]['bbox'][0] - 82/320) < .005
    assert abs(output[1]['bbox'][1] - 53/240) < .005
    assert [row['best_item_id'] for row in output] == ['registered', 'second']
    assert all(row['visual_only'] and not row['observation_evidence'] for row in output)


def test_idle_tracker_does_not_rescale_pixels(monkeypatch):
    tracker = PreviewTracker()

    def unexpected(_frame):
        raise AssertionError('No active track or pending seed needs pixels')

    monkeypatch.setattr(tracker, '_gray_image', unexpected)
    for index in range(10):
        assert tracker.update(frame(), 's', index, index/30) == []


def test_batched_tracks_reject_occluded_item_without_losing_other_item():
    rows = [candidate(), {**candidate(), 'item_id': 'second', 'best_item_id': 'second',
                         'bbox': [205/320, 50/240, 65/320, 60/240]}]
    tracker = PreviewTracker()
    tracker.offer(two_targets(), rows, 's', 1, 1.)
    output = tracker.update(frame(2), 's', 2, 1.03)
    assert len(output) == 1
    assert output[0]['best_item_id'] == 'registered'
    assert abs(output[0]['bbox'][0] - 82/320) < .005


@pytest.mark.parametrize('fail_at', [1, 2])
def test_batched_native_tracking_failure_invalidates_old_points(monkeypatch, fail_at):
    tracker = PreviewTracker()
    tracker.offer(frame(), [candidate()], 's', 1, 1.)
    native = cv2.calcOpticalFlowPyrLK
    calls = 0

    def failing(*args, **kwargs):
        nonlocal calls
        calls += 1
        return (None, None, None) if calls == fail_at else native(*args, **kwargs)

    monkeypatch.setattr(cv2, 'calcOpticalFlowPyrLK', failing)
    assert tracker.update(frame(2), 's', 2, 1.03) == []
    assert tracker._tracks == []
    assert tracker.update(frame(3), 's', 3, 1.06) == []
