"""Read current local action metadata without opening a camera or storing frames."""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import time
from urllib.parse import urlparse
import httpx


STAGES = ('object_detection', 'reference_patch_localization', 'phone_shape_proposal',
          'identity_matching', 'hands', 'hand_actions', 'tracking', 'person_pose')
ACTION_REASONS = {'fresh', 'no_snapshot', 'engine_stopped', 'profile_changed', 'capture_unavailable',
                  'source_session_changed', 'reconnect_epoch_changed', 'invalid_source_timestamp', 'source_frame_expired'}
ACTION_NUMBERS = ('freshness_limit_ms', 'source_age_ms', 'result_age_ms', 'inference_duration_ms',
                  'source_to_result_ms', 'capture_source_frame', 'snapshot_source_frame',
                  'profile_generation', 'applied_profile_generation', 'snapshot_profile_generation')
CAPTURE_STATUSES = {'online', 'ready', 'stopped', 'ended', 'error', 'offline', 'recovering',
                    'reconnecting', 'disconnected', 'starting', 'connecting', 'frame_read_failed', 'unknown'}


def finite_number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e9 else None


def diagnostic_fields(value, health, received_at):
    """Allowlisted small metadata only; never copy arbitrary health/error data."""
    timestamp = value.get('timestamp')
    try:
        source = datetime.fromisoformat(timestamp) if isinstance(timestamp, str) and len(timestamp) <= 64 else None
        if source is None or source.tzinfo is None:
            source = None
    except ValueError:
        source = None
    reason = value.get('reason')
    action = value.get('diagnostics')
    action = action if isinstance(action, dict) else {}
    exact_reason = action.get('freshness_reason')
    capture_status = action.get('capture_status')
    return {'reason': reason if reason is None or isinstance(reason, str) and reason in {'stale_frame', 'camera_not_running', 'camera_changed'} else 'other_reason',
            'source_timestamp': source.isoformat() if source else None,
            'actions_received_at': received_at.isoformat(),
            'action_age_at_receive_ms': finite_number((received_at-source).total_seconds()*1000) if source else None,
            'health_frame_sequence': finite_number(health.get('frame_sequence')),
            'health_latency_ms': finite_number(health.get('latency_ms')),
            'inference_target_fps': finite_number(health.get('inference_target_fps')),
            'preview_age_ms': finite_number(health.get('preview_age_ms')),
            'action_diagnostics': {
                'freshness_reason': exact_reason if isinstance(exact_reason, str) and exact_reason in ACTION_REASONS else 'not_reported',
                'capture_status': capture_status if isinstance(capture_status, str) and capture_status in CAPTURE_STATUSES else 'unknown',
                **{key: finite_number(action.get(key)) for key in ACTION_NUMBERS},
                **{key: action.get(key) if type(action.get(key)) is bool else None
                   for key in ('source_session_matches', 'reconnect_epoch_matches')}},
            'stage_timings_ms': {key: number for key in STAGES
                if (number := finite_number((health.get('stage_timings_ms') or {}).get(key))) is not None}}


