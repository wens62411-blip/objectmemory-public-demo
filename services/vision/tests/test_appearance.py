"""Contract units are synthetic; local ONNX tests exercise real pinned weights, not a camera."""
import copy
import hashlib
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
import pytest
from PIL import Image

from services.vision.detectors.appearance import (
    AppearanceEncoder, AppearanceEncodingError, DEFAULT_MODEL_PATH, DIMENSION,
    MODEL_ID, MODEL_VERSION, ProfileMatcher, _appearance_threads, preprocess_crop,
)

LEGACY_V1 = '8b1f705a3a7f6f062f6bdd21986c1583d3ef105d:onnx-fp32-cls-rgb-bicubic256-centercrop224-l2-v1'
WHOLE_CROP_V2 = '8b1f705a3a7f6f062f6bdd21986c1583d3ef105d:onnx-fp32-cls-rgb-bicubic-fit224-blackpad-l2-v2'


@pytest.mark.parametrize('count, expected', [(None, 1), (0, 1), (1, 1), (2, 2), (4, 4), (24, 4)])
def test_appearance_thread_default_is_bounded_by_hardware(monkeypatch, count, expected):
    monkeypatch.delenv('OM_APPEARANCE_THREADS', raising=False)
    monkeypatch.setattr(os, 'cpu_count', lambda: count)
    assert _appearance_threads() == expected


@pytest.mark.parametrize('value', ['1', '2', '3', '4'])
def test_appearance_thread_override_accepts_only_bounded_integer_budget(monkeypatch, value):
    monkeypatch.setenv('OM_APPEARANCE_THREADS', value)
    monkeypatch.setattr(os, 'cpu_count', lambda: 24)
    assert _appearance_threads() == int(value)


@pytest.mark.parametrize('value', ['', '0', '-1', '5', '999999999999999999999999', '1.0', 'NaN', 'true', 'auto'])
def test_invalid_appearance_thread_override_falls_back_to_hardware_default(monkeypatch, value):
    monkeypatch.setenv('OM_APPEARANCE_THREADS', value)
    monkeypatch.setattr(os, 'cpu_count', lambda: 2)
    assert _appearance_threads() == 2


def test_actual_session_thread_budget_is_applied_and_part_of_cache_identity(monkeypatch):
    if not DEFAULT_MODEL_PATH.is_file():
        pytest.skip('Explicit preparation has not installed the optional pinned local model')
    pytest.importorskip('onnxruntime')
    encoders = []
    try:
        for value in ('2', '4', '4'):
            monkeypatch.setenv('OM_APPEARANCE_THREADS', value)
            encoder = AppearanceEncoder()
            encoders.append(encoder)
            assert encoder.health()['available'], encoder.health()
            assert encoder.health()['intra_op_num_threads'] == int(value)
            assert encoder._bundle.session.get_session_options().intra_op_num_threads == int(value)
        assert encoders[0]._bundle is not encoders[1]._bundle
        assert encoders[1]._bundle is encoders[2]._bundle
    finally:
        for encoder in encoders:
            encoder.close()


class UnitEncoder:
    """Explicit test double for gate tests only; never used by production inference."""
    model_id, model_version, dimension = MODEL_ID, MODEL_VERSION, DIMENSION

    def __init__(self, available=True, vector=None):
        self.is_available = available
        self.vector = vector or [1.0] + [0.0] * (DIMENSION - 1)

    def health(self):
        return {"available": self.is_available, "error": None if self.is_available else "missing"}

    def encode(self, _crop):
        return self.vector


def profile(item_id="phone-a", category="手机", **changes):
    value = {"status": "ready", "profile_version": 1, "model_id": MODEL_ID,
             "model_version": MODEL_VERSION, "dimension": DIMENSION,
             "embeddings": [[1.0] + [0.0] * (DIMENSION - 1)]}
    value.update(changes)
    return {"id": item_id, "type": category, "appearance_profile": value}


def test_preprocess_application_whole_crop_rgb_normalization_and_nchw():
    crop = np.full((200, 400, 3), [255, 0, 0], np.uint8)
    actual = preprocess_crop(crop)
    assert actual.shape == (1, 3, 224, 224) and actual.dtype == np.float32
    np.testing.assert_allclose(actual[0, :, 112, 112], [(0 - .485) / .229, (0 - .456) / .224, (1 - .406) / .225], rtol=1e-6)
    np.testing.assert_allclose(actual[0, :, 10, 10], [(0 - .485) / .229, (0 - .456) / .224, (0 - .406) / .225], rtol=1e-6)


