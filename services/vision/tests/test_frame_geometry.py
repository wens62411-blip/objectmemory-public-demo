import math

import numpy as np
import pytest

from services.vision.geometry import FrameGeometry


def test_identity_preserves_points_boxes_and_source_normalized_coordinates():
    geometry = FrameGeometry(640, 480)
    points = [[0, 0], [640, 480], [123.5, 245.25]]
    np.testing.assert_allclose(geometry.source_pixels_to_target(points), points)
    np.testing.assert_allclose(geometry.source_normalized_to_target([[.5, .5]]), [[320, 240]])
    np.testing.assert_allclose(geometry.target_to_source_normalized([[320, 240]]), [[.5, .5]])
    assert geometry.source_bbox_to_target((10, 20, 30, 40)) == pytest.approx((10, 20, 30, 40))
    assert geometry.to_dict()["coordinate_space"] == "source_normalized"


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("mirror", [False, True])
def test_crop_rotation_mirror_scale_padding_roundtrip(rotation, mirror):
    geometry = FrameGeometry(640, 480, crop_offset=(10, 20), crop_size=(100, 50),
                             rotation_degrees=rotation, mirror_x=mirror, scale=(2, 3), pad=(7, 11))
    points = np.asarray([[10, 20], [110, 70], [50.25, 36.5]])
    transformed = geometry.source_pixels_to_target(points)
    np.testing.assert_allclose(geometry.target_pixels_to_source(transformed), points, atol=1e-10)
    box = geometry.source_bbox_to_target((20, 25, 20, 10))
    assert geometry.target_bbox_to_source(box) == pytest.approx((20, 25, 20, 10))


def test_transform_order_is_crop_then_clockwise_rotation_then_mirror_scale_pad():
    geometry = FrameGeometry(200, 100, crop_offset=(10, 20), crop_size=(100, 50),
                             rotation_degrees=90, mirror_x=True, scale=(2, 3), pad=(7, 11))
    # Source(20,25) -> crop(10,5) -> CW(45,10) -> mirror(5,10) -> scale(10,30) -> pad(17,41).
    np.testing.assert_allclose(geometry.source_pixels_to_target([[20, 25]]), [[17, 41]])


def test_contain_and_stretch_are_explicit_not_silently_confused():
    contain = FrameGeometry.fit(1920, 1080, 640, 480, mode="contain")
    np.testing.assert_allclose(contain.source_pixels_to_target([[0, 0], [1920, 1080]]), [[0, 60], [640, 420]])
    assert contain.scale == pytest.approx((1 / 3, 1 / 3))
    assert contain.pad == pytest.approx((0, 60))
    stretch = FrameGeometry.fit(1920, 1080, 640, 480, mode="stretch")
    np.testing.assert_allclose(stretch.source_pixels_to_target([[0, 0], [1920, 1080]]), [[0, 0], [640, 480]])


def test_contain_with_portrait_rotation_and_source_crop():
    geometry = FrameGeometry.fit(1920, 1080, 640, 480, mode="contain", crop_offset=(100, 50),
                                 crop_size=(800, 400), rotation_degrees=90)
    assert geometry.pad == pytest.approx((200, 0))
    assert geometry.scale == pytest.approx((.6, .6))
    np.testing.assert_allclose(geometry.target_to_source_normalized(geometry.source_normalized_to_target([[.4, .4]])), [[.4, .4]])


@pytest.mark.parametrize("kwargs", [{"source_width": 0}, {"source_height": True}, {"rotation_degrees": 45},
    {"rotation_degrees": True}, {"mirror_x": "yes"}, {"scale": (0, 1)}, {"scale": (math.nan, 1)},
    {"pad": (math.inf, 0)}, {"pad": (1e300, 0)}, {"scale": (1e-300, 1)},
    {"crop_offset": (-1, 0)}, {"crop_size": (641, 480)}, {"target_width": -1}])
def test_invalid_geometry_is_rejected(kwargs):
    with pytest.raises(ValueError):
        FrameGeometry(**{"source_width": 640, "source_height": 480, **kwargs})


def test_invalid_point_and_box_never_silently_clamped():
    geometry = FrameGeometry(100, 100)
    with pytest.raises(ValueError):
        geometry.source_pixels_to_target([[math.nan, 20]])
    with pytest.raises(ValueError):
        geometry.source_pixels_to_target([[1, 2, 3]])
    with pytest.raises(ValueError):
        geometry.source_bbox_to_target((1, 2, -3, 4))
    np.testing.assert_allclose(geometry.source_normalized_to_target([[-.1, 1.1]]), [[-10, 110]])
    # Drawing may clip explicitly; a mapping must not fabricate valid source positions.


def test_geometry_parameters_are_frozen():
    geometry = FrameGeometry(100, 100)
    with pytest.raises(AttributeError):
        geometry.source_width = 200


def test_cover_mapping_has_explicit_negative_pad_and_invertible_visible_crop():
    geometry = FrameGeometry.fit(1920, 1080, 640, 480, mode="cover")
    assert geometry.pad[0] < 0 and geometry.pad[1] == 0
    points = [[0, 0], [640, 480], [320, 240]]
    np.testing.assert_allclose(geometry.source_pixels_to_target(geometry.target_pixels_to_source(points)), points, atol=1e-10)
