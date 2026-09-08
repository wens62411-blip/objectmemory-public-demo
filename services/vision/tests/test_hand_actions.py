"""Synthetic landmark sequence contracts, not physical gesture accuracy claims."""
from dataclasses import replace
import math
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

import pytest

from services.vision.detectors.hands import HandObservation
from services.vision.hand_actions import HandActionAnalyzer


def hand(posture="open_palm", *, x=.5, y=.5, side="Left", score=.99, size=1):
    # Pixel-space geometry on a square canvas: wrist -> palm -> four fingers.
    points = [(0, 60), (-25, 35), (-45, 15), (-65, -5), (-85, -25)]
    for index, mcp_x in enumerate((-30, -10, 12, 32)):
        extended = posture == "open_palm" or posture == "pinch" or (posture == "pointing" and index == 0)
        if extended:
            points.extend([(mcp_x, 0), (mcp_x, -35), (mcp_x, -65), (mcp_x, -90)])
        else:
            points.extend([(mcp_x, 0), (mcp_x, -30), (mcp_x + 8, -9), (mcp_x + 5, 12)])
    if posture in {"closed_fist", "pointing"}:
        points[3], points[4] = (-20, 10), (-5, 5)
    if posture == "pinch":
        points[4] = (-30, -84)
    if posture == "unknown":
        points[7], points[8] = (-45, -48), (-64, -54)
    normalized = [(x + px * size / 1000, y + py * size / 1000) for px, py in points]
    xs, ys = zip(*normalized)
    # MediaPipeHandDetector returns pixel XYWH, not corner coordinates.
    bbox = (min(xs)*1000, min(ys)*1000, (max(xs)-min(xs))*1000, (max(ys)-min(ys))*1000)
    return HandObservation((x*1000, y*1000), bbox, normalized, None, side, score)


def update(analyzer, hands, frame, timestamp=None, session="test-sequence", shape=(1000, 1000, 3)):
    return analyzer.update(hands, shape, session, frame, frame * .1 if timestamp is None else timestamp)


@pytest.mark.parametrize("posture", ["open_palm", "closed_fist", "pinch", "pointing"])
def test_posture_requires_multiple_frames_and_is_geometry_not_grasp(posture):
    analyzer = HandActionAnalyzer()
    first = update(analyzer, [hand(posture)], 1)[0]
    second = update(analyzer, [hand(posture)], 2)[0]
    third = update(analyzer, [hand(posture)], 3)[0]
    assert first["hand_id"] == second["hand_id"] == third["hand_id"]
    assert first["posture"] == second["posture"] == "unknown"
    assert third["posture"] == posture and third["stable"] is True
    assert third["posture_stable_frames"] == 3
    assert third["evidence_type"] == "landmark_geometry_only"
    assert third["grasp_established"] is False and third["confidence"] is None
    assert third["source_session_id"] == "test-sequence" and third["source_frame"] == 3
    assert third["source_timestamp"] == pytest.approx(.3)


def test_uncertain_geometry_and_changed_posture_do_not_retain_prior_claim():
    analyzer = HandActionAnalyzer()
    for frame in range(1, 4):
        value = update(analyzer, [hand()], frame)[0]
    assert value["posture"] == "open_palm"
    value = update(analyzer, [hand("closed_fist")], 4)[0]
    assert value["posture"] == "unknown" and value["stable"] is False
    assert value["tracking_stable"] is True
    for frame in range(5, 9):
        value = update(analyzer, [hand("unknown")], frame)[0]
    assert value["posture"] == "unknown" and value["stable"] is False


@pytest.mark.parametrize("dx,dy,expected", [(.025,0,"right"),(-.025,0,"left"),(0,-.025,"up"),(0,.025,"down"),(0,0,"still")])
def test_motion_is_source_image_direction_from_multiple_observed_frames(dx, dy, expected):
    analyzer = HandActionAnalyzer()
    for frame in range(1, 4):
        value = update(analyzer, [hand(x=.5+(frame-1)*dx, y=.5+(frame-1)*dy)], frame)[0]
        if frame < 3:
            assert value["motion"] == "unknown"
    assert value["motion"] == expected and value["motion_stable"] is True
    assert value["motion_evidence"]["sample_count"] == 3
    assert value["coordinate_space"] == "source_normalized"


def test_diagonal_or_back_and_forth_motion_does_not_invent_axis_direction():
    analyzer = HandActionAnalyzer()
    for frame in range(1, 4):
        value = update(analyzer, [hand(x=.5+frame*.03, y=.5+frame*.03)], frame)[0]
    assert value["motion"] == "unknown"
    analyzer.reset()
    for frame, x in enumerate((.5, .57, .51), 1):
        value = update(analyzer, [hand(x=x)], frame)[0]
    assert value["motion"] == "unknown"


