import time
import threading
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone,timedelta
from pathlib import Path
import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from apps.api.app.main import create_app, ROOT
from apps.api.app.security import redact
from apps.api.app.security import Sessions
from apps.api.app.search import match_items
from apps.api.app.search import location_result


@pytest.fixture
def client(tmp_path):
    app=create_app(tmp_path/'中文 数据',testing=True)
    with TestClient(app) as c:
        assert c.get('/api/session').json()['authenticated']
        yield c


def camera(client):
    response=client.post('/api/cameras',json={'name':'测试摄像头','room_name':'客厅','source_type':'video','source':'demo/sample-videos/object-memory-demo.avi','config':{'loop':False}})
    assert response.status_code==200,response.text
    return response.json()


def phone(client):
    response=client.post('/api/items',json={'name':'我的手机','type':'phone','aliases':['手机'],'aruco_id':1,'ring_enabled':True})
    assert response.status_code==200,response.text
    return response.json()


def record_movement(client,item,cam,zone='桌面'):
    runtime=client.app.state.runtime
    frame=np.zeros((80,120,3),np.uint8)
    event_id=f'test-movement-{time.time_ns()}'
    stamp=datetime.now(timezone.utc);ended=stamp+timedelta(seconds=2)
    runtime.db.save('source_sessions',{'camera_id':cam['id'],'runtime_mode':'TEST','source_type':'video_file','is_simulated':True,'started_at':stamp.isoformat(),'first_frame':1,'last_frame':3,'last_frame_at':ended.isoformat(),'status':'streaming','continuity_ok':True},'test-source')
    return runtime.event({'id':event_id,'event_id':event_id,'item_id':item['id'],'camera_id':cam['id'],'event_type':'movement','movement_session_id':event_id,'runtime_mode':'TEST','source_type':'video_file','is_simulated':True,'source_session_id':'test-source','source_frame_start':1,'source_frame_end':3,'source_timestamp_start':stamp.isoformat(),'source_timestamp_end':ended.isoformat(),'stable_before':True,'stable_after':True,'source_continuity_ok':True,'pickup_evidence':{'source_frame':1,'source_timestamp':stamp.timestamp()},'placement_evidence':{'source_frame':3,'source_timestamp':ended.timestamp()},'from_zone':'入口','to_zone':zone,'zone_name':zone,'meaningful_position_change':True,'confidence':.92,'detector_backend':'aruco','tracker_backend':'stable_identity','detection_mode':'aruco'},frame,[frame.copy(),frame.copy(),frame.copy()])


def test_crud_and_normalized_zones(client):
    cam=camera(client)
    zones=[]
    for name,x in [('桌面',0),('沙发',.34),('柜子',.67)]:
        r=client.post(f'/api/cameras/{cam["id"]}/zones',json={'name':name,'points':[[x,0],[min(x+.3,1),0],[min(x+.3,1),1],[x,1]]})
        assert r.status_code==200,r.text
        zones.append(r.json())
    assert len(client.get(f'/api/cameras/{cam["id"]}/zones').json())==3
    assert client.patch(f'/api/zones/{zones[0]["id"]}',json={'priority':5,'enabled':False}).json()['priority']==5
    assert client.post(f'/api/cameras/{cam["id"]}/zones',json={'name':'错误区域','points':[[0,0],[2,0],[0,1]]}).status_code==422
    assert client.patch(f'/api/cameras/{cam["id"]}',json={'inference_fps':999}).status_code==422
    assert client.delete(f'/api/cameras/{cam["id"]}').status_code==200


def test_reference_images_single_start_preserves_original_and_requires_target(client):
    item=phone(client)
    image=np.full((64,96,3),(80,150,230),np.uint8)
    ok,data=cv2.imencode('.png',image);assert ok
    raw=data.tobytes()
    files=[('files',(f'角度 {i}.png',raw,'image/png')) for i in range(6)]
    response=client.post(f'/api/items/{item["id"]}/reference-images',files=files)
    assert response.status_code==200,response.text
    records=response.json()
    assert len(records)==6
    assert len({record['id'] for record in records})==1  # repeated bytes are not six angles
    assert 'features' not in records[0]
    assert records[0]['region_confirmed'] is False
    assert client.get(records[0]['original_path']).content==raw
    assert client.post(f'/api/items/{item["id"]}/reference-images',files=files[:1]).status_code==200
    assert client.get(f'/api/items/{item["id"]}/profile').json()['registration_status']=='images_saved'


def test_duplicate_marker_refused(client):
    phone(client)
    assert client.post('/api/items',json={'name':'另一部手机','aruco_id':1}).status_code==409


