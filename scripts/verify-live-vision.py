"""Read-only localhost transport probe; not physical item accuracy or browser FPS."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import time
from urllib.parse import urlsplit

import cv2
import httpx
import numpy as np
from websockets.asyncio.client import connect


async def run(args, client, camera):
    report={'started_at':datetime.now(timezone.utc).isoformat(),'read_only':True,
        'api_mock':False,'camera_id':camera['id'],'frames':[], 'errors':[],
        'measures':'decoded WebSocket transport; not DOM display FPS or identity accuracy',
        'images_saved':0,'business_writes':0}
    cookie='; '.join(f'{key}={value}' for key,value in client.cookies.items())
    started=time.monotonic()
    previous=None
    try:
        async with connect(args.url.replace('http:','ws:')+f"/ws/cameras/{camera['id']}/vision",
                           origin=args.url,additional_headers={'Cookie':cookie},proxy=None,
                           max_size=9*1024*1024,close_timeout=2) as ws:
            while time.monotonic()-started<args.seconds:
                data=await asyncio.wait_for(ws.recv(),5)
                if isinstance(data,str):
                    report['errors'].append({'kind':'no_fresh_frame','at':round(time.monotonic()-started,3)})
                    continue
                n=int.from_bytes(data[:4],'big')
                assert 2<=n<=131072
                meta=json.loads(data[4:4+n])
                image=cv2.imdecode(np.frombuffer(data[4+n:],np.uint8),1)
                assert image is not None and image.shape[:2]==(meta['height'],meta['width'])
                key=(meta['source_session_id'],meta['source_frame'])
                assert previous is None or key[0]!=previous[0] or key[1]>previous[1]
                previous=key
                assert meta['camera_id']==camera['id'] and meta['display_only'] is True
                for candidate in meta['candidates']:
                    assert candidate['source_frame']==meta['source_frame']
                    assert candidate['source_session_id']==meta['source_session_id']
                    assert candidate['observation_evidence'] is False
                lag=(datetime.now(timezone.utc)-datetime.fromisoformat(meta['source_timestamp'])).total_seconds()*1000
                report['frames'].append({'at':round(time.monotonic()-started,5),
                    'frame':meta['source_frame'],'session':meta['source_session_id'],
                    'source_timestamp':meta['source_timestamp'],'lag_ms':round(lag,2),
                    'candidates':len(meta['candidates']),'identity_accepted':sum(c.get('accepted') is True for c in meta['candidates']),
                    'model_fps':meta['inference_fps'],'runtime_mode':meta['runtime_mode'],
                    'source_type':meta['source_type'],'is_simulated':meta['is_simulated']})
                await ws.send(json.dumps({'ack':meta['source_frame'],'source_session_id':meta['source_session_id']}))
    except Exception as exc:
        # Do not serialize connection headers/cookies or potentially sensitive URLs.
        report['errors'].append({'kind':type(exc).__name__})
    report['elapsed_seconds']=round(time.monotonic()-started,3)
    frames=report['frames']
    report['decoded_frames']=len(frames)
    report['transport_fps']=(len(frames)-1)/(frames[-1]['at']-frames[0]['at']) if len(frames)>1 else 0
    report['latency_median_ms']=statistics.median(f['lag_ms'] for f in frames) if frames else None
    report['latency_p95_ms']=sorted(f['lag_ms'] for f in frames)[int((len(frames)-1)*.95)] if frames else None
    report['passed_transport']=not report['errors'] and len(frames)>args.seconds*20
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8018')
    parser.add_argument('--seconds',type=int,default=30)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    allowed=(Path(__file__).resolve().parents[1]/'data/verification').resolve()
    if urlsplit(args.url).hostname not in {'127.0.0.1','localhost'} or not 5<=args.seconds<=3600 or not args.output.resolve().is_relative_to(allowed):
        parser.error('Require localhost, bounded 5..3600 seconds, and data/verification output')
    with httpx.Client(base_url=args.url,timeout=5,trust_env=False) as client:
        client.get('/api/session').raise_for_status()
        cameras=client.get('/api/cameras').json()
        camera=next((c for c in cameras if c.get('enabled')),None)
        if camera is None:raise SystemExit('No enabled camera. Probe never starts a camera.')
        before=client.get('/api/events').json()
        result=asyncio.run(run(args,client,camera))
        result['events_before']=len(before)
        result['events_after']=len(client.get('/api/events').json())
        # Keep opt-in numerical stage timing, never credentials or camera URLs.
        health_fields={'capture_fps','raw_read_fps','unique_pixel_fps','duplicate_source_reads',
            'pixel_cadence_is_exposure_proof','deduplicate_identical_frames',
            'preview_fps','inference_fps','preview_target_fps',
            'backend','capture_pipeline_diagnostics','preview_pipeline_diagnostics'}
        after_camera=client.get(f"/api/cameras/{camera['id']}")
        after_camera.raise_for_status()
        result['camera_health_before']={key:value for key,value in camera.get('health',{}).items() if key in health_fields}
        result['camera_health_after']={key:value for key,value in after_camera.json().get('health',{}).items() if key in health_fields}
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in result.items() if k not in {'frames','camera_id','camera_health_before','camera_health_after'}},ensure_ascii=False))
        return 0 if result['passed_transport'] else 1


if __name__=='__main__':raise SystemExit(main())
