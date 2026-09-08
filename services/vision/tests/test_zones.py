from services.vision.zones import Zone, ZoneManager


def test_polygon_contains_boundary_and_excludes_outside():
    zone = Zone("desk", "cam", "桌面", [(0.1, 0.1), (0.5, 0.1), (0.5, 0.6), (0.1, 0.6)])
    assert zone.contains((0.3, 0.3))
    assert zone.contains((0.1, 0.3))
    assert not zone.contains((0.8, 0.3))


def test_overlap_uses_highest_priority_and_tracks_transitions():
    manager = ZoneManager([
        {"id": "room", "name": "大区域", "points": [[0, 0], [1, 0], [1, 1], [0, 1]], "priority": 1},
        {"id": "desk", "name": "桌面", "points": [[0.1, 0.1], [0.5, 0.1], [0.5, 0.6], [0.1, 0.6]], "priority": 10},
    ], camera_id="cam")
    assert manager.find((0.3, 0.3)).id == "desk"
    zone, first = manager.update_track("t1", (0.3, 0.3), 1.0)
    assert zone.id == "desk" and first["new_zone_name"] == "桌面"
    zone, transition = manager.update_track("t1", (0.8, 0.8), 2.0)
    assert zone.id == "room"
    assert transition["previous_zone_name"] == "桌面"
    assert transition["new_zone_name"] == "大区域"


def test_zone_rejects_pixel_coordinates():
    try:
        Zone.from_dict({"id": "bad", "points": [[0, 0], [640, 0], [0, 480]]})
    except ValueError as exc:
        assert "归一化" in str(exc)
    else:
        raise AssertionError("pixel coordinates were accepted")