def test_search_and_manual_correction(client):
    cam=camera(client);item=phone(client)
    rt=client.app.state.runtime
    record_movement(client,item,cam)
    result=client.post('/api/search',json={'query':'我的手机在哪里'}).json()['results'][0]
    assert result['last_confirmed']['zone_name']=='桌面'
    assert result['evidence']['human_review_status']=='unreviewed'
    assert client.get(result['evidence']['screenshot_path']).status_code==200
    event_id=result['evidence']['id']
    correction=client.post(f'/api/events/{event_id}/correct',json={'zone_name':'沙发右侧','notes':'本人核实'}).json()
    assert correction['human_review_status']=='human_corrected'
    repeated=client.post(f'/api/events/{event_id}/correct',json={'zone_name':'沙发右侧','notes':'本人核实'}).json()
    assert repeated['event_id']==correction['event_id']
    updated=client.get(f'/api/items/{item["id"]}/last-location').json()
    assert updated['last_confirmed']['zone_name']=='沙发右侧'
    assert len(client.get('/api/events',params={'item_id':item['id']}).json())==2
    assert '不能判断具体是谁' in client.post('/api/search',json={'query':'最近谁移动了手机'}).json()['results'][0]['answer']
    client.delete(f'/api/events/{event_id}')
    assert client.get(correction['screenshot_path']).status_code==200


def test_concurrent_identical_manual_corrections_are_idempotent(client):
    cam=camera(client);item=phone(client)
    original=record_movement(client,item,cam)
    event_id=original['id']

    def submit(_index):
        response=client.post(
            f'/api/events/{event_id}/correct',
            json={'zone_name':'沙发右侧','notes':'并发重复提交'},
        )
        assert response.status_code==200,response.text
        return response.json()['event_id']

    with ThreadPoolExecutor(max_workers=8) as pool:
        returned=list(pool.map(submit,range(16)))
    assert len(set(returned))==1
    assert len(client.get('/api/events',params={'item_id':item['id']}).json())==2


def test_occlusion_does_not_claim_object_under_cushion():
    item={'id':'occluded-phone','name':'我的手机'}
    common={'zone_name':'沙发右侧','room_name':'客厅','runtime_mode':'REAL','source_type':'opencv_camera','is_simulated':False}
    rows=[{**common,'event_type':'movement','evidence_status':'confirmed','final_status':'confirmed_placed','timestamp_start':'2026-09-03T01:00:00Z','timestamp_end':'2026-09-03T01:00:10Z'}]
    state={'current_room':'客厅','current_zone':'沙发右侧','status':'occluded','last_seen_at':'2026-09-03T01:01:00Z','source_type':'opencv_camera','is_simulated':False}
    result=location_result(item,rows,'REAL',state)
    assert result['last_confirmed']['event_type']=='movement'
    assert result['status']=='occluded'
    assert '当前具体位置未确认' in result['answer']


def test_location_time_order_uses_instants_not_iso_string_offsets():
    item={'id':'time-order-phone','name':'我的手机'}
    rows=[{
        'id':'confirmed','event_id':'confirmed','event_type':'movement',
        'evidence_status':'confirmed','final_status':'confirmed_placed',
        'timestamp_start':'2026-09-03T01:00:00Z','timestamp_end':'2026-09-03T01:00:10Z',
        'room_name':'客厅','zone_name':'桌面','runtime_mode':'REAL','source_type':'opencv_camera','is_simulated':False,
    }]
    # 08:59 +08:00 is 00:59 UTC, so it is older despite sorting after 01:00Z
    # as plain text.  The confirmed placement must remain the current answer.
    older_state={
        'current_room':'客厅','current_zone':'沙发','status':'last_seen',
        'last_seen_at':'2026-09-03T08:59:00+08:00',
        'source_type':'browser_camera','is_simulated':False,
    }
    result=location_result(item,rows,'REAL',older_state)
    assert result['status']=='movement'
    assert result['evidence']['event_id']=='confirmed'
    assert result['answer']=='我的手机最后一次确认放在客厅 · 桌面。'


def test_real_search_excludes_legacy_unattested_network_confirmation():
    item={'name':'我的手机'}
    rows=[{
        'id':'network','event_id':'network','event_type':'movement',
        'evidence_status':'confirmed','final_status':'confirmed_placed',
        'timestamp_start':'2026-09-03T01:00:00Z','timestamp_end':'2026-09-03T01:00:10Z',
        'room_name':'客厅','zone_name':'沙发','runtime_mode':'REAL',
        'source_type':'rtsp','is_simulated':False,
    }]
    result=location_result(item,rows,'REAL',None)
    assert result['last_confirmed'] is None
    assert result['evidence'] is None
    assert result['answer']=='暂时没有真实摄像头产生的位置记录。'


def test_companion_ring_websocket(client):
    item=phone(client)
    assert client.post(f'/api/items/{item["id"]}/ring').json()['delivered']==0
    with client.websocket_connect(f'/ws/companions/{item["id"]}',headers={'origin':'http://testserver'}) as ws:
        assert ws.receive_json()['type']=='connected'
        assert client.post(f'/api/items/{item["id"]}/ring').json()['delivered']==1
        assert ws.receive_json()['type']=='ring'
        client.post(f'/api/items/{item["id"]}/ring/stop')
        assert ws.receive_json()['type']=='stop'
        ws.send_json({'type':'ping'})
        assert ws.receive_json()['type']=='pong'


