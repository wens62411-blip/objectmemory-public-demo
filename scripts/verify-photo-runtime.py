"""Local live-source smoke; records metrics only, never exports camera pixels."""
import argparse
import hashlib
import http.cookiejar
import json
from pathlib import Path
import statistics
import subprocess
import time
import urllib.request
import urllib.error
from urllib.parse import urlsplit


def process_stats(pid):
    """Read counters only for the explicitly selected owned backend process."""
    if pid is None:
        return None
    command=(f'$p=Get-Process -Id {pid} -ErrorAction Stop; '
             '$p | Select-Object Id,WorkingSet64,PrivateMemorySize64,CPU,Handles,'
             "@{n='ThreadCount';e={$_.Threads.Count}} | ConvertTo-Json -Compress")
    try:
        result=subprocess.run(['powershell.exe','-NoProfile','-Command',command],
            capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=8,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        return json.loads(result.stdout) if result.returncode==0 else {'error':'selected process unavailable'}
    except (OSError,subprocess.TimeoutExpired,ValueError) as exc:
        return {'error':type(exc).__name__}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',default='http://127.0.0.1:8018')
    parser.add_argument('--seconds',type=int,default=90)
    parser.add_argument('--sample-interval',type=float,default=1)
    parser.add_argument('--process-id',type=int)
    parser.add_argument('--start-camera')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    parsed=urlsplit(args.url)
    if parsed.scheme!='http' or parsed.hostname not in {'127.0.0.1','localhost','::1'} or parsed.username or parsed.password:
        parser.error('This probe is localhost HTTP only')
    if not 1<=args.seconds<=3600 or not 1<=args.sample_interval<=30:
        parser.error('seconds must be 1..3600 and sample-interval 1..30')
    if args.process_id is not None and args.process_id<=0:parser.error('process-id must be positive')
    client=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def request(path,method='GET',body=None):
        data=json.dumps(body).encode() if body is not None else b'' if method=='POST' else None
        with client.open(urllib.request.Request(args.url+path,data=data,method=method,headers={'Content-Type':'application/json'}),timeout=12) as response:
            return json.load(response)
    request('/api/session')
    before=request('/api/health')
    if args.start_camera:request('/api/cameras/'+args.start_camera+'/start','POST')
    samples=[]
    resources=[]
    start=time.monotonic()
    while time.monotonic()-start<args.seconds:
        rows=[]
        for camera in request('/api/cameras'):
            health=camera.get('health') or {}
            row={'camera_id':camera['id'],'source_type':camera.get('source_type_provenance'),'is_simulated':camera.get('is_simulated'),
                 'health':{key:health.get(key) for key in ('status','status_code','source_session_id','source_frame_sequence','capture_fps','preview_fps','inference_fps','latency_ms','queue_size','preview_buffer_frames','pending_media_jobs','ring_buffer_frames','ring_buffer_bytes','detection_mode','stage_timings_ms','hands','appearance','detector','loaded_profile_versions','scene_stability','error')}}
            try:
                snap=request('/api/cameras/'+camera['id']+'/vision-snapshot')
                # Decode only in this local process. Pixels are never printed,
                # written to this report or sent to a remote service.
                import base64,cv2,numpy as np
                encoded=base64.b64decode(snap['image_data_url'].split(',',1)[1])
                frame=cv2.imdecode(np.frombuffer(encoded,np.uint8),cv2.IMREAD_COLOR)
                row['snapshot']={'source_frame':snap.get('source_frame'),'source_session_id':snap.get('source_session_id'),
                    'source_timestamp':snap.get('source_timestamp'),
                    'width':snap.get('width'),'height':snap.get('height'),'image_decodes':frame is not None,
                    'jpeg_sha256':hashlib.sha256(encoded).hexdigest(),'candidates':len(snap.get('candidates') or []),
                    'accepted':sum(c.get('accepted') is True for c in snap.get('candidates') or []),
                    'hands':len(snap.get('hands') or []),'landmark_counts':[len(h.get('landmarks') or []) for h in snap.get('hands') or []]}
            except (urllib.error.HTTPError,ValueError) as exc:row['snapshot_error']=str(exc)
            rows.append(row)
        samples.append({'elapsed_seconds':round(time.monotonic()-start,3),'cameras':rows})
        if len(samples)==1 or len(samples)%10==0:
            queried_at=round(time.monotonic()-start,3)
            counters=process_stats(args.process_id)
            resources.append({'elapsed_seconds':queried_at,'query_ended_seconds':round(time.monotonic()-start,3),'process':counters})
        if len(samples)%10==0:print(f'live local metadata samples: {len(samples)}',flush=True)
        # Preserve metadata progress if this long-running audit is interrupted.
        # No image bytes or authentication material are written.
        if len(samples)%10==0:
            args.output.parent.mkdir(parents=True,exist_ok=True)
            partial={'completed':False,'private_pixels_exported':False,
                     'physical_item_trial_performed':False,'samples':samples,'resources':resources}
            temporary=args.output.with_suffix('.partial.tmp')
            temporary.write_text(json.dumps(partial,ensure_ascii=False),encoding='utf-8')
            temporary.replace(args.output.with_suffix('.partial.json'))
        time.sleep(args.sample_interval)
    after=request('/api/health')
    all_rows=[camera for sample in samples[10:] for camera in sample['cameras']]
    metrics={key:{'median':statistics.median(values),'min':min(values),'max':max(values)} for key in ('capture_fps','preview_fps','inference_fps','latency_ms')
             if (values:=[row['health'][key] for row in all_rows if isinstance(row['health'].get(key),(int,float))])}
    result={'scope':'actual local camera source and actual loaded models; no user-photo identity accuracy claim',
            'completed':True,'completion_scope':'probe_execution_only_not_continuous_source_acceptance',
            'requested_seconds':args.seconds,'sample_interval':args.sample_interval,
            'duration_seconds':round(time.monotonic()-start,2),'before':before,'after':after,'warm_metrics':metrics,'samples':samples,
            'resources':resources,'private_pixels_exported':False,'physical_item_trial_performed':False}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'duration_seconds':result['duration_seconds'],'warm_metrics':metrics,'before_events':before['stats']['events'],'after_events':after['stats']['events'],'report':str(args.output)},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
