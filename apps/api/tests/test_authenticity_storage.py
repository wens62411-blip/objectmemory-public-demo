from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import gc
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import sqlite3
import threading
import time
import weakref

import numpy as np
import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from apps.api.app.db import Database
from apps.api.app.event_service import EventService, EvidenceRejected
from apps.api.app.firmware import DeviceClaim
from apps.api.app.mode_lock import RuntimeModeBusy, RuntimeModeLease
from scripts.event_audit import audit_database


@contextmanager
def client_for(tmp_path: Path, mode="TEST"):
    app=create_app(tmp_path,testing=mode=="TEST",runtime_mode=mode)
    with TestClient(app,base_url='http://127.0.0.1') as client:
        client.cookies.set("om_session",app.state.runtime.sessions.issue())
        assert client.get("/api/session").status_code==200
        yield client


def start_mode_lease_process(data_root: Path, mode: str):
    """Hold a real OS lock in another interpreter until stdin closes."""
    ready=data_root/f"lease-{mode.lower()}-{time.time_ns()}.ready"
    code=(
        "import sys\n"
        "from pathlib import Path\n"
        "from apps.api.app.mode_lock import RuntimeModeLease\n"
        "lease=RuntimeModeLease(Path(sys.argv[1]),sys.argv[2]).acquire()\n"
        "Path(sys.argv[3]).write_text('READY',encoding='utf-8')\n"
        "sys.stdin.readline()\n"
        "lease.release()\n"
    )
    process=subprocess.Popen(
        [sys.executable,"-c",code,str(data_root),mode,str(ready)],
        cwd=Path(__file__).resolve().parents[3],stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
    )
    deadline=time.monotonic()+10
    while time.monotonic()<deadline and not ready.exists():
        if process.poll() is not None:
            pytest.fail(f"模式锁子进程启动失败：{process.stderr.read()}")
        time.sleep(.05)
    if not ready.exists():
        process.terminate();process.wait(timeout=10)
        pytest.fail("模式锁子进程未在 10 秒内就绪。")
    return process


def add_item_camera(client: TestClient):
    item=client.post("/api/items",json={"name":"钥匙","type":"keys"}).json()
    camera=client.post("/api/cameras",json={"name":"测试摄像头","source_type":"webcam","source":"0"}).json()
    return item,camera


def complete_event(client: TestClient,item:dict,camera:dict,index:int=1):
    runtime=client.app.state.runtime
    event_id=f"movement-{index}"
    start=datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(seconds=index*10)
    runtime.db.save("source_sessions",{
        "camera_id":camera["id"],"runtime_mode":"TEST","source_type":"opencv_camera",
        "is_simulated":True,"started_at":"2026-01-01T00:00:00+00:00",
        "first_frame":0,"last_frame":index*10+9,"last_frame_at":(start+timedelta(seconds=2)).isoformat(),
        "status":"streaming","continuity_ok":True,
    },"source-session-a")
    return {
        "id":event_id,"event_id":event_id,"event_type":"movement","movement_session_id":f"episode-{index}",
        "item_id":item["id"],"camera_id":camera["id"],"runtime_mode":"TEST","source_type":"opencv_camera","is_simulated":True,
        "source_session_id":"source-session-a","source_frame_start":index*10,"source_frame_end":index*10+9,
        "source_timestamp_start":start.isoformat(),"source_timestamp_end":(start+timedelta(seconds=2)).isoformat(),
        "stable_before":True,"stable_after":True,"source_continuity_ok":True,"from_zone":"桌面","to_zone":f"柜子{index}",
        "pickup_evidence":{"source_frame":index*10,"source_timestamp":start.timestamp()},"placement_evidence":{"source_frame":index*10+9,"source_timestamp":(start+timedelta(seconds=2)).timestamp()},
        "zone_name":f"柜子{index}","meaningful_position_change":True,"confidence":.92,"detector_backend":"aruco",
        "tracker_backend":"stable_identity","detection_mode":"aruco",
    }


def record_event(client:TestClient,item:dict,camera:dict,index:int=1,event:dict|None=None):
    frame=np.full((48,64,3),60+index,dtype=np.uint8)
    before=np.full_like(frame,20+index)
    return client.app.state.runtime.event(event or complete_event(client,item,camera,index),frame,[before,frame,frame])


def record_real_event(client:TestClient,item:dict,camera:dict,index:int=1):
    runtime=client.app.state.runtime
    event=complete_event(client,item,camera,index)
    event.update({"runtime_mode":"REAL","source_type":"opencv_camera","is_simulated":False})
    start=datetime.fromisoformat(event["source_timestamp_start"]);end=datetime.fromisoformat(event["source_timestamp_end"])
    runtime.db.save("source_sessions",{
        "camera_id":camera["id"],"runtime_mode":"REAL","source_type":"opencv_camera","is_simulated":False,
        "started_at":"2026-01-01T00:00:00+00:00","first_frame":0,"last_frame":index*10+9,
        "last_frame_at":(end+timedelta(seconds=1)).isoformat(),"status":"streaming","continuity_ok":True,
    },event["source_session_id"])
    return record_event(client,item,camera,index,event)


def test_testing_forces_isolated_test_layout(tmp_path):
    app=create_app(tmp_path,testing=True,runtime_mode="REAL")
    runtime=app.state.runtime
    try:
        assert runtime.mode.value=="TEST"
        assert runtime.db.path==tmp_path/"database"/"objectmemory-test.sqlite"
        assert runtime.media==tmp_path/"test-media"
        with __import__('pytest').raises(RuntimeError):
            create_app(tmp_path/"invalid",runtime_mode="normal")
    finally:
        runtime.mode_lease.close()


def test_importing_factory_does_not_create_any_database(tmp_path):
    env={**os.environ,"OM_DATA_DIR":str(tmp_path),"OM_RUNTIME_MODE":"TEST"}
    subprocess.run([sys.executable,"-c","import apps.api.app.main"],check=True,env=env,cwd=Path(__file__).resolve().parents[3])
    assert not (tmp_path/"database"/"objectmemory.sqlite").exists()
    assert not (tmp_path/"database"/"objectmemory-test.sqlite").exists()


def test_real_rejects_demo_sources_and_has_honest_empty_search(tmp_path):
    with client_for(tmp_path,"REAL") as client:
        item=client.post("/api/items",json={"name":"我的手机","type":"phone"}).json()
        assert client.post("/api/cameras",json={"name":"伪来源","source_type":"video","source":"demo/sample-videos/object-memory-demo.avi"}).status_code==409
        assert client.post("/api/system/demo-seed").status_code==409
        assert client.post("/api/virtual-device/start",json={"source":"video"}).status_code==409
        result=client.post("/api/search",json={"query":"我的手机在哪里"}).json()["results"][0]
        assert result["last_confirmed"] is None
        assert result["answer"]=="暂时没有真实摄像头产生的位置记录。"
        assert client.app.state.runtime.db.count("events")==0


def test_verified_browser_observation_can_update_last_seen_but_never_confirm_history(tmp_path):
    """Unit persistence fixture: verified JPEGs are not physical attestation."""
    with client_for(tmp_path,"REAL") as client:
        runtime=client.app.state.runtime
        item=client.post("/api/items",json={"name":"我的手机","type":"phone"}).json()
        camera=client.post("/api/cameras",json={"name":"浏览器直连","source_type":"browser","source":"browser"}).json()
        start=datetime(2026,1,1,tzinfo=timezone.utc)
        runtime.db.save("source_sessions",{
            "camera_id":camera["id"],"runtime_mode":"REAL","source_type":"browser_camera",
            "is_simulated":False,"started_at":start.isoformat(),"first_frame":0,"last_frame":20,
            "last_frame_at":(start+timedelta(seconds=4)).isoformat(),"status":"streaming","continuity_ok":True,
        },"browser-client-session")
        runtime.event_service.observe({
            "item_id":item["id"],"camera_id":camera["id"],"source_session_id":"browser-client-session",
            "source_frame":5,"last_seen":(start+timedelta(seconds=1)).isoformat(),"zone_name":"桌面",
            "center":[.2,.3],"confidence":.9,"state":"VISIBLE_STATIC",
        }, observation_verified=True)
        candidate={
            "id":"browser-replay","event_id":"browser-replay","event_type":"movement",
            "movement_session_id":"browser-replay-episode","item_id":item["id"],"camera_id":camera["id"],
            "runtime_mode":"REAL","source_type":"browser_camera","is_simulated":False,
            "source_session_id":"browser-client-session","source_frame_start":5,"source_frame_end":15,
            "source_timestamp_start":(start+timedelta(seconds=1)).isoformat(),
            "source_timestamp_end":(start+timedelta(seconds=3)).isoformat(),
            "stable_before":True,"stable_after":True,"source_continuity_ok":True,
            "from_zone":"桌面","to_zone":"沙发","meaningful_position_change":True,"confidence":.92,
            "pickup_evidence":{"source_frame":5,"source_timestamp":(start+timedelta(seconds=1)).timestamp()},
            "placement_evidence":{"source_frame":15,"source_timestamp":(start+timedelta(seconds=3)).timestamp()},
            "detector_backend":"aruco","tracker_backend":"stable_identity","detection_mode":"aruco",
        }
        frame=np.full((48,64,3),90,np.uint8)
        assert runtime.event(candidate,frame,[frame.copy(),frame.copy()]) is False
        assert runtime.db.count("events")==0
        state=runtime.db.get("item_current_state",f"REAL:{item['id']}")
        assert state and state["status"]=="last_seen" and state["source_type"]=="browser_camera"
        assert list((runtime.media/"event-images").glob("*"))==[]
        assert list((runtime.media/"event-clips").glob("*"))==[]


@pytest.mark.parametrize("camera_source,provenance", [
    ("rtsp", "rtsp"),
    ("onvif", "onvif"),
    ("mjpeg", "mjpeg"),
])
def test_real_unattested_network_stream_cannot_confirm_history(tmp_path,camera_source,provenance):
    """A software replay server must not become physical-camera proof."""
    with client_for(tmp_path,"REAL") as client:
        runtime=client.app.state.runtime
        item=client.post("/api/items",json={"name":"我的手机","type":"phone"}).json()
        camera=runtime.db.save("cameras",{
            "name":"未验证网络流","room_name":"客厅","source_type":camera_source,
            "source":"rtsp://192.168.1.20/live" if camera_source in {"rtsp","onvif"} else "http://192.168.1.20/stream",
            "config":{},"enabled":True,"inference_fps":5,"save_clips":True,"runtime_mode":"REAL",
        },f"camera-{camera_source}")
        start=datetime(2026,1,1,tzinfo=timezone.utc);end=start+timedelta(seconds=2)
        runtime.db.save("source_sessions",{
            "camera_id":camera["id"],"runtime_mode":"REAL","source_type":provenance,
            "is_simulated":False,"started_at":start.isoformat(),"first_frame":1,"last_frame":20,
            "last_frame_at":end.isoformat(),"status":"streaming","continuity_ok":True,
        },f"session-{camera_source}")
        event={
            "id":f"network-{camera_source}","event_id":f"network-{camera_source}",
            "event_type":"movement","movement_session_id":f"episode-{camera_source}",
            "item_id":item["id"],"camera_id":camera["id"],"runtime_mode":"REAL",
            "source_type":provenance,"is_simulated":False,"source_session_id":f"session-{camera_source}",
            "source_frame_start":1,"source_frame_end":20,
            "source_timestamp_start":start.isoformat(),"source_timestamp_end":end.isoformat(),
            "stable_before":True,"stable_after":True,"source_continuity_ok":True,
            "from_zone":"桌面","to_zone":"沙发","meaningful_position_change":True,"confidence":.95,
            "pickup_evidence":{"source_frame":1,"source_timestamp":start.timestamp()},
            "placement_evidence":{"source_frame":20,"source_timestamp":end.timestamp()},
            "detector_backend":"aruco","tracker_backend":"stable_identity","detection_mode":"aruco",
        }
        frame=np.full((48,64,3),100,np.uint8)
        assert runtime.event(event,frame,[frame.copy(),frame.copy()]) is False
        assert runtime.db.count("events")==0
        assert list((runtime.media/"event-images").glob("*"))==[]
        assert list((runtime.media/"event-clips").glob("*"))==[]


