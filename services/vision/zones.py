from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(slots=True)
class Zone:
    id: str
    camera_id: str
    name: str
    points: list[tuple[float, float]]
    priority: int = 0
    enabled: bool = True
    room_name: str | None = None
    support_surface_confirmed: bool = False
    scene_version: int | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any], default_camera_id: str = "") -> "Zone":
        points = []
        for point in value.get("points") or []:
            if len(point) != 2:
                raise ValueError("区域坐标必须是 [x,y]")
            x, y = float(point[0]), float(point[1])
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError("区域必须使用 0 到 1 的归一化坐标")
            points.append((x, y))
        if len(points) < 3:
            raise ValueError("区域至少需要三个点")
        return cls(
            id=str(value.get("id") or value.get("zone_id") or ""),
            camera_id=str(value.get("camera_id") or default_camera_id),
            name=str(value.get("name") or "未命名区域"),
            points=points,
            priority=int(value.get("priority", 0)),
            enabled=bool(value.get("enabled", True)),
            room_name=value.get("room_name"),
            support_surface_confirmed=bool((value.get('scene_metadata') or {}).get('confirmed_by_user') is True
                and (value.get('scene_metadata') or {}).get('calibration_status') in {'schematic','geometry_only','calibrated','calibrated_unvalidated'}
                and (value.get('scene_metadata') or {}).get('surface_type') in {'table','sofa','bed','shelf','floor'}),
            scene_version=(value.get('scene_metadata') or {}).get('scene_version'),
        )

    def contains(self, point: tuple[float, float]) -> bool:
        """Boundary-inclusive ray casting in normalized coordinates."""
        x, y = point
        inside = False
        count = len(self.points)
        for index in range(count):
            x1, y1 = self.points[index]
            x2, y2 = self.points[(index + 1) % count]
            cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
            if abs(cross) <= 1e-9 and min(x1, x2) - 1e-9 <= x <= max(x1, x2) + 1e-9 and min(y1, y2) - 1e-9 <= y <= max(y1, y2) + 1e-9:
                return True
            if (y1 > y) != (y2 > y):
                intersection_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
                if x < intersection_x:
                    inside = not inside
        return inside


class ZoneManager:
    def __init__(self, zones: Iterable[dict[str, Any] | Zone] = (), camera_id: str = "", room_name: str = "") -> None:
        self.camera_id = camera_id
        self.room_name = room_name
        self._zones: list[Zone] = []
        self._track_zones: dict[str, str | None] = {}
        self._transitions: dict[str, list[dict[str, Any]]] = {}
        self.replace(zones)

    @property
    def zones(self) -> list[Zone]:
        return list(self._zones)

    def replace(self, zones: Iterable[dict[str, Any] | Zone]) -> None:
        parsed = [zone if isinstance(zone, Zone) else Zone.from_dict(zone, self.camera_id) for zone in zones]
        self._zones = sorted(parsed, key=lambda item: item.priority, reverse=True)

    def find(self, point: tuple[float, float]) -> Zone | None:
        for zone in self._zones:
            if zone.enabled and zone.contains(point):
                return zone
        return None

    def update_track(self, track_id: str, point: tuple[float, float], timestamp: float) -> tuple[Zone | None, dict[str, Any] | None]:
        zone = self.find(point)
        current_id = zone.id if zone else None
        previous_id = self._track_zones.get(track_id)
        self._track_zones[track_id] = current_id
        if track_id not in self._transitions:
            self._transitions[track_id] = []
        if previous_id == current_id:
            return zone, None
        previous_zone = next((candidate for candidate in self._zones if candidate.id == previous_id), None)
        transition = {
            "track_id": track_id,
            "timestamp": timestamp,
            "previous_zone_id": previous_id,
            "previous_zone_name": previous_zone.name if previous_zone else None,
            "new_zone_id": current_id,
            "new_zone_name": zone.name if zone else None,
        }
        self._transitions[track_id].append(transition)
        return zone, transition

    def transitions(self, track_id: str) -> list[dict[str, Any]]:
        return list(self._transitions.get(track_id, []))

    def reset_tracks(self) -> None:
        """Forget temporal zone transitions while retaining configured zones."""
        self._track_zones.clear()
        self._transitions.clear()

    def pixel_polygons(self, width: int, height: int) -> list[tuple[Zone, list[tuple[int, int]]]]:
        return [
            (zone, [(round(x * width), round(y * height)) for x, y in zone.points])
            for zone in self._zones if zone.enabled
        ]
