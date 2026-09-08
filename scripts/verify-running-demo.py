"""Exercise a running server with real generated video, and save a factual report."""
import argparse
import json
import sys
import time
from datetime import datetime,timezone
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',default='http://127.0.0.1:8018')
    parser.add_argument('--leave-running',action='store_true')
    args=parser.parse_args()
    report={'timestamp':datetime.now(timezone.utc).isoformat(),'evidence':'SYNTHETIC_TEST_PLAYBACK','url':args.url,'checks':{}}
    with httpx.Client(base_url=args.url,timeout=30,trust_env=False) as client:
        client.get('/api/session').raise_for_status()
        runtime=client.get('/api/runtime-config');runtime.raise_for_status()
        if runtime.json().get('runtime_mode') != 'DEMO':
            raise RuntimeError('verify-running-demo 只允许连接显式 DEMO 模式服务')
        seed=client.post('/api/system/demo-seed');seed.raise_for_status()
        cid=seed.json()['camera_id']
        camera=client.get(f'/api/cameras/{cid}').json()
        if camera.get('source_type')!='video' or Path(camera.get('source','')).suffix.lower()!='.mp4':
            raise RuntimeError(f"Demo seed did not select the required MP4 playback source: {camera.get('source')}")
        client.post(f'/api/cameras/{cid}/stop').raise_for_status()
        started=datetime.now(timezone.utc).isoformat()
        client.post(f'/api/cameras/{cid}/start').raise_for_status()
        found=None
        deadline=time.monotonic()+40
        while time.monotonic()<deadline:
            events=client.get('/api/events',params={'camera_id':cid,'event_type':'movement'}).json()
            found=next((e for e in events if e['item_name']=='我的手机' and (e.get('to_zone') or e.get('zone_name'))=='沙发右侧' and e.get('evidence_status')=='confirmed' and e.get('final_status')=='confirmed_placed' and e['timestamp_start']>=started),None)
            if found:break
            time.sleep(.25)
        if not found:raise RuntimeError('No newly detected placement event from this playback')
        result=client.post('/api/search',json={'query':'我的手机在哪里'}).json()['results'][0]
        assert result['last_confirmed']['zone_name']=='沙发右侧'
        image=client.get(found['screenshot_path']);image.raise_for_status()
        clip=client.get(found['clip_path']);clip.raise_for_status()
        assert image.content.startswith(b'\xff\xd8') and len(clip.content)>1000
        report['checks']={'source_type':camera['source_type'],'camera_source':camera['source'],'source_is_mp4':True,'search_correct':True,'event_type':found['event_type'],'last_confirmed':'客厅 · 沙发右侧','screenshot_bytes':len(image.content),'clip_bytes':len(clip.content),'screenshot_path':found['screenshot_path'],'clip_path':found['clip_path'],'notes':found['notes'],'health':client.get(f'/api/cameras/{cid}').json()['health']}
        if not args.leave_running:client.post(f'/api/cameras/{cid}/stop').raise_for_status()
    path=ROOT/'data'/'verification'/'running-demo.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    print('Saved:',path)

if __name__=='__main__':main()