def test_runtime_config_uses_current_host(client):
    value=client.get('/api/runtime-config',headers={'host':'127.0.0.1:8123'}).json()
    assert value['api_base_url']=='http://127.0.0.1:8123/api'
    assert value['websocket_base_url']=='ws://127.0.0.1:8123/ws'
    assert value['backend_healthy'] is True


def test_camera_connection_test_releases_transient_engine_and_keeps_real_snapshot(client,monkeypatch):
    cam=camera(client)
    runtime=client.app.state.runtime
    jpeg=cv2.imencode('.jpg',np.full((48,64,3),180,np.uint8))[1].tobytes()

    class FakeEngine:
        def get_jpeg(self):return jpeg
        def health(self):return {'status':'ready','status_code':'STREAMING','fps':12.5,'width':64,'height':48,'latency_ms':3,'dropped_frames':0,'reconnects':0,'error':None}

    stopped=[]
    def fake_start(camera_id):
        runtime.engines[camera_id]=FakeEngine()
        return runtime.camera(runtime.require('cameras',camera_id))
    def fake_stop(camera_id):
        stopped.append(camera_id);runtime.engines.pop(camera_id,None)
    monkeypatch.setattr(runtime,'start',fake_start)
    monkeypatch.setattr(runtime,'stop',fake_stop)
    result=client.post(f'/api/cameras/{cam["id"]}/test')
    assert result.status_code==200,result.text
    payload=result.json()
    assert payload['success'] is True and payload['status_code']=='STREAMING'
    assert stopped==[cam['id']] and cam['id'] not in runtime.engines
    snapshot=client.get(payload['frame_url'])
    assert snapshot.status_code==200 and snapshot.content==jpeg


def test_camera_connection_message_does_not_attest_network_stream_as_physical(tmp_path,monkeypatch):
    app=create_app(tmp_path/'camera-message-real',runtime_mode='REAL')
    with TestClient(app,base_url='http://127.0.0.1') as client:
        client.cookies.set('om_session',app.state.runtime.sessions.issue())
        runtime=app.state.runtime
        jpeg=cv2.imencode('.jpg',np.full((32,48,3),160,np.uint8))[1].tobytes()

        class FakeEngine:
            def get_jpeg(self):return jpeg
            def health(self):return {'status':'ready','status_code':'STREAMING','fps':8,'width':48,'height':32,'latency_ms':2,'dropped_frames':0,'reconnects':0,'error':None}

        def fake_start(camera_id):
            runtime.engines[camera_id]=FakeEngine()
            return runtime.camera(runtime.require('cameras',camera_id))
        monkeypatch.setattr(runtime,'start',fake_start)
        monkeypatch.setattr(runtime,'stop',lambda camera_id:runtime.engines.pop(camera_id,None) is not None)

        network=client.post('/api/cameras',json={'name':'本地 MJPEG 回放','source_type':'mjpeg','source':'http://127.0.0.1:8765/stream'}).json()
        message=client.post(f'/api/cameras/{network["id"]}/test').json()['message']
        assert '物理来源未验证' in message and '真实摄像头的连续画面' not in message

        webcam=client.post('/api/cameras',json={'name':'本机摄像头','source_type':'webcam','source':'0'}).json()
        trusted=client.post(f'/api/cameras/{webcam["id"]}/test').json()['message']
        assert '本机摄像头设备' in trusted and '物理来源未验证' not in trusted


def test_camera_diagnostics_returns_owner_fields_and_recent_logs(client):
    from services.vision.camera_manager import camera_manager
    diagnostics=client.app.state.runtime.data/'diagnostics'
    diagnostics.mkdir(parents=True,exist_ok=True)
    (diagnostics/'camera-report.json').write_text('{"summary":{"status_code":"READY"}}',encoding='utf-8')
    probe=cv2.imencode('.jpg',np.full((12,16,3),120,np.uint8))[1].tobytes()
    (diagnostics/'camera-probe.jpg').write_bytes(probe)
    lease=camera_manager.acquire('webcam:51','camera:diagnostic-test',timeout=0)
    try:
        client.app.state.runtime.camera_log('info','READY','单元测试日志','camera-test')
        payload=client.get('/api/camera-diagnostics').json()
        owner=next(item for item in payload['owners'] if item['key']=='webcam:51')
        assert owner['owner']=='camera:diagnostic-test'
        assert set(['thread_id','started_at','subscribers','acquisition_count']).issubset(owner)
        assert payload['logs'][-1]['code']=='READY'
        assert payload['report_url']=='/api/camera-diagnostics/report'
        assert payload['probe_image_url']=='/api/camera-diagnostics/probe-image'
        assert client.get(payload['report_url']).headers['content-type'].startswith('application/json')
        assert client.get(payload['probe_image_url']).content==probe
    finally:
        camera_manager.release(lease)


