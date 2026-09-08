"""Summarize local phone-fix receipts; missing evidence never becomes a pass.

Only reads the canonical REAL database. Does not export photographs, feature
vectors, passwords or device tokens. Test suites are listed, never added together
because their cases overlap. Physical identity remains a separate manual gate.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data/verification'


def receipt(name):
    path = OUT / name
    if not path.is_file():
        return {'path': str(path), 'status': 'NOT_RUN_OR_NO_COMPLETED_RECEIPT'}
    raw = path.read_bytes()
    value = {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}
    if path.suffix == '.xml':
        tree = ET.fromstring(raw)
        suites = [tree] if tree.tag == 'testsuite' else list(tree.findall('testsuite'))
        value.update({key: sum(int(s.get(key, 0)) for s in suites)
                      for key in ('tests', 'failures', 'errors', 'skipped')})
        value['seconds'] = round(sum(float(s.get('time', 0)) for s in suites), 3)
        value['status'] = 'PASS' if value['tests'] and not value['failures'] and not value['errors'] and not value['skipped'] else 'HAS_FAILURE_ERROR_OR_SKIP'
    else:
        value['data'] = json.loads(raw)
    return value


def real_state():
    path = ROOT / 'data/database/objectmemory.sqlite'
    if not path.is_file():
        return {'status': 'MISSING_DATABASE'}
    result = {'database': str(path), 'database_bytes': path.stat().st_size,
              'read_only': True, 'physical_target_presence_human_verified': False}
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result['counts'] = {table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in ('items', 'item_reference_images', 'item_recognition_profiles',
                          'item_current_state', 'movement_events', 'event_media') if table in tables}
        result['per_item_movement_events'] = [dict(row) for row in conn.execute(
            'SELECT item_id, COUNT(*) AS count FROM movement_events GROUP BY item_id')]
    return result


def storage_snapshot():
    """Logical file sizes, without traversing symlinks or Windows junctions."""
    result = {'measured_at': datetime.now(timezone.utc).isoformat(),
              'metric': 'logical_file_bytes_not_allocated_disk_blocks',
              'project_bytes': 0, 'project_files': 0, 'data_bytes': 0,
              'skipped_links': 0, 'errors': 0, 'service_running_during_scan': True}
    data = ROOT / 'data'
    def scan_error(_error):
        result['errors'] += 1
    for directory, children, names in os.walk(ROOT, followlinks=False, onerror=scan_error):
        for name in list(children):
            try:
                info = (Path(directory) / name).lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    children.remove(name)
                    result['skipped_links'] += 1
            except OSError:
                children.remove(name)
                result['errors'] += 1
        for name in names:
            path = Path(directory) / name
            try:
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    result['skipped_links'] += 1
                    continue
                result['project_files'] += 1
                result['project_bytes'] += info.st_size
                if path.is_relative_to(data):
                    result['data_bytes'] += info.st_size
            except OSError:
                result['errors'] += 1
    return result


def assess_runtime_coverage(report):
    """Elapsed wall time is not proof that a source supplied that many samples."""
    samples = report.get('samples') or []
    elapsed = [float(row['elapsed_seconds']) for row in samples]
    interval = float(report.get('sample_interval') or 5)
    allowed_gap = max(10.0, interval * 3)
    duration = float(report.get('duration_seconds') or 0)
    requested = float(report.get('requested_seconds') or 0)
    gaps = [b - a for a, b in zip(elapsed, elapsed[1:])]
    tail = max(0.0, duration - elapsed[-1]) if elapsed else duration
    snapshots = [camera['snapshot'] for sample in samples for camera in sample.get('cameras', [])
                 if camera.get('snapshot')]
    warm_rows = [camera for sample in samples[10:] for camera in sample.get('cameras', [])]
    errors = sum(bool(row.get('snapshot_error') or row.get('health', {}).get('error')) for row in warm_rows)
    sessions = {row.get('source_session_id') for row in snapshots}
    after_online = int((report.get('after', {}).get('stats') or {}).get('online_cameras') or 0)
    reasons = []
    if not report.get('completed'): reasons.append('probe_execution_not_completed')
    if not elapsed or requested <= 0 or elapsed[-1] < requested - allowed_gap:
        reasons.append('requested_window_not_sampled_to_end')
    if tail > allowed_gap: reasons.append('unsampled_tail')
    if any(gap <= 0 or gap > allowed_gap for gap in gaps): reasons.append('sampling_gap_or_clock_order')
    if elapsed and elapsed[0] > allowed_gap: reasons.append('unsampled_start')
    if after_online == 0: reasons.append('no_online_camera_at_final_health')
    if len(sessions) != 1 or None in sessions: reasons.append('missing_or_changed_source_session')
    frames = [row.get('source_frame') for row in snapshots]
    if (not frames or any(not isinstance(frame, int) for frame in frames)
            or any(b <= a for a, b in zip(frames, frames[1:]))):
        reasons.append('missing_or_non_increasing_source_frames')
    if errors: reasons.append('post_warmup_camera_or_snapshot_errors')
    if not warm_rows or any(not row.get('snapshot', {}).get('image_decodes') for row in warm_rows):
        reasons.append('missing_or_undecodable_post_warmup_frames')
    if any(not sample.get('cameras') for sample in samples): reasons.append('missing_camera_sample')
    return {
        'execution_completed_is_not_acceptance': True,
        'continuous_requested_window_verified': not reasons,
        'rejection_reasons': reasons,
        'sample_count': len(samples),
        'decoded_snapshot_count': sum(row.get('image_decodes') is True for row in snapshots),
        'source_timestamp_recorded_count': sum(row.get('source_timestamp') is not None for row in snapshots),
        'first_sample_seconds': elapsed[0] if elapsed else None,
        'last_sample_seconds': elapsed[-1] if elapsed else None,
        'unsampled_tail_seconds': round(tail, 3),
        'largest_inter_sample_gap_seconds': round(max(gaps, default=0), 3),
        'allowed_sampling_gap_seconds': allowed_gap,
        'post_warmup_error_count': errors, 'source_session_count': len(sessions),
        'accepted_snapshot_occurrences': sum(int(row.get('accepted') or 0) for row in snapshots),
        'warm_capture_below_20_fps_samples': sum(isinstance(row.get('health', {}).get('capture_fps'), (int, float))
            and row['health']['capture_fps'] < 20 for row in warm_rows),
        'warm_preview_below_20_fps_samples': sum(isinstance(row.get('health', {}).get('preview_fps'), (int, float))
            and row['health']['preview_fps'] < 20 for row in warm_rows),
        'physical_phone_acceptance': 'NOT_PERFORMED',
    }


def main():
    stability = receipt('phone-final-runtime-stability.json')
    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'Phone recognition repair; historical full-project audit remains separate',
        'completion_status': 'PHYSICAL_PERSONAL_PHONE_ACCEPTANCE_NOT_COMPLETED',
        'physical_esp32_test': 'NOT_RUN_NO_HARDWARE', 'physical_flashing': 'NOT_PERFORMED',
        'private_pixels_exported': False,
        'test_suites_not_additive': True,
        'tests': {name: receipt(name) for name in (
            'pytest-phone-final-regression.xml', 'pytest-phone-release-regression.xml', 'pytest-phone-release-final.xml',
            'pytest-phone-settings-final.xml', 'pytest-registration-shape-guards-green.xml', 'pytest-phone-paths-green.xml',
            'pytest-phone-shape-http.xml', 'pytest-phone-shape-http-fixed.xml',
            'pytest-phone-receipt-guards.xml',
            'phone-final-vitest.json', 'playwright-phone-final-real.json',
            'pytest-phone-final-public-browser.xml')},
        'physical_camera_observation': receipt('phone-recognition-final-live.json'),
        'physical_camera_stability': stability,
        'stability_coverage_assessment': assess_runtime_coverage(stability.get('data') or {}),
        'controlled_camera_lifecycle': receipt('phone-final-camera-lifecycle.json'),
        'system_standby_evidence': receipt('phone-final-standby-evidence.json'),
        'cleanup': {name: receipt(name) for name in (
            'phone-rejected-models.json', 'phone-public-test-cleanup.json',
            'phone-final-cache-cleanup.json', 'phone-release-cache-cleanup.json',
            'phone-completed-progress-cleanup.json')},
        'real_database': real_state(),
        'storage_after': storage_snapshot(),
        'unverified_capabilities': [
            'User-owned phone identity under front/back/rotated/low-light views',
            'Human-labelled 10 real pickup/carry/place/occlusion trials',
            'Physical phone last-location correctness and automatic confirmed movement',
            'General identity accuracy across similar phones; geometry is display-only',
        ],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / 'phone-recognition-result.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)
    print(json.dumps({'report': str(path), 'status': report['completion_status'],
                      'real_counts': report['real_database'].get('counts')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