@pytest.mark.parametrize('shape', [(224, 224), (53, 171), (181, 47)])
def test_preprocess_preserves_v2_bits_for_strided_inputs(shape):
    height, width = shape
    source = np.random.default_rng(11).integers(0, 256, (height*2, width*2, 3), dtype=np.uint8)
    crop = source[::2, ::2, ::-1]
    before = source.copy()
    scale = min(224/width, 224/height)
    size = max(1, round(width*scale)), max(1, round(height*scale))
    resized = Image.fromarray(np.ascontiguousarray(crop[:, :, ::-1])).resize(size, Image.Resampling.BICUBIC)
    padded = Image.new('RGB', (224, 224), (0, 0, 0))
    padded.paste(resized, ((224-size[0])//2, (224-size[1])//2))
    # Freeze the previous HWC operation order: v2 profiles require identical pixels.
    expected = np.asarray(padded, np.float32) / np.float32(255)
    expected = (expected - np.asarray([.485, .456, .406], np.float32)) / np.asarray([.229, .224, .225], np.float32)
    actual = preprocess_crop(crop)
    np.testing.assert_array_equal(actual, expected.transpose(2, 0, 1)[None])
    np.testing.assert_array_equal(source, before)
    assert actual.flags.c_contiguous and actual.dtype == np.float32


@pytest.mark.parametrize('portrait', [False, True])
def test_preprocess_preserves_both_ends_of_a_long_object_and_does_not_mutate(portrait):
    crop = np.full((40, 200, 3), [0, 200, 0], np.uint8)
    crop[:, :20] = [0, 0, 255]
    crop[:, -20:] = [255, 0, 0]
    if portrait:
        crop = crop.transpose(1, 0, 2)
    before = crop.copy()
    tensor = preprocess_crop(crop)
    pixels = (tensor[0].transpose(1, 2, 0) * np.asarray([.229,.224,.225]) + np.asarray([.485,.456,.406])) * 255
    assert tensor.flags.c_contiguous
    first,last = ((1,112),(222,112)) if portrait else ((112,1),(112,222))
    np.testing.assert_allclose(pixels[first], [255,0,0], atol=.001)
    np.testing.assert_allclose(pixels[last], [0,0,255], atol=.001)
    np.testing.assert_allclose(pixels[0,0], [0,0,0], atol=.001)
    np.testing.assert_array_equal(crop,before)


def test_preprocess_memory_and_aspect_limits_remain_bounded():
    oversized = np.broadcast_to(np.zeros((1,1,3),np.uint8),(4097,4097,3))
    with pytest.raises(ValueError):
        preprocess_crop(oversized)
    with pytest.raises(ValueError):
        preprocess_crop(np.zeros((1,33,3),np.uint8))
    assert preprocess_crop(np.zeros((1,32,3),np.uint8)).shape == (1,3,224,224)


def test_preprocessing_version_changes_without_changing_pinned_weights():
    from services.vision.detectors.appearance import MANIFEST
    assert MODEL_VERSION == WHOLE_CROP_V2
    assert MANIFEST['sha256'] == 'f22797eabf810a75e41de68d378541ebea372122b25c4ce3ef25ff618250c20a'
    assert MANIFEST['bytes'] == 88532934
    assert MANIFEST['preprocessing']['scope'] == 'application_full_object_crop'
    assert MANIFEST['preprocessing']['center_crop'] is False
    assert MANIFEST['preprocessing']['letterbox'] is True
    matcher = ProfileMatcher([profile(model_version=LEGACY_V1)], UnitEncoder())
    assert matcher.loaded_profile_versions == {}
    assert matcher.invalid_profiles['phone-a'] == 'model_version_mismatch'


def test_real_spatial_features_exclude_padding_and_keep_source_coordinates():
    if not DEFAULT_MODEL_PATH.is_file():
        pytest.skip('Explicit preparation has not installed the optional pinned local model')
    pytest.importorskip('onnxruntime')
    encoder = AppearanceEncoder()
    assert encoder.health()['available'], encoder.health()
    try:
        frame = np.full((120, 320, 3), [40, 110, 190], np.uint8)
        before = frame.copy()
        features, xy, size = encoder.encode_patches(frame)
        assert features.shape == (len(xy), DIMENSION) and len(xy) > 0
        assert np.isfinite(features).all() and np.isfinite(xy).all()
        np.testing.assert_allclose(np.linalg.norm(features, axis=1), 1, atol=1e-5)
        assert np.all((xy[:, 0] > 0) & (xy[:, 0] < 320))
        assert np.all((xy[:, 1] > 0) & (xy[:, 1] < 120))
        assert size == pytest.approx((20, 20))
        np.testing.assert_array_equal(frame, before)
        assert len(encoder.encode(frame)) == DIMENSION
    finally:
        encoder.close()


@pytest.mark.parametrize("crop", [np.zeros((10, 10)), np.zeros((10, 10, 3), np.float32),
    np.zeros((0, 10, 3), np.uint8), np.zeros((1, 10000, 3), np.uint8)])
def test_preprocess_rejects_invalid_or_unbounded_crop(crop):
    with pytest.raises(ValueError):
        preprocess_crop(crop)


def test_missing_or_wrong_model_fails_without_network(tmp_path):
    with patch("socket.socket", side_effect=AssertionError("network forbidden")):
        missing = AppearanceEncoder(tmp_path / "missing.onnx")
        assert missing.health()["error_code"] == "model_missing"
        with pytest.raises(AppearanceEncodingError):
            missing.encode(np.zeros((32, 32, 3), np.uint8))
        bad_path = tmp_path / "bad.onnx"
        bad_path.write_bytes(b"not a model")
        wrong = AppearanceEncoder(bad_path)
        assert wrong.health()["error_code"] == "model_hash_mismatch"
        assert not wrong.health()["available"]


def test_unavailable_encoder_never_reports_loaded_profiles():
    matcher = ProfileMatcher([profile()], UnitEncoder(available=False))
    assert not matcher.available and matcher.loaded_profile_versions == {}
    assert matcher.match(None)["rejection"] == "encoder_unavailable"


@pytest.mark.parametrize("changes,reason", [
    ({"status": "draft"}, "profile_not_ready"),
    ({"profile_version": True}, "invalid_profile_version"),
    ({"profile_version": "1"}, "invalid_profile_version"),
    ({"profile_version": 0}, "invalid_profile_version"),
    ({"model_version": "other"}, "model_version_mismatch"),
    ({"model_id": "other"}, "model_version_mismatch"),
    ({"dimension": "384"}, "dimension_mismatch"),
    ({"embeddings": [[1.0]]}, "dimension_mismatch"),
    ({"embeddings": [[math.nan] + [0.0] * 383]}, "features_not_finite_unit_vectors"),
    ({"embeddings": [[True] + [0.0] * 383]}, "features_must_be_numbers_not_strings_or_booleans"),
    ({"embeddings": [["1"] + [0.0] * 383]}, "features_must_be_numbers_not_strings_or_booleans"),
    ({"embeddings": [[0.0] * 384]}, "features_not_finite_unit_vectors"),
])
def test_profile_validation_fails_closed(changes, reason):
    matcher = ProfileMatcher([profile(**changes)], UnitEncoder())
    assert matcher.loaded_profile_versions == {}
    assert matcher.invalid_profiles["phone-a"] == reason
    assert matcher.match(None)["rejection"] == "no_ready_profiles"


def test_category_alias_and_unknown_same_category_only():
    matcher = ProfileMatcher([profile()], UnitEncoder())
    assert matcher.match(None, "cell phone")["accepted"]
    assert matcher.match(None, "wallet")["rejection"] == "no_ready_profiles_for_category"
    unknown = ProfileMatcher([profile(category="Custom Gizmo")], UnitEncoder())
    assert unknown.match(None, "custom_gizmo")["accepted"]
    assert not unknown.match(None, "手机")["accepted"]


def test_similar_identities_are_rejected_not_arbitrarily_named():
    matcher = ProfileMatcher([profile("a"), profile("b")], UnitEncoder())
    result = matcher.match(None, "phone")
    assert not result["accepted"] and result["item_id"] is None
    assert result["rejection"] == "ambiguous_identity" and result["gap"] == 0
    assert result["best_score"] == result["second_score"] == 1


@pytest.mark.parametrize('sign', [-1, 1])
def test_score_clipping_preserves_ties_and_item_order_at_unit_tolerance(sign):
    items = [profile(item_id, embeddings=[[sign*scale] + [0.] * (DIMENSION-1)])
             for item_id, scale in [('z', 1.0005), ('a', 1.0001)]]
    result = ProfileMatcher(items, UnitEncoder(), threshold=-1, margin=0).match(None)
    assert result['best_item_id'] == 'a' and result['second_item_id'] == 'z'
    assert result['best_score'] == result['second_score'] == sign
    assert result['gap'] == 0 and result['accepted']


def test_runner_up_is_another_item_not_another_reference_photo():
    item = profile(embeddings=[UnitEncoder().vector, UnitEncoder().vector])
    result = ProfileMatcher([item], UnitEncoder()).match(None)
    assert result["accepted"] and result["second_item_id"] is None
    assert result["profile_version"] == 1


def test_below_threshold_and_bad_encoder_output_rejected():
    encoder = UnitEncoder(vector=[0.0, 1.0] + [0.0] * 382)
    assert ProfileMatcher([profile()], encoder).match(None)["rejection"] == "below_threshold"
    encoder.vector = [math.nan] * 384
    assert ProfileMatcher([profile()], encoder).match(None)["rejection"] == "encoder_error"


def test_duplicate_id_rejected_and_profile_snapshot_is_immutable():
    item = profile()
    assert ProfileMatcher([item, copy.deepcopy(item)], UnitEncoder()).loaded_profile_versions == {}
    matcher = ProfileMatcher([item], UnitEncoder())
    item["appearance_profile"]["embeddings"][0][0] = 0
    assert matcher.match(None)["accepted"]


@pytest.mark.parametrize("threshold,margin", [(math.nan, .06), (.8, math.inf), (1.1, .06), (.8, -.1)])
def test_invalid_gate_configuration_rejected(threshold, margin):
    with pytest.raises(ValueError):
        ProfileMatcher([], UnitEncoder(), threshold, margin)


def test_real_pinned_onnx_file_inputs_offline_reuse_and_unicode_path(tmp_path):
    if not DEFAULT_MODEL_PATH.is_file():
        pytest.skip("Explicit preparation has not installed the optional pinned local model")
    pytest.importorskip("onnxruntime")
    first = np.full((320, 480, 3), 235, np.uint8)
    cv2.rectangle(first, (140, 40), (300, 285), (20, 25, 30), -1)
    cv2.circle(first, (270, 65), 10, (190, 190, 190), -1)
    second = np.full((320, 480, 3), 40, np.uint8)
    for x in range(0, 480, 32):
        cv2.line(second, (x, 0), (480 - x, 319), (220, 140, 60), 8)
    inputs, paths = [], []
    for name, pixels in (("合成矩形", first), ("合成纹理", second)):
        path = tmp_path / (name + ".png")
        path.write_bytes(cv2.imencode(".png", pixels)[1].tobytes())
        decoded = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
        assert decoded is not None
        inputs.append(decoded)
        paths.append(path)
    unicode_model = tmp_path / "本地外观模型.onnx"
    os.link(DEFAULT_MODEL_PATH, unicode_model)
    with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
         patch("socket.create_connection", side_effect=AssertionError("network forbidden")):
        encoder = AppearanceEncoder(unicode_model)
        assert encoder.health()["available"], encoder.health()
        reused = AppearanceEncoder(unicode_model)
        assert encoder._bundle is reused._bundle
        a, b = encoder.encode(inputs[0]), encoder.encode(inputs[1])
        assert len(a) == len(b) == 384
        assert np.isfinite(a).all() and np.isclose(np.linalg.norm(a), 1, atol=1e-6)
        similarity = float(np.dot(a, b))
        assert similarity < .999, "Distinct file inputs must not return a fixed embedding"
        with ThreadPoolExecutor(max_workers=3) as pool:
            vectors = list(pool.map(reused.encode, [inputs[0]] * 3))
        for vector in vectors:
            np.testing.assert_allclose(a, vector, atol=1e-6)
        matcher = ProfileMatcher([profile(embeddings=[a])], encoder)
        assert matcher.match(inputs[0], "phone")["accepted"]
        encoder.close()
        assert reused.health()["available"] and len(reused.encode(inputs[1])) == 384
        report = {"source": "synthetic_PNG_files_not_real_objects", "physical_camera_used": False,
                  "network_socket_blocked": True, "unicode_model_path_loaded": True,
                  "shared_session_identity_verified": True, "different_input_cosine": similarity,
                  "file_sha256": [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths],
                  "encoder_health": reused.health(), "threaded_identical_inputs": len(vectors)}
        (tmp_path / "real-onnx-offline-result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        reused.close()
def test_actual_bounded_spatial_batch_matches_each_single_window():
    if not DEFAULT_MODEL_PATH.is_file():
        pytest.skip('Actual pinned local appearance weights are unavailable')
    encoder = AppearanceEncoder()
    try:
        images = [np.random.default_rng(index).integers(0, 256, (height, width, 3), dtype=np.uint8)
                  for index, (height, width) in enumerate([(80, 140), (120, 60), (60, 60), (50, 180)])]
        sequential = [encoder.encode_patches(frame) for frame in images]
        batch = encoder.encode_patches_batch(images)
        for (a, ax, az), (b, bx, bz) in zip(sequential, batch):
            np.testing.assert_allclose(a, b, atol=1e-6, rtol=0)
            np.testing.assert_array_equal(ax, bx)
            assert az == bz
        for invalid in ([], images+images, None):
            with pytest.raises(ValueError):
                encoder.encode_patches_batch(invalid)
    finally:
        encoder.close()
