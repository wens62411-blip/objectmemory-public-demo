"""Bounded suggestions, never a new observation, grasp or placement.

Six server evidence groups are required. Screenshot time remains separate from
the newest observation because image storage is throttled. This function is pure.
"""
from datetime import datetime, timezone
import math
import re

from .runtime_mode import REAL_SOURCE_TYPES, SIMULATED_SOURCE_TYPES


def _instant(value):
    if not isinstance(value, str):
        raise ValueError('timestamp required')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone required')
    return result.astimezone(timezone.utc)


def _point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError('point required')
    if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in value):
        raise ValueError('invalid normalized point')
    return [float(v) for v in value]


def _frame(value):
    if type(value) is not int or value < 0:
        raise ValueError('frame index required')
    return value


def candidate_locations(observed, scene, status, state_timestamp, max_age_seconds=8, *, max_gap_seconds=.75):
    try:
        if status not in {'occluded', 'exited_view', 'lost'} or not isinstance(scene, dict) or not isinstance(observed, dict):
            return []
        if scene.get('calibration_status') != 'schematic' or scene.get('invalid_reason'):
            return []
        identity = observed.get('identity_evidence')
        if (observed.get('detection_mode') != 'experimental' or not observed.get('item_id')
                or not isinstance(identity, dict) or identity.get('accepted') is not True):
            return []
        if any(observed.get(key) != scene.get(key) for key in
               ('camera_id', 'source_session_id', 'runtime_mode', 'source_type', 'is_simulated')):
            return []
        if not observed.get('camera_id') or not observed.get('source_session_id'):
            return []
        mode, source, simulated = observed.get('runtime_mode'), observed.get('source_type'), observed.get('is_simulated')
        if mode not in {'REAL', 'DEMO', 'TEST'} or type(simulated) is not bool:
            return []
        if source not in REAL_SOURCE_TYPES | (SIMULATED_SOURCE_TYPES - {'mock', 'demo_seed', 'test_fixture'}):
            return []
        if mode == 'REAL' and (source not in REAL_SOURCE_TYPES or simulated):
            return []
        if mode != 'REAL' and not simulated:
            return []
        interaction = observed.get('interaction')
        if not isinstance(interaction, dict) or interaction.get('hand_near') is not True or interaction.get('hand_model_healthy') is not True:
            return []
        observed_at = _instant(observed['observed_at'])
        age = (_instant(state_timestamp) - observed_at).total_seconds()
        if not math.isfinite(max_age_seconds) or not 0 <= age <= min(8., max_age_seconds):
            return []
        if not math.isfinite(max_gap_seconds) or not 0 < max_gap_seconds <= 1:
            return []
        trail = observed.get('trajectory')
        if not isinstance(trail, list) or not 3 <= len(trail) <= 6:
            return []
        if any(not isinstance(point, dict) or point.get('source_session_id') != observed['source_session_id'] for point in trail):
            return []
        stamps = [_instant(point['timestamp']) for point in trail]
        frames = [_frame(point['source_frame']) for point in trail]
        points = [_point(point['center']) for point in trail]
        gaps = [(b-a).total_seconds() for a, b in zip(stamps, stamps[1:])]
        if any(not 0 < gap <= max_gap_seconds for gap in gaps) or any(a >= b for a, b in zip(frames, frames[1:])):
            return []
        if (stamps[-1]-stamps[0]).total_seconds() > 4 or abs((stamps[-1]-observed_at).total_seconds()) > .001:
            return []
        position = _point(observed['position'])
        if (math.dist(points[0], points[-1]) < .015 or math.dist(position, points[-1]) > .001
                or frames[-1] != _frame(observed['source_frame'])):
            return []
        if (_instant(scene['source_timestamp']) > observed_at or _frame(scene['source_frame']) > frames[-1]
                or type(scene.get('scene_version')) is not int or scene['scene_version'] < 1):
            return []
        image_time = _instant(observed['screenshot_observed_at'])
        image_frame = _frame(observed['screenshot_source_frame'])
        if (observed.get('image_status') != 'available'
                or observed.get('screenshot_camera_id') != observed['camera_id']
                or observed.get('screenshot_source_session_id') != observed['source_session_id']
                or not 0 <= (observed_at-image_time).total_seconds() <= 30 or image_frame > frames[-1]
                or not re.fullmatch(r'/media/event-images/[A-Za-z0-9_-]+\.jpg', str(observed.get('screenshot_path') or ''))
                or not re.fullmatch(r'[a-f0-9]{64}', str(observed.get('screenshot_sha256') or ''))):
            return []
        surfaces = scene.get('surfaces')
        if not isinstance(surfaces, list):
            return []
        surface = next((s for s in surfaces if isinstance(s, dict) and s.get('surface_id') == observed.get('zone_id')
                        and s.get('confirmed_by_user') is True and s.get('calibration_status') == 'schematic'), None)
        if not surface or not isinstance(surface.get('name'), str) or not 1 <= len(surface['name']) <= 80:
            return []
        from services.vision.zones import Zone
        polygon = [_point(point) for point in surface['image_polygon']]
        if not 3 <= len(polygon) <= 32 or not Zone(surface['surface_id'], scene['camera_id'], surface['name'], polygon).contains(tuple(position)):
            return []
        edge_distance = lambda point: min(point[0], point[1], 1-point[0], 1-point[1])
        near_edge = edge_distance(position) <= .08
        if status in {'exited_view', 'lost'} and (not near_edge or edge_distance(points[0])-edge_distance(position) < .005):
            return []
        label = '可能随手离开画面，去向未确认' if status in {'exited_view', 'lost'} else f"可能仍在{surface['name']}附近，尚未重新确认"
        return [{'label': label, 'reason': '同一物品近期连续移动、手部邻近，随后' +
                 ('沿画面边缘方向丢失' if status in {'exited_view', 'lost'} else '在用户确认的区域附近被遮挡') + '；这不是放置或抓握证明。',
                 'evidence_type': 'inferred', 'source_session_id': observed['source_session_id'],
                 'source_frame': observed['source_frame'], 'observed_at': observed['observed_at'],
                 'zone_id': surface['surface_id'], 'position': list(position),
                 'screenshot_path': observed['screenshot_path'], 'screenshot_sha256': observed['screenshot_sha256'],
                 'screenshot_source_frame': image_frame, 'screenshot_observed_at': observed['screenshot_observed_at'],
                 'scene_version': scene['scene_version']}]
    except (KeyError, ValueError, TypeError, OverflowError, AttributeError, IndexError):
        return []