def test_camera_diagnostics_lock_blocks_parallel_probe_and_webcam_start(client):
    runtime=client.app.state.runtime
    cam=client.post('/api/cameras',json={'name':'锁测试摄像头','source_type':'webcam','source':'0'}).json()
    assert runtime.diagnostic_lock.acquire(blocking=False)
    try:
        parallel=client.post('/api/camera-diagnostics/run',json={'indices':'0','frames':30})
        assert parallel.status_code==409
        assert '另一个' in parallel.json()['detail']
    finally:
        runtime.diagnostic_lock.release()
    runtime.diagnostic_running=True
    try:
        start=client.post(f'/api/cameras/{cam["id"]}/start')
        assert start.status_code==409
        assert '诊断正在' in start.json()['detail']
    finally:
        runtime.diagnostic_running=False


def test_invalid_diagnostic_parameters_do_not_leave_lock_held(client):
    runtime=client.app.state.runtime
    response=client.post('/api/camera-diagnostics/run',json={'indices':'bad'})
    assert response.status_code==422
    assert runtime.diagnostic_lock.acquire(blocking=False)
    runtime.diagnostic_lock.release()


def test_browser_camera_binary_ingest_reaches_existing_pipeline(client):
    item=client.post('/api/items',json={'name':'关闭竞态测试物品','type':'phone','aruco_id':49}).json()
    response=client.post('/api/cameras',json={'name':'浏览器直连','source_type':'browser','source':'browser','config':{'device_id':'test-browser'}})
    assert response.status_code==200,response.text
    camera_id=response.json()['id']
    image=np.full((120,160,3),(20,140,220),np.uint8)
    ok,jpeg=cv2.imencode('.jpg',image);assert ok
    with client.websocket_connect(f'/ws/browser-cameras/{camera_id}/ingest',headers={'origin':'http://testserver'}) as ws:
        assert ws.receive_json()['type']=='ready'
        ws.send_bytes(jpeg.tobytes())
        ack=ws.receive_json()
        assert ack['type']=='ack' and ack['status']=='streaming'
        runtime=client.app.state.runtime
        engine=runtime.engines[camera_id]
        session_id=engine.health()['source_session_id']
        assert session_id
        original_stop=engine.stop
        def stop_with_late_track():
            original_stop()
            # Simulate a worker callback reaching Runtime after the socket has
            # removed its generation but before release returns.
            engine.on_track({'id':'late-browser-track','item_id':item['id'],'camera_id':camera_id,
                'source_session_id':session_id,'source_frame':999,'last_seen':'2030-01-01T00:00:00+00:00',
                'zone_name':'不应写入','center':{'x':.5,'y':.5},'confidence':.99,'state':'last_seen'})
        engine.stop=stop_with_late_track
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            frame=client.get(f'/api/cameras/{camera_id}/frame')
            if frame.status_code==200:break
            time.sleep(.05)
        assert frame.status_code==200 and frame.headers['content-type']=='image/jpeg'
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        if client.get(f'/api/cameras/{camera_id}').json()['health']['status']=='stopped':
            break
        time.sleep(.05)
    assert client.get(f'/api/cameras/{camera_id}').json()['health']['status']=='stopped'
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        session=runtime.db.get('source_sessions',session_id,unscoped=True)
        if session and session['status']=='stopped' and session['ended_at']:
            break
        time.sleep(.05)
    assert session and session['status']=='stopped' and session['ended_at']
    final_frame=session['last_frame']
    final_processed=engine.health()['processed_frames']
    time.sleep(.75)
    assert runtime.db.get('source_sessions',session_id,unscoped=True)['status']=='stopped'
    assert runtime.db.get('source_sessions',session_id,unscoped=True)['last_frame']==final_frame
    assert engine.health()['processed_frames']==final_processed
    assert runtime.db.get('item_current_state',f'TEST:{item["id"]}') is None