def metadata_checks(report):
    """A completed sampled window is not an uninterrupted or physical-action pass."""
    rows = report.get('samples') or []
    elapsed = [finite_number(row.get('elapsed')) for row in rows]
    valid_times = len(elapsed) >= 2 and all(value is not None for value in elapsed)
    gaps = [b-a for a, b in zip(elapsed, elapsed[1:])] if valid_times else []
    duration = finite_number(report.get('elapsed_seconds'))
    requested = finite_number(report.get('duration_requested_seconds'))
    tail = duration-elapsed[-1] if valid_times and duration is not None else None
    complete = bool(valid_times and requested and duration is not None and duration >= requested
                    and elapsed[0] <= 2 and tail is not None and 0 <= tail <= 2
                    and all(0 <= gap <= 5 for gap in gaps))
    fresh = [row for row in rows if row.get('fresh') is True]
    valid = [row for row in fresh if type(row.get('frame')) is int and row['frame'] >= 0
             and isinstance(row.get('session'), str) and row['session']]
    sessions = {row['session'] for row in valid}
    frames = [row['frame'] for row in valid]
    monotonic = bool(len(valid) == len(fresh) and len(frames) >= 2 and len(sessions) == 1
                     and all(b >= a for a, b in zip(frames, frames[1:])) and frames[-1] > frames[0])
    fraction = len(fresh)/len(rows) if rows else 0
    passed = bool(complete and not report.get('errors') and fraction >= .95 and monotonic)
    return {'complete_window': complete, 'max_sample_gap_seconds': max(gaps) if gaps else None,
            'unobserved_tail_seconds': tail, 'fresh_fraction': fraction,
            'valid_fresh_samples': len(valid), 'observed_source_sessions': len(sessions),
            'valid_frames_monotonic': monotonic, 'passed_metadata_checks': passed,
            'continuous_fresh_pass': bool(passed and len(fresh) == len(rows)),
            'metadata_check_scope': 'Sampled availability only; not camera exposure, browser FPS or physical hand/item action accuracy'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8018')
    parser.add_argument('--camera',required=True)
    parser.add_argument('--seconds',type=int,default=300)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    parsed=urlparse(args.url)
    if parsed.hostname not in {'127.0.0.1','localhost','::1'} or parsed.scheme!='http':
        parser.error('Only the existing local server is allowed; no frame uploads or remote analysis')
    if not 5<=args.seconds<=3600:
        parser.error('duration must be 5..3600 seconds')
    root=Path(__file__).resolve().parents[1]
    output=args.output.resolve()
    if not output.is_relative_to((root/'data/verification').resolve()):
        parser.error('report must stay under data/verification')
    if output.exists():
        parser.error('use a new report path; prior evidence must not be overwritten')
    report={'started_at':datetime.now(timezone.utc).isoformat(),'duration_requested_seconds':args.seconds,
            'private_frames_saved':False,'camera_opened_by_audit':False,'api_writes':0,
            'physical_grasp_accuracy':'NOT_TESTED','samples':[],'errors':[]}
    postures,motions,interactions=Counter(),Counter(),Counter()
    with httpx.Client(base_url=args.url,timeout=4,trust_env=False) as client:
        # Read session cookies without retaining or printing the access code.
        client.get('/api/session').raise_for_status()
        initial=client.get('/api/cameras/'+args.camera); initial.raise_for_status()
        report['runtime_mode']=initial.json().get('runtime_mode')
        report['source_type']=initial.json().get('source_type')
        report['event_count_before']=len(client.get('/api/events?limit=100').json())
        start=time.monotonic(); announced=-1
        while time.monotonic()-start<args.seconds:
            tick=time.monotonic()
            try:
                response=client.get(f'/api/cameras/{args.camera}/actions');response.raise_for_status()
                value=response.json()
                received_at=datetime.now(timezone.utc)
                actions_request_ms=(time.monotonic()-tick)*1000
                health_started=time.monotonic()
                camera_response=client.get('/api/cameras/'+args.camera);camera_response.raise_for_status()
                camera=camera_response.json()
                health=camera.get('health') or {}
                sample={'elapsed':round(time.monotonic()-start,4),'fresh':value.get('fresh'),
                        'frame':value.get('source_frame'),'session':value.get('source_session_id'),
                        'hand_count':len(value.get('hands') or []),'status':(value.get('hand_status') or {}).get('status'),
                        'profile_count':value.get('profile_count'),'camera_status':health.get('status'),
                        'capture_fps':health.get('capture_fps'),'preview_fps':health.get('preview_fps'),
                        'inference_fps':health.get('inference_fps'),'hand_latency_ms':(health.get('hands') or {}).get('last_latency_ms'),
                        'actions_request_ms':round(actions_request_ms,3),
                        'health_request_ms':round((time.monotonic()-health_started)*1000,3),
                        **diagnostic_fields(value,health,received_at)}
                report['samples'].append(sample)
                for hand in value.get('hands') or []:
                    postures[hand.get('posture','unknown')]+=1
                    motions[hand.get('motion','unknown')]+=1
                for interaction in value.get('interactions') or []:
                    interactions[interaction.get('holding_status','not_established')]+=1
            except (httpx.HTTPError,ValueError,TypeError) as exc:
                report['errors'].append({'elapsed':round(time.monotonic()-start,4),'error':type(exc).__name__})
            elapsed=time.monotonic()-start
            if int(elapsed//30)>announced:
                announced=int(elapsed//30)
                print(json.dumps({'elapsed':round(elapsed),'samples':len(report['samples']),
                                  'max_hands':max((s['hand_count'] for s in report['samples']),default=0),
                                  'postures':dict(postures),'errors':len(report['errors'])},ensure_ascii=False),flush=True)
            time.sleep(max(0,.5-(time.monotonic()-tick)))
        report['event_count_after']=len(client.get('/api/events?limit=100').json())
    report['elapsed_seconds']=round(time.monotonic()-start,3)
    report['posture_sample_counts']=dict(postures)
    report['motion_sample_counts']=dict(motions)
    report['interaction_sample_counts']=dict(interactions)
    report['max_hands']=max((s['hand_count'] for s in report['samples']),default=0)
    report['fresh_samples']=sum(s['fresh'] is True for s in report['samples'])
    report['fps_median']={field:statistics.median(values) if values else None
        for field in ('capture_fps','preview_fps','inference_fps')
        for values in [[s[field] for s in report['samples'] if isinstance(s.get(field),(int,float))]]}
    report.update(metadata_checks(report))
    report['nonfresh_reason_counts']=dict(Counter(row.get('reason') or 'not_reported' for row in report['samples'] if row.get('fresh') is not True))
    report['nonfresh_diagnostic_reason_counts']=dict(Counter(
        (row.get('action_diagnostics') or {}).get('freshness_reason') or 'not_reported'
        for row in report['samples'] if row.get('fresh') is not True))
    report['health_is_separate_read']=True
    report['diagnostic_limits']='Action diagnostics describe one accepted/rejected snapshot read, with unchanged freshness limits. Health is a separate read and stage timings can belong to another frame. Source/result ages and the last completed inference duration distinguish expired input from old completed output; they do not identify an unmeasured in-flight stage or prove physical action accuracy.'
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8') as destination:
        destination.write(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({key:value for key,value in report.items() if key!='samples'},ensure_ascii=False),flush=True)
    return 0 if report['passed_metadata_checks'] else 1


if __name__=='__main__':
    raise SystemExit(main())