def test_real_esp32_event_requires_authoritative_firmware_binding_and_auditor_agrees(tmp_path):
    with client_for(tmp_path,"REAL") as client:
        runtime=client.app.state.runtime
        item=client.post("/api/items",json={"name":"ESP32 前物品","type":"phone"}).json()
        camera=runtime.db.save("cameras",{
            "name":"自报 ESP32","room_name":"客厅","source_type":"esp32",
            "source":"http://127.0.0.1:8766/stream",
            "config":{"device_id":"fake-device","capture_url":"http://127.0.0.1:8766/capture"},
            "enabled":True,"inference_fps":5,"save_clips":True,"runtime_mode":"REAL",
        },"fake-esp32-camera")
        started=datetime(2026,1,1,tzinfo=timezone.utc);ended=started+timedelta(seconds=2)
        runtime.db.save("source_sessions",{
            "camera_id":camera["id"],"runtime_mode":"REAL","source_type":"esp32_real","is_simulated":False,
            "started_at":started.isoformat(),"first_frame":1,"last_frame":20,"last_frame_at":ended.isoformat(),
            "status":"streaming","continuity_ok":True,
        },"self-asserted-esp32-session")
        forged={
            "id":"forged-esp32","event_id":"forged-esp32","event_type":"movement","movement_session_id":"forged-episode",
            "item_id":item["id"],"camera_id":camera["id"],"runtime_mode":"REAL","source_type":"esp32_real","is_simulated":False,
            "source_session_id":"self-asserted-esp32-session","source_frame_start":1,"source_frame_end":20,
            "source_timestamp_start":started.isoformat(),"source_timestamp_end":ended.isoformat(),
            "stable_before":True,"stable_after":True,"source_continuity_ok":True,"from_zone":"桌面","to_zone":"沙发",
            "meaningful_position_change":True,"confidence":.95,
            "pickup_evidence":{"source_frame":1,"source_timestamp":started.timestamp()},
            "placement_evidence":{"source_frame":20,"source_timestamp":ended.timestamp()},
            "detector_backend":"aruco","tracker_backend":"stable_identity","detection_mode":"aruco",
        }
        frame=np.full((48,64,3),112,np.uint8)
        assert runtime.event(forged,frame,[frame.copy(),frame.copy()]) is False
        assert runtime.db.count("events",unscoped=True)==0
        assert list((runtime.media/"event-images").glob("*"))==[]
        assert list((runtime.media/"event-clips").glob("*"))==[]

        # Forge an otherwise internally consistent historical bundle by
        # rewriting a valid webcam event and its signed bindings.  The offline
        # auditor must still consult firmware_devices instead of camera JSON.
        verified_camera=client.post("/api/cameras",json={"name":"临时本机源","source_type":"webcam","source":"0"}).json()
        verified_event=complete_event(client,item,verified_camera,2)
        verified_event.update({"runtime_mode":"REAL","source_type":"opencv_camera","is_simulated":False})
        event_start=datetime.fromisoformat(verified_event["source_timestamp_start"]);event_end=datetime.fromisoformat(verified_event["source_timestamp_end"])
        runtime.db.save("source_sessions",{"camera_id":verified_camera["id"],"runtime_mode":"REAL","source_type":"opencv_camera","is_simulated":False,"started_at":(event_start-timedelta(seconds=1)).isoformat(),"first_frame":0,"last_frame":40,"last_frame_at":(event_end+timedelta(seconds=1)).isoformat(),"status":"streaming","continuity_ok":True},verified_event["source_session_id"])
        saved=record_event(client,item,verified_camera,2,verified_event)
        assert saved
        esp32_config={"device_id":"missing-registry-device","capture_url":"http://127.0.0.1:9000/capture"}
        runtime.db.save("cameras",{"source_type":"esp32","source":"http://127.0.0.1:9000/stream","config":esp32_config},verified_camera["id"])
        runtime.db.save("source_sessions",{"source_type":"esp32_real"},verified_event["source_session_id"])
        runtime.db.save("events",{"source_type":"esp32_real"},saved["id"])
        for media in runtime.db.list("event_media",{"event_id":saved["event_id"]},unscoped=True):
            metadata=dict(media["metadata"]);binding=dict(metadata["write_binding"])
            binding["source_type"]="esp32_real"
            unsigned={key:value for key,value in binding.items() if key!="binding_sha256"}
            binding["binding_sha256"]=hashlib.sha256(json.dumps(unsigned,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode("utf-8")).hexdigest()
            metadata["write_binding"]=binding
            runtime.db.save("event_media",{"metadata":metadata},media["id"])
        rows,_=audit_database(runtime.db.path)
        audited=next(row for row in rows if row["event_id"]==saved["event_id"])
        assert audited["classification"]=="unknown"
        assert audited["provenance_contract"]["camera_provenance"] is False


def test_reconnect_creates_bounded_source_session_and_seals_old_session(tmp_path):
    with client_for(tmp_path,"REAL") as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        base=datetime.now(timezone.utc)-timedelta(seconds=10)
        runtime.db.save("source_sessions",{
            "camera_id":camera["id"],"runtime_mode":"REAL","source_type":"opencv_camera","is_simulated":False,
            "started_at":(base-timedelta(seconds=20)).isoformat(),"first_frame":0,"last_frame":77,
            "last_frame_at":(base-timedelta(seconds=1)).isoformat(),"status":"streaming","continuity_ok":True,
        },"old-source-session")

        class ReconnectedEngine:
            def health(self):return {"source_session_id":"new-source-session","source_session_started_at":base.timestamp(),"source_frame_sequence":30,"reconnect_epoch":1,"status":"ready","dropped_frames":0,"reconnects":1,"width":64,"height":48}
            def stop(self):return None
        runtime.engines[camera["id"]]=ReconnectedEngine()
        runtime.track({"item_id":item["id"],"camera_id":camera["id"],"source_session_id":"new-source-session","source_frame":20,"last_seen":(base+timedelta(seconds=3)).isoformat(),"zone_name":"桌面","center":[.2,.3],"confidence":.9,"state":"VISIBLE_STATIC"})
        old=runtime.db.get("source_sessions","old-source-session",unscoped=True)
        new=runtime.db.get("source_sessions","new-source-session",unscoped=True)
        assert old["status"]=="stopped" and old["ended_at"] and old["continuity_ok"] is False
        assert new["status"]=="streaming" and new["started_at"] and new["first_frame"]==20 and new["last_frame"]==20

        event={
            "id":"after-reconnect","event_id":"after-reconnect","event_type":"movement","movement_session_id":"after-reconnect-episode",
            "item_id":item["id"],"camera_id":camera["id"],"runtime_mode":"REAL","source_type":"opencv_camera","is_simulated":False,
            "source_session_id":"new-source-session","source_frame_start":20,"source_frame_end":30,
            "source_timestamp_start":(base+timedelta(seconds=1)).isoformat(),"source_timestamp_end":(base+timedelta(seconds=2)).isoformat(),
            "stable_before":True,"stable_after":True,"source_continuity_ok":True,"from_zone":"桌面","to_zone":"沙发","meaningful_position_change":True,"confidence":.94,
            "pickup_evidence":{"source_frame":20,"source_timestamp":(base+timedelta(seconds=1)).timestamp()},
            "placement_evidence":{"source_frame":30,"source_timestamp":(base+timedelta(seconds=2)).timestamp()},
            "detector_backend":"aruco","tracker_backend":"stable_identity","detection_mode":"aruco",
        }
        frame=np.full((48,64,3),125,np.uint8)
        saved=runtime.event(event,frame,[frame.copy(),frame.copy()])
        assert saved and saved["source_session_id"]=="new-source-session"
        cross={**event,"id":"cross-session","event_id":"cross-session","movement_session_id":"cross-session-episode","source_session_id":"old-source-session"}
        assert runtime.event(cross,frame,[frame.copy(),frame.copy()]) is False
        assert runtime.db.count("events")==1


def test_evidence_gate_idempotency_and_current_state(tmp_path):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client)
        candidate={"item_id":item["id"],"camera_id":camera["id"],"event_type":"placed"}
        assert client.app.state.runtime.event(candidate,np.zeros((20,20,3),np.uint8)) is False
        event=complete_event(client,item,camera)
        frame=np.full((48,64,3),99,np.uint8)
        first=client.app.state.runtime.event(event,frame,[np.zeros_like(frame),frame])
        repeated=client.app.state.runtime.event({**event,"id":"different-request-id","event_id":"different-request-id"})
        assert first["id"]==repeated["id"]
        assert client.app.state.runtime.db.count("events")==1
        state=client.app.state.runtime.db.get("item_current_state",f"TEST:{item['id']}")
        assert state["evidence_event_id"]==first["event_id"]
        assert first["screenshot_sha256"] and first["clip_sha256"]
        assert first["before_screenshot"]==first["after_screenshot"]==first["screenshot_path"]
        assert len(list((client.app.state.runtime.media/"event-images").glob("*")))==1


@pytest.mark.parametrize("failure_point", ["first_media", "second_media", "current_state"])
def test_event_bundle_failure_rolls_back_database_files_and_search(tmp_path,monkeypatch,failure_point):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        event=complete_event(client,item,camera)
        original_encode=runtime.db._encode
        media_calls=0

        def fail_inside_transaction(table,data):
            nonlocal media_calls
            if table=="event_media":
                media_calls+=1
                if failure_point=="first_media" and media_calls==1:
                    raise sqlite3.OperationalError("forced first media registry failure")
                if failure_point=="second_media" and media_calls==2:
                    raise sqlite3.OperationalError("forced second media registry failure")
            if table=="item_current_state" and failure_point=="current_state":
                raise sqlite3.OperationalError("forced current state failure")
            return original_encode(table,data)

        monkeypatch.setattr(runtime.db,"_encode",fail_inside_transaction)
        saved=record_event(client,item,camera,event=event)
        assert saved is False
        assert runtime.db.count("events",unscoped=True)==0
        assert runtime.db.count("event_media",unscoped=True)==0
        assert runtime.db.count("item_current_state",unscoped=True)==0
        assert not list((runtime.media/"event-images").glob("*"))
        assert not list((runtime.media/"event-clips").glob("*"))
        result=client.post("/api/search",json={"query":"钥匙在哪"}).json()["results"][0]
        assert result["last_confirmed"] is None
        assert "还没有" in result["answer"]
        assert any(row["code"]=="EVENT_PERSIST_FAILED" for row in runtime.camera_logs)