def test_browser_release_publishes_stopped_while_old_generation_joins(client):
    item=client.post('/api/items',json={'name':'浏览器释放代际物品','type':'phone','aruco_id':48}).json()
    camera_id=client.post('/api/cameras',json={
        'name':'浏览器释放代际摄像头','source_type':'browser','source':'browser',
        'config':{'device_id':'release-generation-test'},
    }).json()['id']
    runtime=client.app.state.runtime
    engine,source,sender_token=runtime.acquire_browser_sender(camera_id)
    session_id=engine.health()['source_session_id']
    assert session_id
    original_stop=engine.stop
    stop_entered=threading.Event()
    allow_stop=threading.Event()
    outcome={}

    def delayed_stop():
        stop_entered.set()
        assert allow_stop.wait(5)
        original_stop()

    def release():
        outcome['released']=runtime.release_browser_sender(camera_id,engine,source,sender_token)

    engine.stop=delayed_stop
    worker=threading.Thread(target=release,daemon=True)
    worker.start()
    try:
        assert stop_entered.wait(5)
        row=runtime.db.get('cameras',camera_id,unscoped=True)
        # The engine is detached before its worker join. Concurrent reads must
        # use the terminal public snapshot, never old-generation health.
        for _ in range(25):
            assert runtime.camera(row)['health']['status']=='stopped'
            assert runtime.engines.get(camera_id) is None
            assert runtime.stopping_engines.get(camera_id) is engine
            session=runtime.db.get('source_sessions',session_id,unscoped=True)
            assert session and session['status']=='stopped' and session['ended_at']
        engine.on_track({
            'id':'late-detached-track','item_id':item['id'],'camera_id':camera_id,
            'source_session_id':'detached-session','source_frame':999,
            'last_seen':'2030-01-01T00:00:00+00:00','zone_name':'不应写入',
            'center':{'x':.5,'y':.5},'confidence':.99,'state':'last_seen',
        })
        assert runtime.db.get('item_current_state',f'TEST:{item["id"]}',unscoped=True) is None
    finally:
        allow_stop.set()
        worker.join(8)
    assert not worker.is_alive()
    assert outcome.get('released') is True
    assert camera_id not in runtime.stopping_engines
    assert runtime.camera(row)['health']['status']=='stopped'


def test_real_browser_websocket_replayed_demo_video_is_rejected_as_confirmed_history(tmp_path):
    """Exercise the exact client-JPEG replay route that previously forged REAL."""
    video=ROOT/'demo/sample-videos/object-memory-demo.avi'
    assert video.is_file()
    app=create_app(tmp_path/'browser-replay-real',runtime_mode='REAL')
    with TestClient(app,base_url='http://127.0.0.1') as real:
        real.cookies.set('om_session',app.state.runtime.sessions.issue())
        # This security replay contains ArUco markers, not reference photographs.
        # Select its actual detector explicitly; REAL now defaults to photo mode.
        assert real.patch('/api/settings',json={'detection_mode':'aruco'}).status_code==200
        item=real.post('/api/items',json={'name':'我的手机','type':'phone','aruco_id':1}).json()
        camera=real.post('/api/cameras',json={'name':'浏览器客户端帧','room_name':'客厅','source_type':'browser','source':'browser'}).json()
        runtime=app.state.runtime
        runtime.db.save('zones',{'camera_id':camera['id'],'name':'桌面','points':[[0,.12],[.46,.12],[.46,.9],[0,.9]],'priority':1,'enabled':True},'browser-desk')
        runtime.db.save('zones',{'camera_id':camera['id'],'name':'沙发右侧','points':[[.5,.12],[1,.12],[1,.9],[.5,.9]],'priority':1,'enabled':True},'browser-sofa')
        capture=cv2.VideoCapture(str(video))
        sent=0
        try:
            # TestClient's websocket helper otherwise hardcodes ws://testserver,
            # even when its HTTP base URL is an explicitly trusted localhost.
            with real.websocket_connect(f'ws://127.0.0.1/ws/browser-cameras/{camera["id"]}/ingest',headers={'origin':str(real.base_url).rstrip('/')}) as ws:
                ready=ws.receive_json()
                assert ready['type']=='ready' and ready['source_type']=='browser_camera' and ready['is_simulated'] is False
                session_id=runtime.engines[camera['id']].health()['source_session_id']
                assert session_id
                while True:
                    ok,frame=capture.read()
                    if not ok:break
                    encoded,jpeg=cv2.imencode('.jpg',frame,[cv2.IMWRITE_JPEG_QUALITY,85])
                    assert encoded
                    ws.send_bytes(jpeg.tobytes())
                    assert ws.receive_json()['type']=='ack'
                    sent+=1
                    time.sleep(.1)
                time.sleep(2.5)
        finally:
            capture.release()
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            session=runtime.db.get('source_sessions',session_id,unscoped=True)
            if session and session['status']=='stopped':break
            time.sleep(.05)
        assert session and session['status']=='stopped'
        assert sent==140
        assert runtime.db.count('events')==0
        state=runtime.db.get('item_current_state',f'REAL:{item["id"]}')
        assert state and state['source_type']=='browser_camera' and state['status']=='last_seen'
        # Client frames may prove only an observation, never a confirmed REAL
        # placement.  The one current-state JPEG is now a separate media role.
        observed=state['last_observed']
        assert observed['image_status']=='available'
        assert observed['source_session_id']==observed['screenshot_source_session_id']==session_id
        assert 1<=observed['screenshot_source_frame']<=observed['source_frame']<=sent
        media=runtime.db.list('event_media')
        assert len(media)==1
        assert media[0]['event_id'] is None and media[0]['role']=='last_observed'
        assert media[0]['owner_current_state_id']==state['id']
        assert media[0]['source_session_id']==session_id and media[0]['runtime_mode']=='REAL'
        assert media[0]['path']==observed['screenshot_path']
        assert media[0]['metadata']['source_frame']==observed['screenshot_source_frame']
        assert media[0]['metadata']['observed_at']==observed['screenshot_observed_at']
        response=real.get(observed['screenshot_path'])
        assert response.status_code==200
        assert hashlib.sha256(response.content).hexdigest()==observed['screenshot_sha256']==media[0]['sha256']
        assert cv2.imdecode(np.frombuffer(response.content,np.uint8),cv2.IMREAD_COLOR) is not None
        assert len(list((runtime.media/'event-images').glob('*')))==1
        assert list((runtime.media/'event-clips').glob('*'))==[]


