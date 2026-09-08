#!/usr/bin/env python3
"""Audit and (only with --apply) safely clean ObjectMemory event databases."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import Counter,defaultdict
from contextlib import ExitStack
from datetime import datetime,timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/"data"
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from apps.api.app.mode_lock import RuntimeModeBusy,RuntimeModeLease
from apps.api.app.acceptance import SUITE_CONTRACT_VERSION, SUITE_SCENARIOS
from apps.api.app.db import Database
from apps.api.app.retention import RetentionService
# Only sources whose capture path is locally controlled (or whose ESP32
# identity completed the USB/OMREADY proof) may be classified as verified REAL
# evidence. Browser and arbitrary network streams remain auditable candidates
# but cannot be promoted merely because their frames decode.
REAL_SOURCES={"opencv_camera","esp32_real","authorized_screen_capture"}
ALLOWED_DETECTOR_BACKENDS={"aruco","nanodet"}
ALLOWED_TRACKER_BACKENDS={"stable_identity"}
ALLOWED_DETECTION_MODES={"aruco","experimental"}
DELETE_CLASSES={"demo","test","virtual","seed","hardcoded","duplicate","invalid_media","no_evidence"}
CAMERA_SOURCES={"webcam":"opencv_camera","browser":"browser_camera","rtsp":"rtsp","onvif":"onvif","mjpeg":"mjpeg","esp32":"esp32_real","screen":"authorized_screen_capture","video":"video_file"}
LEGACY_DATABASE_NAME="object_memory.sqlite3"
AUTOMATIC_PRE_AUDIT_BACKUP=re.compile(
    r"^pre-audit-(?:\d{8}-\d{6}|\d{8}T\d{6}Z)(?:-[0-9a-f]{8})?\.sqlite(?:-(?:wal|shm))?$",
    re.IGNORECASE,
)


def sha256(path:Path)->str:
    digest=hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""):digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(path:Path)->tuple[int,int,int]|None:
    try:
        stat=path.stat()
    except FileNotFoundError:
        return None
    return stat.st_size,stat.st_mtime_ns,getattr(stat,"st_ino",0)


def _copy_matches(source:Path,copied:Path)->bool:
    if not source.is_file() or not copied.is_file() or source.stat().st_size!=copied.stat().st_size:
        return False
    return sha256(source)==sha256(copied)


def readonly_snapshot(path:Path,*,max_attempts:int=5)->tuple[sqlite3.Connection,dict[str,Any]]:
    """Return an in-memory, query-only snapshot without opening the source normally.

    ``mode=ro`` is not filesystem read-only for a WAL database: SQLite may create
    ``-shm`` next to the source.  A base-only database is therefore opened with
    ``immutable=1``.  When committed WAL content exists, the database and WAL
    are copied to an isolated temporary directory only after their size, mtime,
    inode and content remain stable for one copy attempt.  SQLite may create
    sidecars beside that private copy; the source directory is never touched.
    """
    resolved=path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    wal=Path(str(resolved)+"-wal")
    metadata:dict[str,Any]={
        "strategy":None,
        "wal_present":False,
        "wal_bytes":0,
        "source_sidecars_created":False,
        "copy_attempts":0,
    }
    memory=None
    last_error="source changed during snapshot capture"
    for attempt in range(1,max_attempts+1):
        metadata["copy_attempts"]=attempt
        database_before=_fingerprint(resolved)
        wal_before=_fingerprint(wal)
        if database_before is None:
            raise FileNotFoundError(resolved)
        if wal_before is None or wal_before[0]==0:
            source=sqlite3.connect(resolved.as_uri()+"?mode=ro&immutable=1",uri=True,timeout=5)
            candidate=sqlite3.connect(":memory:")
            try:
                source.backup(candidate)
            except BaseException:
                candidate.close()
                raise
            finally:
                source.close()
            database_after=_fingerprint(resolved)
            wal_after=_fingerprint(wal)
            if database_before==database_after and (wal_after is None or wal_after[0]==0):
                memory=candidate
                metadata.update({"strategy":"immutable_base","wal_present":wal_after is not None,"wal_bytes":0})
                break
            candidate.close()
            last_error="database changed or a non-empty WAL appeared during immutable capture"
            time.sleep(.025)
            continue
        try:
            with tempfile.TemporaryDirectory(prefix="objectmemory-audit-") as folder:
                copied=Path(folder)/resolved.name
                copied_wal=Path(str(copied)+"-wal")
                shutil.copyfile(resolved,copied)
                shutil.copyfile(wal,copied_wal)
                if not _copy_matches(resolved,copied) or not _copy_matches(wal,copied_wal):
                    last_error="source database or WAL changed during snapshot copy"
                    time.sleep(.025)
                    continue
                after=(_fingerprint(resolved),_fingerprint(wal))
                if (database_before,wal_before)!=after:
                    last_error="source database or WAL changed during snapshot verification"
                    time.sleep(.025)
                    continue
                source=sqlite3.connect(copied.as_uri()+"?mode=ro",uri=True,timeout=5)
                try:
                    if source.execute("PRAGMA integrity_check").fetchone()[0]!="ok":
                        raise sqlite3.DatabaseError("copied WAL snapshot failed integrity_check")
                    candidate=sqlite3.connect(":memory:")
                    try:
                        source.backup(candidate)
                    except BaseException:
                        candidate.close()
                        raise
                    memory=candidate
                finally:
                    source.close()
                metadata.update({"strategy":"stable_wal_copy","wal_present":True,"wal_bytes":wal_before[0]})
                break
        except FileNotFoundError:
            last_error="WAL disappeared during snapshot copy"
            time.sleep(.025)
            continue
    if memory is None:
        raise RuntimeError(f"无法取得一致 SQLite 快照：{last_error}")
    memory.row_factory=sqlite3.Row
    memory.execute("PRAGMA query_only=ON")
    if memory.execute("PRAGMA integrity_check").fetchone()[0]!="ok":
        memory.close()
        raise sqlite3.DatabaseError("in-memory audit snapshot failed integrity_check")
    return memory,metadata


def readonly(path:Path):
    connection,_=readonly_snapshot(path)
    return connection


def tables(connection)->set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def columns(connection,table:str)->set[str]:
    return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def database_candidates(explicit:Path|None)->list[Path]:
    if explicit:return [explicit.resolve()]
    database=DATA/"database"
    canonical=database/"objectmemory.sqlite"
    legacy=database/"object_memory.sqlite3"
    return [path for path in (canonical,legacy) if path.is_file()] or [canonical]


def media_root(database:Path)->Path:
    data_root=database.resolve().parent.parent
    names={"objectmemory.sqlite":"real-media","objectmemory-demo.sqlite":"demo-media","objectmemory-test.sqlite":"test-media"}
    return data_root/names[database.name] if database.name in names else data_root


def safe_media_path(database:Path,url:str|None)->Path|None:
    if not url or not str(url).startswith("/media/"):return None
    relative=Path(str(url).removeprefix("/media/"))
    if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] not in (("event-images",),("event-clips",),("thumbnails",)):return None
    root=media_root(database).resolve();path=(root/relative).resolve()
    return path if path.is_relative_to(root) and path!=root else None


def json_value(value,default):
    if not isinstance(value,str):return value if value is not None else default
    try:return json.loads(value)
    except (TypeError,ValueError):return default


def parse_timestamp(value: Any) -> datetime | None:
    try:
        parsed=datetime.fromisoformat(str(value).replace("Z","+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError,ValueError):
        return None


def evidence_timestamp(value:Any)->datetime|None:
    if isinstance(value,(int,float)):
        try:return datetime.fromtimestamp(float(value),timezone.utc)
        except (OSError,OverflowError,ValueError):return None
    return parse_timestamp(value)


def normalized_runtime(value:Any)->str:
    runtime=str(value or "").strip().upper()
    return runtime if runtime in {"REAL","DEMO","TEST"} else "UNKNOWN"


def runtime_scope(table_columns:set[str],runtime_mode:str,*,legacy_allowed:bool)->tuple[str,list[Any]]:
    """Return an explicit mode predicate for every mutating SQL statement.

    Old ``object_memory.sqlite3`` tables predate ``runtime_mode``.  They are the
    only tables allowed to use the record/event id as their complete scope.
    Modern or arbitrarily named databases without the provenance column fail
    closed instead of silently turning a scoped delete into a global one.
    """
    if "runtime_mode" in table_columns:
        return "UPPER(COALESCE(NULLIF(TRIM(runtime_mode),''),'UNKNOWN'))=?",[normalized_runtime(runtime_mode)]
    if legacy_allowed:
        return "1=1",[]
    raise RuntimeError("目标表缺少 runtime_mode，且不属于明确允许的旧版 object_memory.sqlite3。")


def automatic_backup(path:Path)->bool:
    return bool(AUTOMATIC_PRE_AUDIT_BACKUP.fullmatch(path.name))


def camera_provenance(camera:dict[str,Any],config:dict[str,Any],firmware_rows:list[dict[str,Any]]|None=None)->tuple[str,bool]:
    source=CAMERA_SOURCES.get(str(camera.get("source_type") or ""),"unknown")
    simulated=bool(config.get("simulated")) or source=="video_file"
    if source=="esp32_real":
        if simulated:return "virtual_esp32",True
        # Camera JSON is editable configuration, not hardware attestation.
        # Promote ESP32 evidence only when exactly one firmware-registry row
        # proves the same device/camera and the serial OMREADY flow recorded the
        # exact URLs used by this camera.
        rows=list(firmware_rows or [])
        row=rows[0] if len(rows)==1 else {}
        verified=bool(
            row
            and str(config.get("device_id") or "")==str(row.get("id") or "")
            and str(camera.get("id") or "")==str(row.get("camera_id") or "")
            and row.get("simulated") in (0,False)
            and row.get("hardware_verified") in (1,True)
            and row.get("verification_method")=="usb_serial_omready"
            and bool(row.get("serial_verified_at"))
            and bool(row.get("source_attestation_id"))
            and not row.get("token_revoked_at")
            and row.get("stream_url")==row.get("attested_stream_url")
            and row.get("capture_url")==row.get("attested_capture_url")
            and camera.get("source")==row.get("stream_url")
            and config.get("capture_url")==row.get("capture_url")
        )
        return ("esp32_real",False) if verified else ("esp32_unverified",False)
    return source,simulated


def acceptance_contract(event:dict[str,Any],runs:dict[str,dict[str,Any]],suites:dict[str,dict[str,Any]],checks:dict[str,Any],bindings:dict[str,Any])->bool:
    """Validate v2 only as a complete server-run and three-media contract.

    Inputs come from the read-only SQLite snapshot. No Database/Runtime service
    is constructed here: audit must never migrate or write the source database.
    """
    run=runs.get(str(event.get("validation_run_id") or ""),{})
    suite=suites.get(str(run.get("suite_id") or ""),{})
    try:
        index=int(run.get("scenario_index") or 0)
        if not 1<=index<=len(SUITE_SCENARIOS):return False
        scenario=SUITE_SCENARIOS[index-1]
        origin=json_value(suite.get("zone_a" if scenario["from_zone"]=="A" else "zone_b"),{})
        destination=json_value(suite.get("zone_a" if scenario["to_zone"]=="A" else "zone_b"),{})
        result=json_value(run.get("result"),{})
        if not all(isinstance(value,dict) for value in (origin,destination,result)):return False
        if not (
            suite and run and scenario["trial_kind"]=="movement"
            and run.get("status")==run.get("outcome")=="PASSED" and run.get("user_executed") in (1,True)
            and run.get("runtime_mode")==suite.get("runtime_mode")=="REAL"
            and run.get("source_type")==suite.get("source_type")==event.get("source_type")=="opencv_camera"
            and run.get("is_simulated") in (0,False) and suite.get("is_simulated") in (0,False)
            and run.get("contract_version")==suite.get("contract_version")==SUITE_CONTRACT_VERSION
            and json_value(suite.get("contract"),[])==list(SUITE_SCENARIOS)
            and run.get("item_id")==suite.get("item_id")==event.get("item_id")
            and run.get("camera_id")==suite.get("camera_id")==event.get("camera_id")
            and run.get("aruco_id")==suite.get("aruco_id")==event.get("aruco_id")
            and run.get("event_id")==event.get("event_id") and run.get("validation_run_id")==event.get("validation_run_id")
            and run.get("source_session_id")==event.get("source_session_id")
            and int(run.get("reconnect_epoch") or 0)==int(event.get("reconnect_epoch") or 0)
            and run.get("scenario_index")==event.get("scenario_index")
            and run.get("trial_kind")=="movement" and run.get("detection_mode")=="aruco_screen_validation"
            and json_value(run.get("thresholds"),{})==json_value(suite.get("thresholds"),{})
            and json_value(run.get("origin_zone"),{})==origin and json_value(run.get("destination_zone"),{})==destination
            and run.get("origin_zone_id")==event.get("from_zone_id")==origin.get("id")
            and run.get("destination_zone_id")==event.get("to_zone_id")==destination.get("id")
            and run.get("expected_from_zone")==event.get("from_zone")==origin.get("name")
            and run.get("expected_to_zone")==event.get("to_zone")==destination.get("name")
            and all(re.fullmatch(r"[0-9a-f]{64}",str(suite.get(key) or "")) for key in ("camera_config_sha256","camera_source_sha256"))
            and all(result.get(key)==event.get(key) for key in (
                "event_id","source_session_id","detection_mode","aruco_id","before_screenshot","after_screenshot",
                "before_screenshot_sha256","after_screenshot_sha256","clip_path","clip_sha256",
            ))
            and set(checks)=={"before","image","clip"} and all(check["full_valid"] for check in checks.values())
            and len({check.get("path") for check in checks.values()})==3
            and event.get("screenshot_path")==event.get("after_screenshot")
            and event.get("screenshot_sha256")==event.get("after_screenshot_sha256")
        ):return False
        binding=bindings.get("clip") or {}
        if any(candidate!=binding for candidate in bindings.values()):return False
        count=int(binding.get("clip_input_frames") or 0)
        before_index=event.get("before_frame_index");after_index=event.get("after_frame_index")
        if not (
            count>=2 and int(binding.get("clip_written_frames") or 0)==count
            and isinstance(before_index,int) and isinstance(after_index,int) and 0<=before_index<after_index<count
            and binding.get("before_frame_index")==before_index and binding.get("after_frame_index")==after_index
            and all(binding.get(key)==event.get(key) for key in (
                "before_screenshot","after_screenshot","before_screenshot_sha256","after_screenshot_sha256",
                "before_frame_sha256","after_frame_sha256",
            ))
            and event.get("clip_post_roll_complete") in (1,True)
            and float(event.get("clip_required_pre_seconds") or 0)>=5
            and float(event.get("clip_required_post_seconds") or 0)>=5
            and float(event.get("clip_collected_post_seconds") or 0)>=5
        ):return False
        capture=cv2.VideoCapture(str(checks["clip"]["path"]))
        decoded_count=0;keyframes={}
        try:
            while True:
                ok,frame=capture.read()
                if not ok or frame is None:break
                if decoded_count in (before_index,after_index):keyframes[decoded_count]=frame
                decoded_count+=1
                if decoded_count>count:return False
        finally:capture.release()
        if decoded_count!=count or len(keyframes)!=2:return False
        detector=cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50))
        for key,frame_index,zone in (("before",before_index,origin),("image",after_index,destination)):
            image=cv2.imdecode(np.frombuffer(Path(checks[key]["path"]).read_bytes(),np.uint8),cv2.IMREAD_COLOR)
            if image is None:return False
            for frame in (image,keyframes[frame_index]):
                corners,ids,_=detector.detectMarkers(frame)
                matching=[] if ids is None else [corner for marker_id,corner in zip(ids.flatten().tolist(),corners) if marker_id==event.get("aruco_id")]
                if len(matching)!=1:return False
                center=matching[0].reshape(-1,2).mean(axis=0)/np.array([frame.shape[1],frame.shape[0]])
                polygon=np.asarray(zone.get("points"),np.float32)
                if cv2.pointPolygonTest(polygon,(float(center[0]),float(center[1])),False)<0:return False
        return True
    except (OSError,TypeError,ValueError,KeyError,IndexError,cv2.error):return False


def audit_database(database:Path)->tuple[list[dict[str,Any]],dict[str,Any]]:
    if not database.is_file():return [],{"path":str(database),"exists":False}
    connection,snapshot=readonly_snapshot(database);present=tables(connection)
    event_table="movement_events" if "movement_events" in present else "events" if "events" in present else None
    if not event_table:
        connection.close();return [],{"path":str(database),"exists":True,"event_table":None,"event_count":0,"snapshot":snapshot}
    event_columns=columns(connection,event_table)
    events=[dict(row) for row in connection.execute(f'SELECT * FROM "{event_table}"')]
    cameras={}
    if "cameras" in present:
        cameras={row["id"]:dict(row) for row in connection.execute("SELECT * FROM cameras")}
    firmware_by_camera=defaultdict(list)
    if "firmware_devices" in present:
        firmware_columns=columns(connection,"firmware_devices")
        required={"id","camera_id","simulated","hardware_verified","verification_method","serial_verified_at","source_attestation_id","stream_url","capture_url","attested_stream_url","attested_capture_url","token_revoked_at"}
        if required.issubset(firmware_columns):
            for row in connection.execute(
                "SELECT id,camera_id,simulated,hardware_verified,verification_method,serial_verified_at,source_attestation_id,stream_url,capture_url,attested_stream_url,attested_capture_url,token_revoked_at FROM firmware_devices"
            ):
                firmware_by_camera[str(row["camera_id"] or "")].append(dict(row))
    media=defaultdict(list);registered_media=[]
    if "event_media" in present:
        for row in connection.execute("SELECT * FROM event_media"):
            decoded=dict(row);media[str(row["event_id"])].append(decoded);registered_media.append(decoded)
    sessions={}
    if "source_sessions" in present:
        sessions={str(row["id"]):dict(row) for row in connection.execute("SELECT * FROM source_sessions")}
    acceptance_runs={str(row["id"]):dict(row) for row in connection.execute("SELECT * FROM acceptance_runs")} if "acceptance_runs" in present else {}
    acceptance_suites={str(row["id"]):dict(row) for row in connection.execute("SELECT * FROM acceptance_suites")} if "acceptance_suites" in present else {}
    # Current-state evidence is mode-scoped in modern databases.  Treating the
    # event id alone as global lets a dirty REAL row accidentally protect a
    # DEMO/TEST event with the same id (or vice versa).  The exact legacy schema
    # has no runtime mode, so it keeps the historical id-only fallback.
    current:set[tuple[str,str]]|set[str]=set()
    current_is_mode_scoped=False
    if "item_current_state" in present and "evidence_event_id" in columns(connection,"item_current_state"):
        state_columns=columns(connection,"item_current_state")
        current_is_mode_scoped="runtime_mode" in state_columns
        if current_is_mode_scoped:
            current={
                (normalized_runtime(row[0]),str(row[1]))
                for row in connection.execute(
                    "SELECT runtime_mode,evidence_event_id FROM item_current_state WHERE evidence_event_id IS NOT NULL"
                )
            }
        else:
            current={
                str(row[0])
                for row in connection.execute(
                    "SELECT evidence_event_id FROM item_current_state WHERE evidence_event_id IS NOT NULL"
                )
            }
    minimum_confidence=.55
    if "settings" in present and "value" in columns(connection,"settings"):
        setting=connection.execute("SELECT value FROM settings WHERE id='main' LIMIT 1").fetchone()
        value=json_value(setting[0],{}) if setting else {}
        try:minimum_confidence=max(0.0,min(1.0,float((value or {}).get("min_confidence",.55))))
        except (TypeError,ValueError):minimum_confidence=.55
    connection.close()
    duplicate_groups=defaultdict(list);result=[];file_cache={}

    def inspect_file(path:Path|None,kind:str)->dict[str,Any]:
        key=(str(path),kind)
        if key in file_cache:return file_cache[key]
        exists=bool(path and path.is_file());actual=sha256(path) if exists else None;decodable=False
        if exists:
            try:
                if kind=="image":
                    decodable=cv2.imdecode(np.frombuffer(path.read_bytes(),np.uint8),cv2.IMREAD_COLOR) is not None
                else:
                    capture=cv2.VideoCapture(str(path))
                    try:ok,frame=capture.read();decodable=bool(ok and frame is not None)
                    finally:capture.release()
            except (OSError,cv2.error):decodable=False
        file_cache[key]={"exists":exists,"actual_sha256":actual,"decodable":decodable}
        return file_cache[key]
    for event in sorted(events,key=lambda row:str(row.get("timestamp_start") or row.get("created_at") or "")):
        event_id=str(event.get("event_id") or event.get("id"));camera=cameras.get(event.get("camera_id"),{})
        config=json_value(camera.get("config"),{}) or {}
        source=str(event.get("source_type") or "unknown")
        simulated=event.get("is_simulated")
        runtime=str(event.get("runtime_mode") or "UNKNOWN").upper()
        evidence=str(event.get("evidence_type") or "").lower();notes=str(event.get("notes") or "").lower()
        camera_kind=str(camera.get("source_type") or "")
        expected_source,expected_simulated=camera_provenance(camera,config,firmware_by_camera.get(str(camera.get("id") or ""),[]))
        if source=="demo_seed":classification="seed"
        elif source=="virtual_esp32" or config.get("simulated"):classification="virtual"
        elif runtime=="TEST" or source in {"test_fixture","mock"}:classification="test"
        elif runtime=="DEMO" or source=="video_file" or str(event.get("camera_id") or "").startswith("demo-"):classification="demo"
        elif any(word in evidence+" "+notes for word in ("hardcoded","mock","fixture","test_replay","demo_seed")):classification="hardcoded" if "hardcoded" in notes or "mock" in notes else "test"
        elif runtime=="REAL" and source in REAL_SOURCES and not bool(simulated):classification="real_candidate"
        else:classification="unknown"
        media_rows=media.get(event_id,[])
        manual=bool(event.get("manually_corrected")) or event.get("event_type")=="manual_correction"
        p0=not manual and event.get("detection_mode")=="aruco_screen_validation"
        checks={};hashes={};bindings={};manual_reference_ids=set()
        if manual and event.get("manual_reference_event_id"):
            # The online writer persists both the normalized root reference and
            # the signed media binding.  Auditing requires them to agree; a
            # hand-edited top-level pointer must not be silently ignored.
            manual_reference_ids.add(str(event["manual_reference_event_id"]))
        specs=(("before_screenshot","image","before","before"),("screenshot_path","image","image","after"),("clip_path","clip","clip","clip")) if p0 else (("screenshot_path","image","image",None),("clip_path","clip","clip",None))
        for field,kind,check_key,role in specs:
            url=event.get(field);kind_rows=[row for row in media_rows if row.get("kind")==kind and str(row.get("status") or "active")=="active" and (not p0 or row.get("role")==role)]
            row=kind_rows[0] if len(kind_rows)==1 else {}
            event_hash=event.get("before_screenshot_sha256" if check_key=="before" else "screenshot_sha256" if kind=="image" else "clip_sha256")
            registry_hash=row.get("sha256")
            path=safe_media_path(database,url)
            inspected=inspect_file(path,kind);actual=inspected["actual_sha256"]
            metadata=json_value(row.get("metadata"),{}) or {}
            binding=json_value(metadata.get("write_binding"),{}) or {}
            bindings[check_key]=binding
            if manual and binding.get("manual_reference_event_id"):
                manual_reference_ids.add(str(binding["manual_reference_event_id"]))
            supplied_binding_hash=binding.get("binding_sha256")
            unsigned_binding={key:value for key,value in binding.items() if key!="binding_sha256"}
            calculated_binding_hash=hashlib.sha256(
                json.dumps(unsigned_binding,ensure_ascii=False,sort_keys=True,separators=(",", ":")).encode("utf-8")
            ).hexdigest() if binding else None
            expected_binding_version="manual_correction_reference_v1" if manual else "event_service_atomic_v2" if p0 else "event_service_atomic_v1"
            expected_origin="admin_manual_correction" if manual else "vision_callback"
            binding_valid=bool(
                binding.get("binding_version")==expected_binding_version
                and binding.get("capture_origin")==expected_origin
                and supplied_binding_hash and supplied_binding_hash==calculated_binding_hash
                and str(binding.get("runtime_mode") or "").upper()==runtime
                and binding.get("source_type")==source
                and bool(binding.get("is_simulated"))==bool(simulated)
                and binding.get("source_session_id")==event.get("source_session_id")
                and binding.get("source_frame_start")==event.get("source_frame_start")
                and binding.get("source_frame_end")==event.get("source_frame_end")
                and binding.get("source_timestamp_start")==event.get("source_timestamp_start")
                and binding.get("source_timestamp_end")==event.get("source_timestamp_end")
                and binding.get("screenshot_path")==event.get("screenshot_path")
                and binding.get("screenshot_sha256")==event.get("screenshot_sha256")
                and binding.get("clip_path")==event.get("clip_path")
                and binding.get("clip_sha256")==event.get("clip_sha256")
                and (
                    bool(binding.get("manual_reference_event_id"))
                    if manual else (
                        int(binding.get("clip_input_frames") or 0)>=2
                        and bool(re.fullmatch(r"[0-9a-f]{64}",str(binding.get("clip_first_frame_sha256") or "")))
                        and bool(re.fullmatch(r"[0-9a-f]{64}",str(binding.get("clip_last_frame_sha256") or "")))
                        and bool(re.fullmatch(r"[0-9a-f]{64}",str(binding.get("after_frame_sha256") or "")))
                    )
                )
            )
            metadata_valid=bool(
                metadata.get("event_time_start")==event.get("source_timestamp_start")
                and metadata.get("event_time_end")==event.get("source_timestamp_end")
                and metadata.get("source_frame_start")==event.get("source_frame_start")
                and metadata.get("source_frame_end")==event.get("source_frame_end")
            )
            registry_valid=bool(
                len(kind_rows)==1 and row.get("path")==url and row.get("source_session_id")==event.get("source_session_id")
                and str(row.get("runtime_mode") or "").upper()==runtime and registry_hash==event_hash
                and str(row.get("status") or "active")=="active"
                and (not p0 or (not row.get("pending_delete_at") and not row.get("deleted_at")))
            )
            hash_valid=bool(event_hash and registry_hash and actual and event_hash==registry_hash==actual)
            checks[check_key]={"url":url,"path":str(path) if path else None,"exists":inspected["exists"],"decodable":inspected["decodable"],"event_sha256":event_hash,"registry_sha256":registry_hash,"actual_sha256":actual,"hash_valid":hash_valid,"registry_count":len(kind_rows),"registry_valid":registry_valid,"metadata_valid":metadata_valid,"binding_valid":binding_valid,"full_valid":bool(inspected["exists"] and inspected["decodable"] and hash_valid and registry_valid and metadata_valid and binding_valid)}
            hashes[check_key]=actual
        confirmed=(event.get("evidence_status")=="confirmed" and event.get("final_status")=="confirmed_placed") or (event_table=="events" and event.get("event_type")=="placed")
        flags=[]
        invalid=any(check["url"] and not check["full_valid"] for check in checks.values())
        if invalid:flags.append("invalid_media")
        if confirmed and (not checks["image"]["full_valid"] or (not manual and not checks["clip"]["full_valid"]) or (p0 and not checks["before"]["full_valid"])):flags.append("no_evidence")
        frame_start=event.get("source_frame_start");frame_end=event.get("source_frame_end")
        time_start=parse_timestamp(event.get("source_timestamp_start"));time_end=parse_timestamp(event.get("source_timestamp_end"))
        session=sessions.get(str(event.get("source_session_id") or ""),{})
        session_identity=bool(
            session and session.get("camera_id")==event.get("camera_id")
            and str(session.get("runtime_mode") or "").upper()==runtime
            and session.get("source_type")==source and bool(session.get("is_simulated"))==bool(simulated)
            and session.get("continuity_ok") in (1,True)
            and str(session.get("status") or "") not in {"error","disconnected","reconnecting"}
        )
        session_started=parse_timestamp(session.get("started_at"));session_ended=parse_timestamp(session.get("ended_at") or session.get("last_frame_at"))
        session_bounds=bool(
            session_identity and isinstance(session.get("first_frame"),int) and isinstance(session.get("last_frame"),int)
            and isinstance(frame_start,int) and isinstance(frame_end,int)
            and session["first_frame"]<=frame_start<frame_end<=session["last_frame"]
            and session_started and session_ended and time_start and time_end
            and session_started<=time_start<time_end<=session_ended
        )
        pickup=json_value(event.get("pickup_evidence"),{}) or {};placement=json_value(event.get("placement_evidence"),{}) or {}
        pickup_time=evidence_timestamp(pickup.get("source_timestamp"));placement_time=evidence_timestamp(placement.get("source_timestamp"))
        phase_evidence=bool(
            isinstance(pickup.get("source_frame"),int) and isinstance(placement.get("source_frame"),int)
            and isinstance(frame_start,int) and isinstance(frame_end,int)
            and frame_start<=pickup["source_frame"]<=placement["source_frame"]<=frame_end
            and time_start and time_end and pickup_time and placement_time
            and time_start<=pickup_time<=placement_time<=time_end
        )
        firmware_rows=firmware_by_camera.get(str(camera.get("id") or ""),[])
        attestation_matches=bool(
            source!="esp32_real"
            or (
                len(firmware_rows)==1
                and event.get("source_attestation_id")
                and event.get("source_attestation_id")==firmware_rows[0].get("source_attestation_id")
            )
        )
        camera_matches=bool(camera and simulated is not None and expected_source==source and expected_simulated==bool(simulated) and runtime=="REAL" and not expected_simulated and str(camera.get("runtime_mode") or "REAL").upper()=="REAL" and attestation_matches)
        meaningful=bool(event.get("from_zone") and event.get("to_zone") and (event.get("from_zone")!=event.get("to_zone") or event.get("meaningful_position_change") in (1,True)))
        try:confidence=float(event.get("confidence"))
        except (TypeError,ValueError):confidence=-1.0
        pipeline_valid=bool(
            event.get("ingested_at") and confidence>=minimum_confidence and confidence<=1.0
            and str(event.get("detector_backend") or "").strip().lower() in ALLOWED_DETECTOR_BACKENDS
            and (event.get("tracker_backend")=="aruco_acceptance_state_machine" if p0 else str(event.get("tracker_backend") or "").strip().lower() in ALLOWED_TRACKER_BACKENDS)
            and (event.get("detector_backend")=="aruco" if p0 else str(event.get("detection_mode") or "").strip().lower() in ALLOWED_DETECTION_MODES)
        )
        contract={
            "confirmed_event":bool(confirmed and (event.get("event_type")=="movement" or manual)),
            "identity":bool(event.get("event_id") and event.get("movement_session_id") and event.get("item_id") and event.get("camera_id")),
            "camera_provenance":camera_matches,
            "frame_range":bool(manual or (isinstance(frame_start,int) and isinstance(frame_end,int) and 0<=frame_start<frame_end)),
            "timestamp_range":bool(manual or (time_start and time_end and time_start<time_end)),
            "session":bool(manual or session_bounds),
            "phase_evidence":bool(manual or phase_evidence),
            "continuity":bool(manual or (event.get("source_continuity_ok") in (1,True) and event.get("reconnect_epoch_changed") not in (1,True))),
            "stable_transition":bool(manual or (event.get("stable_before") in (1,True) and event.get("stable_after") in (1,True) and meaningful)),
            "pipeline":bool(manual or pipeline_valid),
            "human_correction":bool(not manual or (event.get("manually_corrected") in (1,True) and event.get("created_by")=="user")),
            "media":bool(checks["image"]["full_valid"] and (manual or checks["clip"]["full_valid"])),
            "acceptance_v2":bool(not p0 or (len(media_rows)==3 and acceptance_contract(event,acceptance_runs,acceptance_suites,checks,bindings))),
        }
        if classification=="real_candidate":
            if not flags and all(contract.values()):classification="real_verified"
            else:
                classification="unknown"
                if "provenance_incomplete" not in flags:flags.append("provenance_incomplete")
        pinned=bool(event.get("pinned"));manually_corrected=bool(event.get("manually_corrected"))
        current_evidence=(runtime,event_id) in current if current_is_mode_scoped else event_id in current
        protected=pinned or manually_corrected or current_evidence
        result.append({"database":str(database),"table":event_table,"id":event.get("id"),"event_id":event_id,"event_type":event.get("event_type"),"movement_session_id":event.get("movement_session_id"),"idempotency_key":event.get("idempotency_key"),"item_id":event.get("item_id"),"camera_id":event.get("camera_id"),"timestamp":event.get("timestamp_end") or event.get("timestamp_start"),"runtime_mode":runtime,"source_type":source,"is_simulated":bool(simulated) if simulated is not None else None,"classification":classification,"flags":flags,"pinned":pinned,"manually_corrected":manually_corrected,"manual_reference_event_id":next(iter(manual_reference_ids)) if len(manual_reference_ids)==1 else None,"manual_reference_ids":sorted(manual_reference_ids),"current_evidence":current_evidence,"protected":protected,"provenance_contract":contract,"media":checks})
        if event.get("movement_session_id"):
            duplicate_groups[("movement_session",event.get("item_id"),event.get("camera_id"),event.get("movement_session_id"))].append(result[-1])
        if event.get("idempotency_key"):
            duplicate_groups[("idempotency",event.get("idempotency_key"))].append(result[-1])
        if event.get("source_session_id") and hashes["image"] and hashes["clip"] and time_end:
            duplicate_groups[("evidence_window",event.get("item_id"),event.get("camera_id"),event.get("source_session_id"),event.get("from_zone"),event.get("to_zone"),hashes["image"],hashes["clip"],int(time_end.timestamp()//5))].append(result[-1])
    for rows in duplicate_groups.values():
        if len(rows)<2:continue
        winner=max(rows,key=lambda row:(bool(row.get("current_evidence")),bool(row.get("pinned")),bool(row.get("manually_corrected")),str(row.get("timestamp") or "")))
        for row in rows:
            if row is winner:continue
            if "duplicate" not in row["flags"]:row["flags"].append("duplicate")
            row["duplicate_of"]=winner["event_id"]

    # A self-signed manual binding is not enough to prove provenance. Resolve
    # it to one existing event in the same mode, require that original event to
    # satisfy the complete non-simulated REAL evidence contract, and verify that
    # every piece of evidence reused by the correction has the same active
    # registry path and hash. This keeps hand-crafted/corrupt DB rows unknown.
    references=defaultdict(list)
    for row in result:
        for identity in {str(row.get("id") or ""),str(row.get("event_id") or "")} - {""}:
            references[(row.get("runtime_mode"),identity)].append(row)
    for row in result:
        manual=bool(row.get("manually_corrected")) or row.get("event_type")=="manual_correction"
        if not manual:
            row["provenance_contract"]["manual_reference"]=True
            continue
        reference_id=row.get("manual_reference_event_id")
        candidates=references.get((row.get("runtime_mode"),str(reference_id or "")),[])
        reference=candidates[0] if len(candidates)==1 else None
        reference_qualified=bool(
            reference and reference is not row
            and row.get("runtime_mode")=="REAL"
            and reference.get("runtime_mode")=="REAL"
            and reference.get("is_simulated") is False
            and not reference.get("manually_corrected")
            and reference.get("event_type")=="movement"
            and reference.get("classification")=="real_verified"
            and not DELETE_CLASSES.intersection(reference.get("flags") or [])
        )

        def reused_media_matches(kind:str)->bool:
            current=(row.get("media") or {}).get(kind) or {}
            original=(reference.get("media") or {}).get(kind) if reference else {}
            if not current.get("url"):
                return kind=="clip"
            return bool(
                current.get("full_valid") and original and original.get("full_valid")
                and current.get("url")==original.get("url")
                and current.get("event_sha256")==original.get("event_sha256")
                and current.get("actual_sha256")==original.get("actual_sha256")
            )

        valid_reference=bool(
            reference_qualified
            and len(row.get("manual_reference_ids") or [])==1
            and str(reference.get("event_id") or reference.get("id"))==str(reference_id)
            and reused_media_matches("image")
            and reused_media_matches("clip")
        )
        row["provenance_contract"]["manual_reference"]=valid_reference
        if not valid_reference:
            row["classification"]="unknown"
            if "no_evidence" not in row["flags"]:row["flags"].append("no_evidence")
            if "provenance_incomplete" not in row["flags"]:row["flags"].append("provenance_incomplete")
    referenced={str(check["url"]) for row in result for check in row["media"].values() if check.get("url")}
    referenced.update(str(row.get("path")) for row in registered_media if row.get("path") and str(row.get("status") or "active")!="deleted")
    orphans=[];root=media_root(database).resolve()
    orphan_cutoff=datetime.now(timezone.utc).timestamp()-300
    for folder_name in ("event-images","event-clips","thumbnails"):
        folder=root/folder_name
        if folder.is_dir():
            for path in folder.iterdir():
                if path.is_file() and path.stat().st_mtime<orphan_cutoff and f"/media/{folder_name}/{path.name}" not in referenced:orphans.append(str(path.resolve()))
    media_bytes=sum(
        path.stat().st_size
        for folder_name in ("event-images","event-clips","thumbnails")
        for path in (root/folder_name).rglob("*")
        if path.is_file()
    ) if root.is_dir() else 0
    info={"path":str(database),"exists":True,"bytes":database.stat().st_size,"media_bytes":media_bytes,"event_table":event_table,"event_count":len(result),"orphan_media":orphans,"snapshot":snapshot,"schema":{"tables":sorted(present),"event_columns":sorted(event_columns)}}
    return result,info


def audit(databases:list[Path])->dict[str,Any]:
    rows=[];infos=[]
    for database in databases:
        found,info=audit_database(database);rows.extend(found);infos.append(info)
    classes=Counter(row["classification"] for row in rows);flags=Counter(flag for row in rows for flag in row["flags"])
    return {"generated_at":datetime.now(timezone.utc).isoformat(),"read_only":True,"databases":infos,"summary":{"events":len(rows),"classifications":dict(classes),"flags":dict(flags),"unknown":classes.get("unknown",0)},"events":rows}


def write_audit(report:dict[str,Any],json_path:Path,csv_path:Path)->None:
    json_path.parent.mkdir(parents=True,exist_ok=True);csv_path.parent.mkdir(parents=True,exist_ok=True)
    json_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    fields=["database","table","id","event_id","item_id","camera_id","timestamp","runtime_mode","source_type","is_simulated","classification","flags","protected","screenshot_path","screenshot_hash_valid","screenshot_decodable","screenshot_registry_valid","screenshot_full_valid","clip_path","clip_hash_valid","clip_decodable","clip_registry_valid","clip_full_valid"]
    with csv_path.open("w",encoding="utf-8-sig",newline="") as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
        for row in report["events"]:
            writer.writerow({**{key:row.get(key) for key in fields},"flags":";".join(row["flags"]),"screenshot_path":row["media"]["image"]["path"],"screenshot_hash_valid":row["media"]["image"]["hash_valid"],"screenshot_decodable":row["media"]["image"]["decodable"],"screenshot_registry_valid":row["media"]["image"]["registry_valid"],"screenshot_full_valid":row["media"]["image"]["full_valid"],"clip_path":row["media"]["clip"]["path"],"clip_hash_valid":row["media"]["clip"]["hash_valid"],"clip_decodable":row["media"]["clip"]["decodable"],"clip_registry_valid":row["media"]["clip"]["registry_valid"],"clip_full_valid":row["media"]["clip"]["full_valid"]})


def cleanup_plan(report:dict[str,Any])->dict[str,Any]:
    grouped=defaultdict(list)
    delete=[]
    for row in report["events"]:
        if row["protected"]:continue
        if row["classification"] in DELETE_CLASSES or DELETE_CLASSES.intersection(row["flags"]):delete.append(row)
    for row in report["events"]:
        if row["classification"]=="real_verified" and not row.get("pinned"):grouped[(row["database"],row["item_id"])].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda row:str(row.get("timestamp") or ""),reverse=True)
        current_count=sum(row.get("current_evidence") for row in rows)
        slots=max(0,2-current_count)
        delete.extend([row for row in rows if not row.get("current_evidence")][slots:])
    unique={(row["database"],row["table"],row["id"]):row for row in delete}
    orphans=[{"database":info["path"],"path":path} for info in report["databases"] for path in info.get("orphan_media",[])]
    before_items=Counter(str(row.get("item_id") or "unknown") for row in report["events"])
    remaining_items=Counter(str(row.get("item_id") or "unknown") for key,row in ((key,row) for key,row in {(row["database"],row["table"],row["id"]):row for row in report["events"]}.items()) if key not in unique)
    data_roots={Path(info["path"]).resolve().parent.parent for info in report["databases"] if info.get("exists")}
    remaining=[row for key,row in {(row["database"],row["table"],row["id"]):row for row in report["events"]}.items() if key not in unique]
    duplicate_identified=sum("duplicate" in row.get("flags",[]) for row in report["events"])
    merged=sum("duplicate" in row.get("flags",[]) and row.get("duplicate_of") and all(target.get("event_id")!=row.get("duplicate_of") for target in unique.values()) for row in unique.values())
    return {"dry_run":True,"generated_at":datetime.now(timezone.utc).isoformat(),"audited_databases":[str(Path(info["path"]).resolve()) for info in report["databases"]],"bytes_before":sum(sum(path.stat().st_size for path in root.rglob("*") if path.is_file()) for root in data_roots if root.is_dir()),"managed_bytes_before":sum(int(info.get("bytes") or 0)+int(info.get("media_bytes") or 0) for info in report["databases"]),"delete_events":len(unique),"duplicate_events_identified":duplicate_identified,"merged_events":merged,"delete_orphan_media":len(orphans),"retain_events":len(remaining),"events_by_item_before":dict(before_items),"events_by_item_after_preview":dict(remaining_items),"unknown_retained":sum(row["classification"]=="unknown" for row in remaining),"targets":list(unique.values()),"orphan_media":orphans}


def _apply_cleanup_with_lease(plan:dict[str,Any],allowed_data_root:Path,eligible_targets:set[tuple[str,str,str]])->dict[str,Any]:
    applied_at=datetime.now(timezone.utc).isoformat()
    deleted_events=deleted_images=deleted_clips=deleted_tracks=deleted_states=0
    failed_files=failed_event_targets=failed_backup_deletes=0
    skipped_orphans=0;failures:list[dict[str,Any]]=[]
    by_database=defaultdict(list)
    for row in plan.get("targets",[]):by_database[Path(row["database"]).resolve()].append(row)
    for orphan in plan.get("orphan_media",[]):by_database.setdefault(Path(orphan["database"]).resolve(),[])

    def failure(stage:str,error:BaseException|str,**details:Any)->None:
        entry={"stage":stage,"error":f"{type(error).__name__}: {error}" if isinstance(error,BaseException) else str(error)}
        entry.update({key:value for key,value in details.items() if value is not None});failures.append(entry)

    def tree_bytes(path:Path)->int:
        total=0
        if not path.exists():return 0
        for child in path.rglob("*"):
            if child.is_file():
                try:total+=child.stat().st_size
                except OSError:pass
        return total

    def result(*,backup:Path|None,backup_status:str,events_by_item:dict[str,int]|None=None)->dict[str,Any]:
        nonlocal failed_files,failed_event_targets,failed_backup_deletes
        complete=not failures and deleted_events==len(next(iter(by_database.values()),[]))
        status="succeeded" if complete else "partial_failure" if deleted_events or deleted_images or deleted_clips else "failed"
        if not by_database:status="no_changes";complete=True
        database=next(iter(by_database),None);root=media_root(database).resolve() if database else None
        managed_after=(database.stat().st_size if database and database.is_file() else 0)+(tree_bytes(root/"event-images")+tree_bytes(root/"event-clips")+tree_bytes(root/"thumbnails") if root else 0)
        if not by_database:
            # An empty deletion plan still audited a database. Re-measure that
            # scope instead of treating an empty target map as zero disk usage.
            measured_databases={Path(value).resolve() for value in plan.get("audited_databases",[])}
            measured_databases={path for path in measured_databases if path.parent==(allowed_data_root.resolve()/"database").resolve() and path.suffix.lower() in {".sqlite",".sqlite3"}}
            if measured_databases:
                managed_after=sum(
                    (path.stat().st_size if path.is_file() else 0)
                    + sum(tree_bytes(media_root(path)/folder) for folder in ("event-images","event-clips","thumbnails"))
                    for path in measured_databases
                )
            else:
                # Compatibility for an older saved plan without audit scope:
                # no mutation occurred, so never manufacture reclaimed bytes.
                managed_after=plan.get("managed_bytes_before")
        return {
            **plan,"dry_run":False,"applied_at":applied_at,"success":complete,"apply_complete":complete,"apply_status":status,"counts_complete":True,
            "bytes_after":tree_bytes(allowed_data_root.resolve()),"managed_bytes_after":managed_after,
            "planned_event_deletes":len(next(iter(by_database.values()),[])),"deleted_events":deleted_events,
            "deleted_screenshots":deleted_images,"deleted_clips":deleted_clips,"failed_file_deletes":failed_files,
            "failed_event_targets":failed_event_targets,"failed_backup_deletes":failed_backup_deletes,
            "skipped_orphan_media":skipped_orphans,"deleted_tracks":deleted_tracks,"deleted_current_states":deleted_states,
            "events_by_item_after":events_by_item if events_by_item is not None else plan.get("events_by_item_after_preview",{}),
            "backup":str(backup) if backup else None,"backup_status":backup_status,"failures":failures,
        }

    if len(by_database)>1:
        failure("scope_validation","一次只能清理一个数据库；请用 --database 明确指定，避免单份备份覆盖。")
        return result(backup=None,backup_status="not_created")
    if not by_database:return result(backup=None,backup_status="not_required")
    data_root=allowed_data_root.resolve();allowed_database=(data_root/"database").resolve()
    database=next(iter(by_database));rows=by_database[database]
    if database.parent!=allowed_database or database.suffix.lower() not in {".sqlite",".sqlite3"}:
        failure("scope_validation",f"--apply 只允许明确 data root 下的 database/*.sqlite(3)：{database}")
        return result(backup=None,backup_status="not_created")

    backup_dir=data_root/"backups";backup_path=backup_dir/"pre-audit-latest.sqlite";temporary=backup_path.with_suffix(".tmp")
    source=None;destination=None
    try:
        backup_dir.mkdir(parents=True,exist_ok=True)
        temporary.unlink(missing_ok=True)
        source=sqlite3.connect(database);destination=sqlite3.connect(temporary)
        source.backup(destination);destination.commit()
        source.close();source=None;destination.close();destination=None
        os.replace(temporary,backup_path)
        for suffix in ("-wal","-shm"):(backup_path.parent/(backup_path.name+suffix)).unlink(missing_ok=True)
    except (OSError,sqlite3.Error) as exc:
        failure("backup_create",exc,path=str(backup_path))
        for handle in (source,destination):
            if handle:
                try:handle.close()
                except sqlite3.Error:pass
        try:temporary.unlink(missing_ok=True)
        except OSError as cleanup_exc:failure("backup_temp_cleanup",cleanup_exc,path=str(temporary))
        return result(backup=None,backup_status="failed")

    connection=None;events_by_item:dict[str,int]={};root=media_root(database).resolve()
    receipt_service=None
    successful:list[dict[str,Any]]=[]
    try:
        connection=sqlite3.connect(database,timeout=15);connection.row_factory=sqlite3.Row
        present=tables(connection)
        actual_table="movement_events" if "movement_events" in present else "events" if "events" in present else None
        requested_tables={str(row.get("table") or "") for row in rows}
        if rows and (not actual_table or requested_tables!={actual_table}):
            raise RuntimeError(f"清理计划表与当前数据库不一致：plan={sorted(requested_tables)}, database={actual_table}")
        table=actual_table
        event_columns=columns(connection,table) if table else set()
        legacy_allowed=bool(database.name==LEGACY_DATABASE_NAME and table=="events" and "runtime_mode" not in event_columns)
        if table:runtime_scope(event_columns,"UNKNOWN",legacy_allowed=legacy_allowed)
        target_record_ids={str(row["id"]) for row in rows}
        path_columns=[name for name in ("screenshot_path","before_screenshot","after_screenshot","clip_path") if name in event_columns]
        retained_paths=set()
        if table and path_columns:
            selected=",".join(["id",*(f'"{name}"' for name in path_columns)])
            for retained in connection.execute(f'SELECT {selected} FROM "{table}"'):
                if str(retained["id"]) not in target_record_ids:
                    retained_paths.update(str(retained[name]) for name in path_columns if retained[name])
        media_columns=columns(connection,"event_media") if "event_media" in present else set()
        if media_columns:
            runtime_scope(media_columns,"UNKNOWN",legacy_allowed=legacy_allowed)
            if "status" not in media_columns:
                connection.execute("ALTER TABLE event_media ADD COLUMN status TEXT");media_columns.add("status")
            if "pending_delete_at" not in media_columns:
                connection.execute("ALTER TABLE event_media ADD COLUMN pending_delete_at TEXT");media_columns.add("pending_delete_at")

        connection.commit()
        # Only the applying branch reaches here, after the mode lease and the
        # latest pre-audit backup. Do not run the runtime constructor's global
        # pending-media recovery: this offline apply owns its audited targets.
        # Existing pending receipts are completed by the same explicit deletion
        # protocol below, including recovery after an earlier partial apply.
        has_p0_targets=any(
            normalized_runtime(target.get("runtime_mode"))=="REAL"
            and "before" in (target.get("media") or {})
            for target in rows
        )
        if has_p0_targets:
            receipt_service=RetentionService(
                Database(database,"REAL"),database.parent.parent,root,
                recover_pending_on_start=False,
            )
        for target in rows:
            runtime=normalized_runtime(target.get("runtime_mode"));event_id=str(target.get("event_id") or "")
            target_key=(str(database),str(target.get("table") or ""),str(target.get("id") or ""))
            if target_key not in eligible_targets:
                failed_event_targets+=1
                failure("event_reclassified","目标事件在加锁后的重新审计中已受保护或不再满足清理条件。",event_id=event_id,runtime_mode=runtime)
                continue
            connection.execute("BEGIN IMMEDIATE")
            event_clause,event_params=runtime_scope(event_columns,runtime,legacy_allowed=legacy_allowed)
            current=connection.execute(f'SELECT * FROM "{table}" WHERE id=? AND {event_clause} LIMIT 1',(target["id"],*event_params)).fetchone()
            if not current or str(current["event_id"] or current["id"])!=event_id:
                connection.rollback();failed_event_targets+=1;failure("event_scope_mismatch","目标事件已变化、已不存在或 runtime_mode 不匹配。",event_id=event_id,runtime_mode=runtime);continue
            current_values=dict(current)
            protected_now=bool(current_values.get("pinned")) or bool(current_values.get("manually_corrected")) or current_values.get("event_type")=="manual_correction"
            if not protected_now and "item_current_state" in present and "evidence_event_id" in columns(connection,"item_current_state"):
                state_columns=columns(connection,"item_current_state")
                state_clause,state_params=runtime_scope(state_columns,runtime,legacy_allowed=legacy_allowed)
                protected_now=connection.execute(
                    f"SELECT 1 FROM item_current_state WHERE evidence_event_id=? AND {state_clause} LIMIT 1",
                    (event_id,*state_params),
                ).fetchone() is not None
            if protected_now:
                connection.rollback();failed_event_targets+=1
                failure("event_became_protected","目标事件已固定、人工纠正或成为当前状态证据，本次不删除。",event_id=event_id,runtime_mode=runtime)
                continue
            if (
                receipt_service is not None and runtime=="REAL"
                and current_values.get("detection_mode")=="aruco_screen_validation"
                and target.get("classification")=="real_verified"
                and not DELETE_CLASSES.intersection(target.get("flags") or [])
            ):
                # This row was selected solely by the history limit. The
                # helper rechecks actual files and run binding and writes the
                # receipt intent inside this exact tombstone transaction.
                receipt_service._prepare_acceptance_receipt(
                    connection,receipt_service.db._decode("movement_events",current),
                    datetime.now(timezone.utc).isoformat(),
                )
            media_rows=[];media_clause="";media_params:list[Any]=[]
            if media_columns:
                media_clause,media_params=runtime_scope(media_columns,runtime,legacy_allowed=legacy_allowed)
                media_rows=[dict(item) for item in connection.execute(f"SELECT * FROM event_media WHERE event_id=? AND {media_clause}",(event_id,*media_params))]
                connection.execute(f"UPDATE event_media SET status='pending_delete',pending_delete_at=? WHERE event_id=? AND {media_clause}",(datetime.now(timezone.utc).isoformat(),event_id,*media_params));connection.commit()
            if not media_rows:
                for kind in ("image","clip"):
                    check=target.get("media",{}).get(kind,{})
                    if check.get("url"):media_rows.append({"id":None,"event_id":event_id,"path":check["url"],"kind":kind})
            event_ok=True
            for media in media_rows:
                url=str(media.get("path") or "");shared=url in retained_paths
                if not shared and media_columns:
                    if media.get("id") is None:
                        shared=connection.execute("SELECT 1 FROM event_media WHERE path=? AND COALESCE(status,'active')='active' LIMIT 1",(url,)).fetchone() is not None
                    else:
                        shared=connection.execute("SELECT 1 FROM event_media WHERE path=? AND id<>? AND COALESCE(status,'active')='active' LIMIT 1",(url,media["id"])).fetchone() is not None
                if shared:continue
                path=safe_media_path(database,url)
                if not path:
                    event_ok=False;failed_files+=1;failure("event_media_path","媒体路径不在允许目录。",event_id=event_id,path=url);continue
                try:
                    existed=path.exists();path.unlink(missing_ok=True)
                    if existed and media.get("kind")=="image":deleted_images+=1
                    elif existed and media.get("kind")=="clip":deleted_clips+=1
                except OSError as exc:
                    event_ok=False;failed_files+=1;failure("event_media_delete",exc,event_id=event_id,path=str(path))
            if not event_ok:
                failed_event_targets+=1;connection.commit();continue
            if media_columns:
                for media in media_rows:
                    if media.get("id") is not None:
                        deleted_media=connection.execute(f"DELETE FROM event_media WHERE id=? AND {media_clause}",(media["id"],*media_params)).rowcount
                        if deleted_media!=1:
                            event_ok=False;failure("event_media_scope_mismatch","媒体注册行未按目标 runtime_mode 删除。",event_id=event_id,media_id=media["id"],runtime_mode=runtime)
            if not event_ok:
                failed_event_targets+=1;connection.commit();continue
            deleted=int(connection.execute(f'DELETE FROM "{table}" WHERE id=? AND {event_clause}',(target["id"],*event_params)).rowcount)
            if deleted!=1:
                failed_event_targets+=1;failure("event_delete_scope_mismatch","事件未按目标 runtime_mode 删除。",event_id=event_id,runtime_mode=runtime);connection.commit();continue
            deleted_events+=1;successful.append({**target,"runtime_mode":runtime});connection.commit()
            if receipt_service is not None:
                receipt_service._finalize_acceptance_receipts()

        if "item_current_state" in present and "evidence_event_id" in columns(connection,"item_current_state"):
            state_columns=columns(connection,"item_current_state")
            for target in successful:
                clause,params=runtime_scope(state_columns,target["runtime_mode"],legacy_allowed=legacy_allowed)
                deleted_states+=int(connection.execute(f"DELETE FROM item_current_state WHERE evidence_event_id=? AND {clause}",(target["event_id"],*params)).rowcount)
        if "tracks" in present:
            track_columns=columns(connection,"tracks")
            if {"item_id","camera_id"}.issubset(track_columns):
                for target in successful:
                    event_clause,event_params=runtime_scope(event_columns,target["runtime_mode"],legacy_allowed=legacy_allowed)
                    retained=connection.execute(f'SELECT 1 FROM "{table}" WHERE item_id=? AND camera_id=? AND {event_clause} LIMIT 1',(target.get("item_id"),target.get("camera_id"),*event_params)).fetchone()
                    if not retained:
                        track_clause,track_params=runtime_scope(track_columns,target["runtime_mode"],legacy_allowed=legacy_allowed)
                        deleted_tracks+=int(connection.execute(f"DELETE FROM tracks WHERE item_id=? AND camera_id=? AND {track_clause}",(target.get("item_id"),target.get("camera_id"),*track_params)).rowcount)
        connection.commit()

        media_has_status=bool(media_columns and "status" in media_columns)
        def still_referenced(url:str)->bool:
            if table and path_columns:
                clause=" OR ".join(f'"{name}"=?' for name in path_columns)
                if connection.execute(f'SELECT 1 FROM "{table}" WHERE {clause} LIMIT 1',tuple(url for _ in path_columns)).fetchone():return True
            if media_columns:
                status=" AND COALESCE(status,'active')<>'deleted'" if media_has_status else ""
                if connection.execute("SELECT 1 FROM event_media WHERE path=?"+status+" LIMIT 1",(url,)).fetchone():return True
            return False
        cutoff=datetime.now(timezone.utc).timestamp()-300
        for orphan in (entry for entry in plan.get("orphan_media",[]) if Path(entry["database"]).resolve()==database):
            path=Path(orphan["path"]).resolve()
            if not (path.is_relative_to(root) and path!=root and path.parent.name in {"event-images","event-clips","thumbnails"}):
                failed_files+=1;failure("orphan_scope","孤儿媒体路径不在允许目录。",path=str(path));continue
            try:
                url=f"/media/{path.parent.name}/{path.name}"
                if still_referenced(url) or not path.exists() or path.stat().st_mtime>=cutoff:
                    skipped_orphans+=1;continue
                path.unlink()
                if path.parent.name=="event-images":deleted_images+=1
                elif path.parent.name=="event-clips":deleted_clips+=1
            except OSError as exc:
                failed_files+=1;failure("orphan_media_delete",exc,path=str(path))
        if table:
            events_by_item={str(item or "unknown"):int(count) for item,count in connection.execute(f'SELECT item_id,COUNT(*) FROM "{table}" GROUP BY item_id')}
        try:connection.execute("VACUUM")
        except sqlite3.Error as exc:failure("database_vacuum",exc,path=str(database))
    except (OSError,sqlite3.Error,RuntimeError) as exc:
        failure("database_apply",exc,path=str(database))
    finally:
        if connection:
            try:connection.close()
            except sqlite3.Error as exc:failure("database_close",exc,path=str(database))

    try:
        candidates=list(backup_dir.iterdir())
    except OSError as exc:
        candidates=[];failed_backup_deletes+=1;failure("backup_inventory",exc,path=str(backup_dir))
    for old in candidates:
        try:is_automatic_file=old.is_file() and automatic_backup(old)
        except OSError as exc:
            failed_backup_deletes+=1;failure("old_backup_inspect",exc,path=str(old));continue
        if is_automatic_file:
            try:old.unlink()
            except OSError as exc:
                failed_backup_deletes+=1;failure("old_backup_delete",exc,path=str(old))
    return result(backup=backup_path,backup_status="created",events_by_item=events_by_item)


def _cleanup_modes(database:Path,plan:dict[str,Any])->tuple[str,...]:
    names={
        "objectmemory.sqlite":"REAL",
        LEGACY_DATABASE_NAME:"REAL",
        "objectmemory-demo.sqlite":"DEMO",
        "objectmemory-test.sqlite":"TEST",
    }
    # A contaminated canonical database may contain rows from another mode.
    # Hold both the physical database's mode and every row mode.  This prevents
    # a forged/stale plan from bypassing the owner of a legacy database and also
    # prevents a DEMO cleanup inside a dirty REAL database from racing either
    # active runtime.  Sorting gives all callers the same acquisition order.
    target_modes={normalized_runtime(row.get("runtime_mode")) for row in plan.get("targets",[])}
    known_modes=target_modes.intersection({"REAL","DEMO","TEST"})
    database_mode=names.get(database.name)
    if "UNKNOWN" in target_modes and known_modes:
        raise ValueError(f"清理目标同时包含明确及未知运行模式：{sorted(target_modes)}")
    modes=set(known_modes)
    if database_mode:modes.add(database_mode)
    if not modes:
        raise ValueError(f"无法从数据库名或清理目标确定运行模式：{database}")
    return tuple(sorted(modes))


def _lease_failure(plan:dict[str,Any],error:BaseException)->dict[str,Any]:
    return {
        **plan,"dry_run":False,"applied_at":datetime.now(timezone.utc).isoformat(),
        "success":False,"apply_complete":False,"apply_status":"failed","counts_complete":True,
        "deleted_events":0,"deleted_screenshots":0,"deleted_clips":0,"failed_file_deletes":0,
        "failed_event_targets":len(plan.get("targets",[])),"failed_backup_deletes":0,
        "skipped_orphan_media":len(plan.get("orphan_media",[])),"deleted_tracks":0,"deleted_current_states":0,
        "backup":None,"backup_status":"not_created",
        "failures":[{"stage":"runtime_mode_lease","error":f"{type(error).__name__}: {error}"}],
    }


def _database_apply_failure(plan:dict[str,Any],error:BaseException)->dict[str,Any]:
    """Return a zero-mutation apply failure for an unsafe database contract."""
    result=_lease_failure(plan,error)
    result["failures"]=[{"stage":"database_apply","error":f"{type(error).__name__}: {error}"}]
    return result


def apply_cleanup(plan:dict[str,Any],allowed_data_root:Path)->dict[str,Any]:
    """Apply one fresh cleanup plan while exclusively owning its runtime mode.

    The second audit happens after the lease is acquired.  This prevents an
    administrator pin/current-state change made between the dry-run snapshot
    and apply from being treated as an approved stale target.
    """
    databases={Path(row["database"]).resolve() for row in plan.get("targets",[])}
    databases.update(Path(row["database"]).resolve() for row in plan.get("orphan_media",[]))
    if not databases:
        return _apply_cleanup_with_lease(plan,allowed_data_root,set())
    if len(databases)!=1:
        return _apply_cleanup_with_lease(plan,allowed_data_root,set())
    database=next(iter(databases));data_root=allowed_data_root.resolve()
    try:
        modes=_cleanup_modes(database,plan)
        with ExitStack() as leases:
            for mode in modes:
                leases.enter_context(RuntimeModeLease(data_root,mode))
            fresh_report=audit([database])
            fresh_plan=cleanup_plan(fresh_report)
            eligible={
                (str(Path(row["database"]).resolve()),str(row.get("table") or ""),str(row.get("id") or ""))
                for row in fresh_plan.get("targets",[])
            }
            # The one explicitly recognised pre-provenance database is allowed
            # to clean an UNKNOWN row selected by an existing dry-run plan.  It
            # is still re-read under the REAL lease and must remain unprotected;
            # arbitrary databases without runtime_mode never receive this path.
            if database.name==LEGACY_DATABASE_NAME:
                requested={
                    (str(database),str(row.get("table") or ""),str(row.get("id") or ""))
                    for row in plan.get("targets",[])
                }
                eligible.update(
                    key
                    for row in fresh_report.get("events",[])
                    if not row.get("protected")
                    for key in [(
                        str(Path(row["database"]).resolve()),
                        str(row.get("table") or ""),
                        str(row.get("id") or ""),
                    )]
                    if key in requested
                )
            return _apply_cleanup_with_lease(plan,data_root,eligible)
    except RuntimeModeBusy as exc:
        return _lease_failure(plan,exc)
    except ValueError as exc:
        return _database_apply_failure(plan,exc)


def main()->int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--database",type=Path)
    parser.add_argument("--json",type=Path,default=ROOT/"audit"/"event-audit.json")
    parser.add_argument("--csv",type=Path,default=ROOT/"audit"/"event-audit.csv")
    parser.add_argument("--cleanup",action="store_true")
    parser.add_argument("--apply",action="store_true")
    parser.add_argument("--data-root",type=Path,default=DATA,help="Safety boundary for --apply; database must be inside its database directory.")
    parser.add_argument("--result",type=Path)
    args=parser.parse_args()
    report=audit(database_candidates(args.database));write_audit(report,args.json,args.csv)
    exit_code=0
    if args.cleanup:
        plan=cleanup_plan(report)
        if args.apply:
            try:result=apply_cleanup(plan,args.data_root)
            except Exception as exc:  # preserve a machine-readable failure result for unexpected faults
                result={**plan,"dry_run":False,"applied_at":datetime.now(timezone.utc).isoformat(),"success":False,"apply_complete":False,"apply_status":"failed","counts_complete":False,"deleted_events":None,"deleted_screenshots":None,"deleted_clips":None,"failed_file_deletes":None,"failed_event_targets":len(plan.get("targets",[])),"failed_backup_deletes":None,"backup":None,"backup_status":"unknown","failures":[{"stage":"unexpected_apply_error","error":f"{type(exc).__name__}: {exc}"}]}
            if not result.get("success",False):exit_code=1
        else:result=plan
        output=args.result or ROOT/"audit"/("cleanup-result.json" if args.apply else "cleanup-dry-run.json")
        output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({key:value for key,value in result.items() if key!="targets"},ensure_ascii=False))
    else:print(json.dumps(report["summary"],ensure_ascii=False))
    return exit_code


if __name__=="__main__":raise SystemExit(main())