def test_concurrent_whitespace_identity_variants_are_one_event_bundle(tmp_path,monkeypatch):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        first=complete_event(client,item,camera)
        second={**first,
            "id":"movement-whitespace","event_id":"movement-whitespace",
            "runtime_mode":" test ","item_id":f" {item['id']} ","camera_id":f" {camera['id']} ",
            "source_session_id":f" {first['source_session_id']} ",
            "movement_session_id":f" {first['movement_session_id']} ",
        }
        original_list=runtime.db.list
        barrier=threading.Barrier(2)

        def synchronize_preflight(table,filters=None,*args,**kwargs):
            if table=="events" and isinstance(filters,dict) and "movement_session_id" in filters:
                barrier.wait(timeout=10)
            return original_list(table,filters,*args,**kwargs)

        monkeypatch.setattr(runtime.db,"list",synchronize_preflight)
        frame=np.full((48,64,3),91,np.uint8);before=np.full_like(frame,21)
        results=[]
        def submit(candidate):
            results.append(runtime.event(candidate,frame.copy(),[before.copy(),frame.copy()]))
        threads=[threading.Thread(target=submit,args=(candidate,)) for candidate in (first,second)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(30)
        assert all(not thread.is_alive() for thread in threads)
        assert len(results)==2 and results[0]["id"]==results[1]["id"]
        assert runtime.db.count("events",unscoped=True)==1
        assert runtime.db.count("event_media",unscoped=True)==2
        assert runtime.db.count("item_current_state",unscoped=True)==1
        row=original_list("events",limit=2,unscoped=True)[0]
        assert row["runtime_mode"]=="TEST"
        assert row["item_id"]==item["id"] and row["camera_id"]==camera["id"]
        assert row["source_session_id"]==first["source_session_id"]
        assert row["movement_session_id"]==first["movement_session_id"]
        assert len(list((runtime.media/"event-images").glob("*")))==1
        assert len(list((runtime.media/"event-clips").glob("*")))==1


def test_cross_service_concurrent_source_window_conflict_keeps_one_bundle(tmp_path,monkeypatch):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        first=complete_event(client,item,camera)
        second={**first,"id":"conflicting-window","event_id":"conflicting-window","movement_session_id":"conflicting-episode"}
        services=[EventService(runtime.db,runtime.mode,runtime.media),EventService(runtime.db,runtime.mode,runtime.media)]
        original_insert=runtime.db.insert_event_bundle_idempotent
        barrier=threading.Barrier(2)
        def synchronize_insert(*args,**kwargs):
            barrier.wait(timeout=15)
            return original_insert(*args,**kwargs)
        monkeypatch.setattr(runtime.db,"insert_event_bundle_idempotent",synchronize_insert)
        before=np.full((48,64,3),21,np.uint8)
        after=[np.full((48,64,3),value,np.uint8) for value in (91,92)]
        saved=[];rejected=[]
        def submit(service,candidate,frame):
            try:saved.append(service.record(candidate,frame,[before.copy(),frame.copy()]))
            except EvidenceRejected as exc:rejected.append(str(exc))
        threads=[threading.Thread(target=submit,args=(services[index],candidate,after[index])) for index,candidate in enumerate((first,second))]
        for thread in threads:thread.start()
        for thread in threads:thread.join(30)
        assert all(not thread.is_alive() for thread in threads)
        assert len(saved)==1 and len(rejected)==1 and "像素哈希" in rejected[0]
        assert runtime.db.count("events",unscoped=True)==1
        assert runtime.db.count("event_media",unscoped=True)==2
        assert len(list((runtime.media/"event-images").glob("*")))==1
        assert len(list((runtime.media/"event-clips").glob("*")))==1


def test_blank_source_session_identity_falls_back_to_engine_health(tmp_path):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        event=complete_event(client,item,camera)
        event["source_session_id"]="   "
        class RunningEngine:
            @staticmethod
            def health():
                return {"source_session_id":"source-session-a","source_frame_sequence":19,"status":"running"}
            @staticmethod
            def stop():
                return None
        runtime.engines[camera["id"]]=RunningEngine()
        saved=record_event(client,item,camera,event=event)
        assert saved and saved["source_session_id"]=="source-session-a"
        assert runtime.db.count("events",unscoped=True)==1
        assert runtime.db.count("event_media",unscoped=True)==2


def test_vision_callback_cannot_spoof_manual_or_supply_media_paths(tmp_path):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        frame=np.full((48,64,3),90,np.uint8)
        manual_claim={**complete_event(client,item,camera),"manually_corrected":True,"created_by":"user"}
        assert runtime.event(manual_claim,frame,[frame.copy(),frame.copy()]) is False
        type_claim={**complete_event(client,item,camera),"event_type":"manual_correction"}
        assert runtime.event(type_claim,frame,[frame.copy(),frame.copy()]) is False
        supplied={**complete_event(client,item,camera),"screenshot_path":"/media/event-images/unrelated.jpg","screenshot_sha256":"a"*64}
        assert runtime.event(supplied,frame,[frame.copy(),frame.copy()]) is False
        assert runtime.db.count("events")==0
        assert not list((runtime.media/"event-images").glob("*"))


def test_pipeline_confidence_allowlist_and_atomic_binding_are_enforced(tmp_path):
    with client_for(tmp_path,"REAL") as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        base=complete_event(client,item,camera)
        base.update({"runtime_mode":"REAL","source_type":"opencv_camera","is_simulated":False})
        start=datetime.fromisoformat(base["source_timestamp_start"]);end=datetime.fromisoformat(base["source_timestamp_end"])
        runtime.db.save("source_sessions",{
            "camera_id":camera["id"],"runtime_mode":"REAL","source_type":"opencv_camera","is_simulated":False,
            "started_at":(start-timedelta(seconds=1)).isoformat(),"first_frame":0,"last_frame":30,
            "last_frame_at":(end+timedelta(seconds=1)).isoformat(),"status":"streaming","continuity_ok":True,
        },base["source_session_id"])
        assert record_event(client,item,camera,event={**base,"confidence":.2}) is False
        assert record_event(client,item,camera,event={**base,"detector_backend":"payload_claim"}) is False
        assert record_event(client,item,camera,event={**base,"tracker_backend":"centroid"}) is False
        assert record_event(client,item,camera,event={**base,"detection_mode":"magic"}) is False
        saved=record_event(client,item,camera,event=base)
        assert saved and runtime.db.count("events")==1
        media=runtime.db.list("event_media",{"event_id":saved["event_id"]},unscoped=True)
        assert len(media)==2
        for row in media:
            binding=row["metadata"]["write_binding"]
            assert binding["capture_origin"]=="vision_callback"
            assert binding["source_session_id"]==saved["source_session_id"]
            assert binding["source_frame_start"]==saved["source_frame_start"]
            assert binding["source_frame_end"]==saved["source_frame_end"]
            assert binding["clip_input_frames"]>=2
            assert len(binding["binding_sha256"])==64


def test_unverified_track_cannot_upsert_current_state(tmp_path):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client)
        runtime=client.app.state.runtime
        runtime.track({"item_id":item["id"],"camera_id":camera["id"],"source_session_id":"session-track","last_seen":datetime.now(timezone.utc).isoformat(),"zone_name":"桌面","center":[.2,.3],"confidence":.9,"observation_verified":True})
        assert runtime.db.count("events")==0
        assert runtime.db.count("tracks")==0
        assert runtime.db.get("item_current_state",f"TEST:{item['id']}") is None


def test_newer_uppercase_occlusion_overrides_answer_but_keeps_confirmation(tmp_path):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        confirmed=record_event(client,item,camera)
        runtime.event_service.observe({"item_id":item["id"],"camera_id":camera["id"],"source_session_id":"source-session-a","last_seen":"2026-01-01T00:01:00+00:00","zone_name":"柜子1","state":"OCCLUDED","center":[.2,.3],"confidence":.5}, observation_verified=True)
        result=client.post("/api/search",json={"query":"钥匙在哪"}).json()["results"][0]
        assert result["last_confirmed"]["event_id"]==confirmed["event_id"]
        assert result["status"]=="occluded"
        assert "被遮挡" in result["answer"] and "当前具体位置未确认" in result["answer"]
        assert result["evidence"]["evidence_status"]=="observation_only"
        assert result["evidence"]["event_type"]=="occluded"
        assert result["evidence"]["source_session_id"]=="source-session-a"


def test_minimal_counts_current_evidence_within_two(tmp_path):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        runtime.retention.set_policy("FORENSIC")
        for index in range(1,4):record_event(client,item,camera,index)
        if runtime.retention._cleanup_thread:runtime.retention._cleanup_thread.join(10)
        runtime.retention.set_policy("MINIMAL")
        report=runtime.retention.cleanup(trigger="test")
        rows=runtime.db.list("events",{"item_id":item["id"]},limit=20)
        assert len(rows)==2
        assert runtime.db.get("item_current_state",f"TEST:{item['id']}")["evidence_event_id"] in {row["event_id"] for row in rows}
        assert report["deleted_events"]==1


def test_pending_delete_is_recovered_after_file_failure(tmp_path,monkeypatch):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        event=record_event(client,item,camera)
        target=runtime.event_service.media_path(event["screenshot_path"])
        original=Path.unlink
        def fail_once(path,*args,**kwargs):
            if path==target:raise OSError("locked")
            return original(path,*args,**kwargs)
        monkeypatch.setattr(Path,"unlink",fail_once)
        runtime.retention.delete_event(event["id"])
        pending=runtime.db.list("event_media",{"status":"pending_delete"},unscoped=True)
        assert any(row["path"]==event["screenshot_path"] for row in pending)
        monkeypatch.setattr(Path,"unlink",original)
        runtime.retention.recover_pending()
        assert not target.exists()
        assert not runtime.db.list("event_media",{"status":"pending_delete"},unscoped=True)


def test_pending_recovery_expires_event_but_preserves_shared_active_file(tmp_path):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        runtime.retention.set_policy("FORENSIC")
        first=record_event(client,item,camera,1)
        second=record_event(client,item,camera,2)
        if runtime.retention._cleanup_thread:runtime.retention._cleanup_thread.join(10)
        first_media=next(row for row in runtime.db.list("event_media",{"event_id":first["event_id"]},unscoped=True) if row["kind"]=="image")
        second_media=next(row for row in runtime.db.list("event_media",{"event_id":second["event_id"]},unscoped=True) if row["kind"]=="image")
        shared_path=first_media["path"];shared_file=runtime.event_service.media_path(shared_path)
        runtime.db.save("events",{"screenshot_path":shared_path},second["id"])
        runtime.db.save("event_media",{"path":shared_path,"status":"active"},second_media["id"])
        runtime.db.save("event_media",{"status":"pending_delete"},first_media["id"])
        runtime.retention.recover_pending()
        assert shared_file.exists()
        assert runtime.db.get("events",first["id"])["screenshot_path"] is None
        assert runtime.db.get("events",first["id"])["evidence_status"]=="media_expired"
        assert runtime.db.get("event_media",first_media["id"],unscoped=True) is None
        assert runtime.db.get("event_media",second_media["id"],unscoped=True)["status"]=="active"