def test_known_handedness_preserves_ids_when_list_order_changes_and_hands_cross():
    analyzer = HandActionAnalyzer({"hand_action_match_max_distance": .3})
    first = update(analyzer, [hand(x=.43, side="Left"), hand(x=.57, side="Right")], 1)
    ids = {value["handedness"]: value["hand_id"] for value in first}
    next_frame = update(analyzer, [hand(x=.49, side="Right"), hand(x=.51, side="Left")], 2)
    assert {value["handedness"]: value["hand_id"] for value in next_frame} == ids


def test_ambiguous_same_side_or_untrusted_hands_get_new_ids_not_swapped():
    analyzer = HandActionAnalyzer()
    first = update(analyzer, [hand(x=.45, side=None), hand(x=.55, side=None)], 1)
    second = update(analyzer, [hand(x=.495, side=None), hand(x=.505, side=None)], 2)
    assert not {value["hand_id"] for value in first} & {value["hand_id"] for value in second}
    assert all(value["association"] == "ambiguous_new_identity" for value in second)
    assert all(value["posture_stable_frames"] == 1 for value in second)


def test_handedness_flip_and_short_missing_hand_cannot_continue_old_identity():
    analyzer = HandActionAnalyzer()
    first = update(analyzer, [hand(side="Left")], 1)[0]
    flipped = update(analyzer, [hand(side="Right")], 2)[0]
    assert first["hand_id"] != flipped["hand_id"]
    assert update(analyzer, [], 3) == []
    reappeared = update(analyzer, [hand(side="Right")], 4)[0]
    assert reappeared["hand_id"] != flipped["hand_id"]
    assert reappeared["posture_stable_frames"] == 1


def test_one_missing_hand_does_not_reset_other_visible_hand():
    analyzer = HandActionAnalyzer()
    first = update(analyzer, [hand(x=.3, side="Left"), hand(x=.7, side="Right")], 1)
    second = update(analyzer, [hand(x=.7, side="Right")], 2)[0]
    third = update(analyzer, [hand(x=.3, side="Left"), hand(x=.7, side="Right")], 3)
    assert second["hand_id"] == first[1]["hand_id"] == third[1]["hand_id"]
    assert third[0]["hand_id"] != first[0]["hand_id"]


@pytest.mark.parametrize("change", ["session", "gap", "geometry", "duplicate", "backward_frame", "backward_time"])
def test_source_discontinuities_require_new_hand_identity_and_stability(change):
    analyzer = HandActionAnalyzer()
    original = update(analyzer, [hand()], 4, timestamp=1)[0]
    kwargs = {"frame": 5, "timestamp": 1.1}
    if change == "session": kwargs["session"] = "new-session"
    if change == "gap": kwargs["timestamp"] = 2
    if change == "geometry": kwargs["shape"] = (1001, 1000, 3)
    if change == "duplicate": kwargs["frame"] = 4
    if change == "backward_frame": kwargs["frame"] = 3
    if change == "backward_time": kwargs["timestamp"] = .9
    result = update(analyzer, [hand()], **kwargs)
    if change in {"duplicate", "backward_frame", "backward_time"}:
        assert result == []
        result = update(analyzer, [hand()], 6, timestamp=1.2)
    assert result[0]["hand_id"] != original["hand_id"]
    assert result[0]["posture"] == "unknown" and result[0]["posture_stable_frames"] == 1


def test_bad_landmarks_are_rejected_and_do_not_update_stability():
    analyzer = HandActionAnalyzer()
    old = update(analyzer, [hand()], 1)[0]
    for frame, bad in enumerate(([], [(math.nan,0)]*21, [(0,0)]*21), 2):
        assert update(analyzer, [replace(hand(), landmarks=bad)], frame) == []
    recovered = update(analyzer, [hand()], 5)[0]
    assert recovered["hand_id"] != old["hand_id"] and recovered["posture"] == "unknown"


def test_history_and_identity_storage_remain_bounded_and_returned_data_is_detached():
    analyzer = HandActionAnalyzer({"hand_action_history_size": 5})
    for frame in range(1, 1001):
        values = update(analyzer, [hand(x=.3), hand(x=.7, side="Right")], frame)
    assert analyzer.health()["active_tracks"] == 2
    assert analyzer.health()["history_samples"] <= 10
    values[0]["landmarks"][0][0] = 999
    next_values = update(analyzer, [hand(x=.3), hand(x=.7, side="Right")], 1001)
    assert next_values[0]["landmarks"][0][0] < 1


def test_configured_stability_threshold_applies_and_invalid_settings_fail_closed():
    analyzer = HandActionAnalyzer({"hand_action_min_stable_frames": 4})
    for frame in range(1, 4):
        assert update(analyzer, [hand()], frame)[0]["posture"] == "unknown"
    assert update(analyzer, [hand()], 4)[0]["posture"] == "open_palm"
    for settings in ({"hand_action_max_gap_seconds": math.nan}, {"hand_action_history_size": 100000},
                     {"hand_action_min_stable_frames": True}, {"hand_action_pinch_ratio": -1}):
        with pytest.raises(ValueError):
            HandActionAnalyzer(settings)


