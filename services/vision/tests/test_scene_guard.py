"""Real ORB on synthetic textures; no physical camera or real-world accuracy claim.

Only the two explicitly named affine-fault tests replace the geometry estimator.
All keypoints, descriptors and matches, including those cases, are computed by
the actual OpenCV implementation. No engine/business code is patched here.
"""
import hashlib
import json
import time

import cv2
import numpy as np
import pytest

from services.vision.scene_guard import SceneGuard


@pytest.fixture(scope="module")
def rich_texture():
    image = np.random.default_rng(20260905).integers(20, 235, (480, 640, 3), np.uint8)
    image = cv2.GaussianBlur(image, (3, 3), .5)
    for y in (65, 165, 280, 400):
        for x in (65, 200, 370, 560):
            cv2.rectangle(image, (x - 15, y - 12), (x + 15, y + 12), (10, 20, 30), 3)
    return image


@pytest.fixture(scope="module")
def actual_results(tmp_path_factory):
    folder = tmp_path_factory.mktemp("scene-orb-evidence")
    data = {"input_kind": "synthetic_texture_arrays_not_a_physical_camera", "opencv": cv2.__version__,
            "physical_camera_used": False, "model_downloaded": False, "measurements": {}}
    yield data["measurements"]
    (folder / "scene-orb-result.json").write_text(json.dumps(data, indent=2), encoding="utf-8")


def shifted(image, dx, dy=0):
    return cv2.warpAffine(image, np.float32([[1, 0, dx], [0, 1, dy]]),
                          (image.shape[1], image.shape[0]), borderMode=cv2.BORDER_CONSTANT)


def measure(guard, frame, timestamp, results, key):
    started = time.perf_counter()
    changed = guard.check(frame, timestamp)
    results[key] = {**guard.last_result, "returned_changed": changed,
                   "elapsed_ms": (time.perf_counter() - started) * 1000,
                   "input_sha256": hashlib.sha256(frame.tobytes()).hexdigest()}
    return changed


def test_real_orb_detects_global_translation_across_background_quadrants(rich_texture, actual_results):
    guard = SceneGuard(0, .04)
    guard.establish(rich_texture)
    assert measure(guard, shifted(rich_texture, 48), 1, actual_results, "global_translation") is True
    assert guard.last_result["status"] == "changed"
    assert guard.last_result["inliers"] >= 25 and guard.last_result["quadrants"] >= 3
    assert guard.last_result["normalized_shift"] >= .04


@pytest.mark.parametrize("shift", [0, 2])
def test_static_and_tiny_shift_do_not_invalidate_scene(rich_texture, actual_results, shift):
    guard = SceneGuard(0, .04)
    guard.establish(rich_texture)
    image = rich_texture if shift == 0 else shifted(rich_texture, shift)
    assert measure(guard, image, 1, actual_results, "static" if shift == 0 else "tiny_shift") is False
    assert guard.last_result["status"] == "stable"
    assert guard.last_result["normalized_shift"] < .04


def test_small_foreground_patch_motion_is_not_camera_translation(rich_texture, actual_results):
    before, after = rich_texture.copy(), rich_texture.copy()
    patch = np.random.default_rng(17).integers(0, 255, (60, 60, 3), np.uint8)
    before[200:260, 250:310] = patch
    after[200:260, 340:400] = patch
    guard = SceneGuard(0, .04)
    guard.establish(before)
    assert measure(guard, after, 1, actual_results, "moving_foreground_patch") is False
    assert guard.last_result["status"] == "stable"
    assert guard.last_result["normalized_shift"] < .01


def test_check_interval_skips_feature_work_but_next_due_frame_detects_shift(rich_texture, actual_results, monkeypatch):
    guard = SceneGuard(3, .04)
    guard.establish(rich_texture)
    assert not guard.check(rich_texture, 10)
    real_features, calls = guard._features, []
    def counted(frame):
        calls.append(frame.shape)
        return real_features(frame)
    monkeypatch.setattr(guard, "_features", counted)
    translated = shifted(rich_texture, 48)
    assert not guard.check(translated, 11)
    assert calls == []
    assert measure(guard, translated, 13, actual_results, "interval_due_shift")
    assert len(calls) == 1


def test_aspect_change_is_detected_and_equal_aspect_resize_is_not_a_view_shift(rich_texture, actual_results):
    guard = SceneGuard(0, .04)
    guard.establish(rich_texture)
    resized = cv2.resize(rich_texture, (1280, 960), interpolation=cv2.INTER_NEAREST)
    assert not measure(guard, resized, 1, actual_results, "equal_aspect_resolution_change")
    assert guard.last_result["status"] == "stable"
    stretched = cv2.resize(rich_texture, (640, 360))
    assert measure(guard, stretched, 2, actual_results, "aspect_ratio_change")
    assert guard.last_result["reason"] == "aspect_ratio_changed"
    # Raw resolution changes are separately rejected by engine FrameGeometry;
    # ORB's normalized working frame is not the authority for original sizes.


def test_texture_loss_is_insufficient_not_stable_or_proven_camera_motion(rich_texture, actual_results):
    guard = SceneGuard(0, .04)
    guard.establish(rich_texture)
    assert not guard.check(rich_texture, 0)
    assert not measure(guard, np.zeros_like(rich_texture), 1, actual_results, "texture_loss")
    assert guard.last_result["status"] == "insufficient_detail"