def test_storage_api_separates_cleanup_from_real_delete(tmp_path):
    with client_for(tmp_path,"REAL") as client:
        assert client.post("/api/system/cleanup",json={"all":True}).status_code==409
        assert client.post("/api/storage/delete-real",json={"confirmation":"yes"}).status_code==422
        status=client.get("/api/storage/status").json()
        assert status["runtime_mode"]=="REAL" and status["retention_policy"]=="MINIMAL"
        assert client.patch("/api/storage/policy",json={"policy":"BALANCED"}).json()["retention_policy"]=="BALANCED"


def test_evidence_threshold_settings_round_trip_and_validate(tmp_path):
    with client_for(tmp_path) as client:
        update={"min_detection_frames":4,"min_stable_frames":7,"max_frame_gap_seconds":.9,"min_move_distance":.05,"same_zone_move_distance":.14,"min_confidence":.7,"stable_speed":.02,"stable_position_jitter":.012,"min_stable_seconds":2.2,"occluded_seconds":.8,"lost_seconds":9}
        response=client.patch("/api/settings",json=update)
        assert response.status_code==200,response.text
        current=client.get("/api/settings").json()
        assert all(current[key]==value for key,value in update.items())
        assert client.patch("/api/settings",json={"min_move_distance":.2,"same_zone_move_distance":.1}).status_code==422
        assert client.patch("/api/settings",json={"occluded_seconds":10,"lost_seconds":5}).status_code==422


def test_clear_test_empties_isolated_tables_and_keeps_real_database(tmp_path):
    with client_for(tmp_path,"REAL") as real_client:
        real_item=real_client.post("/api/items",json={"name":"真实配置","type":"keys"}).json()
    with client_for(tmp_path,"TEST") as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        record_event(client,item,camera)
        response=client.post("/api/storage/clear-test")
        assert response.status_code==200,response.text
        for table in ("items","cameras","zones","movement_events","event_media","item_current_state","tracks","source_sessions"):
            assert runtime.db.count(table,unscoped=True)==0
        assert not any(path.is_file() for path in runtime.media.rglob("*"))
    real=create_app(tmp_path,runtime_mode="REAL").state.runtime
    try:
        assert real.db.get("items",real_item["id"],unscoped=True) is not None
    finally:
        real.mode_lease.close()


def test_cross_process_clear_rejects_active_target_then_succeeds_after_release(tmp_path):
    demo_db=Database(tmp_path/"database"/"objectmemory-demo.sqlite","DEMO")
    demo_db.save("items",{"name":"活动演示物品","type":"phone"},"active-demo-item")
    media=tmp_path/"demo-media"/"event-images"/"active.jpg"
    media.parent.mkdir(parents=True,exist_ok=True);media.write_bytes(b"active-demo-media")
    holder=start_mode_lease_process(tmp_path,"DEMO")
    try:
        with pytest.raises(RuntimeModeBusy,match="DEMO 模式已有活动进程"):
            create_app(tmp_path,runtime_mode="DEMO")
        real_app=create_app(tmp_path,runtime_mode="REAL")
        with TestClient(real_app,base_url='http://127.0.0.1') as client:
            client.cookies.set("om_session",real_app.state.runtime.sessions.issue())
            blocked=client.post("/api/storage/clear-demo")
            assert blocked.status_code==409 and "活动进程" in blocked.json()["detail"]
            assert demo_db.get("items","active-demo-item",unscoped=True) is not None
            assert media.read_bytes()==b"active-demo-media"

            assert holder.stdin is not None
            holder.stdin.write("\n");holder.stdin.flush()
            assert holder.wait(timeout=10)==0
            cleared=client.post("/api/storage/clear-demo")
            assert cleared.status_code==200,cleared.text
            assert demo_db.get("items","active-demo-item",unscoped=True) is None
            assert not media.exists()

            # The endpoint must release its temporary target lease as well.
            restarted=create_app(tmp_path,runtime_mode="DEMO")
            with TestClient(restarted,base_url='http://127.0.0.1') as demo_client:
                demo_client.cookies.set("om_session",restarted.state.runtime.sessions.issue())
                assert demo_client.get("/api/health").status_code==200
    finally:
        if holder.poll() is None:
            holder.terminate();holder.wait(timeout=10)


def test_current_demo_can_clear_itself_and_releases_mode_on_shutdown(tmp_path):
    app=create_app(tmp_path,runtime_mode="DEMO")
    with TestClient(app,base_url='http://127.0.0.1') as client:
        client.cookies.set("om_session",app.state.runtime.sessions.issue())
        runtime=app.state.runtime
        runtime.db.save("items",{"name":"自清演示物品","type":"phone"},"self-demo-item")
        media=runtime.media/"event-images"/"self.jpg";media.write_bytes(b"self-demo-media")
        with pytest.raises(RuntimeModeBusy,match="DEMO 模式已有活动进程"):
            create_app(tmp_path,runtime_mode="DEMO")
        response=client.post("/api/storage/clear-demo")
        assert response.status_code==200,response.text
        assert runtime.mode_lease.held
        assert runtime.db.get("items","self-demo-item",unscoped=True) is None
        assert not media.exists()
    assert not app.state.runtime.mode_lease.held
    restarted=create_app(tmp_path,runtime_mode="DEMO")
    with TestClient(restarted,base_url='http://127.0.0.1') as client:
        client.cookies.set("om_session",restarted.state.runtime.sessions.issue())
        assert client.get("/api/health").status_code==200


def test_mode_lease_is_recoverable_after_abrupt_process_exit(tmp_path):
    holder=start_mode_lease_process(tmp_path,"TEST")
    try:
        with pytest.raises(RuntimeModeBusy):
            RuntimeModeLease(tmp_path,"TEST").acquire()
        holder.terminate()
        holder.wait(timeout=10)
        deadline=time.monotonic()+5
        recovered=None
        while time.monotonic()<deadline:
            try:
                recovered=RuntimeModeLease(tmp_path,"TEST").acquire();break
            except RuntimeModeBusy:
                time.sleep(.05)
        assert recovered is not None and recovered.held
        recovered.release()
    finally:
        if holder.poll() is None:
            holder.terminate();holder.wait(timeout=10)


def test_unstarted_app_holds_mode_until_explicit_close_and_raw_lease_finalizes(tmp_path):
    explicit=create_app(tmp_path/"explicit",runtime_mode="DEMO")
    assert explicit.state.runtime.mode_lease.held
    with pytest.raises(RuntimeModeBusy,match="DEMO 模式已有活动进程"):
        create_app(tmp_path/"explicit",runtime_mode="DEMO")
    explicit.state.runtime.mode_lease.close()

    second=create_app(tmp_path/"explicit",runtime_mode="DEMO")
    assert second.state.runtime.mode_lease.held
    second.state.runtime.mode_lease.close()

    def abandoned_lease_ref():
        lease=RuntimeModeLease(tmp_path/"finalizer","TEST").acquire()
        return weakref.ref(lease)
    lease_ref=abandoned_lease_ref()
    for _ in range(3):gc.collect()
    assert lease_ref() is None
    with RuntimeModeLease(tmp_path/"finalizer","TEST") as recovered:
        assert recovered.held


def test_factory_failure_releases_mode_lease(tmp_path,monkeypatch):
    from apps.api.app import firmware

    def fail_setup(*args,**kwargs):
        raise RuntimeError("injected firmware setup failure")

    monkeypatch.setattr(firmware,"setup",fail_setup)
    with pytest.raises(RuntimeError,match="injected firmware setup failure"):
        create_app(tmp_path/"factory-failure",runtime_mode="DEMO")
    with RuntimeModeLease(tmp_path/"factory-failure","DEMO") as recovered:
        assert recovered.held


def test_runtime_stop_joins_media_callbacks_outside_runtime_lock(tmp_path):
    app=create_app(tmp_path/"two-phase-stop",runtime_mode="TEST")
    runtime=app.state.runtime
    callback_acquired=threading.Event()

    class FakeEngine:
        def health(self):
            return {
                "source_session_id":"stop-session",
                "source_frame_sequence":7,
                "dropped_frames":0,
                "reconnects":0,
            }

        def stop(self):
            def callback():
                with runtime.lock:
                    callback_acquired.set()
            worker=threading.Thread(target=callback,name="fake-media-callback")
            worker.start()
            assert callback_acquired.wait(2),"media callback remained blocked by Runtime.stop"
            worker.join(timeout=2)

    engine=FakeEngine()
    runtime.db.save("source_sessions",{
        "camera_id":"camera-stop","runtime_mode":"TEST","source_type":"test_fixture",
        "is_simulated":True,"started_at":now_for_test(),"first_frame":0,"last_frame":7,
        "last_frame_at":now_for_test(),"status":"streaming","continuity_ok":True,
    },"stop-session")
    runtime.engines["camera-stop"]=engine
    try:
        assert runtime.stop("camera-stop") is True
        assert callback_acquired.is_set()
        assert "camera-stop" not in runtime.engines
        assert "camera-stop" not in runtime.stopping_engines
        session=runtime.db.get("source_sessions","stop-session",unscoped=True)
        assert session["status"]=="stopped"
        assert session["continuity_ok"] is False
    finally:
        app.state.firmware_service.shutdown()
        runtime.mode_lease.close()


def now_for_test():
    return datetime.now(timezone.utc).isoformat()


def test_current_demo_clear_rejects_active_or_closing_firmware_without_mutation(tmp_path):
    app=create_app(tmp_path/"firmware-clear",runtime_mode="DEMO")
    with TestClient(app,base_url='http://127.0.0.1') as client:
        client.cookies.set("om_session",app.state.runtime.sessions.issue())
        runtime=app.state.runtime
        firmware=app.state.firmware_service
        runtime.db.save("items",{"name":"保留到任务结束","type":"phone"},"guarded-item")
        media=runtime.media/"event-images"/"guarded.jpg"
        media.write_bytes(b"guarded-media")
        started=threading.Event();release=threading.Event()

        def slow_worker():
            started.set();release.wait(5)

        worker=firmware._start_background_worker(slow_worker,name="test-clear-worker")
        assert started.wait(2)
        blocked=client.post("/api/storage/clear-demo")
        assert blocked.status_code==409
        assert runtime.db.get("items","guarded-item",unscoped=True) is not None
        assert media.read_bytes()==b"guarded-media"
        assert firmware._shutdown_started is False
        release.set();worker.join(timeout=5)

        with firmware._worker_condition:
            firmware._shutdown_started=True
        try:
            closing=client.post("/api/storage/clear-demo")
            assert closing.status_code==409
            assert runtime.db.get("items","guarded-item",unscoped=True) is not None
            assert media.read_bytes()==b"guarded-media"
        finally:
            with firmware._worker_condition:
                firmware._shutdown_started=False

        cleared=client.post("/api/storage/clear-demo")
        assert cleared.status_code==200,cleared.text
        assert runtime.db.get("items","guarded-item",unscoped=True) is None
        assert not media.exists()