def test_second_browser_sender_cannot_stop_first(client):
    camera_id=client.post('/api/cameras',json={'name':'浏览器唯一发送者','source_type':'browser','source':'browser'}).json()['id']
    ok,jpeg=cv2.imencode('.jpg',np.full((64,96,3),120,np.uint8));assert ok
    with client.websocket_connect(f'/ws/browser-cameras/{camera_id}/ingest',headers={'origin':'http://testserver'}) as first:
        assert first.receive_json()['type']=='ready'
        with client.websocket_connect(f'/ws/browser-cameras/{camera_id}/ingest',headers={'origin':'http://testserver'}) as second:
            error=second.receive_json()
            assert error['code']=='BUSY'
        first.send_bytes(jpeg.tobytes())
        assert first.receive_json()['type']=='ack'


def test_stale_browser_sender_cannot_stop_replacement_generation(client,monkeypatch):
    camera_id=client.post('/api/cameras',json={'name':'浏览器代际测试','source_type':'browser','source':'browser'}).json()['id']
    ok,jpeg=cv2.imencode('.jpg',np.full((64,96,3),160,np.uint8));assert ok
    path=f'/ws/browser-cameras/{camera_id}/ingest'
    headers={'origin':'http://testserver'}
    runtime=client.app.state.runtime
    original_begin_release=runtime.begin_browser_sender_release
    old_release_finished=threading.Event()
    old_generation={}

    def observed_begin_release(release_camera_id,expected_engine,source,sender_token):
        try:
            return original_begin_release(release_camera_id,expected_engine,source,sender_token)
        finally:
            if expected_engine is old_generation.get('engine'):
                old_release_finished.set()

    monkeypatch.setattr(runtime,'begin_browser_sender_release',observed_begin_release)
    first_context=client.websocket_connect(path,headers=headers)
    first=first_context.__enter__()
    second_context=None
    try:
        assert first.receive_json()['type']=='ready'
        old_generation['engine']=runtime.engines[camera_id]
        assert client.post(f'/api/cameras/{camera_id}/stop').status_code==200
        second_context=client.websocket_connect(path,headers=headers)
        second=second_context.__enter__()
        try:
            assert second.receive_json()['type']=='ready'
            replacement=client.app.state.runtime.engines[camera_id]
            # Finishing an old socket runs its finally block while the new
            # sender remains live.  That cleanup must be generation-scoped.
            first_context.__exit__(None,None,None)
            first_context=None
            assert old_release_finished.wait(5)
            second.send_bytes(jpeg.tobytes())
            ack=second.receive_json()
            assert ack['type']=='ack' and ack['status']=='streaming'
            assert runtime.engines.get(camera_id) is replacement
        finally:
            second_context.__exit__(None,None,None)
            second_context=None
    finally:
        if second_context is not None:
            second_context.__exit__(None,None,None)
        if first_context is not None:
            first_context.__exit__(None,None,None)


def test_browser_ingest_websocket_auth_and_origin_matrix(client):
    camera_id=client.post('/api/cameras',json={'name':'浏览器安全测试','source_type':'browser','source':'browser'}).json()['id']
    path=f'/ws/browser-cameras/{camera_id}/ingest'

    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect(path,headers={'origin':'https://evil.example'}):
            pass
    assert rejected.value.code==4403

    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect(path):
            pass
    assert rejected.value.code==4403

    client.cookies.clear()
    with pytest.raises(WebSocketDisconnect) as rejected:
        with client.websocket_connect(path,headers={'origin':'http://testserver'}):
            pass
    assert rejected.value.code==4401


def test_admin_auth_and_csrf(tmp_path):
    app=create_app(tmp_path,testing=True)
    with TestClient(app) as client:
        assert client.get('/api/items').status_code==401
        client.get('/api/session')
        assert client.post('/api/items',json={'name':'恶意跨站'},headers={'origin':'https://other.example'}).status_code==403
        assert client.get('/media/database/object_memory.sqlite3').status_code==404
        assert client.get('/media/../database/object_memory.sqlite3').status_code==404
    with TestClient(app) as device_client:
        assert device_client.get('/api/items',headers={'Authorization':'Bearer device-token'}).status_code==401