def test_invalid_source_metadata_and_more_than_two_hands_do_not_create_actions():
    analyzer = HandActionAnalyzer()
    for arguments in (("", 1, .1), ("test", True, .1), ("test", 1, math.nan)):
        with pytest.raises(ValueError):
            analyzer.update([hand()], (1000,1000,3), *arguments)
    assert update(analyzer, [hand(),hand(),hand()], 1) == []
    assert analyzer.health()["active_tracks"] == 0


def test_replayed_old_frames_never_establish_stable_identity_after_watermark():
    analyzer = HandActionAnalyzer()
    update(analyzer, [hand()], 100, timestamp=10)
    for frame in range(1, 10):
        assert update(analyzer, [hand()], frame) == []
    recovered = update(analyzer, [hand()], 101, timestamp=10.1)[0]
    assert recovered["tracking_stable"] is False and recovered["tracking_frames"] == 1


def test_five_fps_unknown_posture_has_independent_stable_tracking_and_motion():
    analyzer = HandActionAnalyzer()
    for frame in range(1, 4):
        value = update(analyzer, [hand("unknown", x=.4+frame*.025)], frame, timestamp=frame/5)[0]
    assert value["posture"] == "unknown" and value["stable"] is False
    assert value["tracking_stable"] is True and value["motion"] == "right"
    assert value["tracking_frames"] == 3 and value["tracking_duration_seconds"] == pytest.approx(.4)


def test_actual_hand_observation_bbox_is_nonorigin_xywh_and_stays_xywh():
    observation = hand(x=.75, y=.7)
    assert observation.bbox[0] > observation.bbox[2]  # left is greater than width
    actions = update(HandActionAnalyzer(), [observation], 1)
    assert len(actions) == 1
    assert actions[0]["bbox"] == pytest.approx([value/1000 for value in observation.bbox])
    assert actions[0]["bbox_format"] == "xywh"


def test_four_extended_fingers_give_conservative_feedback_without_looser_thumb_gate():
    original = hand()
    # Thumb stays folded against the palm; all four non-thumb fingers extend.
    points = list(original.landmarks)
    points[3], points[4] = (.48, .51), (.495, .505)
    observation = replace(original, landmarks=points)
    analyzer = HandActionAnalyzer()
    for frame in range(1, 4):
        value = update(analyzer, [observation], frame)[0]
        if frame < 3:
            assert value["posture"] == "unknown"
    assert value["posture"] == "fingers_extended"
    assert value["posture_label"] == "四指展开（几何推断）"
    assert value["stable"] is True and value["tracking_stable"] is True
    assert value["posture_evidence"]["finger_states"] == ["extended"]*4
    assert value["grasp_established"] is False


@pytest.mark.parametrize("posture", ["open_palm", "closed_fist", "pinch", "pointing"])
@pytest.mark.parametrize("transform", ["rotate_clockwise", "mirror"])
def test_posture_geometry_is_rotation_and_mirror_invariant(posture, transform):
    original = hand(posture)
    points = [(1-y, x) if transform == "rotate_clockwise" else (1-x, y) for x, y in original.landmarks]
    xs, ys = zip(*points)
    transformed = replace(original, landmarks=points,
                          bbox=(min(xs)*1000, min(ys)*1000, (max(xs)-min(xs))*1000, (max(ys)-min(ys))*1000))
    analyzer = HandActionAnalyzer()
    for frame in range(1, 4):
        action = update(analyzer, [transformed], frame)[0]
    assert action["posture"] == posture


def test_actual_local_public_photo_hands_are_accepted_with_true_xywh_at_five_fps():
    from services.vision.detectors.hands import MediaPipeHandDetector

    root = Path(__file__).resolve().parents[3]
    public_photo = root/"data/temporary/official-mediapipe-woman-hands.jpg"
    model = root/"data/models/hand_landmarker.task"
    if not public_photo.is_file() or not model.is_file():
        pytest.skip("Actual local public photo/model unavailable; no download or synthetic replacement")
    image = cv2.imdecode(np.frombuffer(public_photo.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    with patch("socket.socket", side_effect=AssertionError("No network in actual local model test")), \
         patch("cv2.VideoCapture", side_effect=AssertionError("No physical camera in public-photo test")):
        detector = MediaPipeHandDetector(True, 2, model)
        analyzer = HandActionAnalyzer()
        try:
            assert detector.available, detector.error
            for frame in range(1, 5):
                hands = detector.detect(image, timestamp_ms=frame*200)
                actions = analyzer.update(hands, image.shape, "public-photo-only", frame, frame/5)
                assert len(hands) == len(actions) == 2
                for detected, action in zip(hands, actions):
                    assert len(action["landmarks"]) == 21
                    assert action["bbox"] == pytest.approx([detected.bbox[0]/image.shape[1], detected.bbox[1]/image.shape[0],
                                                           detected.bbox[2]/image.shape[1], detected.bbox[3]/image.shape[0]])
                    assert action["confidence"] is None and action["grasp_established"] is False
            assert all(action["tracking_stable"] and action["motion"] == "still" for action in actions)
            assert all(action["posture"] == "fingers_extended" for action in actions)
        finally:
            detector.close()