def test_real_search_requires_authoritative_persisted_esp32_attestation(tmp_path):
    with client_for(tmp_path/"esp32-search","REAL") as client:
        runtime=client.app.state.runtime
        firmware=client.app.state.firmware_service
        item=client.post("/api/items",json={"name":"手机","type":"phone"}).json()
        enrollment=firmware.create_enrollment("真实板候选","客厅")
        claim=firmware.claim(DeviceClaim(
            device_id="omcam-search",
            pairing_code=enrollment["pairing_code"],
            mac_address="02:00:00:00:00:55",
            firmware_version="0.1.0",
            ip_address="127.0.0.1",
            stream_url="http://127.0.0.1:8766/stream",
            capture_url="http://127.0.0.1:8766/capture",
            capabilities=["camera","mjpeg","capture"],
        ),"127.0.0.1")
        camera_id=claim["camera_id"]
        runtime.db.save("cameras",{
            "name":"ESP32 候选","room_name":"客厅","source_type":"esp32",
            "source":"http://127.0.0.1:8766/stream",
            "config":{"capture_url":"http://127.0.0.1:8766/capture","device_id":"omcam-search"},
            "enabled":False,"inference_fps":5,"save_clips":True,"runtime_mode":"REAL",
        },camera_id)
        runtime.db.save("events",{
            "event_id":"legacy-esp32","movement_session_id":"legacy","item_id":item["id"],
            "item_name":item["name"],"camera_id":camera_id,"room_name":"客厅",
            "event_type":"movement","runtime_mode":"REAL","source_type":"esp32_real",
            "is_simulated":False,"source_attestation_id":"forged-proof",
            "zone_name":"沙发","to_zone":"沙发","evidence_status":"confirmed",
            "final_status":"confirmed_placed","timestamp_start":"2026-01-01T00:00:00+00:00",
            "timestamp_end":"2026-01-01T00:00:02+00:00",
        },"legacy-esp32")
        runtime.db.save("item_current_state",{
            "item_id":item["id"],"current_camera":camera_id,"current_room":"客厅",
            "current_zone":"沙发","last_seen_at":"2026-01-01T00:00:02+00:00",
            "last_confirmed_placed_at":"2026-01-01T00:00:02+00:00",
            "evidence_event_id":"legacy-esp32","runtime_mode":"REAL","source_type":"esp32_real",
            "is_simulated":False,"source_session_id":"legacy-session",
            "source_attestation_id":"forged-proof","status":"confirmed_placed",
        },f"REAL:{item['id']}")

        def search_result():
            return client.post("/api/search",json={"query":"手机在哪里"}).json()["results"][0]

        initial=search_result()
        assert initial["last_confirmed"] is None
        assert initial["current_state"]["source_type"]=="esp32_unverified"
        assert initial["current_state"]["status"]=="last_seen"
        listed=client.get("/api/events").json()[0]
        detailed=client.get("/api/events/legacy-esp32").json()
        assert listed["source_type"]==detailed["source_type"]=="esp32_unverified"
        assert listed["evidence_status"]==detailed["evidence_status"]=="source_unverified"
        assert listed["final_status"]==detailed["final_status"]=="source_unverified"
        with firmware.connect() as connection:
            connection.execute(
                "UPDATE firmware_devices SET hardware_verified=1,verification_method='usb_serial_omready',"
                "serial_verified_at=?,source_attestation_id='authoritative-proof',"
                "attested_stream_url=stream_url,attested_capture_url=capture_url WHERE id='omcam-search'",
                (now_for_test(),),
            )
        # A legacy/event-supplied proof does not become trusted merely because
        # a real device record exists for the same camera.
        assert search_result()["last_confirmed"] is None
        runtime.db.save("events",{"source_attestation_id":"authoritative-proof"},"legacy-esp32")
        assert search_result()["last_confirmed"]["event_id"]=="legacy-esp32"
        assert client.get("/api/items/"+item["id"]+"/events").json()[0]["source_type"]=="esp32_real"

        # Endpoint drift cannot retain physical provenance without a fresh local
        # serial attestation for that exact stream/capture pair.
        with firmware.connect() as connection:
            connection.execute(
                "UPDATE firmware_devices SET stream_url='http://127.0.0.2:8766/stream' WHERE id='omcam-search'"
            )
        assert search_result()["last_confirmed"] is None
        assert client.get("/api/events").json()[0]["source_type"]=="esp32_unverified"


def test_current_demo_clear_rejects_concurrent_firmware_transaction(tmp_path,monkeypatch):
    app=create_app(tmp_path/"firmware-sync-clear",runtime_mode="DEMO")
    with TestClient(app,base_url='http://127.0.0.1') as client:
        client.cookies.set("om_session",app.state.runtime.sessions.issue())
        runtime=app.state.runtime
        firmware=app.state.firmware_service
        runtime.db.save("items",{"name":"并发写保护","type":"phone"},"sync-guarded-item")
        media=runtime.media/"event-images"/"sync-guarded.jpg"
        media.write_bytes(b"sync-guarded-media")
        original_connect=firmware.connect
        entered=threading.Event();release=threading.Event();errors=[]

        @contextmanager
        def blocking_connect():
            if threading.current_thread().name=="firmware-sync-writer":
                entered.set();release.wait(5)
            with original_connect() as connection:
                yield connection

        monkeypatch.setattr(firmware,"connect",blocking_connect)

        def write_enrollment():
            try:firmware.create_enrollment("并发设备","客厅")
            except Exception as exc:errors.append(exc)

        writer=threading.Thread(target=write_enrollment,name="firmware-sync-writer")
        writer.start();assert entered.wait(2)
        blocked=client.post("/api/storage/clear-demo")
        assert blocked.status_code==409
        assert runtime.db.get("items","sync-guarded-item",unscoped=True) is not None
        assert media.read_bytes()==b"sync-guarded-media"
        release.set();writer.join(timeout=5)
        assert not writer.is_alive() and not errors
        cleared=client.post("/api/storage/clear-demo")
        assert cleared.status_code==200,cleared.text
        assert runtime.db.get("items","sync-guarded-item",unscoped=True) is None
        assert not media.exists()


def test_event_audit_cleanup_default_is_read_only(tmp_path):
    with client_for(tmp_path,"TEST") as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        runtime.retention.set_policy("FORENSIC")
        event=record_event(client,item,camera,1)
        record_event(client,item,camera,2)
        if runtime.retention._cleanup_thread:runtime.retention._cleanup_thread.join(10)
        database_bytes=runtime.db.path.read_bytes()
        media_path=runtime.event_service.media_path(event["screenshot_path"]);media_bytes=media_path.read_bytes()
        output=tmp_path/"outputs";output.mkdir()
        script=Path(__file__).resolve().parents[3]/"scripts"/"event_audit.py"
        subprocess.run([sys.executable,str(script),"--database",str(runtime.db.path),"--cleanup","--json",str(output/"audit.json"),"--csv",str(output/"audit.csv"),"--result",str(output/"dry.json")],check=True)
        plan=__import__('json').loads((output/"dry.json").read_text(encoding="utf-8"))
        assert plan["dry_run"] is True and plan["delete_events"]==1
        assert runtime.db.path.read_bytes()==database_bytes
        assert media_path.read_bytes()==media_bytes


def test_legacy_migration_copies_only_user_configuration_not_events(tmp_path):
    legacy=Database(tmp_path/"database"/"object_memory.sqlite3","REAL")
    legacy.save("cameras",{"name":"用户摄像头","source_type":"webcam","source":"0","config":{},"enabled":True,"runtime_mode":"REAL"},"user-camera")
    legacy.save("cameras",{"name":"旧屏幕采集","source_type":"screen","source":"screen","config":{"authorized":True,"capture_grant":"stale-secret","region":{"left":0,"top":0,"width":320,"height":240}},"enabled":True,"runtime_mode":"REAL"},"legacy-screen")
    legacy.save("cameras",{"name":"旧演示回放","source_type":"video","source":"demo.avi","config":{"simulated":True},"enabled":True,"runtime_mode":"REAL"},"demo-camera")
    legacy.save("items",{"name":"用户钥匙","type":"keys","aliases":[]},"user-item")
    legacy.save("items",{"name":"我的手机","type":"phone","aliases":[]},"demo-phone")
    legacy.save("item_reference_images",{"item_id":"user-item","path":"/media/registered-items/reference.jpg","sha256":"a"*64,"features":{}},"reference")
    legacy.save("events",{"event_id":"polluted","item_id":"user-item","camera_id":"user-camera","runtime_mode":"REAL","source_type":"opencv_camera","is_simulated":False,"idempotency_key":"polluted"},"polluted")
    legacy_events=legacy.count("events",unscoped=True)
    runtime=create_app(tmp_path,runtime_mode="REAL").state.runtime
    try:
        assert runtime.legacy_migration["legacy_events_imported"] is False
        assert runtime.db.get("cameras","user-camera",unscoped=True) is not None
        assert runtime.db.get("cameras","demo-camera",unscoped=True) is None
        migrated_screen=runtime.db.get("cameras","legacy-screen",unscoped=True)
        assert migrated_screen and migrated_screen["enabled"] is False
        assert migrated_screen["config"]["authorized"] is False
        assert "capture_grant" not in migrated_screen["config"]
        assert runtime.db.get("items","user-item",unscoped=True) is not None
        assert runtime.db.get("items","demo-phone",unscoped=True) is None
        assert runtime.db.get("item_reference_images","reference",unscoped=True) is not None
        assert runtime.db.count("events",unscoped=True)==0
        assert legacy.count("events",unscoped=True)==legacy_events
    finally:
        runtime.mode_lease.close()


def test_balanced_expiry_and_storage_pressure_protect_current_evidence(tmp_path):
    with client_for(tmp_path,"TEST") as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        runtime.retention.set_policy("FORENSIC")
        old=record_event(client,item,camera,1)
        current=record_event(client,item,camera,2)
        if runtime.retention._cleanup_thread:runtime.retention._cleanup_thread.join(10)
        runtime.retention.set_policy("BALANCED")
        preview=runtime.retention.preview()
        old_media={row["id"] for row in runtime.db.list("event_media",{"event_id":old["event_id"]},unscoped=True)}
        current_media={row["id"] for row in runtime.db.list("event_media",{"event_id":current["event_id"]},unscoped=True)}
        assert old_media.issubset(set(preview["media_ids"]))
        assert current_media.isdisjoint(set(preview["media_ids"]))
        runtime.retention.max_storage_mb=0
        pressure=runtime.retention._enforce_storage_limit()
        assert pressure["deleted_images"]==1 and pressure["deleted_clips"]==1
        assert runtime.db.get("events",current["id"])["screenshot_path"]
        expired=runtime.db.get("events",old["id"])
        assert expired["evidence_status"]=="media_expired" and expired["screenshot_path"] is None and expired["clip_path"] is None
        assert expired["before_screenshot"] is None and expired["after_screenshot"] is None


def test_evidence_gate_rejects_frames_or_times_outside_source_session(tmp_path):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        event=complete_event(client,item,camera)
        event["source_frame_end"]=999
        assert record_event(client,item,camera,event=event) is False
        event["source_frame_end"]=19
        event["source_timestamp_end"]="2030-01-01T00:00:00+00:00"
        assert record_event(client,item,camera,event=event) is False
        assert runtime.db.count("events")==0