def test_credential_redaction():
    redacted=redact('rtsp://admin:private-secret@192.168.1.2/live password=wifi-secret token=abc pairing_code=OM-ABCDEF')
    for secret in ['private-secret','wifi-secret','abc','OM-ABCDEF']:assert secret not in redacted


def test_dev_origin_is_explicit_and_loopback_only(monkeypatch):
    sessions=Sessions()
    assert not sessions.allowed_origin('http://127.0.0.1:5173','127.0.0.1:8018','127.0.0.1')
    monkeypatch.setenv('OM_DEV_ORIGIN','http://127.0.0.1:5173')
    sessions=Sessions()
    assert sessions.allowed_origin('http://127.0.0.1:5173','127.0.0.1:8018','127.0.0.1')
    assert not sessions.allowed_origin('http://127.0.0.1:5173','127.0.0.1:8018','192.168.1.10')
    assert not sessions.allowed_origin('https://evil.example','127.0.0.1:8018','127.0.0.1')


def test_exact_item_alias_ignores_shared_words():
    items=[{'name':'我的手机','type':'phone','aliases':['手机']},{'name':'我的钥匙','type':'keys','aliases':['钥匙']},{'name':'蓝色钱包','type':'wallet','aliases':['钱包']}]
    for query,name in [('我的手机在哪里','我的手机'),('钥匙放哪了','我的钥匙'),('找一下钱包','蓝色钱包')]:
        assert [i['name'] for i in match_items(query,items)]==[name]


def test_forbidden_camera_sources(client):
    assert client.post('/api/cameras',json={'name':'公网','source_type':'rtsp','source':'rtsp://8.8.8.8/live'}).status_code==422
    assert client.post('/api/cameras',json={'name':'屏幕','source_type':'screen','source':'screen','config':{'left':0,'top':0,'width':100,'height':100}}).status_code==422


def test_screen_grant_is_local_single_use_and_required_for_every_start(client,monkeypatch):
    region={'left':10,'top':20,'width':120,'height':80}
    payload={'name':'授权区域','source_type':'screen','source':'screen','config':{'authorized':True,'region':region}}
    assert client.post('/api/cameras',json=payload).status_code==403
    grant=client.post('/api/screen-capture/authorize',json={'region':region}).json()['capture_grant']
    payload['config']['capture_grant']=grant
    result=client.post('/api/cameras',json=payload)
    assert result.status_code==200,result.text
    camera_id=result.json()['id']
    assert 'capture_grant' not in result.json()['config']
    assert client.post('/api/cameras',json=payload).status_code==403
    grant=client.post('/api/screen-capture/authorize',json={'region':region}).json()['capture_grant']
    payload['config']['capture_grant']=grant
    payload['config']['region']={**region,'width':200}
    assert client.post('/api/cameras',json=payload).status_code==403
    assert client.post(f'/api/cameras/{camera_id}/start').status_code==403

    stolen=client.post('/api/screen-capture/authorize',json={'region':region}).json()['capture_grant']
    remote=TestClient(client.app,client=('192.168.1.20',12345))
    try:
        remote.cookies.update(client.cookies)
        assert remote.get('/api/items').status_code==200
        assert remote.post('/api/screen-capture/authorize',json={'region':region}).status_code==403
        assert remote.post(f'/api/cameras/{camera_id}/start',json={'capture_grant':stolen}).status_code==403
    finally:
        remote.close()
    # A failed remote attempt consumes the bearer grant; it cannot be replayed
    # later from localhost.
    assert client.post(f'/api/cameras/{camera_id}/start',json={'capture_grant':stolen}).status_code==403
    fresh=client.post('/api/screen-capture/authorize',json={'region':region}).json()['capture_grant']
    monkeypatch.setattr(client.app.state.runtime,'start',lambda requested:{'id':requested,'started':True})
    assert client.post(f'/api/cameras/{camera_id}/start',json={'capture_grant':fresh}).status_code==200
    assert client.post(f'/api/cameras/{camera_id}/start',json={'capture_grant':fresh}).status_code==403
    assert client.post(f'/api/cameras/{camera_id}/test').status_code==403
    jpeg=cv2.imencode('.jpg',np.full((24,32,3),140,np.uint8))[1].tobytes()
    class ScreenEngine:
        def get_jpeg(self):return jpeg
        def health(self):return {'status':'ready','status_code':'STREAMING','fps':5,'width':32,'height':24,'latency_ms':1,'dropped_frames':0,'reconnects':0,'error':None}
    def start_screen(requested):
        client.app.state.runtime.engines[requested]=ScreenEngine()
        return {'id':requested,'started':True}
    monkeypatch.setattr(client.app.state.runtime,'start',start_screen)
    monkeypatch.setattr(client.app.state.runtime,'stop',lambda requested:client.app.state.runtime.engines.pop(requested,None) is not None)
    test_grant=client.post('/api/screen-capture/authorize',json={'region':region}).json()['capture_grant']
    tested=client.post(f'/api/cameras/{camera_id}/test',json={'capture_grant':test_grant})
    assert tested.status_code==200 and tested.json()['success'] is True
    assert tested.json()['source_type']=='authorized_screen_capture' and tested.json()['is_simulated'] is True