def test_featureless_baseline_does_not_become_verified_stable(actual_results):
    image = np.full((480, 640, 3), 120, np.uint8)
    guard = SceneGuard(0, .04)
    guard.establish(image)
    assert not measure(guard, image, 1, actual_results, "featureless_baseline")
    assert guard.last_result["status"] == "insufficient_detail"


def test_unrelated_texture_reports_insufficient_matches_not_stable(rich_texture, actual_results):
    guard = SceneGuard(0, .04)
    guard.establish(rich_texture)
    unrelated = np.random.default_rng(901).integers(0, 255, rich_texture.shape, np.uint8)
    assert not measure(guard, unrelated, 1, actual_results, "unrelated_texture")
    assert guard.last_result["status"] == "insufficient_matches"


def test_affine_failure_cannot_keep_previous_stable_status(rich_texture, actual_results, monkeypatch):
    guard = SceneGuard(0, .04)
    guard.establish(rich_texture)
    assert not guard.check(rich_texture, 0)
    assert guard.last_result["status"] == "stable"
    # Explicit estimator fault injection; ORB and BF matching remain actual.
    monkeypatch.setattr(cv2, "estimateAffinePartial2D", lambda *_args, **_kwargs: (None, None))
    assert not measure(guard, rich_texture, 1, actual_results, "injected_affine_failure")
    assert guard.last_result["status"] == "insufficient_consensus"


def test_one_quadrant_consensus_is_not_claimed_stable(rich_texture, actual_results, monkeypatch):
    guard = SceneGuard(0, .04)
    guard.establish(rich_texture)
    def limited_consensus(before, _after, **_kwargs):
        accepted = ((before[:, 0] < 200) & (before[:, 1] < 150)).astype(np.uint8).reshape(-1, 1)
        return np.float64([[1, 0, 0], [0, 1, 0]]), accepted
    # Explicit estimator consensus fault; features/matches are still computed.
    monkeypatch.setattr(cv2, "estimateAffinePartial2D", limited_consensus)
    assert not measure(guard, rich_texture, 1, actual_results, "injected_one_quadrant_consensus")
    assert guard.last_result["status"] == "insufficient_consensus"


def test_reestablish_clears_prior_view_comparison(rich_texture, actual_results):
    guard = SceneGuard(0, .04)
    guard.establish(rich_texture)
    translated = shifted(rich_texture, 48)
    assert guard.check(translated, 1)
    guard.establish(translated)
    assert not measure(guard, translated, 2, actual_results, "reestablished_view")
    assert guard.last_result["status"] == "stable"


def test_feature_exception_clears_historical_stable_through_throttle_then_recovers(rich_texture, actual_results, monkeypatch):
    guard = SceneGuard(3, .04)
    guard.establish(rich_texture)
    assert not guard.check(rich_texture, 0)
    assert guard.last_result["status"] == "stable"
    features = guard._features
    def broken(_frame):
        raise cv2.error("controlled feature failure")
    monkeypatch.setattr(guard, "_features", broken)
    assert not measure(guard, rich_texture, 3, actual_results, "injected_feature_exception")
    assert guard.last_result["status"] == "failed"
    assert "controlled feature failure" in guard.last_result["error"]
    assert not guard.check(rich_texture, 4)
    assert guard.last_result["status"] == "failed", "Interval skip must not restore stale stability"
    monkeypatch.setattr(guard, "_features", features)
    assert not guard.check(rich_texture, 6)
    assert guard.last_result["status"] == "stable"


def test_affine_exception_is_reported_without_stale_stability(rich_texture, actual_results, monkeypatch):
    guard = SceneGuard(0, .04)
    guard.establish(rich_texture)
    assert not guard.check(rich_texture, 0)
    def broken(*_args, **_kwargs):
        raise RuntimeError("controlled estimator failure")
    monkeypatch.setattr(cv2, "estimateAffinePartial2D", broken)
    assert not measure(guard, rich_texture, 1, actual_results, "injected_affine_exception")
    assert guard.last_result["status"] == "failed"


def test_establish_failure_drops_previous_reference_and_does_not_raise(rich_texture, actual_results, monkeypatch):
    guard = SceneGuard(0, .04)
    assert guard.establish(rich_texture) is True
    assert not guard.check(rich_texture, 0)
    features = guard._features
    monkeypatch.setattr(guard, "_features", lambda _frame: (_ for _ in ()).throw(ValueError("controlled baseline failure")))
    assert guard.establish(rich_texture) is False
    assert guard.reference is None and guard.last_result["status"] == "failed"
    assert not measure(guard, rich_texture, 1, actual_results, "injected_baseline_exception")
    assert guard.reference is None and guard.last_result["status"] == "failed"
    monkeypatch.setattr(guard, "_features", features)
    assert guard.establish(rich_texture) is True
    assert guard.last_result["status"] == "baseline"


def test_zero_affine_inliers_are_insufficient_and_json_safe(rich_texture, actual_results, monkeypatch):
    guard = SceneGuard(0, .04)
    guard.establish(rich_texture)
    monkeypatch.setattr(cv2, "estimateAffinePartial2D", lambda before, *_args, **_kwargs:
                        (np.float64([[1, 0, 0], [0, 1, 0]]), np.zeros((len(before), 1), np.uint8)))
    assert not measure(guard, rich_texture, 1, actual_results, "injected_zero_inliers")
    assert guard.last_result["status"] == "insufficient_consensus"
    assert guard.last_result["normalized_shift"] is None
    json.dumps(guard.last_result, allow_nan=False)