def test_auditor_only_promotes_complete_real_evidence_contract(tmp_path):
    with client_for(tmp_path,"REAL") as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        event=complete_event(client,item,camera)
        event.update({"runtime_mode":"REAL","source_type":"opencv_camera","is_simulated":False,"source_continuity_ok":True,"reconnect_epoch_changed":False})
        start=datetime.fromisoformat(event["source_timestamp_start"]);end=datetime.fromisoformat(event["source_timestamp_end"])
        runtime.db.save("source_sessions",{"camera_id":camera["id"],"runtime_mode":"REAL","source_type":"opencv_camera","is_simulated":False,"started_at":(start-timedelta(seconds=1)).isoformat(),"first_frame":0,"last_frame":30,"last_frame_at":(end+timedelta(seconds=1)).isoformat(),"status":"streaming","continuity_ok":True},event["source_session_id"])
        saved=record_event(client,item,camera,event=event)
        assert saved and saved["stable_before"] is True and saved["stable_after"] is True
        rows,_=audit_database(runtime.db.path)
        audited=next(row for row in rows if row["event_id"]==saved["event_id"])
        assert audited["classification"]=="real_verified" and all(audited["provenance_contract"].values())
        runtime.db.save("events",{"stable_after":False},saved["id"])
        rows,_=audit_database(runtime.db.path)
        audited=next(row for row in rows if row["event_id"]==saved["event_id"])
        assert audited["classification"]=="unknown"
        assert audited["provenance_contract"]["stable_transition"] is False


def test_auditor_does_not_infer_unknown_source_or_trust_corrupt_binding(tmp_path):
    with client_for(tmp_path,"REAL") as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        event=complete_event(client,item,camera)
        event.update({"runtime_mode":"REAL","source_type":"opencv_camera","is_simulated":False})
        start=datetime.fromisoformat(event["source_timestamp_start"]);end=datetime.fromisoformat(event["source_timestamp_end"])
        runtime.db.save("source_sessions",{"camera_id":camera["id"],"runtime_mode":"REAL","source_type":"opencv_camera","is_simulated":False,"started_at":(start-timedelta(seconds=1)).isoformat(),"first_frame":0,"last_frame":30,"last_frame_at":(end+timedelta(seconds=1)).isoformat(),"status":"streaming","continuity_ok":True},event["source_session_id"])
        saved=record_event(client,item,camera,event=event)
        rows,_=audit_database(runtime.db.path)
        assert next(row for row in rows if row["event_id"]==saved["event_id"])["classification"]=="real_verified"
        image=next(row for row in runtime.db.list("event_media",{"event_id":saved["event_id"]},unscoped=True) if row["kind"]=="image")
        metadata=dict(image["metadata"]);metadata["write_binding"]={**metadata["write_binding"],"capture_origin":"payload_claim"}
        runtime.db.save("event_media",{"metadata":metadata},image["id"])
        rows,_=audit_database(runtime.db.path)
        audited=next(row for row in rows if row["event_id"]==saved["event_id"])
        assert audited["classification"]=="unknown"
        assert audited["media"]["image"]["binding_valid"] is False
        runtime.db.save("events",{"source_type":"unknown"},saved["id"])
        rows,_=audit_database(runtime.db.path)
        audited=next(row for row in rows if row["event_id"]==saved["event_id"])
        assert audited["source_type"]=="unknown" and audited["classification"]=="unknown"


