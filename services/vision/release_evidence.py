"""Shared, conservative hand-release contract (not a tactile grasp claim)."""
from __future__ import annotations
import math


def verified_release(value, session_id, frame_start, frame_end, *, min_stable_seconds=5.) -> bool:
    if not isinstance(value, dict):
        return False
    if not (value.get('release_observed') is True and value.get('holding_status') == 'released'
            and value.get('input_accepted') is True and value.get('separation_verified') is True
            and value.get('hand_model_healthy') is True and value.get('item_detected') is True
            and isinstance(value.get('hand_id'), str) and value['hand_id']
            and value.get('source_session_id') == session_id):
        return False
    keys = ('co_motion_start_frame', 'co_motion_end_frame', 'release_start_frame', 'release_end_frame', 'source_frame')
    frames = [value.get(key) for key in keys]
    if any(type(number) is not int or number < 0 for number in [frame_start, frame_end, *frames]):
        return False
    start, moved, separating, released, current = frames
    steps = value.get('co_motion_frames')
    visible_steps, separation_frame = value.get('release_visible_frames'), value.get('separation_frame')
    times = [value.get(key) for key in ('co_motion_start_timestamp', 'co_motion_end_timestamp',
             'release_start_timestamp', 'separation_timestamp', 'release_timestamp', 'source_timestamp')]
    duration = value.get('release_stable_seconds')
    if any(type(number) not in (float, int) or not math.isfinite(number) or number < 0
           for number in [min_stable_seconds, duration, *times]):
        return False
    motion_start, motion_end, release_start, separation_time, release_time, current_time = times
    if not (motion_start < motion_end <= release_start < separation_time <= release_time <= current_time
            and release_time - release_start + 1e-9 >= min_stable_seconds
            and math.isclose(duration, current_time - release_start, rel_tol=1e-9, abs_tol=1e-6)):
        return False
    return (type(steps) is int and 2 <= steps <= moved-start
            and type(visible_steps) is int and type(separation_frame) is int
            and 2 <= visible_steps <= separation_frame - separating + 1
            and separating < separation_frame <= released
            and frame_start <= start < moved <= separating < released == current == frame_end)