def test_video_upload_boundary_and_fixed_camera_addresses(client,tmp_path):
    outside=tmp_path/'private.avi'
    outside.write_bytes(b'private video')
    payload={'name':'视频','source_type':'video','source':str(outside)}
    assert client.post('/api/cameras',json=payload).status_code==422
    uploaded=client.post('/api/cameras/upload-video',files={'file':('test.avi',b'video bytes','video/x-msvideo')})
    assert uploaded.status_code==200
    payload['source']=uploaded.json()['source']
    assert client.post('/api/cameras',json=payload).status_code==200
    assert client.post('/api/cameras',json={'name':'域名','source_type':'mjpeg','source':'http://camera.local/stream'}).status_code==422


def test_failed_video_start_is_error_not_success(client):
    cam=client.post('/api/cameras',json={'name':'缺失文件','source_type':'video','source':'demo/sample-videos/missing.avi'}).json()
    response=client.post(f'/api/cameras/{cam["id"]}/start')
    assert response.status_code==400,response.text
    health=client.get(f'/api/cameras/{cam["id"]}').json()['health']
    assert health['status']=='error' and health['status_code']=='ERROR'
    assert '不存在' in health['error']


def test_disabling_camera_stops_capture(client):
    cam=camera(client)
    assert client.post(f'/api/cameras/{cam["id"]}/start').status_code==200
    updated=client.patch(f'/api/cameras/{cam["id"]}',json={'enabled':False})
    assert updated.status_code==200,updated.text
    assert updated.json()['health']['status']=='stopped'
    assert client.post(f'/api/cameras/{cam["id"]}/start').status_code==400


def test_settings_cleanup(client):
    assert client.patch('/api/settings',json={'retention_days':30}).json()['retention_days']==30
    assert client.patch('/api/settings',json={'retention_days':99}).status_code==422
    assert client.post('/api/system/cleanup',json={'all':True}).status_code==409
    assert client.post('/api/storage/cleanup').json()['deleted_events']==0


def test_global_fps_and_privacy_recording_switch(client):
    cam=camera(client);item=phone(client)
    assert client.patch('/api/settings',json={'inference_fps':3,'record_events':False}).status_code==200
    assert client.get(f'/api/cameras/{cam["id"]}').json()['inference_fps']==3
    client.app.state.runtime.event({'item_id':item['id'],'camera_id':cam['id'],'event_type':'seen'},np.zeros((40,40,3),np.uint8))
    assert client.get('/api/events').json()==[]
    assert client.patch('/api/settings',json={'privacy_mode':False}).status_code==422
    assert client.patch('/api/settings',json={'retention_days':0}).json()['retention_days']==0


def test_video_to_database_search_media(tmp_path):
    assert (ROOT/'demo/sample-videos/object-memory-demo.avi').is_file(),'Run scripts/generate-demo-assets.py first'
    app=create_app(tmp_path/'demo-data',runtime_mode='DEMO')
    with TestClient(app,base_url='http://127.0.0.1') as client:
        client.cookies.set('om_session',app.state.runtime.sessions.issue())
        response=client.post('/api/system/demo-seed')
        assert response.status_code==200,response.text
        cid=response.json()['camera_id']
        client.patch('/api/settings',json={'pre_seconds':1,'post_seconds':1})
        assert client.post(f'/api/cameras/{cid}/start').status_code==200
        deadline=time.monotonic()+45
        found=None
        while time.monotonic()<deadline:
            rows=client.get('/api/events',params={'event_type':'movement'}).json()
            found=next((e for e in rows if e['item_name']=='我的手机' and (e.get('to_zone') or e.get('zone_name'))=='沙发右侧' and e.get('evidence_status')=='confirmed'),None)
            if found:break
            time.sleep(.25)
        assert found,client.get(f'/api/cameras/{cid}').json()
        result=client.post('/api/search',json={'query':'我的手机在哪里'}).json()['results'][0]
        assert result['last_confirmed']['zone_name']=='沙发右侧'
        assert client.get(found['screenshot_path']).status_code==200
        assert found['clip_path'],found
        assert len(client.get(found['clip_path']).content)>1000
        assert client.get(f'/api/cameras/{cid}/frame').headers['content-type']=='image/jpeg'
        client.post(f'/api/cameras/{cid}/stop')