def test_auditor_requires_existing_qualified_real_manual_reference(tmp_path):
    with client_for(tmp_path,"REAL") as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        event=complete_event(client,item,camera)
        event.update({"runtime_mode":"REAL","source_type":"opencv_camera","is_simulated":False})
        start=datetime.fromisoformat(event["source_timestamp_start"]);end=datetime.fromisoformat(event["source_timestamp_end"])
        runtime.db.save("source_sessions",{"camera_id":camera["id"],"runtime_mode":"REAL","source_type":"opencv_camera","is_simulated":False,"started_at":(start-timedelta(seconds=1)).isoformat(),"first_frame":0,"last_frame":30,"last_frame_at":(end+timedelta(seconds=1)).isoformat(),"status":"streaming","continuity_ok":True},event["source_session_id"])
        original=record_event(client,item,camera,event=event)
        response=client.post(f"/api/events/{original['id']}/correct",json={"zone_name":"抽屉","room_name":"书房","notes":"本人核实"})
        assert response.status_code==200,response.text
        correction=response.json()

        rows,_=audit_database(runtime.db.path)
        audited=next(row for row in rows if row["event_id"]==correction["event_id"])
        assert audited["classification"]=="real_verified"
        assert audited["provenance_contract"]["manual_reference"] is True

        original_metadata={}
        for media in runtime.db.list("event_media",{"event_id":correction["event_id"]},unscoped=True):
            original_metadata[media["id"]]=media["metadata"]
            metadata=dict(media["metadata"]);binding=dict(metadata["write_binding"])
            binding["manual_reference_event_id"]="missing-event"
            unsigned={key:value for key,value in binding.items() if key!="binding_sha256"}
            binding["binding_sha256"]=hashlib.sha256(json.dumps(unsigned,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode("utf-8")).hexdigest()
            metadata["write_binding"]=binding
            runtime.db.save("event_media",{"metadata":metadata},media["id"])
        rows,_=audit_database(runtime.db.path)
        missing=next(row for row in rows if row["event_id"]==correction["event_id"])
        assert missing["classification"]=="unknown" and "no_evidence" in missing["flags"]
        assert missing["provenance_contract"]["manual_reference"] is False

        for media_id,metadata in original_metadata.items():
            runtime.db.save("event_media",{"metadata":metadata},media_id)
        runtime.db.save("events",{"stable_after":False},original["id"])
        rows,_=audit_database(runtime.db.path)
        unqualified=next(row for row in rows if row["event_id"]==correction["event_id"])
        assert unqualified["classification"]=="unknown" and "no_evidence" in unqualified["flags"]
        assert unqualified["provenance_contract"]["manual_reference"] is False


def test_firmware_service_is_scoped_per_real_and_demo_app(tmp_path):
    real_app=create_app(tmp_path,runtime_mode="REAL")
    demo_app=create_app(tmp_path,runtime_mode="DEMO")
    with TestClient(real_app,base_url='http://127.0.0.1') as real_client,TestClient(demo_app,base_url='http://127.0.0.1') as demo_client:
        real_client.cookies.set("om_session",real_app.state.runtime.sessions.issue())
        demo_client.cookies.set("om_session",demo_app.state.runtime.sessions.issue())
        real_response=real_client.post("/api/device-enrollment/create",json={"device_name":"真实板候选","room_name":"客厅"})
        demo_response=demo_client.post("/api/device-enrollment/create",json={"device_name":"虚拟板","room_name":"演示间"})
        assert real_response.status_code==200 and demo_response.status_code==200
        assert real_app.state.firmware_service is not demo_app.state.firmware_service
        assert real_app.state.firmware_service.db_path==tmp_path/"database"/"objectmemory.sqlite"
        assert demo_app.state.firmware_service.db_path==tmp_path/"database"/"objectmemory-demo.sqlite"
        with sqlite3.connect(real_app.state.firmware_service.db_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM device_enrollments").fetchone()[0]==1
            assert connection.execute("SELECT device_name FROM device_enrollments").fetchone()[0]=="真实板候选"
        with sqlite3.connect(demo_app.state.firmware_service.db_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM device_enrollments").fetchone()[0]==1
            assert connection.execute("SELECT device_name FROM device_enrollments").fetchone()[0]=="虚拟板"


def test_minimal_and_pressure_protect_manual_correction_and_shared_current_media(tmp_path):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        runtime.retention.set_policy("FORENSIC")
        manual=record_event(client,item,camera,1)
        old=record_event(client,item,camera,2)
        current=record_event(client,item,camera,3)
        if runtime.retention._cleanup_thread:runtime.retention._cleanup_thread.join(10)
        runtime.db.save("events",{"manually_corrected":True,"created_by":"user"},manual["id"])
        shared_path=old["screenshot_path"]
        current_image=next(row for row in runtime.db.list("event_media",{"event_id":current["event_id"]},unscoped=True) if row["kind"]=="image")
        runtime.db.save("event_media",{"path":shared_path},current_image["id"])
        runtime.db.save("events",{"screenshot_path":shared_path,"before_screenshot":shared_path,"after_screenshot":shared_path},current["id"])
        shared_file=runtime.event_service.media_path(shared_path)
        runtime.retention.set_policy("MINIMAL")
        assert manual["id"] not in runtime.retention.preview()["event_ids"]
        runtime.retention.max_storage_mb=0
        runtime.retention._enforce_storage_limit()
        assert shared_file and shared_file.exists()
        protected=runtime.db.get("events",current["id"])
        assert protected["screenshot_path"]==shared_path and protected["before_screenshot"]==shared_path and protected["after_screenshot"]==shared_path
        assert runtime.db.get("events",manual["id"])["screenshot_path"]


def test_orphan_scan_honors_registry_direct_references_and_write_grace(tmp_path):
    with client_for(tmp_path) as client:
        runtime=client.app.state.runtime
        image_dir=runtime.media/"event-images"
        registered=image_dir/"registered-only.jpg";registered.write_bytes(b"registered")
        runtime.db.save("event_media",{"event_id":"registry-only","path":"/media/event-images/registered-only.jpg","kind":"image","status":"active","runtime_mode":"TEST"},"registry-only")
        recent=image_dir/"recent-unregistered.jpg";recent.write_bytes(b"recent")
        assert "/media/event-images/registered-only.jpg" not in runtime.retention.preview()["orphan_media"]
        assert "/media/event-images/recent-unregistered.jpg" not in runtime.retention.preview()["orphan_media"]
        old=(datetime.now(timezone.utc)-timedelta(minutes=10)).timestamp();os.utime(recent,(old,old))
        assert "/media/event-images/recent-unregistered.jpg" in runtime.retention.preview()["orphan_media"]


def test_semantic_idempotency_ignores_regenerated_movement_id(tmp_path):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        runtime.retention.set_policy("FORENSIC")
        original=complete_event(client,item,camera,1)
        first=record_event(client,item,camera,1,original)
        before=np.full((48,64,3),21,dtype=np.uint8)
        after=np.full((48,64,3),61,dtype=np.uint8)
        assert first["pickup_evidence"]["raw_frame_sha256"]==runtime.event_service._frame_sha256(before)
        assert first["placement_evidence"]["raw_frame_sha256"]==runtime.event_service._frame_sha256(after)
        assert first["idempotency_key"]==runtime.event_service._idempotency(first)
        changed_hash={**first,"placement_evidence":{**first["placement_evidence"],"raw_frame_sha256":"0"*64}}
        assert runtime.event_service._idempotency(changed_hash)!=first["idempotency_key"]
        regenerated={**original,"id":"regenerated-request","event_id":"regenerated-request","movement_session_id":"regenerated-episode"}
        second=record_event(client,item,camera,1,regenerated)
        conflicting={**original,"id":"conflicting-request","event_id":"conflicting-request","movement_session_id":"conflicting-episode"}
        conflicting_after=np.full_like(after,62)
        with pytest.raises(EvidenceRejected,match="像素哈希"):
            runtime.event_service.record(conflicting,conflicting_after,[before,conflicting_after,conflicting_after])
        if runtime.retention._cleanup_thread:runtime.retention._cleanup_thread.join(10)
        assert first["id"]==second["id"]
        assert runtime.db.count("events",unscoped=True)==1
        assert runtime.db.count("event_media",unscoped=True)==2
        assert len(list((runtime.media/"event-images").glob("*")))==1
        assert len(list((runtime.media/"event-clips").glob("*")))==1


def test_out_of_order_event_does_not_rewind_current_state(tmp_path):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        runtime.retention.set_policy("FORENSIC")
        newer=record_event(client,item,camera,2)
        older=record_event(client,item,camera,1)
        if runtime.retention._cleanup_thread:runtime.retention._cleanup_thread.join(10)
        assert datetime.fromisoformat(older["timestamp_end"])<datetime.fromisoformat(newer["timestamp_end"])
        state=runtime.db.get("item_current_state",f"TEST:{item['id']}")
        assert state["evidence_event_id"]==newer["event_id"]
        assert state["last_seen_at"]==newer["timestamp_end"]
        result=client.get(f"/api/items/{item['id']}/last-location").json()
        assert result["last_confirmed"]["event_id"]==newer["event_id"]


def test_server_owned_confirmation_fields_override_callback_payload(tmp_path):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        event={**complete_event(client,item,camera,1),
            "evidence_status":"candidate","final_status":"occluded",
            "pinned":True,"ingested_at":"1999-01-01T00:00:00+00:00",
        }
        saved=record_event(client,item,camera,1,event)
        assert saved["event_type"]=="movement"
        assert saved["evidence_status"]=="confirmed"
        assert saved["final_status"]=="confirmed_placed"
        assert saved["pinned"] is False
        assert saved["ingested_at"]!="1999-01-01T00:00:00+00:00"


def test_delayed_observation_preserves_newer_confirmed_state_and_evidence(tmp_path):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        runtime.retention.set_policy("FORENSIC")
        confirmed=record_event(client,item,camera,2)
        if runtime.retention._cleanup_thread:runtime.retention._cleanup_thread.join(10)
        before=runtime.db.get("item_current_state",f"TEST:{item['id']}")
        observed=runtime.event_service.observe({
            "item_id":item["id"],"camera_id":camera["id"],
            "source_session_id":confirmed["source_session_id"],
            "last_seen":"2026-01-01T00:00:01+00:00","zone_name":"过期区域",
            "center":{"x":1,"y":1},"confidence":0.99,"state":"last_seen",
        }, observation_verified=True)
        assert observed["last_seen_at"]==before["last_seen_at"]
        assert observed["current_zone"]==before["current_zone"]
        assert observed["evidence_event_id"]==confirmed["event_id"]
        assert observed["last_confirmed_placed_at"]==confirmed["timestamp_end"]
        assert observed["status"]=="confirmed_placed"


def test_newer_observation_preserves_last_confirmed_evidence_link(tmp_path):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        confirmed=record_event(client,item,camera,1)
        if runtime.retention._cleanup_thread:runtime.retention._cleanup_thread.join(10)
        observed_at=(datetime.fromisoformat(confirmed["timestamp_end"])+timedelta(seconds=2)).isoformat()
        observed=runtime.event_service.observe({
            "item_id":item["id"],"camera_id":camera["id"],
            "source_session_id":confirmed["source_session_id"],
            "last_seen":observed_at,"zone_name":confirmed["to_zone"],
            "center":{"x":0.8,"y":0.7},"confidence":0.97,"state":"last_seen",
        }, observation_verified=True)
        # A new coordinate is a last sighting, not another confirmed placement
        # merely because the human-readable zone name has not changed.
        assert observed["status"]=="last_seen"
        assert observed["evidence_event_id"]==confirmed["event_id"]
        assert observed["last_confirmed_placed_at"]==confirmed["timestamp_end"]
        assert observed["last_confirmed_placement"]["event_id"]==confirmed["event_id"]
        assert runtime.retention.preview()["protected_current_event_ids"]==[confirmed["event_id"]]


def test_delayed_event_attaches_confirmation_without_rewinding_newer_observation(tmp_path):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        candidate=complete_event(client,item,camera,1)
        observed_at=(datetime.fromisoformat(candidate["source_timestamp_end"])+timedelta(seconds=2)).isoformat()
        newer=runtime.event_service.observe({
            "item_id":item["id"],"camera_id":camera["id"],
            "source_session_id":candidate["source_session_id"],
            "last_seen":observed_at,"zone_name":"随后观察区域",
            "center":{"x":0.91,"y":0.82},"confidence":0.96,"state":"last_seen",
        }, observation_verified=True)
        assert newer["evidence_event_id"] is None
        confirmed=record_event(client,item,camera,1,candidate)
        current=runtime.db.get("item_current_state",f"TEST:{item['id']}")
        assert current["last_seen_at"]==observed_at
        assert current["current_zone"]=="随后观察区域"
        assert current["status"]=="last_seen"
        assert current["evidence_event_id"]==confirmed["event_id"]
        assert current["last_confirmed_placed_at"]==confirmed["timestamp_end"]


def test_cleanup_rechecks_pin_after_plan(tmp_path):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        runtime.retention.set_policy("FORENSIC")
        for index in (1,2,3):record_event(client,item,camera,index)
        if runtime.retention._cleanup_thread:runtime.retention._cleanup_thread.join(10)
        runtime.retention.set_policy("MINIMAL")
        original_plan=runtime.retention._plan;marker={}
        def plan_then_pin():
            plan=original_plan()
            if not marker and plan["event_ids"]:
                marker["victim"]=plan["event_ids"][0]
                runtime.db.save("events",{"pinned":True},marker["victim"])
            return plan
        runtime.retention._plan=plan_then_pin
        try:runtime.retention.cleanup(trigger="pin-race")
        finally:runtime.retention._plan=original_plan
        victim=runtime.db.get("events",marker["victim"],unscoped=True)
        assert victim and victim["pinned"] is True
        assert runtime.db.count("event_media",{"event_id":victim["event_id"]},unscoped=True)==2


def test_manual_correction_chain_flattens_and_root_survives_minimal(tmp_path):
    with client_for(tmp_path,"REAL") as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        runtime.retention.set_policy("FORENSIC")
        original=record_real_event(client,item,camera,1)
        assert client.post(f"/api/events/{original['id']}/pin",json={"pinned":True}).status_code==200
        first=client.post(f"/api/events/{original['id']}/correct",json={"zone_name":"抽屉","notes":"第一次纠正"})
        assert first.status_code==200,first.text
        first_event=first.json()
        assert first_event["pinned"] is False
        assert first_event["evidence_status"]=="confirmed" and first_event["final_status"]=="confirmed_placed"
        assert datetime.fromisoformat(first_event["ingested_at"])>datetime.fromisoformat(original["ingested_at"])
        second=client.post(f"/api/events/{first.json()['id']}/correct",json={"zone_name":"柜子","notes":"第二次纠正"})
        assert second.status_code==200,second.text
        correction=second.json()
        assert correction["manual_reference_event_id"]==original["event_id"]
        for row in runtime.db.list("event_media",{"event_id":correction["event_id"]},unscoped=True):
            assert row["metadata"]["write_binding"]["manual_reference_event_id"]==original["event_id"]
        for index in (2,3,4):record_real_event(client,item,camera,index)
        if runtime.retention._cleanup_thread:runtime.retention._cleanup_thread.join(10)
        runtime.retention.set_policy("MINIMAL");runtime.retention.cleanup(trigger="manual-dependency")
        assert runtime.db.get("events",original["id"],unscoped=True) is not None
        rows,_=audit_database(runtime.db.path)
        audited=next(row for row in rows if row["event_id"]==correction["event_id"])
        assert audited["classification"]=="real_verified"
        assert audited["provenance_contract"]["manual_reference"] is True
        refused=client.delete(f"/api/events/{original['id']}")
        assert refused.status_code==409


@pytest.mark.parametrize("entrypoint", ["api_advancing_clock", "internal_stale_alias"])
def test_manual_correction_has_one_canonical_evidence_time(tmp_path, monkeypatch, entrypoint):
    """Real API/SQLite contract with generated media; not physical-camera evidence."""
    import apps.api.app.main as api_main

    with client_for(tmp_path, "REAL") as client:
        item, camera = add_item_camera(client)
        runtime = client.app.state.runtime
        original = record_real_event(client, item, camera)
        if entrypoint == "api_advancing_clock":
            ticks = []

            def advancing_now():
                value = datetime(2026, 9, 5, tzinfo=timezone.utc) + timedelta(milliseconds=len(ticks))
                ticks.append(value)
                return value.isoformat()

            monkeypatch.setattr(api_main, "now", advancing_now)
            response = client.post(f"/api/events/{original['id']}/correct", json={"zone_name": "抽屉"})
            assert response.status_code == 200, response.text
            correction = response.json()
            assert correction["source_timestamp_start"] == correction["source_timestamp_end"]
        else:
            correction = runtime.event_service.record_manual_correction({
                **original,
                "id": "manual-clock-correction", "event_id": "manual-clock-correction",
                "movement_session_id": "manual-clock-episode", "event_type": "manual_correction",
                "manual_reference_event_id": original["event_id"], "manually_corrected": True,
                "from_zone": original["to_zone"], "to_zone": "抽屉", "zone_name": "抽屉",
                "timestamp_start": "2026-09-05T09:44:12.150493+00:00",
                "timestamp_end": "2026-09-05T09:44:12.150493+00:00",
                "source_timestamp_start": "2026-09-05T09:44:12.151493+00:00",
                "source_timestamp_end": "2026-09-05T09:44:12.151493+00:00",
            })

        assert correction["event_type"] == "manual_correction"
        assert correction["manually_corrected"] is True
        assert correction["created_by"] == "user"
        assert correction["human_review_status"] == "human_corrected"
        for alias, canonical in (("timestamp_start", "source_timestamp_start"),
                                 ("started_at", "source_timestamp_start"),
                                 ("timestamp_end", "source_timestamp_end"),
                                 ("ended_at", "source_timestamp_end")):
            assert correction[alias] == correction[canonical]
        media = runtime.db.list("event_media", {"event_id": correction["event_id"]}, unscoped=True)
        assert len(media) == 2
        for asset in media:
            metadata = asset["metadata"]
            assert metadata["event_time_start"] == correction["source_timestamp_start"]
            assert metadata["event_time_end"] == correction["source_timestamp_end"]
            assert metadata["write_binding"]["source_timestamp_start"] == metadata["event_time_start"]
            assert metadata["write_binding"]["source_timestamp_end"] == metadata["event_time_end"]
        rows, _ = audit_database(runtime.db.path)
        audited = next(row for row in rows if row["event_id"] == correction["event_id"])
        assert audited["classification"] == "real_verified"
        assert audited["provenance_contract"]["manual_reference"] is True


def test_storage_pressure_reclaims_old_orphan_without_database(tmp_path):
    data=tmp_path/"data";db=Database(data/"database"/"objectmemory.sqlite","REAL")
    from apps.api.app.retention import RetentionService
    retention=RetentionService(db,data,data/"real-media",max_storage_mb=50)
    retention.project_root=tmp_path/"empty-project"
    orphan=data/"demo-media"/"event-clips"/"orphan.mp4";orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"x")
    with orphan.open("r+b") as stream:stream.truncate(51*1024*1024)
    old=(datetime.now(timezone.utc)-timedelta(minutes=10)).timestamp();os.utime(orphan,(old,old))
    result=retention._enforce_storage_limit()
    assert not orphan.exists()
    assert result["deleted_clips"]==1
    assert result["remaining_bytes"]<=50*1024*1024


def test_delete_current_event_clears_state_evidence(tmp_path):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        event=record_event(client,item,camera,1)
        if runtime.retention._cleanup_thread:runtime.retention._cleanup_thread.join(10)
        response=client.delete(f"/api/events/{event['id']}")
        assert response.status_code==200,response.text
        state=runtime.db.get("item_current_state",f"TEST:{item['id']}",unscoped=True)
        assert state and state["evidence_event_id"] is None
        assert state["last_confirmed_placed_at"] is None and state["status"]=="last_seen"
        result=client.get(f"/api/items/{item['id']}/last-location").json()
        assert result["last_confirmed"] is None
        assert result["evidence"]["evidence_status"]=="observation_only"


def test_item_and_camera_delete_refuse_history_then_remove_children(tmp_path):
    with client_for(tmp_path) as client:
        item,camera=add_item_camera(client);runtime=client.app.state.runtime
        event=record_event(client,item,camera,1)
        if runtime.retention._cleanup_thread:runtime.retention._cleanup_thread.join(10)
        assert client.delete(f"/api/items/{item['id']}").status_code==409
        assert client.delete(f"/api/cameras/{camera['id']}").status_code==409
        assert runtime.db.get("items",item["id"],unscoped=True)
        assert runtime.db.get("cameras",camera["id"],unscoped=True)
        assert client.delete(f"/api/events/{event['id']}").status_code==200
        assert client.delete(f"/api/items/{item['id']}").status_code==200
        assert runtime.db.count("item_current_state",{"item_id":item["id"]},unscoped=True)==0
        assert runtime.db.count("tracks",{"item_id":item["id"]},unscoped=True)==0
        assert runtime.db.count("companions",{"item_id":item["id"]},unscoped=True)==0
        assert client.delete(f"/api/cameras/{camera['id']}").status_code==200
        assert runtime.db.count("source_sessions",{"camera_id":camera["id"]},unscoped=True)==0
        assert runtime.db.count("zones",{"camera_id":camera["id"]},unscoped=True)==0
        assert runtime.db.count("events",unscoped=True)==0
        assert runtime.db.count("event_media",unscoped=True)==0


def test_runtime_firmware_retention_leaves_shared_archives_to_locked_builder(tmp_path):
    with client_for(tmp_path/"data") as client:
        retention=client.app.state.runtime.retention
        project=tmp_path/"repo";retention.project_root=project;retention.manage_project_firmware=True
        public=project/"artifacts"/"firmware";builds=public/"builds"
        current=project/"firmware"/"esp32cam"/"build"
        current.mkdir(parents=True);(current/"firmware.bin").write_bytes(b"current")
        public.mkdir(parents=True);(public/"manifest.json").write_bytes(b"manifest")
        old_success=builds/"old";new_success=builds/"new"
        young_in_progress=builds/"young-no-manifest";stale_invalid=builds/"stale-invalid"
        for folder in (old_success,new_success,young_in_progress,stale_invalid):folder.mkdir(parents=True)
        for folder in (old_success,new_success):(folder/"manifest.json").write_text(json.dumps({"compile_passed":True}),encoding="utf-8")
        now_stamp=datetime.now(timezone.utc).timestamp()
        os.utime(old_success,(now_stamp-300,now_stamp-300));os.utime(new_success,(now_stamp-100,now_stamp-100));os.utime(stale_invalid,(now_stamp-7200,now_stamp-7200))
        lock=public/".build.lock";lock.write_text("locked",encoding="utf-8")
        retention._trim_firmware_builds()
        assert all(folder.exists() for folder in (old_success,new_success,young_in_progress,stale_invalid))
        lock.unlink();retention._trim_firmware_builds()
        assert all(folder.exists() for folder in (old_success,new_success,young_in_progress,stale_invalid))
        expected=sum(path.stat().st_size for root in (retention.data_root/"firmware-builds",public,current) if root.exists() for path in root.rglob("*") if path.is_file())
        assert retention.status()["firmware_artifacts_size"]==expected


def test_pressure_latest_two_ignores_newer_unconfirmed_movements(tmp_path):
    with client_for(tmp_path,"REAL") as client:
        runtime=client.app.state.runtime
        item,camera=add_item_camera(client)
        runtime.retention.set_policy("FORENSIC")
        confirmed=[record_real_event(client,item,camera,index) for index in (1,2,3)]
        if runtime.retention._cleanup_thread:
            runtime.retention._cleanup_thread.join(10)

        newest=confirmed[-1]
        invalid_rows=[]
        for suffix,evidence_status,final_status in (
            ("candidate","candidate","confirmed_placed"),
            ("occluded","confirmed","occluded"),
        ):
            row={
                **newest,
                "id":f"newer-{suffix}",
                "event_id":f"newer-{suffix}",
                "movement_session_id":f"newer-{suffix}-episode",
                "idempotency_key":f"newer-{suffix}-idempotency",
                "source_window_key":f"newer-{suffix}-source-window",
                "timestamp_start":f"2027-01-01T00:00:0{len(invalid_rows)}+00:00",
                "timestamp_end":f"2027-01-01T00:00:1{len(invalid_rows)}+00:00",
                "source_timestamp_start":f"2027-01-01T00:00:0{len(invalid_rows)}+00:00",
                "source_timestamp_end":f"2027-01-01T00:00:1{len(invalid_rows)}+00:00",
                "evidence_status":evidence_status,
                "final_status":final_status,
                "screenshot_path":None,
                "before_screenshot":None,
                "after_screenshot":None,
                "clip_path":None,
                "screenshot_sha256":None,
                "clip_sha256":None,
            }
            runtime.db.save("events",row,row["id"])
            invalid_rows.append(row)

        runtime.retention.set_policy("MINIMAL")
        planned=set(runtime.retention.preview()["event_ids"])
        assert confirmed[0]["id"] in planned
        assert {confirmed[1]["id"],confirmed[2]["id"]}.isdisjoint(planned)
        assert {row["id"] for row in invalid_rows}.issubset(planned)

        protected_media=[]
        for event in confirmed[1:]:
            protected_media.extend(runtime.db.list("event_media",{"event_id":event["event_id"]},unscoped=True))
        old_media=runtime.db.list("event_media",{"event_id":confirmed[0]["event_id"]},unscoped=True)
        assert len(protected_media)==4 and len(old_media)==2
        assert all(runtime.event_service.media_path(row["path"]).exists() for row in [*protected_media,*old_media])

        runtime.retention._purge_mode_media("REAL",10**9)
        assert runtime.db.count("event_media",{"event_id":confirmed[0]["event_id"]},unscoped=True)==0
        assert all(not runtime.event_service.media_path(row["path"]).exists() for row in old_media)
        for row in protected_media:
            assert runtime.db.get("event_media",row["id"],unscoped=True)["status"]=="active"
            assert runtime.event_service.media_path(row["path"]).exists()


def test_balanced_and_forensic_keep_confirmed_history_after_media_expiry(tmp_path):
    with client_for(tmp_path) as client:
        runtime=client.app.state.runtime
        item,camera=add_item_camera(client)
        event=record_event(client,item,camera,1)
        if runtime.retention._cleanup_thread:
            runtime.retention._cleanup_thread.join(10)
        runtime.db.save("events",{"evidence_status":"media_expired"},event["id"])
        for policy in ("BALANCED","FORENSIC"):
            runtime.retention.set_policy(policy)
            assert event["id"] not in set(runtime.retention.preview()["event_ids"])


def _make_test_directory_link(link:Path,target:Path) -> None:
    try:
        link.symlink_to(target,target_is_directory=True)
        return
    except (NotImplementedError,OSError) as symlink_error:
        if os.name=="nt":
            result=subprocess.run(
                ["cmd.exe","/d","/c","mklink","/J",str(link),str(target)],
                capture_output=True,check=False,
            )
            if result.returncode==0:
                return
        pytest.skip(f"此平台不能创建符号链接或目录联接：{symlink_error}")


def test_run_log_trim_never_follows_internal_or_root_symlinks(tmp_path):
    from apps.api.app.retention import RetentionService

    outside=tmp_path/"outside-logs"
    outside.mkdir()
    secret=outside/"must-survive.log"
    secret.write_bytes(b"outside-log")
    logs=tmp_path/"data"/"logs"
    logs.mkdir(parents=True)
    files=[]
    for index in range(3):
        path=logs/f"run-{index}.log"
        path.write_bytes(str(index).encode())
        stamp=1_700_000_000+index
        os.utime(path,(stamp,stamp))
        files.append(path)
    link=logs/"outside-link"
    _make_test_directory_link(link,outside)

    report=RetentionService._trim_run_files(logs,keep=1)
    assert secret.read_bytes()==b"outside-log"
    assert not link.exists() and not link.is_symlink()
    assert report["deleted_links"]==1
    assert files[2].exists() and not files[0].exists() and not files[1].exists()

    files[2].unlink()
    logs.rmdir()
    _make_test_directory_link(logs,outside)
    root_report=RetentionService._trim_run_files(logs,keep=0)
    assert secret.read_bytes()==b"outside-log"
    assert logs.exists()
    assert root_report["skipped_unsafe"]==1


def test_clear_isolated_mode_never_follows_media_symlinks(tmp_path):
    from apps.api.app.retention import RetentionService

    data=tmp_path/"data"
    database=Database(data/"database"/"objectmemory-demo.sqlite","DEMO")
    database.save("items",{"name":"演示物品","type":"phone"},"demo-item")
    outside=tmp_path/"outside-demo"
    outside.mkdir()
    secret=outside/"must-survive.jpg"
    secret.write_bytes(b"outside-demo-media")
    media=data/"demo-media"
    nested=media/"event-images"
    nested.mkdir(parents=True)
    local=nested/"local.jpg"
    local.write_bytes(b"local-demo-media")
    linked=nested/"outside-directory"
    _make_test_directory_link(linked,outside)

    with RuntimeModeLease(data,"DEMO") as lease:
        report=RetentionService.clear_isolated_mode(data,"DEMO",lease=lease)
        assert database.get("items","demo-item",unscoped=True) is None
        assert secret.read_bytes()==b"outside-demo-media"
        assert not local.exists() and not linked.exists() and not linked.is_symlink()
        assert report["deleted_links"]==1 and report["skipped_unsafe"]==0

        if nested.exists():
            nested.rmdir()
        media.rmdir()
        _make_test_directory_link(media,outside)
        root_report=RetentionService.clear_isolated_mode(data,"DEMO",lease=lease)
        assert secret.read_bytes()==b"outside-demo-media"
        assert not media.exists() and not media.is_symlink()
        assert root_report["deleted_links"]==1
