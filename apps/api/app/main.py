from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import socket
import sqlite3
import threading
import time
import contextlib
import zipfile
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit,quote
from uuid import uuid4

import cv2
import numpy as np
from anyio import CancelScope, to_thread as anyio_to_thread
from pydantic import ValidationError
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response, UploadFile, File, WebSocket, WebSocketDisconnect, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .db import Database, now
from .schemas import CameraInput, CameraDiagnosticInput, ZoneInput, ItemInput, SettingsInput, SearchInput, CorrectionInput, PinInput, PolicyInput, DeleteRealInput
from .security import Sessions, LocalSecurityMiddleware, redact, local_url
from .search import location_result, match_items
from .runtime_mode import RuntimeMode, resolve_runtime_mode, runtime_layout, validate_source_for_mode, provenance_for_camera
from .event_service import EventService, EvidenceRejected, file_sha256
from .acceptance import AcceptanceService, AcceptanceRejected
from .retention import RetentionService
from .mode_lock import RuntimeModeLease
from .registration import RegistrationService, RegistrationError
from .scene import SceneService, SceneError

ROOT=Path(__file__).resolve().parents[3]
load_dotenv(ROOT/'.env')
VERSION='0.1.0'


def public_camera_config(value):
    """Camera configuration is public metadata, never a credential transport."""
    from services.vision.camera_sources.base import SENSITIVE_KEYS, redact_value
    if isinstance(value,dict):
        return {key:public_camera_config(item) for key,item in value.items()
                if str(key).lower() not in SENSITIVE_KEYS | {'username'}}
    if isinstance(value,list):return [public_camera_config(item) for item in value]
    return redact_value(value)


def timestamp_after(candidate, baseline) -> bool:
    """Compare evidence instants, not their ISO-8601 string spellings."""
    try:
        left=datetime.fromisoformat(str(candidate).replace('Z','+00:00'))
        right=datetime.fromisoformat(str(baseline).replace('Z','+00:00'))
        if left.tzinfo is None:left=left.replace(tzinfo=timezone.utc)
        if right.tzinfo is None:right=right.replace(tzinfo=timezone.utc)
        return left.astimezone(timezone.utc)>right.astimezone(timezone.utc)
    except (TypeError,ValueError):
        return str(candidate or '')>str(baseline or '')


def lan_addresses():
    values={'127.0.0.1','localhost','::1'}
    try:
        values.update(socket.gethostbyname_ex(socket.gethostname())[2])
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        try:
            s.connect(('192.0.2.1',80))
            values.add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    return sorted(values)


class Runtime:
    def __init__(self, layout, testing=False):
        self.mode=layout.mode
        self.data=layout.data_root
        self.media=layout.media_root
        self.temporary=layout.temporary_root
        self.logs=layout.logs_root
        for folder in [self.data/'database',self.data/'registered-items',self.media/'event-images',self.media/'event-clips',self.media/'thumbnails',self.temporary,self.logs]:
            folder.mkdir(parents=True,exist_ok=True)
        canonical_existed=layout.database_path.exists()
        self.db=Database(layout.database_path,self.mode.value)
        self.legacy_migration=self._migrate_legacy_configuration() if self.mode is RuntimeMode.REAL and not canonical_existed else {'performed':False}
        self.sessions=Sessions(testing)
        self.engines={}
        self.stopping_engines={}
        self.lock=threading.RLock()
        self.track_writes={}
        self.track_write_states={}
        self.companions={}
        self.camera_subscribers={}
        self.shutdown_requested=threading.Event()
        self.camera_logs=deque(maxlen=50)
        self.camera_test_frames={}
        self.last_camera_health={}
        self.diagnostic_lock=threading.Lock()
        self.diagnostic_running=False
        self.diagnostic_job=None
        self.diagnostic_cancel=threading.Event()
        self.diagnostic_task=None
        self.screen_grants={}
        self.testing=testing
        self.db.save('settings',{'value': self.settings()},'main')
        self.retention=RetentionService(self.db,self.data,self.media,max_storage_mb=self.settings()['max_storage_mb'])
        self.acceptance=AcceptanceService(self.db,self.mode,self.camera_provenance,media_root=self.media)
        self.event_service=EventService(self.db,self.mode,self.media,self.retention,provenance_resolver=self.camera_provenance,acceptance=self.acceptance)
        self.registration=RegistrationService(self.db,self.data,ROOT/'data/models')
        self.scenes=SceneService(self.db,self.media,ROOT/'data/models')

    def refresh_recognition(self, activate=False):
        items=self.registration.items_for_inference()
        if activate:
            self.db.save('settings', {'value': {**self.settings(), 'detection_mode': 'experimental'}}, 'main')
        encoder=self.registration.encoder if activate else None
        with self.lock:
            engines=list(self.engines.values())
        for engine in engines:
            engine.reload_profiles(items,encoder,enable=activate)

    def _migrate_legacy_configuration(self):
        """Copy user configuration only; legacy events/media are quarantined.

        The old mixed database remains untouched and available to the audit and
        cleanup scripts. Demo cameras/items and every historical observation are
        deliberately not promoted into canonical REAL storage.
        """
        legacy=self.data/'database'/'object_memory.sqlite3'
        if not legacy.is_file() or legacy.resolve()==self.db.path.resolve():return {'performed':False}
        copied={'cameras':0,'zones':0,'items':0,'reference_images':0,'events':0}
        try:
            uri=legacy.resolve().as_uri()+'?mode=ro'
            connection=sqlite3.connect(uri,uri=True);connection.row_factory=sqlite3.Row
            tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            def rows(table):return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')] if table in tables else []
            allowed_cameras=set()
            for row in rows('cameras'):
                try:config=json.loads(row.get('config') or '{}') if isinstance(row.get('config'),str) else row.get('config') or {}
                except (TypeError,ValueError):config={}
                if row.get('source_type')=='video' or bool(config.get('simulated')):continue
                if row.get('source_type')=='screen':
                    # A persisted region is not a perpetual permission.  Legacy
                    # rows migrate disabled and must receive a fresh local,
                    # single-use grant before every future start.
                    config.pop('capture_grant',None)
                    config['authorized']=False
                    row['enabled']=False
                row['config']=config;row['runtime_mode']='REAL'
                self.db.save('cameras',row,row['id']);allowed_cameras.add(row['id']);copied['cameras']+=1
            for row in rows('zones'):
                if row.get('camera_id') not in allowed_cameras:continue
                if isinstance(row.get('points'),str):
                    try:row['points']=json.loads(row['points'])
                    except ValueError:continue
                self.db.save('zones',row,row['id']);copied['zones']+=1
            allowed_items=set()
            for row in rows('items'):
                if row.get('id') in {'demo-phone','demo-keys','demo-wallet'}:continue
                if isinstance(row.get('aliases'),str):
                    try:row['aliases']=json.loads(row['aliases'])
                    except ValueError:row['aliases']=[]
                self.db.save('items',row,row['id']);allowed_items.add(row['id']);copied['items']+=1
            for row in rows('item_reference_images'):
                if row.get('item_id') not in allowed_items:continue
                if isinstance(row.get('features'),str):
                    try:row['features']=json.loads(row['features'])
                    except ValueError:row['features']={}
                self.db.save('item_reference_images',row,row['id']);copied['reference_images']+=1
            settings=next((row for row in rows('settings') if row.get('id')=='main'),None)
            if settings:
                try:value=json.loads(settings.get('value') or '{}') if isinstance(settings.get('value'),str) else settings.get('value') or {}
                except (TypeError,ValueError):value={}
                value.update({'retention_policy':'MINIMAL','max_storage_mb':int(os.environ.get('MAX_STORAGE_MB','500'))})
                self.db.save('settings',{'value':value},'main')
            connection.close()
            return {'performed':True,'legacy_path':str(legacy),'canonical_path':str(self.db.path),'copied':copied,'legacy_events_imported':False}
        except (sqlite3.Error,OSError,ValueError) as exc:
            return {'performed':False,'error':redact(exc),'legacy_events_imported':False}

    def camera_log(self,level,code,message,camera_id=None,details=None):
        self.camera_logs.append({
            'timestamp':datetime.now(timezone.utc).isoformat(),
            'level':str(level),
            'code':str(code),
            'message':redact(str(message)),
            'camera_id':camera_id,
            'details':redact(details or {}),
        })

    def webcam_key(self,camera_id):
        camera=self.db.get('cameras',camera_id)
        if not camera or camera.get('source_type')!='webcam':return None
        try:
            from services.vision.camera_manager import camera_manager
            return camera_manager.webcam_key(int(camera.get('source') or (camera.get('config') or {}).get('index',0)))
        except (TypeError,ValueError):
            return None

    def subscribers(self,camera_id,change):
        value=max(0,self.camera_subscribers.get(camera_id,0)+int(change))
        self.camera_subscribers[camera_id]=value
        key=self.webcam_key(camera_id)
        if key:
            from services.vision.camera_manager import camera_manager
            camera_manager.update_subscribers(key,int(change))
        return value

    def settings(self):
        row=self.db.get('settings','main')
        try:max_storage=max(50,min(10240,int(os.environ.get('MAX_STORAGE_MB','500'))))
        except ValueError:max_storage=500
        defaults=SettingsInput(max_storage_mb=max_storage).model_dump()
        # A fresh REAL installation must look for ordinary objects without
        # requiring a marker/profile first. Keep explicit saved choices, and
        # preserve the labelled marker playback used by DEMO/TEST.
        if self.mode is RuntimeMode.REAL:
            defaults['detection_mode']='experimental'
        values={**defaults,**((row or {}).get('value') or {})}
        # Upgrade only the old release timer, preserving all user configuration.
        # Runtime construction persists these effective values once. API writes
        # validate the same five-second floor, so an old .6s setting cannot keep
        # generating early placements after a normal restart.
        try: release_seconds=float(values['hand_interaction_release_stable_seconds'])
        except (TypeError,ValueError): release_seconds=5.0
        values['hand_interaction_release_stable_seconds']=max(5.0,min(10.0,release_seconds))
        return values

    def require(self,table,record_id):
        row=self.db.get(table,record_id)
        if not row:
            raise HTTPException(404,'这条记录不存在或已经被删除。')
        return row

    def firmware_device_for_camera(self,camera):
        """Return database-backed ESP32 identity; camera config is not proof.

        A network client controls the values in a claim and an administrator can
        edit a camera JSON document.  Neither may promote a source to physical
        hardware.  Exactly one firmware row, linked by both camera and device id,
        must carry the USB-serial verification written by FirmwareService.
        """
        default={'found':False,'ambiguous':False,'device_id':None,'camera_id':str((camera or {}).get('id') or ''),'simulated':False,'hardware_verified':False,'verification_method':None,'serial_verified_at':None,'source_attestation_id':None,'stream_url':None,'capture_url':None,'attested_stream_url':None,'attested_capture_url':None,'token_revoked_at':None}
        if not isinstance(camera,dict) or camera.get('source_type')!='esp32' or not default['camera_id']:
            return default
        configured_device_id=str(((camera.get('config') or {}).get('device_id') or '')).strip()
        try:
            with self.db.connect() as connection:
                table=connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='firmware_devices'").fetchone()
                if not table:
                    return default
                columns={row[1] for row in connection.execute('PRAGMA table_info(firmware_devices)')}
                required={'id','camera_id','simulated','hardware_verified','verification_method','serial_verified_at','source_attestation_id','stream_url','capture_url','attested_stream_url','attested_capture_url','token_revoked_at'}
                if not required.issubset(columns):
                    return default
                rows=connection.execute(
                    'SELECT id,camera_id,simulated,hardware_verified,verification_method,serial_verified_at,source_attestation_id,stream_url,capture_url,attested_stream_url,attested_capture_url,token_revoked_at '
                    'FROM firmware_devices WHERE camera_id=?',
                    (default['camera_id'],),
                ).fetchall()
        except sqlite3.Error:
            return default
        if len(rows)!=1:
            return {**default,'ambiguous':len(rows)>1}
        row=dict(rows[0])
        if not configured_device_id or configured_device_id!=str(row.get('id') or ''):
            return default
        simulated=bool(row.get('simulated'))
        verified=bool(
            row.get('hardware_verified')
            and not simulated
            and row.get('verification_method')=='usb_serial_omready'
            and row.get('serial_verified_at')
            and row.get('source_attestation_id')
            and not row.get('token_revoked_at')
            and row.get('stream_url')==row.get('attested_stream_url')
            and row.get('capture_url')==row.get('attested_capture_url')
        )
        return {**default,**row,'found':True,'device_id':row['id'],'simulated':simulated,'hardware_verified':verified}

    def camera_provenance(self,camera):
        if camera.get('source_type')!='esp32':
            return provenance_for_camera(camera)
        identity=self.firmware_device_for_camera(camera)
        if identity.get('simulated') or bool((camera.get('config') or {}).get('simulated')):
            return 'virtual_esp32',True
        binding_verified=bool(
            identity.get('hardware_verified')
            and camera.get('source')==identity.get('stream_url')
            and (camera.get('config') or {}).get('capture_url')==identity.get('capture_url')
        )
        if binding_verified:
            return 'esp32_real',False
        return 'esp32_unverified',False

    @staticmethod
    def _downgrade_evidence_record(record,integrity_status,reason):
        result=dict(record)
        result['evidence_integrity_status']=integrity_status
        result['evidence_integrity_reason']=reason
        result['claimed_evidence_status']=result.get('evidence_status') or result.get('status')
        if result.get('final_status'):
            result['claimed_final_status']=result.get('final_status')
        result['evidence_status']=integrity_status
        if result.get('final_status')=='confirmed_placed':
            result['final_status']=integrity_status
        if result.get('status')=='confirmed_placed':
            result['status']='last_seen'
        return result

    def _p0_acceptance_read_integrity(self,event):
        """Revalidate persisted P0 evidence at every REAL read boundary.

        Retention and external filesystem changes happen after the atomic write,
        so a once-valid event must not remain a confirmed placement when its
        acceptance binding or any of its three active media files is gone.
        """
        event_id=str(event.get('event_id') or '').strip()
        validation_run_id=str(event.get('validation_run_id') or '').strip()
        run=self.db.get('acceptance_runs',validation_run_id) if validation_run_id else None
        if (
            not run
            or run.get('status')!='PASSED'
            or str(run.get('event_id') or '').strip()!=event_id
        ):
            return 'source_unverified','验收任务未通过或与事件标识不一致。'

        active_media=[
            row for row in self.db.list('event_media',{'event_id':event_id},limit=20)
            if row.get('status')=='active' and not row.get('pending_delete_at') and not row.get('deleted_at')
        ]
        expected={
            'before':('image',event.get('before_screenshot'),event.get('before_screenshot_sha256')),
            'after':('image',event.get('after_screenshot'),event.get('after_screenshot_sha256')),
            'clip':('clip',event.get('clip_path'),event.get('clip_sha256')),
        }
        by_role={}
        for media in active_media:
            role=str(media.get('role') or '')
            if role in by_role:
                return 'media_missing','验收事件存在重复的活动媒体角色。'
            by_role[role]=media
        if len(active_media)!=3 or set(by_role)!=set(expected):
            return 'media_missing','验收事件缺少 before、after 或 clip 活动媒体。'

        paths=[]
        for role,(kind,expected_url,expected_sha) in expected.items():
            media=by_role[role]
            media_url=media.get('path')
            media_sha=str(media.get('sha256') or '').lower()
            expected_sha=str(expected_sha or '').lower()
            if (
                media.get('kind')!=kind
                or media_url!=expected_url
                or not expected_sha
                or media_sha!=expected_sha
                or media.get('runtime_mode')!='REAL'
                or media.get('source_session_id')!=event.get('source_session_id')
            ):
                return 'media_missing',f'验收事件的 {role} 媒体登记与事件证据不一致。'
            path=self.event_service.media_path(media_url,kind)
            if path is None or not path.is_file():
                return 'media_missing',f'验收事件的 {role} 媒体缺失或路径不安全。'
            try:
                actual_sha=file_sha256(path).lower()
            except OSError:
                return 'media_missing',f'验收事件的 {role} 媒体当前无法读取。'
            if actual_sha!=expected_sha:
                return 'media_missing',f'验收事件的 {role} 媒体 SHA-256 校验失败。'
            paths.append(path)
        if len(set(paths))!=3:
            return 'media_missing','验收事件的三份媒体不是相互独立的文件。'
        return 'verified',None

    def verified_search_record(self,record):
        """Fail closed for REAL ESP32 claims not bound to current serial proof."""
        if not record or self.mode is not RuntimeMode.REAL:
            return record
        result=dict(record)
        is_confirmed=(
            result.get('event_type')=='movement'
            or result.get('evidence_status')=='confirmed'
            or result.get('final_status')=='confirmed_placed'
            or result.get('status')=='confirmed_placed'
        )
        evidence=result
        linked_evidence=False
        if is_confirmed and not result.get('event_type') and result.get('evidence_event_id'):
            linked=next(iter(self.db.list('events',{'event_id':result['evidence_event_id']},limit=1)),None)
            if linked:
                evidence=linked
                linked_evidence=True
        p0_current_state=bool(
            is_confirmed and not result.get('event_type') and result.get('validation_run_id')
        )
        p0_event=bool(
            evidence.get('detection_mode')=='aruco_screen_validation'
            and evidence.get('validation_run_id')
        )
        if p0_current_state and not linked_evidence:
            return self._downgrade_evidence_record(
                result,'source_unverified','当前状态引用的真实验收事件不存在。',
            )
        if p0_event:
            if p0_current_state and (
                str(result.get('validation_run_id'))!=str(evidence.get('validation_run_id'))
                or str(result.get('evidence_event_id'))!=str(evidence.get('event_id'))
            ):
                return self._downgrade_evidence_record(
                    result,'source_unverified','当前状态与真实验收事件绑定不一致。',
                )
            integrity_status,integrity_reason=self._p0_acceptance_read_integrity(evidence)
            if integrity_status!='verified':
                return self._downgrade_evidence_record(result,integrity_status,integrity_reason)
            result['evidence_integrity_status']='verified'
        accepted_real=(
            bool(evidence.get('manually_corrected'))
            or (
                evidence.get('source_type')=='opencv_camera'
                and evidence.get('is_simulated') is False
                and evidence.get('detection_mode')=='aruco_screen_validation'
                and bool(evidence.get('validation_run_id'))
            )
            # ESP32 authenticity is decided against the current persisted USB
            # attestation immediately below.  Do not downgrade the record
            # before that authoritative comparison has a chance to run.
            or (
                evidence.get('source_type')=='esp32_real'
                and evidence.get('is_simulated') is False
            )
        )
        if is_confirmed and not accepted_real:
            result['claimed_evidence_status']=result.get('evidence_status') or result.get('status')
            if result.get('evidence_status')=='confirmed':result['evidence_status']='source_unverified'
            if result.get('final_status')=='confirmed_placed':result['final_status']='source_unverified'
            if result.get('status')=='confirmed_placed':result['status']='last_seen'
        if result.get('source_type')!='esp32_real':
            return result
        camera_id=str(result.get('camera_id') or result.get('current_camera') or '')
        camera=self.db.get('cameras',camera_id,unscoped=True) if camera_id else None
        identity=self.firmware_device_for_camera(camera) if camera else {}
        supplied=str(result.get('source_attestation_id') or '')
        expected=str(identity.get('source_attestation_id') or '')
        if identity.get('hardware_verified') and supplied and supplied==expected:
            return result
        result['claimed_source_type']='esp32_real'
        result['source_type']='esp32_unverified'
        result['source_attestation_id']=None
        if result.get('evidence_status')=='confirmed':
            result['evidence_status']='source_unverified'
        if result.get('final_status')=='confirmed_placed':
            result['final_status']='source_unverified'
        if result.get('status')=='confirmed_placed':
            result['status']='last_seen'
        return result

    def event(self,event,frame=None,clip_frames=None,expected_engine=None):
        if not self.settings().get('record_events',True):return False
        event=dict(event or {})
        event['runtime_mode']=str(event.get('runtime_mode') or self.mode.value).strip().upper()
        for key in ('item_id','camera_id','source_session_id','movement_session_id'):
            event[key]=str(event.get(key) or '').strip()
        if expected_engine is not None:
            with self.lock:
                if self.engines.get(event.get('camera_id')) is not expected_engine:
                    return False
        camera=self.db.get('cameras',event.get('camera_id',''),unscoped=True)
        if camera:
            source_type,simulated=self.camera_provenance(camera)
            event.setdefault('runtime_mode',self.mode.value);event.setdefault('source_type',source_type);event.setdefault('is_simulated',simulated or self.mode is not RuntimeMode.REAL)
            if source_type=='esp32_real':
                event['source_attestation_id']=self.firmware_device_for_camera(camera).get('source_attestation_id')
            else:
                event.pop('source_attestation_id',None)
        engine=self.engines.get(event.get('camera_id'))
        health=engine.health() if engine else {}
        event['source_session_id']=str(event.get('source_session_id') or health.get('source_session_id') or '').strip()
        if engine and event.get('source_session_id') and event.get('source_session_id')==str(health.get('source_session_id') or '').strip():
            self.db.save('source_sessions',{
                'last_frame':health.get('source_frame_sequence'),
                'last_frame_at':now(),
                'status':'streaming' if health.get('status') not in {'error','offline','reconnecting'} else health.get('status'),
                'continuity_ok':health.get('status') not in {'error','offline','reconnecting'},
            },event['source_session_id'])
        elif engine and event.get('source_session_id'):
            event['reconnect_epoch_changed']=True
        try:
            return self.event_service.record(event,frame,clip_frames)
        except EvidenceRejected as exc:
            self.camera_log('info','EVENT_REJECTED',str(exc),(event or {}).get('camera_id'),{'item_id':(event or {}).get('item_id'),'event_type':(event or {}).get('event_type')})
            return False
        except (sqlite3.Error,OSError) as exc:
            # EventService owns compensating removal of its just-written random
            # media.  Keep the vision worker alive while exposing an explicit
            # persistence failure instead of leaving/searching a half event.
            self.camera_log('error','EVENT_PERSIST_FAILED',str(exc),(event or {}).get('camera_id'),{'item_id':(event or {}).get('item_id'),'event_type':(event or {}).get('event_type')})
            return False

    def acceptance_engine_status(self,status,expected_engine=None):
        """Mirror terminal vision truth even when no browser is polling.

        ``stop`` removes an engine generation from ``self.engines`` before it
        joins workers, so this identity check also rejects late callbacks from
        a replaced or disconnected camera generation.
        """
        if not isinstance(status,dict):
            return None
        camera_id=str(status.get('camera_id') or '').strip()
        validation_run_id=str(status.get('validation_run_id') or '').strip()
        if not camera_id or not validation_run_id:
            return None
        if expected_engine is not None and self.engines.get(camera_id) is not expected_engine:
            return None
        try:
            return self.acceptance.sync_engine_status(validation_run_id,status)
        except (AcceptanceRejected,sqlite3.Error,ValueError,TypeError) as exc:
            self.camera_log(
                'error','ACCEPTANCE_STATUS_PERSIST_FAILED',str(exc),camera_id,
                {'validation_run_id':validation_run_id,'state':status.get('state')},
            )
            return None

    def _clear_track_writes(self,camera_id,keep_session=None):
        prefix=f'{camera_id}-'
        with self.lock:
            for mapping in (self.track_writes,self.track_write_states):
                for key in list(mapping):
                    if key.startswith(prefix) and (keep_session is None or not key.endswith(f':{keep_session}')):
                        mapping.pop(key,None)

    def track(self,track,expected_engine=None):
        if not isinstance(track,dict):
            return
        track=dict(track)
        if expected_engine is not None:
            with self.lock:
                if self.engines.get(str(track.get('camera_id') or '')) is not expected_engine:
                    return
        track['last_seen']=track.get('last_seen') or track.get('timestamp') or now()
        frame_at=track.get('source_timestamp') or track.get('timestamp') or track['last_seen']
        engine=self.engines.get(track.get('camera_id'))
        health=engine.health() if engine else {}
        track['source_session_id']=str(track.get('source_session_id') or health.get('source_session_id') or '').strip()
        if track.get('source_frame') is None:
            track['source_frame']=health.get('source_frame_sequence')
        camera=self.db.get('cameras',track.get('camera_id',''),unscoped=True)
        if expected_engine is not None:
            snapshot=getattr(expected_engine,'_observation_camera_snapshot',None)
            if not camera or snapshot is None or any(camera.get(key)!=value for key,value in snapshot.items()):
                return
        if camera:
            source_type,simulated=self.camera_provenance(camera)
            track.update({'runtime_mode':self.mode.value,'source_type':source_type,'is_simulated':simulated or self.mode is not RuntimeMode.REAL})
            if source_type=='esp32_real':
                track['source_attestation_id']=self.firmware_device_for_camera(camera).get('source_attestation_id')
            else:
                track.pop('source_attestation_id',None)
            if track.get('source_session_id'):
                session_id=track['source_session_id']
                existing=self.db.get('source_sessions',session_id,unscoped=True)
                source_continuous=health.get('status') not in {'error','offline','reconnecting','disconnected'}
                if not existing:
                    self._clear_track_writes(camera['id'],keep_session=session_id)
                    raw_started=health.get('source_session_started_at')
                    if isinstance(raw_started,(int,float)):
                        session_started=datetime.fromtimestamp(float(raw_started),timezone.utc).isoformat()
                    else:
                        session_started=str(raw_started or track['last_seen'])
                    first_frame=track.get('source_frame') if isinstance(track.get('source_frame'),int) else 0
                    # A reconnect starts a new evidence timeline.  Seal every
                    # older live session for this camera before publishing the
                    # new one, so no event can span the reset frame sequence.
                    for prior in self.db.list('source_sessions',{'camera_id':camera['id']},limit=1000,unscoped=True):
                        if (
                            prior.get('id')!=session_id
                            and str(prior.get('runtime_mode') or '').upper()==self.mode.value
                            and prior.get('status') in {'streaming','reconnecting','connecting'}
                        ):
                            metadata={**(prior.get('metadata') or {}),'ended_reason':'source_session_changed','superseded_by':session_id}
                            self.db.save('source_sessions',{'ended_at':session_started,'last_frame_at':prior.get('last_frame_at') or session_started,'status':'stopped','continuity_ok':False,'metadata':metadata},prior['id'])
                    self.db.save('source_sessions',{'camera_id':camera['id'],'runtime_mode':self.mode.value,'source_type':source_type,'is_simulated':track['is_simulated'],'started_at':session_started,'first_frame':first_frame,'last_frame':track.get('source_frame'),'last_frame_at':frame_at,'status':'streaming','continuity_ok':source_continuous,'metadata':{'track_recovered':bool(track.get('recovered')),'reconnect_epoch':int(health.get('reconnect_epoch') or track.get('reconnect_epoch') or 0)}},session_id)
                else:
                    self.db.save('source_sessions',{'last_frame':track.get('source_frame'),'last_frame_at':frame_at,'status':'streaming','continuity_ok':bool(existing.get('continuity_ok')) and source_continuous,'metadata':{**(existing.get('metadata') or {}),'track_recovered':bool(track.get('recovered'))}},session_id)
        acceptance_observation=self.acceptance.observe_track(track)
        if acceptance_observation:
            acceptance_run=acceptance_observation.get('run') or {}
            run_id=acceptance_run.get('validation_run_id') or acceptance_run.get('id')
            if acceptance_observation.get('interrupted') and engine and run_id:
                try:engine.cancel_acceptance(run_id,'source_session_changed')
                except (AttributeError,KeyError,RuntimeError,ValueError):pass
            elif engine and run_id and acceptance_run.get('status')=='ACTIVE':
                try:
                    engine_status=engine.acceptance_status(run_id)
                    synchronized=self.acceptance.sync_engine_status(run_id,engine_status)
                    if synchronized.get('status')=='PASSED' and synchronized.get('trial_kind')!='movement':
                        try:engine.cancel_acceptance(run_id,'server_negative_trial_passed')
                        except (KeyError,RuntimeError,ValueError):pass
                except (AttributeError,KeyError,RuntimeError,ValueError) as exc:
                    self.camera_log('warning','ACCEPTANCE_STATUS_SYNC_FAILED',str(exc),track.get('camera_id'),{'validation_run_id':run_id})
        # Keep raw candidates available to diagnostics and P0 preflight above,
        # but only a matching live state-machine proof can publish last_seen.
        machine=getattr(expected_engine,'state_machines',{}).get(str(track.get('item_id') or '')) if expected_engine is not None else None
        verified=getattr(machine,'last_verified_observation',None)
        photo_proof=None
        if track.get('detection_mode')=='experimental' and expected_engine is not None:
            photo_proof=getattr(getattr(expected_engine,'observation_gate',None),'verified',{}).get(str(track.get('item_id') or ''))
            verified=photo_proof['motion'] if photo_proof else None
        missing=str(track.get('state') or '').upper() in {'OCCLUDED','LOST'}
        proof=bool(
            expected_engine is not None and engine is expected_engine
            and track.get('observation_verified') is True and verified is not None
            and track.get('source_session_id') == getattr(machine,'source_session_id',None)
            and track.get('source_frame') == machine.diagnostics()['last_frame']
            and str(track.get('state') or '').upper() == machine.state.value
            and track.get('observation_source_frame') == verified.source_frame
            and track.get('center') == list(verified.center_norm)
            and verified.source_timestamp is not None
            and track.get('last_seen') == datetime.fromtimestamp(verified.source_timestamp,timezone.utc).isoformat()
            and (photo_proof is not None or getattr(machine,'observation_verified',False) or missing)
        )
        if not proof:
            return
        if track.get('detection_mode')=='experimental':
            identity=track.get('identity_evidence') or {}
            live_identity=(photo_proof or {}).get('identity') or {}
            profile=self.db.get('item_recognition_profiles',str(track.get('item_id') or '')) or {}
            if not (
                identity.get('accepted') is True and live_identity.get('accepted') is True
                and identity.get('item_id')==track.get('item_id')
                and profile.get('status')=='ready'
                and profile.get('profile_version')==identity.get('profile_version')==live_identity.get('profile_version')
                and profile.get('model_version')==identity.get('model_version')==live_identity.get('model_version')
                and verified.source_frame==track.get('observation_source_frame')
                and verified.source_session_id==track.get('source_session_id')
                and expected_engine._applied_profile_revision==expected_engine._profile_revision
            ):
                return
        # Interaction truth comes from this engine's same-frame analyzer, not
        # even an otherwise-valid callback's self-reported held/release fields.
        interaction=getattr(expected_engine,'_item_interactions',{}).get(str(track.get('item_id') or '')) or {}
        if (interaction.get('source_session_id')!=track.get('source_session_id')
                or interaction.get('source_frame')!=track.get('source_frame')):
            interaction={}
        track['hand_interaction']=dict(interaction)
        track['holding_status']=interaction.get('holding_status','not_established')
        # A session switch must bypass the ordinary observation throttle so its
        # first frame creates the new source timeline immediately.
        tid=f"{track.get('camera_id','')}-{track.get('item_id','')}:{track.get('source_session_id') or 'none'}"
        observed_status=str(track.get('state') or '').upper()+':'+str(track['holding_status'])
        with self.lock:
            if self.engines.get(str(track.get('camera_id') or '')) is not expected_engine:
                return
            if self.track_write_states.get(tid)==observed_status and time.monotonic()-self.track_writes.get(tid,0)<1:
                return
            self.track_writes[tid]=time.monotonic()
            self.track_write_states[tid]=observed_status
            snapshot_reader=getattr(expected_engine,'recognition_snapshot',None)
            snapshot=snapshot_reader() if not missing and callable(snapshot_reader) else None
            jpeg=(snapshot or {}).get('jpeg') if (
                snapshot and snapshot.get('source_session_id')==track.get('source_session_id')
                and snapshot.get('source_frame')==track.get('observation_source_frame')
            ) else None
            scene=self.scenes.get(camera['id'])
            if scene and getattr(engine,'scene_requires_review',False) and scene.get('calibration_status') in {'schematic','geometry_only','calibrated','calibrated_unvalidated'}:
                self.scenes.invalidate(camera['id'],'视频源或机位变化，请重新核对场景区域。')
            state=self.event_service.observe(track,observation_verified=proof,observation_jpeg=jpeg)
        if state and track.get('source_type')=='esp32_real' and track.get('source_attestation_id'):
            self.db.bind_current_state_attestation(
                str(state.get('id') or f"{self.mode.value}:{track.get('item_id','')}"),
                camera_id=str(track.get('camera_id') or ''),
                source_session_id=str(track.get('source_session_id') or ''),
                attestation_id=str(track.get('source_attestation_id') or ''),
            )

    def camera(self,row):
        result=dict(row)
        result['source']=redact(row['source'])
        result['config']=public_camera_config(row.get('config') or {})
        # Select the public engine generation and take its health snapshot under
        # the same lock used by stop/release.  Previously camera() could retain
        # an old engine reference, release it on another thread, and then return
        # that detached generation's transient ``stalled`` health after STOPPED
        # had already been published.
        acquired=self.lock.acquire(blocking=False)
        try:
            engine=self.engines.get(row['id']) if acquired else None
            if not acquired:
                # Driver open/release may block its lifecycle worker. A read
                # must not wait behind it or claim a stale sample is live.
                health={'status':'checking','status_code':'STATE_UPDATING','fps':0,'health_snapshot_stale':True,'error':'采集连接或释放正在进行，状态暂不可用；页面会自动刷新。'}
            elif engine is not None:
                sampled_health=engine.health()
                # Keep the generation check even while holding the RLock: test
                # doubles and future callbacks may re-enter Runtime and detach
                # the engine while health() is sampled.
                if self.engines.get(row['id']) is engine and self.stopping_engines.get(row['id']) is not engine:
                    health=dict(sampled_health)
                else:
                    health=dict(self.last_camera_health.get(row['id']) or {})
            else:
                health=dict(self.last_camera_health.get(row['id']) or {})
            if not health:
                health={'status':'stopped','status_code':'STOPPED','fps':0,'latency_ms':0,'dropped_frames':0,'reconnects':0,'error':None,'width':0,'height':0}
        finally:
            if acquired:self.lock.release()
        result['health']=health
        result['health']['subscribers']=self.camera_subscribers.get(row['id'],0)
        result['health']['error']=redact(result['health'].get('error') or '')
        source_type,simulated=self.camera_provenance(row)
        result.update({'runtime_mode':self.mode.value,'source_type_provenance':source_type,'is_simulated':simulated or self.mode is not RuntimeMode.REAL})
        if row.get('source_type')=='esp32':
            identity=self.firmware_device_for_camera(row)
            binding_verified=self.camera_provenance(row)==('esp32_real',False)
            result['config'].update({
                'hardware_verified':binding_verified,
                'verification_method':identity.get('verification_method') if binding_verified else None,
                'serial_verified_at':identity.get('serial_verified_at') if binding_verified else None,
                'simulated':bool(identity.get('simulated')),
            })
            result.update({
                'hardware_verified':binding_verified,
                'hardware_type':'virtual' if identity.get('simulated') else 'physical' if binding_verified else 'unverified',
                'verification_method':identity.get('verification_method') if binding_verified else None,
                'serial_verified_at':identity.get('serial_verified_at') if binding_verified else None,
            })
        return result

    def _configure_esp32_read_auth(self,engine,camera):
        # Private adapter state only: never engine.camera/config or a DB row.
        from .firmware import camera_read_token
        with self.db.connect() as connection:
            credential=connection.execute('SELECT token_hash FROM firmware_devices WHERE id=? AND camera_id=? AND simulated=0 AND token_revoked_at IS NULL AND hardware_verified=1 AND stream_url=? AND capture_url=?',
                ((camera.get('config') or {}).get('device_id'),camera['id'],camera['source'],(camera.get('config') or {}).get('capture_url'))).fetchone()
        if not credential:raise ValueError('设备视频凭据未配置、未验证或已撤销')
        engine.source.set_camera_read_token(camera_read_token(credential['token_hash']))

    def start(self,camera_id):
        with self.lock:
            stale_engine=self.engines.get(camera_id)
            stale_engine=stale_engine if stale_engine and stale_engine.health().get('status') in {'error','stopped','ended'} else None
        if stale_engine is not None:
            self.stop(camera_id,expected_engine=stale_engine)
        with self.lock:
            if camera_id in self.stopping_engines:
                raise HTTPException(409,'摄像头仍在释放上一代采集资源，请稍后重试。')
            camera=self.require('cameras',camera_id)
            if camera.get('source_type')=='esp32':
                identity=self.firmware_device_for_camera(camera)
                allowed=identity.get('hardware_verified') if self.mode is RuntimeMode.REAL else identity.get('simulated')
                if not identity.get('found') or not allowed:
                    raise HTTPException(409,'ESP32 来源尚未验证；REAL 模式只接受本机合格 USB 串口配网后返回匹配 OMREADY 的设备。')
                if camera.get('source')!=identity.get('stream_url') or (camera.get('config') or {}).get('capture_url')!=identity.get('capture_url'):
                    raise HTTPException(409,'ESP32 摄像头地址与已验证设备记录不一致，请重新执行设备配网。')
                source_type,simulated=self.camera_provenance(camera)
                simulated=simulated or self.mode is not RuntimeMode.REAL
            else:
                try:source_type,simulated=validate_source_for_mode(self.mode,camera)
                except ValueError as exc:raise HTTPException(409,str(exc))
            self.camera_log('info','STARTING','开始连接摄像头。',camera_id,{'source_type':camera.get('source_type'),'device_index':camera.get('source') if camera.get('source_type')=='webcam' else None})
            if camera.get('source_type') in {'webcam','browser'} and self.diagnostic_running:
                self.camera_log('warning','BUSY','完整摄像头诊断正在运行，暂不启动原生摄像头。',camera_id)
                raise HTTPException(409,'摄像头诊断正在逐项读取物理设备，请等待诊断完成后再启动。')
            if not camera['enabled']:
                raise HTTPException(400,'这台摄像头已禁用，请先在摄像头设置中启用。')
            conflicting=[]
            for other_id,other in self.engines.items():
                other_type=other.camera.get('source_type')
                if other_id!=camera_id and {camera['source_type'],other_type}=={'webcam','browser'}:
                    conflicting.append(other.camera.get('name') or other_id)
            if conflicting:
                raise HTTPException(409,'摄像头正由'+conflicting[0]+'占用；请先停止它，再切换原生或浏览器直连模式。')
            if camera_id in self.engines:
                return self.camera(camera)
            from services.vision.engine import VisionEngine
            settings={**self.settings(),'data_dir':str(self.media),'reference_root':str(self.registration.folder),
                      'runtime_mode':self.mode.value,'source_type':source_type,'is_simulated':simulated}
            # Resolve source paths against the repository, never against the shell cwd.
            if camera['source_type']=='video':
                path=Path(camera['source'])
                camera['source']=str((ROOT/path if not path.is_absolute() else path).resolve())
            elif camera['source_type'] in {'rtsp','onvif','mjpeg'} and camera.get('config',{}).get('username'):
                parsed=urlsplit(camera['source'])
                if not parsed.username:
                    auth=quote(str(camera['config']['username']),safe='')+':'+quote(str(camera['config'].get('password','')),safe='')
                    camera['source']=urlunsplit((parsed.scheme,auth+'@'+parsed.netloc,parsed.path,parsed.query,parsed.fragment))
            items=self.registration.items_for_inference()
            encoder=self.registration.encoder if settings.get('detection_mode')=='experimental' else None
            engine=VisionEngine(camera,items,self.db.list('zones',{'camera_id':camera_id}),settings,self.event,self.track,appearance_encoder=encoder)
            if camera['source_type']=='esp32' and not simulated:
                try:
                    self._configure_esp32_read_auth(engine,camera)
                except (ValueError,AttributeError) as exc:
                    engine.stop()
                    raise HTTPException(409,str(exc)) from exc
            saved_scene=self.scenes.get(camera_id)
            if saved_scene:
                # Restart cannot certify an unchanged viewpoint. Keep the sketch,
                # but require a new source-bound frame and explicit confirmation.
                if saved_scene.get('calibration_status')!='needs_review':
                    self.scenes.invalidate(camera_id,'摄像头会话重启，请重新获取画面并核对区域。')
                engine.update_scene(self.db.list('zones',{'camera_id':camera_id}),confirmed=False)
            # Bind persistence callbacks to this exact engine generation.  A
            # closing browser socket removes its engine from ``self.engines``
            # before joining workers, so late callbacks from that generation
            # cannot revive its source session or mutate current item state.
            engine.on_event=lambda event,frame,clip_frames,_engine=engine:self.event(event,frame,clip_frames,expected_engine=_engine)
            engine.on_track=lambda track,_engine=engine:self.track(track,expected_engine=_engine)
            engine.on_acceptance_status=lambda status,_engine=engine:self.acceptance_engine_status(status,expected_engine=_engine)
            self.engines[camera_id]=engine
            try:
                if not engine.start():
                    raise RuntimeError(engine.health().get('error') or '没有读取到画面，请检查设备连接。')
            except Exception as exc:
                failed_health=engine.health()
                self.last_camera_health[camera_id]={**failed_health,'status':failed_health.get('status') or 'error','status_code':failed_health.get('status_code') or 'ERROR','error':redact(str(exc))}
                self.camera_log('error',self.last_camera_health[camera_id]['status_code'],str(exc),camera_id,failed_health)
                engine.stop()
                self.engines.pop(camera_id,None)
                self._clear_track_writes(camera_id)
                code=self.last_camera_health[camera_id]['status_code']
                raise HTTPException(409 if code=='BUSY' else 400,'摄像头暂时无法启动：'+redact(exc))
            self.last_camera_health.pop(camera_id,None)
            health=engine.health()
            session_id=health.get('source_session_id')
            if session_id:
                self.db.save('source_sessions',{'camera_id':camera_id,'runtime_mode':self.mode.value,'source_type':source_type,'is_simulated':simulated,'started_at':now(),'first_frame':0,'last_frame':health.get('source_frame_sequence',0),'last_frame_at':now(),'status':'streaming','continuity_ok':True,'metadata':{'reconnect_epoch':health.get('reconnect_epoch',0)}},session_id)
            self.camera_log('info','READY','采集工作线程已启动，等待前端确认连续画面。',camera_id,engine.health())
            return self.camera(camera)

    def stop(self,camera_id,expected_engine=None,preserve_acceptance_run_id=None):
        with self.lock:
            engine=self.engines.get(camera_id)
            if expected_engine is not None and engine is not expected_engine:
                return False
            if not engine:
                return False
            health=engine.health()
            if self.engines.get(camera_id) is engine:
                self.engines.pop(camera_id,None)
                self._clear_track_writes(camera_id)
            self.stopping_engines[camera_id]=engine
            self.last_camera_health[camera_id]={'status':'stopped','status_code':'STOPPED','fps':0,'latency_ms':0,'dropped_frames':health.get('dropped_frames',0),'reconnects':health.get('reconnects',0),'error':None,'width':health.get('width',0),'height':health.get('height',0),'backend':health.get('backend')}
        active_run=self.acceptance.active_for_camera(camera_id)
        if active_run and active_run.get('id')!=preserve_acceptance_run_id:
            run_id=active_run.get('validation_run_id') or active_run.get('id')
            try:engine.cancel_acceptance(run_id,'camera_stopped')
            except (AttributeError,KeyError,RuntimeError,ValueError):pass
            self.acceptance.cancel(active_run['id'],'camera_stopped',status='CAMERA_INTERRUPTED')
        session_id=health.get('source_session_id')
        final_health=health
        try:
            if session_id:
                self.db.save('source_sessions',{'last_frame':health.get('source_frame_sequence',0),'last_frame_at':now(),'status':'disconnected','continuity_ok':False},session_id)
            # Media callbacks briefly acquire Runtime.lock to prove that their
            # engine generation is still public.  Joining them while holding
            # that lock creates an unbounded stop/media-worker deadlock.
            engine.stop()
            final_health=engine.health()
            final_session=final_health.get('source_session_id') or session_id
            if final_session:
                self.db.save('source_sessions',{
                    'ended_at':now(),
                    'last_frame':final_health.get('source_frame_sequence',health.get('source_frame_sequence',0)),
                    'last_frame_at':now(),
                    'status':'stopped',
                    'continuity_ok':False,
                },final_session)
        finally:
            with self.lock:
                if self.stopping_engines.get(camera_id) is engine:
                    self.stopping_engines.pop(camera_id,None)
                if camera_id not in self.engines:
                    self.last_camera_health[camera_id]={
                        **self.last_camera_health[camera_id],
                        'dropped_frames':final_health.get('dropped_frames',health.get('dropped_frames',0)),
                        'reconnects':final_health.get('reconnects',health.get('reconnects',0)),
                    }
        self.camera_log('info','STOPPED','摄像头已停止并释放采集资源。',camera_id,self.last_camera_health.get(camera_id,{}))
        return True

    def acquire_browser_sender(self,camera_id):
        """Start a browser engine and bind exactly one sender to its generation."""
        with self.lock:
            self.start(camera_id)
            engine=self.engines.get(camera_id)
            source=getattr(engine,'source',None)
            from services.vision.camera_sources import BrowserCameraSource
            if not isinstance(source,BrowserCameraSource):
                raise RuntimeError('浏览器摄像头采集服务未就绪。')
            sender_token=source.sender_connected()
            return engine,source,sender_token

    def begin_browser_sender_release(self,camera_id,expected_engine,source,sender_token):
        """Atomically detach one browser sender and publish terminal health.

        This phase deliberately avoids worker joins so the WebSocket task can run
        it before scheduling those joins in a thread pool.  Under executor
        pressure the old implementation left the disconnected generation public
        long enough for camera() to report it as ``stalled``.
        """
        with self.lock:
            current=self.engines.get(camera_id)
            if current is not expected_engine or getattr(current,'source',None) is not source:
                source.sender_disconnected(sender_token)
                return None
            if not source.sender_disconnected(sender_token):
                return None
            # Publish the transport as stopped before waiting for worker joins.
            # A browser read may be blocked for up to its bounded frame timeout;
            # leaving the engine in the public map during that join produced a
            # flaky `stalled` state after the WebSocket had already closed.
            health=expected_engine.health()
            self.engines.pop(camera_id,None)
            self._clear_track_writes(camera_id)
            self.stopping_engines[camera_id]=expected_engine
            self.last_camera_health[camera_id]={'status':'stopped','status_code':'STOPPED','fps':0,'latency_ms':0,'dropped_frames':health.get('dropped_frames',0),'reconnects':health.get('reconnects',0),'error':None,'width':health.get('width',0),'height':health.get('height',0),'backend':health.get('backend')}
            session_id=health.get('source_session_id')
            if session_id:
                # The sender lease is now revoked and this engine generation is
                # no longer public, so its evidence timeline has logically
                # ended even if native worker joins take a few more seconds.
                # Persist that fact in this synchronous phase: an ASGI task may
                # be cancelled immediately after websocket.disconnect.
                ended_at=now()
                self.db.save('source_sessions',{
                    'ended_at':ended_at,
                    'last_frame':health.get('source_frame_sequence',0),
                    'last_frame_at':ended_at,
                    'status':'stopped',
                    'continuity_ok':False,
                },session_id)
            return expected_engine,dict(health)

    def finish_browser_sender_release(self,camera_id,expected_engine,health):
        """Join a browser generation already detached by begin_*()."""
        # Do not hold Runtime.lock while worker joins complete.  The stopping
        # registry prevents a replacement from starting until the old source is
        # fully disconnected, while camera() consistently serves the published
        # STOPPED snapshot during that bounded interval.
        final_health=health
        try:
            self.camera_log('info','STOPPED','浏览器发送连接已关闭，正在释放采集资源。',camera_id,self.last_camera_health.get(camera_id,{}))
            expected_engine.stop()
            final_health=expected_engine.health()
            # Only finalize the session captured by begin_*().  A late cleanup
            # from an old socket must never target a replacement generation.
            final_session=health.get('source_session_id')
            if final_session:
                self.db.save('source_sessions',{
                    'last_frame':final_health.get('source_frame_sequence',health.get('source_frame_sequence',0)),
                    'last_frame_at':now(),
                    'status':'stopped',
                    'continuity_ok':False,
                },final_session)
        finally:
            with self.lock:
                if self.stopping_engines.get(camera_id) is expected_engine:
                    self.stopping_engines.pop(camera_id,None)
                # Never let an old generation overwrite a replacement's public
                # health.  start() is normally blocked by stopping_engines, but
                # this identity check also protects future lifecycle changes.
                if camera_id not in self.engines:
                    self.last_camera_health[camera_id]={
                        **self.last_camera_health.get(camera_id,{}),
                        'status':'stopped',
                        'status_code':'STOPPED',
                        'fps':0,
                        'latency_ms':0,
                        'error':None,
                        'dropped_frames':final_health.get('dropped_frames',health.get('dropped_frames',0)),
                        'reconnects':final_health.get('reconnects',health.get('reconnects',0)),
                    }
        return True

    def release_browser_sender(self,camera_id,expected_engine,source,sender_token):
        """Synchronous release used outside the WebSocket coroutine."""
        release=self.begin_browser_sender_release(camera_id,expected_engine,source,sender_token)
        if release is None:
            return False
        engine,health=release
        return self.finish_browser_sender_release(camera_id,engine,health)

    def restart(self,camera_id):
        self.stop(camera_id)
        return self.start(camera_id)

    def acceptance_preflight(self,camera_id):
        with self.lock:
            engine=self.engines.get(camera_id)
            health=dict(engine.health()) if engine else dict(self.last_camera_health.get(camera_id) or {})
        return self.acceptance.preflight(camera_id,health)

    def _wait_acceptance_preflight(self,camera_id,timeout_seconds=10.0):
        deadline=time.monotonic()+max(0.1,float(timeout_seconds))
        latest=self.acceptance_preflight(camera_id)
        while not latest.get('ready') and time.monotonic()<deadline:
            time.sleep(0.05)
            latest=self.acceptance_preflight(camera_id)
        return latest

    def acceptance_status(self,validation_run_id):
        run=self.acceptance.require(validation_run_id)
        engine_status=None
        with self.lock:
            engine=self.engines.get(run['camera_id'])
        if engine and run.get('status')=='ACTIVE':
            try:engine_status=engine.acceptance_status(run['validation_run_id'])
            except (AttributeError,KeyError,RuntimeError,ValueError) as exc:
                self.camera_log('warning','ACCEPTANCE_STATUS_READ_FAILED',str(exc),run['camera_id'],{'validation_run_id':run['validation_run_id']})
            if engine_status is not None:
                run=self.acceptance.sync_engine_status(run['id'],engine_status)
        return run,engine_status

    def start_acceptance(self,validation_run_id):
        run=self.acceptance.require(validation_run_id)
        if run.get('status')!='CREATED':
            raise AcceptanceRejected('只有尚未开始的验收任务可以启动。')
        # Always rebuild the engine after marker binding so item_by_marker and
        # the source-session generation are both fresh before arming.
        self.restart(run['camera_id'])
        preflight=self._wait_acceptance_preflight(run['camera_id'])
        if not preflight.get('ready'):
            self.acceptance.cancel(run['id'],'camera_preflight_not_ready',status='FAILED')
            raise AcceptanceRejected('摄像头重建后没有形成连续真实帧，验收未启动。')
        activated=self.acceptance.activate(run['id'],preflight)
        with self.lock:
            engine=self.engines.get(run['camera_id'])
        if not engine or not all(callable(getattr(engine,name,None)) for name in ('arm_acceptance','cancel_acceptance','acceptance_status')):
            self.acceptance.cancel(run['id'],'vision_acceptance_contract_unavailable',status='FAILED')
            raise AcceptanceRejected('当前视觉服务不支持真实验收合同。')
        try:
            engine_status=engine.arm_acceptance(self.acceptance.arm_config(activated))
            synchronized=self.acceptance.sync_engine_status(run['id'],engine_status)
        except Exception as exc:
            try:engine.cancel_acceptance(run['id'],'arm_failed')
            except Exception:pass
            self.acceptance.cancel(run['id'],'vision_arm_failed:'+redact(exc),status='FAILED')
            raise AcceptanceRejected('视觉验收无法安全启动：'+redact(exc)) from exc
        return synchronized,engine_status,preflight

    def controlled_acceptance_reconnect(self,validation_run_id):
        run=self.acceptance.begin_disconnect_reconnect(validation_run_id)
        camera_id=run['camera_id']
        with self.lock:
            engine=self.engines.get(camera_id)
        if not engine:
            self.acceptance.cancel(run['id'],'camera_not_running_before_controlled_disconnect',status='FAILED')
            raise AcceptanceRejected('受控断开前摄像头并未运行。')
        try:engine.cancel_acceptance(run['id'],'controlled_disconnect')
        except (AttributeError,KeyError,RuntimeError,ValueError):pass
        self.stop(camera_id,preserve_acceptance_run_id=run['id'])
        try:
            self.start(camera_id)
            preflight=self._wait_acceptance_preflight(camera_id)
            rebound=self.acceptance.rebind_after_reconnect(run['id'],preflight)
            with self.lock:
                replacement=self.engines.get(camera_id)
            if not replacement or not callable(getattr(replacement,'arm_acceptance',None)):
                raise AcceptanceRejected('重连后的视觉服务不支持真实验收合同。')
            engine_status=replacement.arm_acceptance(self.acceptance.arm_config(rebound))
            synchronized=self.acceptance.sync_engine_status(run['id'],engine_status)
            return synchronized,engine_status,preflight
        except Exception as exc:
            self.acceptance.cancel(run['id'],'controlled_reconnect_failed:'+redact(exc),status='FAILED')
            raise AcceptanceRejected('摄像头受控重连失败：'+redact(exc)) from exc

    def stop_all(self):
        for camera_id in list(self.engines):
            self.stop(camera_id)

    def bind_device(self,device):
        device_id=device.get('device_id') or device.get('id')
        camera_id=device.get('camera_id') or f'cam_{device_id}'
        existing=self.db.get('cameras',camera_id)
        simulated=bool(device.get('simulated'))
        if simulated and self.mode is RuntimeMode.REAL:
            raise HTTPException(409,'REAL 模式禁止绑定虚拟 ESP32；请重启到 DEMO 模式。')
        identity=self.firmware_device_for_camera({'id':camera_id,'source_type':'esp32','config':{'device_id':device_id}})
        allowed=identity.get('hardware_verified') if self.mode is RuntimeMode.REAL else identity.get('simulated')
        if not identity.get('found') or not allowed or bool(identity.get('simulated'))!=simulated:
            raise HTTPException(409,'ESP32 设备身份尚未由当前运行模式的可信流程验证，拒绝绑定摄像头。')
        camera={'name':device.get('device_name') or device.get('name') or 'ESP32 摄像头','room_name':device.get('room_name') or '客厅','installation':'虚拟 ESP32-CAM' if simulated else 'ESP32-CAM（USB 串口已验证）','source_type':'esp32','source':identity['stream_url'],'config':{**public_camera_config(device.get('config') or {}),'capture_url':identity['capture_url'],'device_id':identity['device_id'],'simulated':simulated,'hardware_verified':bool(identity.get('hardware_verified')),'verification_method':identity.get('verification_method'),'serial_verified_at':identity.get('serial_verified_at')},'enabled':True,'inference_fps':5,'save_clips':True,'runtime_mode':self.mode.value}
        self.db.save('cameras',camera,camera_id)
        if existing:
            self.stop(camera_id)
        self.start(camera_id)
        return camera_id


def create_app(data_dir=None,testing=False,runtime_mode=None,listener_info_provider=None):
    mode=resolve_runtime_mode(testing=testing,explicit=runtime_mode)
    layout=runtime_layout(ROOT,data_dir,mode)
    mode_lease=RuntimeModeLease(layout.data_root,mode.value)
    mode_lease.acquire()
    try:
        return _build_app(layout,testing,mode,mode_lease,listener_info_provider)
    except BaseException:
        # Factory failures must never strand the mode lease.  Successful app
        # objects retain it until lifespan shutdown (or an explicit close by a
        # constructor-only caller such as init-project.py).
        mode_lease.release()

        raise


def _build_app(layout,testing,mode,mode_lease,listener_info_provider=None):
    runtime=Runtime(layout,testing)
    runtime.mode_lease=mode_lease

    @asynccontextmanager
    async def lifespan(app):
        # Reusing the same FastAPI object in a later server/TestClient lifetime
        # must reacquire the lease released by the previous shutdown.
        runtime.mode_lease.acquire()
        runtime.shutdown_requested.clear()
        async def maintain_retention():
            while True:
                try:await asyncio.to_thread(runtime.retention.cleanup,trigger='scheduled',dry_run=False)
                except (OSError,sqlite3.Error):pass
                await asyncio.sleep(1800)
        maintenance=asyncio.create_task(maintain_retention()) if not testing else None
        try:
            await asyncio.to_thread(runtime.retention.cleanup,trigger='startup',dry_run=False)
            yield
        finally:
            runtime.shutdown_requested.set()
            runtime.diagnostic_cancel.set()
            if runtime.diagnostic_task is not None:
                # The worker owns bounded, cancellable probe children. Wait for
                # their release before relinquishing this runtime's data lease.
                with contextlib.suppress(Exception):await runtime.diagnostic_task
            cleanup_finished=threading.Event()
            firmware_shutdown_complete=False

            def release_after_firmware_drain():
                # A non-cancellable build/serial worker may outlive the bounded
                # lifespan wait.  Keep the mode lease until its final database
                # write is complete, then stop any camera it may have bound.
                cleanup_finished.wait()
                try:runtime.stop_all()
                finally:runtime.mode_lease.release()

            try:
                if maintenance:
                    maintenance.cancel()
                    with contextlib.suppress(asyncio.CancelledError):await maintenance
                service=getattr(app.state,'firmware_service',None)
                if service:
                    await asyncio.to_thread(service.shutdown,on_drained=release_after_firmware_drain)
                firmware_shutdown_complete=True
            finally:
                try:
                    await asyncio.to_thread(runtime.stop_all)
                    with runtime.lock:
                        runtime.screen_grants.clear()
                    if firmware_shutdown_complete:
                        with contextlib.suppress(OSError,sqlite3.Error):await asyncio.to_thread(runtime.retention.cleanup,trigger='shutdown',dry_run=False)
                finally:
                    cleanup_finished.set()
                    if firmware_shutdown_complete:
                        runtime.mode_lease.release()

    app=FastAPI(title='物忆 ObjectMemory',version=VERSION,lifespan=lifespan,docs_url='/api/docs' if testing else None,redoc_url=None)
    app.state.runtime=runtime
    app.add_middleware(LocalSecurityMiddleware,sessions=runtime.sessions)

    def response_meta(source_type='service',simulated=None):
        return {'runtime_mode':runtime.mode.value,'source_type':source_type,'is_simulated':runtime.mode is not RuntimeMode.REAL if simulated is None else bool(simulated)}

    def acceptance_response(record:dict, response_source_type:str='physical_acceptance_control'):
        """Add transport provenance without overwriting physical source provenance."""
        return {
            **response_meta(response_source_type,False),
            **record,
            'response_source_type':response_source_type,
        }

    @app.exception_handler(RequestValidationError)
    async def validation_error(request,exc):
        errors=['.'.join(str(p) for p in e['loc'][1:])+': '+e['msg'] for e in exc.errors()]
        return JSONResponse({'detail':'输入信息不完整或格式不正确。'+'；'.join(errors)},422)

    @app.exception_handler(ValidationError)
    async def model_error(request,exc):
        return JSONResponse({'detail':'修改的信息格式不正确，请检查名称、坐标和参数范围。'},422)

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict_error(request,exc):
        return JSONResponse({'detail':'这个标签编号已绑定其他物品，请选择不同编号。'},409)

    @app.exception_handler(AcceptanceRejected)
    async def acceptance_error(request,exc):
        return JSONResponse({'detail':str(exc),**response_meta('acceptance_control')},409)

    @app.exception_handler(RegistrationError)
    async def registration_error(request,exc):
        return JSONResponse({'detail':str(exc),**response_meta('recognition_profile')},422)

    @app.exception_handler(SceneError)
    async def scene_error(request,exc):
        return JSONResponse({'detail':str(exc),**response_meta('scene_configuration')},exc.status_code)

    @app.get('/api/health')
    def health():
        cameras=runtime.db.list('cameras')
        statuses=[runtime.camera(c)['health'] for c in cameras]
        events=runtime.db.list('events',limit=10000)
        today=datetime.now().astimezone().date()
        today_count=sum(datetime.fromisoformat(e['timestamp_start'].replace('Z','+00:00')).astimezone().date()==today for e in events)
        return {'status':'ok','version':VERSION,'local_processing':True,'runtime_mode':runtime.mode.value,'source_type':'service','is_simulated':runtime.mode is not RuntimeMode.REAL,'stats':{'cameras':len(cameras),'online_cameras':sum(s.get('status') in {'ready','running','online'} for s in statuses),'items':len(runtime.db.list('items')),'events':len(events),'today_events':today_count,'fps':round(sum(s.get('fps',0) or 0 for s in statuses),1),'latency_ms':round(max([s.get('latency_ms',0) or 0 for s in statuses] or [0]),1)}}

    @app.get('/api/runtime-config')
    def runtime_config(request:Request):
        scheme=request.url.scheme
        host=request.headers.get('host') or request.url.netloc
        ws_scheme='wss' if scheme=='https' else 'ws'
        return {'frontend_url':f'{scheme}://{host}','api_base_url':f'{scheme}://{host}/api','websocket_base_url':f'{ws_scheme}://{host}/ws','backend_healthy':True,'backend_version':VERSION,'mode':runtime.mode.value,'runtime_mode':runtime.mode.value,'source_type':'service','is_simulated':runtime.mode is not RuntimeMode.REAL,'api_port':int(os.environ.get('OM_PORT','8018'))}

    @app.post('/api/acceptance/items/{item_id}/marker')
    def bind_acceptance_marker(item_id:str,body:dict|None=None):
        body=body or {}
        if set(body)-{'aruco_id'}:
            raise AcceptanceRejected('标记绑定请求包含未知字段。')
        item=runtime.acceptance.bind_marker(item_id,body.get('aruco_id'))
        for camera_id in list(runtime.engines):
            camera=runtime.db.get('cameras',camera_id,unscoped=True)
            if camera and camera.get('source_type')=='webcam':
                runtime.restart(camera_id)
        return acceptance_response(
            {**item,'marker_url':f"/api/acceptance/markers/{item['aruco_id']}.png"},
            'server_bound_aruco_marker',
        )

    @app.get('/api/acceptance/markers/{aruco_id}.png')
    def acceptance_marker_png(aruco_id:int,size:int=Query(1000,ge=400,le=1600)):
        return Response(runtime.acceptance.marker_png(aruco_id,size),media_type='image/png',headers={'Cache-Control':'no-store'})

    @app.post('/api/acceptance/suites')
    def create_acceptance_suite(body:dict):
        suite=runtime.acceptance.create_suite(body)
        return acceptance_response(runtime.acceptance.suite_summary(suite['id']))

    @app.get('/api/acceptance/suites')
    def list_acceptance_suites(limit:int=Query(100,ge=1,le=1000)):
        return [acceptance_response(suite) for suite in runtime.acceptance.list_suites(limit=limit)]

    @app.get('/api/acceptance/suites/{suite_id}')
    def get_acceptance_suite(suite_id:str):
        return acceptance_response(runtime.acceptance.suite_summary(suite_id))

    @app.post('/api/acceptance/runs')
    def create_acceptance_run(body:dict):
        run=runtime.acceptance.create(body)
        return acceptance_response(run)

    @app.get('/api/acceptance/runs')
    def list_acceptance_runs(validation_run_id:str|None=None,suite_id:str|None=None,status:str|None=None,limit:int=Query(100,ge=1,le=1000)):
        return [
            acceptance_response(row)
            for row in runtime.acceptance.list(validation_run_id=validation_run_id,suite_id=suite_id,status=status,limit=limit)
        ]

    @app.get('/api/acceptance/runs/{validation_run_id}')
    def get_acceptance_run(validation_run_id:str):
        run,engine_status=runtime.acceptance_status(validation_run_id)
        return acceptance_response({**run,'engine_status':engine_status or run.get('engine_status')})

    @app.post('/api/acceptance/runs/{validation_run_id}/start')
    def start_acceptance_run(validation_run_id:str,body:dict|None=None):
        if body:
            raise AcceptanceRejected('开始验收接口不接受客户端结果或额外字段。')
        run,engine_status,preflight=runtime.start_acceptance(validation_run_id)
        return acceptance_response({**run,'engine_status':engine_status,'preflight':preflight})

    @app.post('/api/acceptance/runs/{validation_run_id}/cancel')
    def cancel_acceptance_run(validation_run_id:str,body:dict|None=None):
        body=body or {}
        if set(body)-{'reason'}:
            raise AcceptanceRejected('取消验收请求包含未知字段。')
        run=runtime.acceptance.require(validation_run_id)
        with runtime.lock:
            engine=runtime.engines.get(run['camera_id'])
        if engine and run.get('status')=='ACTIVE':
            try:engine.cancel_acceptance(run['id'],str(body.get('reason') or 'user_cancelled'))
            except (AttributeError,KeyError,RuntimeError,ValueError):pass
        cancelled=runtime.acceptance.cancel(run['id'],str(body.get('reason') or 'user_cancelled'))
        return acceptance_response(cancelled)

    @app.post('/api/acceptance/runs/{validation_run_id}/disconnect-reconnect')
    def disconnect_reconnect_acceptance_run(validation_run_id:str,body:dict|None=None):
        if body:
            raise AcceptanceRejected('受控断开重连接口不接受客户端结果或额外字段。')
        run,engine_status,preflight=runtime.controlled_acceptance_reconnect(validation_run_id)
        return acceptance_response({**run,'engine_status':engine_status,'preflight':preflight})

    @app.get('/api/acceptance/preflight')
    def acceptance_preflight(camera_id:str):
        result=runtime.acceptance_preflight(camera_id)
        service=getattr(app.state,'firmware_service',None)
        listener=service.listener_status() if service is not None else {}
        # Only this process's verified listening sockets may advertise a phone
        # address. A request Host, query parameter or configured port is not proof.
        candidates=listener.get('lan_urls',[]) if listener.get('listener_verified') is True and listener.get('lan_enabled') is True else []
        result['companion_url']=(candidates[0].rstrip('/')+'/companion/validation-marker') if candidates else None
        result['companion_access_status']='candidate_device_reachability_unverified' if candidates else 'localhost_only_or_unverified'
        return acceptance_response(result,'physical_acceptance_preflight')

    @app.get('/api/acceptance/marker-status')
    def acceptance_marker_status(validation_run_id:str|None=None,item_id:str|None=None,camera_id:str|None=None):
        engine_status=None
        if validation_run_id:
            run,engine_status=runtime.acceptance_status(validation_run_id)
            item_id=item_id or run['item_id']
            camera_id=camera_id or run['camera_id']
        if not item_id or not camera_id:
            raise AcceptanceRejected('请提供 validation_run_id，或同时提供 item_id 和 camera_id。')
        with runtime.lock:
            engine=runtime.engines.get(camera_id)
            health=dict(engine.health()) if engine else dict(runtime.last_camera_health.get(camera_id) or {})
        return runtime.acceptance.marker_status(
            item_id=item_id,camera_id=camera_id,engine_health=health,
            validation_run_id=validation_run_id,engine_status=engine_status,
        )

    def camera_diagnostics_payload():
        report_path=runtime.data/'diagnostics'/'camera-report.json'
        report=None
        report_error=None
        if report_path.is_file():
            try:report=json.loads(report_path.read_text(encoding='utf-8'))
            except (OSError,ValueError) as exc:report_error='诊断报告无法读取：'+str(exc)
        from services.vision.camera_manager import camera_manager
        return {
            'report':report,
            'report_error':report_error,
            'report_path':str(report_path),
            'report_url':'/api/camera-diagnostics/report' if report_path.is_file() else None,
            'probe_image_url':'/api/camera-diagnostics/probe-image' if (runtime.data/'diagnostics'/'camera-probe.jpg').is_file() else None,
            'logs':list(runtime.camera_logs),
            'owners':camera_manager.snapshot(),
            'running_cameras':[runtime.camera(row) for row in runtime.db.list('cameras') if row['id'] in runtime.engines],
            'job':dict(runtime.diagnostic_job) if runtime.diagnostic_job else None,
            'diagnostic_running':runtime.diagnostic_running,
            'report_is_previous':runtime.diagnostic_running,
            **response_meta('camera_diagnostics'),
        }

    @app.get('/api/camera-diagnostics')
    def camera_diagnostics():
        return camera_diagnostics_payload()

    @app.get('/api/camera-diagnostics/report')
    def camera_diagnostics_report():
        path=runtime.data/'diagnostics'/'camera-report.json'
        if not path.is_file():raise HTTPException(404,'尚未生成摄像头诊断报告。')
        return FileResponse(path,media_type='application/json',filename='camera-report.json')

    @app.get('/api/camera-diagnostics/probe-image')
    def camera_diagnostics_probe_image():
        path=runtime.data/'diagnostics'/'camera-probe.jpg'
        if not path.is_file():raise HTTPException(404,'本次诊断没有读取到真实摄像头截图。')
        return FileResponse(path,media_type='image/jpeg',headers={'Cache-Control':'no-store'})

    @app.post('/api/camera-diagnostics/run')
    async def run_camera_diagnostics(request:Request,response:Response,body:CameraDiagnosticInput|None=None):
        if not runtime.sessions.local(request):
            raise HTTPException(403,'摄像头诊断会读取这台电脑的物理摄像头，请在运行物忆的电脑上操作。')
        options=body or CameraDiagnosticInput()
        try:
            from scripts.camera_diagnose import parse_indices,run_diagnostics
            parsed_indices=parse_indices(options.indices)
            if any(index>16 for index in parsed_indices):raise ValueError('页面诊断的摄像头编号必须为 0 到 16。')
        except (TypeError,ValueError) as exc:
            raise HTTPException(422,'诊断参数无效：'+str(exc))
        if not runtime.diagnostic_lock.acquire(blocking=False):
            raise HTTPException(409,'另一个完整摄像头诊断正在运行，请等待它完成。')
        if not runtime.lock.acquire(blocking=False):
            runtime.diagnostic_lock.release()
            raise HTTPException(409,'摄像头正在连接或释放资源，请稍后刷新状态；不会再启动一组探测。')
        try:
            active=[row for row in runtime.db.list('cameras') if row.get('source_type') in {'webcam','browser'} and row['id'] in runtime.engines]
            if active:
                runtime.diagnostic_lock.release()
                if options.profile=='quick':
                    return {**camera_diagnostics_payload(),'status':'LIVE_STATUS','message':'已读取当前采集状态，未重新打开摄像头。完整索引探测需要先手动停止正在使用的电脑摄像头。'}
                raise HTTPException(409,'请先停止正在运行的电脑摄像头，再执行完整诊断；诊断不会抢占或强行关闭设备。')
            runtime.diagnostic_running=True
            runtime.diagnostic_cancel=threading.Event()
            runtime.diagnostic_job={'id':uuid4().hex,'status':'RUNNING','started_at':now(),'completed':0,'total':len(parsed_indices)*3,'indices':parsed_indices,'message':'正在读取设备清单；不会抢占其他采集任务。','overall_timeout_seconds':options.overall_timeout_seconds}
        finally:
            runtime.lock.release()
        runtime.camera_log('info','DIAGNOSTIC_STARTED','开始有界摄像头诊断。',details={'indices':parsed_indices,'frames':options.frames})
        output=runtime.data/'diagnostics'/'camera-report.json'
        screenshot=runtime.data/'diagnostics'/'camera-probe.jpg'
        def progress(value):
            with runtime.lock:runtime.diagnostic_job.update(value)
        async def worker():
            try:
                report=await asyncio.to_thread(run_diagnostics,parsed_indices,options.frames,output,screenshot,options.timeout_seconds,cancel_event=runtime.diagnostic_cancel,progress=progress,overall_timeout=options.overall_timeout_seconds,quick=options.profile=='quick')
                summary=report.get('summary') or {}
                status=report.get('status') or 'COMPLETED'
                progress({'status':status,'ended_at':now(),'message':summary.get('message') or '诊断结束。'})
                runtime.camera_log('info' if summary.get('status_code')=='READY' else 'warning',summary.get('status_code') or status,summary.get('message') or '摄像头诊断结束。')
            except Exception as exc:
                progress({'status':'FAILED','ended_at':now(),'message':'诊断失败：'+redact(str(exc))})
                runtime.camera_log('error','DIAGNOSTIC_FAILED',redact(str(exc)))
            finally:
                with runtime.lock:runtime.diagnostic_running=False
                runtime.diagnostic_lock.release()
        runtime.diagnostic_task=asyncio.create_task(worker())
        response.status_code=202
        return camera_diagnostics_payload()

    @app.post('/api/camera-diagnostics/cancel')
    def cancel_camera_diagnostics(request:Request,body:dict|None=None):
        if not runtime.sessions.local(request):raise HTTPException(403,'请在本机取消摄像头诊断。')
        with runtime.lock:
            job=runtime.diagnostic_job
            if body and body.get('job_id') and (not job or body['job_id']!=job['id']):
                raise HTTPException(409,'诊断任务已变化，请刷新后重试。')
            if runtime.diagnostic_running:
                runtime.diagnostic_cancel.set()
                job.update({'status':'CANCELLING','message':'正在取消；仅终止本次诊断自己创建的探测进程并释放设备。'})
        return camera_diagnostics_payload()

    @app.get('/api/session')
    def session(request:Request,response:Response):
        authenticated=runtime.sessions.valid(request.cookies.get('om_session'))
        host=request.url.hostname
        local=runtime.sessions.local(request) and (host in lan_addresses() or testing)
        if local and not authenticated:
            response.set_cookie('om_session',runtime.sessions.issue(),httponly=True,samesite='strict',max_age=86400)
            authenticated=True
        service=getattr(app.state,'firmware_service',None)
        listener=service.listener_status() if service is not None else {'lan_enabled':False,'listener_verified':False,'lan_urls':[],'lan_url_status':'unverified'}
        if not authenticated:listener={**listener,'lan_urls':[],'listener_bindings':[]}
        return {'authenticated':authenticated,'local':local,'pairing_pin':runtime.sessions.pin if local else None,**listener,**response_meta('admin_session')}

    @app.post('/api/session')
    async def pair_session(request:Request,response:Response):
        body=await request.json()
        runtime.sessions.verify_pin(request.client.host,str(body.get('pin','')))
        response.set_cookie('om_session',runtime.sessions.issue(),httponly=True,samesite='strict',max_age=86400)
        return {'authenticated':True,**response_meta('admin_session')}

    @app.get('/api/cameras')
    def cameras():
        return [runtime.camera(c) for c in runtime.db.list('cameras')]

    @app.post('/api/camera-discovery/onvif')
    def discover_cameras(body:dict):
        if body.get('authorized') is not True:
            raise HTTPException(422,'请主动确认只搜索你有权管理的局域网摄像头。')
        from .onvif import discover
        try:return {'devices':discover(),'message':'搜索完成。没有发现时可手动输入已知 ONVIF 或 RTSP 地址。',**response_meta('onvif_discovery')}
        except OSError:raise HTTPException(400,'局域网发现暂不可用，请检查网络连接或使用已知摄像头地址。')

    @app.post('/api/camera-discovery/onvif/resolve')
    def resolve_camera(body:dict):
        from .onvif import get_stream_uri
        address=str(body.get('address',''))
        username=str(body.get('username',''))
        password=str(body.get('password',''))
        if not username or len(username)>128 or len(password)>256:raise HTTPException(422,'请输入这台摄像头的合法管理员账号。')
        return {'stream_uri':get_stream_uri(address,username,password),'message':'已获取标准视频流地址。',**response_meta('onvif')}

    def validate_region(region):
        if not isinstance(region,dict) or any(type(region.get(k)) is not int for k in ['left','top','width','height']) or not 32<=region['width']<=7680 or not 32<=region['height']<=4320:
            raise HTTPException(422,'请填写有效的屏幕矩形：左、上、宽、高；最小尺寸为 32 像素。')
        return {k:region[k] for k in ['left','top','width','height']}

    @app.post('/api/screen-capture/authorize')
    def authorize_screen(request:Request,body:dict):
        if not runtime.sessions.local(request):
            raise HTTPException(403,'屏幕采集必须由这台电脑上的用户选择区域，请在运行物忆的电脑打开 127.0.0.1 地址操作。')
        region=validate_region(body.get('region'))
        with runtime.lock:
            runtime.screen_grants={k:v for k,v in runtime.screen_grants.items() if v['expires']>time.monotonic()}
            if len(runtime.screen_grants)>=128:raise HTTPException(429,'授权请求太多，请稍后重试。')
            grant=uuid4().hex
            runtime.screen_grants[grant]={'region':region,'expires':time.monotonic()+120,'session':request.cookies.get('om_session')}
        return {'capture_grant':grant,'expires_in':120,'region':region,**response_meta('authorized_screen_capture')}

    def consume_screen_grant(request:Request,region,grant_token):
        rect=validate_region(region)
        with runtime.lock:
            grant=runtime.screen_grants.pop(str(grant_token or ''),None)
        if not runtime.sessions.local(request) or not grant or grant['expires']<time.monotonic() or grant['region']!=rect or grant['session']!=request.cookies.get('om_session'):
            raise HTTPException(403,'请在本机重新选择并授权这个屏幕区域；每次启动或测试都需要新的单次授权。')
        return rect

    def validate_camera(data,request,existing=None):
        data['runtime_mode']=runtime.mode.value
        data['config']=dict(data.get('config') or {})
        for key in ('hardware_verified','verification_method','serial_verified_at'):
            # These fields are outputs of the firmware registry, never client
            # assertions accepted by the general camera CRUD API.
            data['config'].pop(key,None)
        if runtime.mode is RuntimeMode.REAL and (data['source_type']=='video' or bool((data.get('config') or {}).get('simulated'))):
            raise HTTPException(409,'REAL 模式禁止测试视频和模拟摄像头；请重启到 DEMO 模式。')
        if data['source_type']=='esp32':
            if not existing or existing.get('source_type')!='esp32':
                raise HTTPException(409,'ESP32 摄像头不能手工声明为物理设备；请从设备中心使用本机 USB 串口安装或配网。')
            identity=runtime.firmware_device_for_camera(existing)
            allowed=identity.get('hardware_verified') if runtime.mode is RuntimeMode.REAL else identity.get('simulated')
            if not identity.get('found') or not allowed:
                raise HTTPException(409,'该 ESP32 来源尚未通过当前运行模式要求的设备验证，不能修改或启动。')
            if data['source']!=identity.get('stream_url') or data['config'].get('capture_url')!=identity.get('capture_url') or data['config'].get('device_id')!=identity.get('device_id'):
                raise HTTPException(409,'ESP32 的设备编号和视频地址由已验证设备记录管理，不能从普通摄像头接口修改。')
            data['config'].update({
                'hardware_verified':bool(identity.get('hardware_verified')),
                'verification_method':identity.get('verification_method'),
                'serial_verified_at':identity.get('serial_verified_at'),
                'simulated':bool(identity.get('simulated')),
            })
            local_url(data['source'])
        elif data['source_type'] in {'rtsp','mjpeg','onvif'}:
            local_url(data['source'])
        elif data['source_type']=='webcam':
            if not data['source'].isdigit() or int(data['source'])>16:
                raise HTTPException(422,'摄像头编号应为 0 到 16 之间的数字。')
            index=int(data['source'])
            configured=data['config'].get('index',index)
            if type(configured) is not int or configured!=index:
                raise HTTPException(422,'摄像头 source 编号与 config.index 不一致，请重新选择同一个设备编号。')
            data['source']=str(index)
            data['config']['index']=index
            duplicate=next((row for row in runtime.db.list('cameras') if row.get('source_type')=='webcam' and str(row.get('source','')).isdigit() and int(row['source'])==index and row['id']!=(existing or {}).get('id')),None)
            if duplicate:
                raise HTTPException(409,f'摄像头编号 {index} 已添加为“{duplicate["name"]}”。请使用已有摄像头，不要重复添加或重复占用。')
        elif data['source_type']=='video':
            path=Path(data['source'])
            if path.suffix.lower() not in {'.mp4','.avi','.mov','.mkv','.webm'}:
                raise HTTPException(422,'请选择 MP4、AVI、MOV、MKV 或 WebM 视频文件。')
            resolved=(path if path.is_absolute() else ROOT/path).resolve()
            if not any(resolved.is_relative_to(folder.resolve()) for folder in [ROOT/'demo'/'sample-videos',runtime.temporary]):
                raise HTTPException(422,'请通过“上传视频”选择本地文件，或使用项目内的演示视频。不能直接读取其他本机目录。')
        elif data['source_type']=='browser':
            data['source']='browser'
            device_id=str((data.get('config') or {}).get('device_id') or '')
            if len(device_id)>512:raise HTTPException(422,'浏览器摄像头设备标识过长。')
        elif data['source_type']=='screen':
            config=data.get('config') or {}
            if not config.get('authorized'):
                raise HTTPException(422,'请先明确授权采集你选定的屏幕区域。')
            rect=validate_region(config.get('region') or config.get('rectangle') or config)
            consume_screen_grant(request,rect,config.get('capture_grant'))
            data['config'].pop('capture_grant',None)
            data['config']['region']=rect
        return data

    @app.post('/api/cameras')
    def add_camera(body:CameraInput,request:Request):
        with runtime.lock:
            return runtime.camera(runtime.db.save('cameras',validate_camera(body.model_dump(),request)))

    @app.post('/api/cameras/upload-video')
    async def upload_video(file:UploadFile=File(...)):
        if runtime.mode is RuntimeMode.REAL:
            raise HTTPException(409,'REAL 模式不接收测试视频；请重启到 DEMO 模式。')
        suffix=Path(file.filename or '').suffix.lower()
        if suffix not in {'.mp4','.avi','.mov','.mkv','.webm'}:
            raise HTTPException(422,'请选择支持的视频文件。')
        path=runtime.temporary/f'{uuid4().hex}{suffix}'
        temporary=path.with_suffix(path.suffix+'.part')
        size=0
        try:
            with temporary.open('xb') as output:
                while chunk:=await file.read(1024*1024):
                    size+=len(chunk)
                    if size>512*1024*1024:
                        raise HTTPException(413,'演示视频请控制在 512 MB 以内。')
                    output.write(chunk)
            os.replace(temporary,path)
        finally:
            temporary.unlink(missing_ok=True)
        return {'source':str(path),'size':size,**response_meta('video_file',True)}

    @app.get('/api/cameras/{camera_id}')
    def get_camera(camera_id:str):
        return runtime.camera(runtime.require('cameras',camera_id))

    @app.patch('/api/cameras/{camera_id}')
    def patch_camera(camera_id:str,body:dict,request:Request):
        existing=runtime.require('cameras',camera_id)
        old={k:existing[k] for k in CameraInput.model_fields}
        if 'config' in body:body['config']={**old.get('config',{}),**body['config']}
        if body.get('source') and '***' in body['source']:
            body.pop('source')
        data=CameraInput.model_validate({**old,**body}).model_dump()
        with runtime.lock:
            result=runtime.db.save('cameras',validate_camera(data,request,existing),camera_id)
        # Display metadata is not a source change. Avoid releasing/reopening a
        # device just because its label or installation note was saved. The
        # enabled preference is not a start request for an idle camera either.
        connection_changed=any(existing.get(key)!=result.get(key) for key in ('source_type','source','config','enabled','inference_fps','save_clips'))
        if connection_changed:
            runtime.scenes.invalidate(camera_id,'摄像头配置变化，请重新获取画面并核对区域。')
            if result['enabled'] and result.get('source_type')!='screen':runtime.restart(camera_id)
            else:runtime.stop(camera_id)
        else:
            with runtime.lock:
                engine=runtime.engines.get(camera_id)
                if engine:
                    engine.camera.update({key:result[key] for key in ('name','room_name','installation')})
                    engine.camera_name=result['name'];engine.room_name=result['room_name']
                    engine.zone_manager.room_name=result['room_name']
        return runtime.camera(result)

    @app.delete('/api/cameras/{camera_id}')
    def delete_camera(camera_id:str):
        runtime.require('cameras',camera_id)
        runtime.stop(camera_id)
        with runtime.retention.lock:
            if runtime.db.count('events',{'camera_id':camera_id}):
                raise HTTPException(409,'这台摄像头仍有历史证据；请先逐条删除相关事件，再删除摄像头。')
            if runtime.scenes.delete(camera_id).get('pending_delete'):
                raise HTTPException(409,'场景图暂时无法安全删除，请稍后重试；摄像头记录已保留。')
            for zone in runtime.db.list('zones',{'camera_id':camera_id}):runtime.db.delete('zones',zone['id'])
            for track in runtime.db.list('tracks',{'camera_id':camera_id}):runtime.db.delete('tracks',track['id'])
            for state in runtime.db.list('item_current_state',{'current_camera':camera_id}):runtime.db.delete('item_current_state',state['id'])
            for session in runtime.db.list('source_sessions',{'camera_id':camera_id}):runtime.db.delete('source_sessions',session['id'])
            runtime.db.delete('cameras',camera_id)
        return {'success':True,**response_meta('admin_action')}

    @app.post('/api/cameras/{camera_id}/start')
    def start_camera(camera_id:str,request:Request,body:dict|None=None):
        camera=runtime.require('cameras',camera_id)
        if camera.get('source_type')=='screen':
            config=camera.get('config') or {}
            consume_screen_grant(request,config.get('region'),(body or {}).get('capture_grant'))
        return runtime.start(camera_id)

    @app.post('/api/cameras/{camera_id}/stop')
    def stop_camera(camera_id:str):
        runtime.require('cameras',camera_id);runtime.stop(camera_id)
        return get_camera(camera_id)

    @app.post('/api/cameras/{camera_id}/test')
    async def test_camera(camera_id:str,request:Request,body:dict|None=None):
        camera=runtime.require('cameras',camera_id)
        if camera.get('source_type')=='screen':
            config=camera.get('config') or {}
            consume_screen_grant(request,config.get('region'),(body or {}).get('capture_grant'))
        evidence_source,evidence_simulated=runtime.camera_provenance(camera)
        evidence_simulated=evidence_simulated or runtime.mode is not RuntimeMode.REAL
        was_running=camera_id in runtime.engines
        started_for_test=False
        tested_engine=None
        first_preview=None
        try:
            if not was_running:
                await asyncio.to_thread(runtime.start,camera_id)
                started_for_test=True
            tested_engine=runtime.engines.get(camera_id)
            for _ in range(80):
                if await request.is_disconnected():
                    raise HTTPException(499,'连接测试已取消；正在释放本次测试启动的采集资源。')
                engine=runtime.engines.get(camera_id)
                if engine is not tested_engine:raise HTTPException(409,'摄像头状态已变化，请重新测试。')
                snapshot=engine.get_preview_snapshot() if engine and hasattr(engine,'get_preview_snapshot') else None
                jpeg=snapshot['jpeg'] if snapshot else engine.get_jpeg() if engine and not hasattr(engine,'get_preview_snapshot') else None
                if snapshot:
                    current=(snapshot['source_session_id'],snapshot['source_frame_sequence'])
                    if first_preview is None or first_preview[0]!=current[0]:
                        first_preview=current
                        await asyncio.sleep(.05)
                        continue
                    if current[1]<=first_preview[1]:
                        await asyncio.sleep(.05)
                        continue
                if jpeg:
                    info=runtime.camera(runtime.require('cameras',camera_id))['health']
                    runtime.camera_test_frames[camera_id]=(time.monotonic(),bytes(jpeg))
                    resolution={'width':int(info.get('width') or 0),'height':int(info.get('height') or 0),'available':True}
                    if evidence_simulated:
                        message='已读取演示或测试视频帧；该结果不证明真实家庭摄像头可用。'
                    elif evidence_source=='opencv_camera':
                        message='已从本机摄像头设备读取画面。'+('复用正在运行的采集，没有重新打开或停止摄像头。' if was_running else '本次测试的临时采集将在响应前释放。')
                    elif evidence_source=='esp32_real':
                        message='已从通过本机 USB 串口验证的 ESP32-CAM 读取画面。'
                    elif evidence_source=='authorized_screen_capture':
                        message='已读取本机用户明确授权的屏幕区域；这不是物理摄像头证明。'
                    else:
                        message='已收到连续视频帧，但其物理来源未验证；连接成功不等于真实摄像头身份已确认。'
                    runtime.camera_log('info','STREAMING',message,camera_id,info)
                    return {'success':True,**info,'reused_running_source':was_running,'continuous_frames_verified':bool(snapshot),'resolutions':[resolution],'message':message,'frame_url':f'/api/cameras/{camera_id}/test-frame',**response_meta(evidence_source,evidence_simulated)}
                await asyncio.sleep(.1)
            info=runtime.camera(runtime.require('cameras',camera_id))['health']
            runtime.camera_log('error',info.get('status_code') or 'FRAME_READ_FAILED',info.get('error') or '连接测试期间没有读到画面。',camera_id,info)
            return {'success':False,**info,'message':info.get('error') or '视频源已打开但没有连续画面，请检查来源、权限、驱动或设备占用。',**response_meta(evidence_source,evidence_simulated)}
        finally:
            if started_for_test and runtime.engines.get(camera_id) is tested_engine:
                await asyncio.to_thread(runtime.stop,camera_id)

    @app.get('/api/cameras/{camera_id}/test-frame')
    def test_frame(camera_id:str):
        runtime.require('cameras',camera_id)
        cached=runtime.camera_test_frames.get(camera_id)
        if not cached or time.monotonic()-cached[0]>120:
            runtime.camera_test_frames.pop(camera_id,None)
            raise HTTPException(404,'测试截图已过期，请重新测试连接。')
        camera=runtime.require('cameras',camera_id);source_type,simulated=runtime.camera_provenance(camera)
        return Response(content=cached[1],media_type='image/jpeg',headers={'Cache-Control':'no-store','X-ObjectMemory-Runtime-Mode':runtime.mode.value,'X-ObjectMemory-Source-Type':source_type,'X-ObjectMemory-Is-Simulated':str(simulated or runtime.mode is not RuntimeMode.REAL).lower()})

    @app.get('/api/cameras/{camera_id}/frame')
    def frame(camera_id:str):
        runtime.require('cameras',camera_id)
        engine=runtime.engines.get(camera_id)
        snapshot=engine.get_preview_snapshot() if engine and hasattr(engine,'get_preview_snapshot') else None
        jpeg=snapshot['jpeg'] if snapshot else engine.get_jpeg() if engine and not hasattr(engine,'get_preview_snapshot') else None
        if not jpeg:raise HTTPException(503,'还没有画面，请启动或测试摄像头。')
        camera=runtime.require('cameras',camera_id);source_type,simulated=runtime.camera_provenance(camera)
        headers={'Cache-Control':'no-store','X-ObjectMemory-Runtime-Mode':runtime.mode.value,'X-ObjectMemory-Source-Type':source_type,'X-ObjectMemory-Is-Simulated':str(simulated or runtime.mode is not RuntimeMode.REAL).lower()}
        if snapshot:headers.update({'X-Frame-Sequence':str(snapshot['source_frame_sequence']),'X-Preview-Sequence':str(snapshot['sequence']),'X-Source-Session-Id':str(snapshot['source_session_id']),'X-Source-Timestamp':str(snapshot['source_timestamp'])})
        return Response(content=jpeg,media_type='image/jpeg',headers=headers)

    @app.get('/api/cameras/{camera_id}/vision-snapshot')
    def vision_snapshot(camera_id:str):
        camera_before=runtime.require('cameras',camera_id)
        engine=runtime.engines.get(camera_id)
        snapshot=engine.recognition_snapshot() if engine else None
        if not snapshot:
            raise HTTPException(503,'没有新鲜识别帧。请先启动摄像头；不会复用过期画面。')
        if runtime.engines.get(camera_id) is not engine or runtime.db.get('cameras',camera_id)!=camera_before:
            raise HTTPException(409,'摄像头正在切换，请重新读取识别帧。')
        jpeg=snapshot.pop('jpeg')
        snapshot.pop('created_at',None)
        health=engine.health()
        return {**snapshot,'image_data_url':'data:image/jpeg;base64,'+base64.b64encode(jpeg).decode('ascii'),
                'latency_ms':health.get('latency_ms'),'hand_status':snapshot.get('hand_status',health.get('hands')),
                'stage_timings_ms':health.get('stage_timings_ms'), 'reference_is_test_input':False}

    @app.get('/api/cameras/{camera_id}/actions')
    def camera_actions(camera_id:str, response:Response):
        camera=runtime.require('cameras',camera_id)
        engine=runtime.engines.get(camera_id)
        source,simulated=runtime.camera_provenance(camera)
        simulated=simulated or runtime.mode is not RuntimeMode.REAL
        response.headers['Cache-Control']='no-store'
        empty={'camera_id':camera_id,'source_session_id':None,'source_frame':None,'timestamp':None,
               'fresh':False,'hands':[],'interactions':[],'profile_count':0,
               'hand_status':{'enabled':runtime.settings()['hand_detection_enabled'],
                              'available':False,'status':'unavailable','error':None},
               **response_meta(source,simulated)}
        if engine is None:
            return {**empty,'reason':'camera_not_running'}
        snapshot=engine.action_snapshot()
        # A read must never open a camera or launch inference. Reject replaced
        # engines as well as changed inputs, even if an old frame is still fresh.
        if runtime.engines.get(camera_id) is not engine or runtime.db.get('cameras',camera_id)!=camera:
            return {**empty,'reason':'camera_changed'}
        return {**empty,**snapshot,**response_meta(source,simulated)}

    def scene_result(camera_id,scene):
        camera=runtime.require('cameras',camera_id)
        source,simulated=runtime.camera_provenance(camera)
        states=runtime.db.list('item_current_state',{'current_camera':camera_id})
        item_ids={row['item_id'] for row in states}
        engine=runtime.engines.get(camera_id)
        return {'scene':scene,'items':[last_location(item['id']) for item in runtime.db.list('items') if item['id'] in item_ids],
                'markers':runtime.scenes.item_markers(camera,states),
                'persons':runtime.scenes.person_markers(camera,engine.person_snapshot() if engine and hasattr(engine,'person_snapshot') else []),
                'scene_stability':dict(engine.scene_guard.last_result) if engine else None,**response_meta(source,simulated)}

    @app.get('/api/cameras/{camera_id}/scene')
    def get_scene(camera_id:str):
        runtime.require('cameras',camera_id)
        scene=runtime.scenes.get(camera_id)
        engine=runtime.engines.get(camera_id)
        if scene and engine and engine.scene_requires_review and scene.get('calibration_status') in {'schematic','geometry_only','calibrated','calibrated_unvalidated'}:
            scene=runtime.scenes.invalidate(camera_id,'视角或分辨率发生变化，请重新获取场景画面。')
        return scene_result(camera_id,scene)

    def scene_packet(camera_id):
        camera=runtime.require('cameras',camera_id)
        engine=runtime.engines.get(camera_id)
        latest=engine.capture.preview_frames.latest(timeout=0) if engine else None
        packet=latest[1] if latest else None
        if packet is None or not 0<=time.monotonic()-packet.monotonic_time<=2:
            raise HTTPException(409,'请先启动摄像头并保持新画面；场景操作不会自动打开摄像头。')
        return camera,engine,packet

    @app.post('/api/cameras/{camera_id}/scene/propose')
    def propose_scene(camera_id:str,only_if_empty:bool=False):
        from services.vision.geometry import FrameGeometry
        camera,engine,packet=scene_packet(camera_id)
        height,width=packet.frame.shape[:2]
        ok,encoded=cv2.imencode('.jpg',packet.frame,[cv2.IMWRITE_JPEG_QUALITY,95])
        if not ok:raise HTTPException(503,'场景关键帧编码失败，请重新获取。')
        source,simulated=runtime.camera_provenance(camera)
        meta={'camera_id':camera_id,'source_session_id':packet.source_session_id,'source_frame':packet.sequence,
              'source_timestamp':datetime.fromtimestamp(packet.wall_time,timezone.utc).isoformat(),'created_at':packet.monotonic_time,
              'width':width,'height':height,'geometry':FrameGeometry(width,height).to_dict(),**response_meta(source,simulated)}
        with runtime.lock:
            if runtime.engines.get(camera_id) is not engine or runtime.db.get('cameras',camera_id)!=camera:
                raise HTTPException(409,'摄像头刚刚切换，请重新获取场景画面。')
            scene=runtime.scenes.propose(camera,meta,encoded.tobytes(),only_if_empty=only_if_empty)
            engine.update_scene(runtime.db.list('zones',{'camera_id':camera_id}),confirmed=False)
        return scene_result(camera_id,scene)

    @app.put('/api/cameras/{camera_id}/scene')
    def save_scene(camera_id:str,body:dict):
        camera,engine,packet=scene_packet(camera_id)
        scene=runtime.scenes.get(camera_id)
        if not scene or packet.source_session_id!=scene.get('source_session_id'):
            raise HTTPException(409,'摄像头会话已变化，请重新获取场景画面。')
        geometry=scene.get('geometry') or {}
        height,width=packet.frame.shape[:2]
        if width!=geometry.get('source_width') or height!=geometry.get('source_height'):
            raise HTTPException(409,'分辨率已变化，请重新获取场景画面。')
        if engine.scene_guard.last_result.get('status')=='changed' and scene.get('calibration_status') in {'schematic','geometry_only','calibrated','calibrated_unvalidated'}:
            runtime.scenes.invalidate(camera_id,'视角已移动，请重新获取场景画面。')
            raise HTTPException(409,'视角已移动，请重新获取场景画面。')
        # Compare the exact draft screenshot with the current source, not with
        # an older engine baseline. An untextured scene remains uncalibrated.
        from services.vision.scene_guard import SceneGuard
        original=runtime.scenes._safe_path(scene['screenshot_path'].removeprefix('/media/scene-images/')).read_bytes()
        image=cv2.imdecode(np.frombuffer(original,np.uint8),cv2.IMREAD_COLOR)
        guard=SceneGuard(0,runtime.settings()['scene_shift_threshold'])
        if image is None:raise HTTPException(409,'场景关键帧无法解码，请重新获取。')
        guard.establish(image)
        if guard.check(packet.frame,packet.monotonic_time):
            runtime.scenes.invalidate(camera_id,'编辑期间视角发生变化，请重新获取画面。')
            raise HTTPException(409,'编辑期间视角发生变化，请重新获取画面。')
        with runtime.lock:
            if runtime.engines.get(camera_id) is not engine or runtime.db.get('cameras',camera_id)!=camera:
                raise HTTPException(409,'摄像头刚刚切换，请重新获取场景画面。')
            scene=runtime.scenes.save(camera,body)
            engine.update_scene(runtime.db.list('zones',{'camera_id':camera_id}),image)
        return scene_result(camera_id,scene)

    @app.get('/api/cameras/{camera_id}/stream')
    async def stream(camera_id:str,request:Request):
        runtime.require('cameras',camera_id)
        async def frames():
            runtime.subscribers(camera_id,1)
            last_key=None
            try:
                while not runtime.shutdown_requested.is_set() and not await request.is_disconnected():
                    engine=runtime.engines.get(camera_id)
                    snapshot=engine.get_preview_snapshot() if engine and hasattr(engine,'get_preview_snapshot') else None
                    if snapshot:
                        key=(id(engine),snapshot['source_session_id'],snapshot['sequence'])
                        if key!=last_key:
                            last_key=key
                            headers=f'--frame\r\nContent-Type: image/jpeg\r\nX-Frame-Sequence: {snapshot["source_frame_sequence"]}\r\nX-Preview-Sequence: {snapshot["sequence"]}\r\nX-Source-Session-Id: {snapshot["source_session_id"]}\r\nX-Source-Timestamp: {snapshot["source_timestamp"]}\r\n\r\n'
                            yield headers.encode('ascii')+snapshot['jpeg']+b'\r\n'
                    elif engine and not hasattr(engine,'get_preview_snapshot'):
                        jpeg=engine.get_jpeg()
                        key=(id(engine),hashlib.sha256(jpeg).digest()) if jpeg else None
                        if key is not None and key!=last_key:
                            last_key=key
                            yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'+jpeg+b'\r\n'
                    # The engine, not each subscriber, owns the encoder. Poll
                    # its bounded latest snapshot; never pad FPS with old JPEGs.
                    await asyncio.sleep(.01)
            finally:runtime.subscribers(camera_id,-1)
        camera=runtime.require('cameras',camera_id);source_type,simulated=runtime.camera_provenance(camera)
        return StreamingResponse(frames(),media_type='multipart/x-mixed-replace; boundary=frame',headers={'X-ObjectMemory-Runtime-Mode':runtime.mode.value,'X-ObjectMemory-Source-Type':source_type,'X-ObjectMemory-Is-Simulated':str(simulated or runtime.mode is not RuntimeMode.REAL).lower()})

    @app.websocket('/ws/cameras/{camera_id}')
    async def camera_ws(ws:WebSocket,camera_id:str):
        if not await runtime.sessions.websocket(ws,require_origin=True):return
        runtime.subscribers(camera_id,1)
        try:
            while not runtime.shutdown_requested.is_set():
                camera=runtime.db.get('cameras',camera_id)
                if not camera:break
                await ws.send_json({'camera':runtime.camera(camera),'tracks':[runtime.verified_search_record(row) for row in runtime.db.list('item_current_state',{'current_camera':camera_id})]})
                await asyncio.sleep(.5)
        except (WebSocketDisconnect,RuntimeError):pass
        finally:runtime.subscribers(camera_id,-1)

    @app.websocket('/ws/cameras/{camera_id}/vision')
    async def live_vision_ws(ws:WebSocket,camera_id:str):
        # Session and Origin checks are identical to other admin-only sockets.
        # Subscribing never starts a camera, model, encoder, or recorder.
        if not await runtime.sessions.websocket(ws,require_origin=True):return
        if not runtime.db.get('cameras',camera_id):
            await ws.close(4404);return
        runtime.subscribers(camera_id,1)
        last_key=None
        status_sent=False
        incoming=asyncio.create_task(ws.receive_json())
        try:
            while not runtime.shutdown_requested.is_set():
                engine=runtime.engines.get(camera_id)
                snapshot=engine.tracking_snapshot() if engine and hasattr(engine,'tracking_snapshot') else None
                if not snapshot:
                    if incoming.done():
                        incoming.result()
                        await ws.close(4400);break
                    if not status_sent:
                        await asyncio.wait_for(ws.send_json({'type':'unavailable','message':'没有新鲜跟踪帧，已撤下旧框。'}),2)
                        status_sent=True
                    await asyncio.sleep(.05)
                    continue
                key=(id(engine),snapshot['source_session_id'],snapshot['source_frame'])
                if key==last_key:
                    if incoming.done():
                        incoming.result()
                        await ws.close(4400);break
                    await asyncio.sleep(.005)
                    continue
                jpeg=snapshot.pop('jpeg')
                snapshot.pop('created_at',None)
                meta=json.dumps({**snapshot,'type':'frame'},ensure_ascii=False,separators=(',',':')).encode('utf-8')
                if len(meta)>131072 or len(jpeg)>8*1024*1024:
                    await ws.close(4400);break
                packet=len(meta).to_bytes(4,'big')+meta+jpeg
                await asyncio.wait_for(ws.send_bytes(packet),2)
                # Exactly one in-flight frame per client. A slow decoder ACKs
                # that exact frame; the next send picks newest cache, not a queue.
                acknowledgement=await asyncio.wait_for(incoming,2.5)
                if (not isinstance(acknowledgement,dict)
                    or type(acknowledgement.get('ack')) is not int
                    or acknowledgement.get('ack')!=snapshot['source_frame']
                    or acknowledgement.get('source_session_id')!=snapshot['source_session_id']):
                    await ws.close(4400);break
                last_key=key
                status_sent=False
                incoming=asyncio.create_task(ws.receive_json())
        except (WebSocketDisconnect,RuntimeError,asyncio.TimeoutError,ValueError,KeyError):
            pass
        finally:
            try:
                incoming.cancel()
                # Consume any failure of the receiver, including malformed
                # binary input (KeyError in Starlette.receive_json). Cleanup
                # must not depend on which malformed packet ended the stream.
                with contextlib.suppress(asyncio.CancelledError,Exception):await incoming
            finally:
                runtime.subscribers(camera_id,-1)
                with contextlib.suppress(RuntimeError,WebSocketDisconnect):await ws.close()

    @app.websocket('/ws/browser-cameras/{camera_id}/ingest')
    async def browser_camera_ingest(ws:WebSocket,camera_id:str):
        if not await runtime.sessions.websocket(ws,require_origin=True):return
        camera=runtime.db.get('cameras',camera_id)
        if not camera or camera.get('source_type')!='browser':
            await ws.send_json({'type':'error','code':'NO_DEVICE','error':'浏览器摄像头记录不存在。'});await ws.close(4404);return
        engine=source=sender_token=None
        try:
            try:
                engine,source,sender_token=await asyncio.to_thread(runtime.acquire_browser_sender,camera_id)
            except RuntimeError as exc:
                await ws.send_json({'type':'error','code':'BUSY','error':str(exc)});await ws.close(4409);return
            source_type,simulated=runtime.camera_provenance(camera)
            provenance=response_meta(source_type,simulated or runtime.mode is not RuntimeMode.REAL)
            await ws.send_json({'type':'ready','camera_id':camera_id,**provenance})
            sequence=0
            browser_idle_timeout=max(2.0,min(4.0,float(runtime.settings().get('max_frame_gap_seconds',.75))*4.0))
            while True:
                try:
                    message=await asyncio.wait_for(ws.receive(),timeout=browser_idle_timeout)
                except asyncio.TimeoutError:
                    # A suspended/lost browser may never deliver a disconnect
                    # frame.  Once the configured evidence-continuity window is
                    # long gone, terminate its sender lease instead of leaving a
                    # stalled engine and stale session alive indefinitely.
                    with contextlib.suppress(RuntimeError):
                        await ws.send_json({'type':'error','code':'FRAME_TIMEOUT','error':'浏览器画面长时间没有新帧，采集连接已释放。'})
                    break
                if message.get('type')=='websocket.disconnect':break
                content=message.get('bytes')
                if content is None:
                    await ws.send_json({'type':'error','code':'INVALID_FRAME','error':'请发送二进制 JPEG 帧。'});continue
                sequence+=1
                try:
                    result=await asyncio.to_thread(source.push_jpeg,content,sender_token)
                    await ws.send_json({'type':'ack','sequence':sequence,'status':'streaming',**result,**provenance})
                except (ValueError,RuntimeError) as exc:
                    await ws.send_json({'type':'error','sequence':sequence,'code':'INVALID_FRAME','error':str(exc)})
        except WebSocketDisconnect:pass
        except (HTTPException,RuntimeError) as exc:
            detail=exc.detail if isinstance(exc,HTTPException) else str(exc)
            with contextlib.suppress(RuntimeError):await ws.send_json({'type':'error','code':'BUSY' if isinstance(exc,HTTPException) and exc.status_code==409 else 'ERROR','error':detail})
        finally:
            if engine is not None and source is not None and sender_token is not None:
                # Detach the disconnected generation and publish STOPPED in the
                # WebSocket task itself.  If the whole release is first queued
                # in the executor, concurrent health reads can still observe
                # the old engine long enough for it to transition to stalled.
                release=runtime.begin_browser_sender_release(camera_id,engine,source,sender_token)
                if release is not None:
                    release_engine,release_health=release
                    # Starlette's WebSocket test/client lifecycle (and real
                    # server shutdown) may cancel the handler immediately after
                    # delivering websocket.disconnect.  Shield the bounded
                    # worker join so the detached engine and its source session
                    # cannot be left alive/streaming after public STOPPED.
                    with CancelScope(shield=True):
                        await anyio_to_thread.run_sync(
                            runtime.finish_browser_sender_release,
                            camera_id,release_engine,release_health,
                            abandon_on_cancel=False,
                        )

    @app.get('/api/cameras/{camera_id}/zones')
    def zones(camera_id:str):
        runtime.require('cameras',camera_id)
        return [{**row,**response_meta('zone_configuration')} for row in runtime.db.list('zones',{'camera_id':camera_id})]

    @app.post('/api/cameras/{camera_id}/zones')
    def add_zone(camera_id:str,body:ZoneInput):
        runtime.require('cameras',camera_id)
        row=runtime.db.save('zones',{'camera_id':camera_id,**body.model_dump()})
        runtime.restart(camera_id)
        return {**row,**response_meta('zone_configuration')}

    @app.patch('/api/zones/{zone_id}')
    def patch_zone(zone_id:str,body:dict):
        old=runtime.require('zones',zone_id)
        data=ZoneInput.model_validate({**{k:old[k] for k in ZoneInput.model_fields},**body}).model_dump()
        row=runtime.db.save('zones',data,zone_id)
        runtime.restart(old['camera_id'])
        return {**row,**response_meta('zone_configuration')}

    @app.delete('/api/zones/{zone_id}')
    def delete_zone(zone_id:str):
        old=runtime.require('zones',zone_id);runtime.db.delete('zones',zone_id);runtime.restart(old['camera_id'])
        return {'success':True,**response_meta('admin_action')}

    def item_result(row):
        seeded=runtime.mode is RuntimeMode.DEMO and str(row.get('id','')).startswith('demo-')
        return {**row,'reference_images':[runtime.registration.public_reference(ref) for ref in runtime.db.list('item_reference_images',{'item_id':row['id']})],'recognition_profile':runtime.registration.profile(row['id'],dict(runtime.engines)),'companion_online':bool(runtime.companions.get(row['id'])),**response_meta('demo_seed' if seeded else 'user_registration',seeded)}

    @app.get('/api/items')
    def items():return [item_result(i) for i in runtime.db.list('items')]

    @app.post('/api/items')
    def add_item(body:ItemInput):
        item=runtime.db.save('items',body.model_dump())
        runtime.refresh_recognition()
        return item_result(item)

    @app.get('/api/items/{item_id}')
    def get_item(item_id:str):return item_result(runtime.require('items',item_id))

    @app.patch('/api/items/{item_id}')
    def patch_item(item_id:str,body:dict):
        old=runtime.require('items',item_id)
        data=ItemInput.model_validate({**{k:old[k] for k in ItemInput.model_fields},**body}).model_dump()
        item=runtime.db.save('items',data,item_id)
        runtime.refresh_recognition()
        return item_result(item)

    @app.delete('/api/items/{item_id}')
    def delete_item(item_id:str):
        runtime.require('items',item_id)
        with runtime.retention.lock:
            if runtime.db.count('events',{'item_id':item_id}):
                raise HTTPException(409,'这个物品仍有历史证据；请先逐条删除相关事件，再删除物品。')
            runtime.registration.delete_profile(item_id)
            for state in runtime.db.list('item_current_state',{'item_id':item_id}):runtime.db.delete('item_current_state',state['id'])
            for track in runtime.db.list('tracks',{'item_id':item_id}):runtime.db.delete('tracks',track['id'])
            for companion in runtime.db.list('companions',{'item_id':item_id}):runtime.db.delete('companions',companion['id'])
            runtime.db.delete('items',item_id)
        runtime.refresh_recognition()
        return {'success':True,**response_meta('admin_action')}

    @app.post('/api/items/{item_id}/reference-images')
    async def reference_images(item_id:str,files:list[UploadFile]=File(...)):
        runtime.require('items',item_id)
        if not 1<=len(files)<=12:raise HTTPException(422,'一次可选择 1 至 12 张照片；一张就能开始，后续可补充角度。')
        prepared=[]
        total_size=0
        for file in files:
            content=await file.read(12*1024*1024+1)
            if len(content)>12*1024*1024:raise HTTPException(413,'每张照片请控制在 12 MB 以内。')
            total_size+=len(content)
            if total_size>64*1024*1024:raise HTTPException(413,'单次参考图片上传总量请控制在 64 MB 以内。')
            await anyio_to_thread.run_sync(runtime.registration.decode,content)
            prepared.append(content)
        try:
            result=await anyio_to_thread.run_sync(runtime.registration.add_batch,item_id,prepared)
        finally:
            # Also invalidate live matchers if an I/O failure interrupted a save.
            runtime.refresh_recognition()
        return [{**row,**response_meta('user_reference_image',False)} for row in result]

    @app.patch('/api/items/{item_id}/reference-images/{reference_id}')
    def confirm_reference(item_id:str,reference_id:str,body:dict):
        runtime.require('items',item_id)
        row=runtime.registration.confirm_region(item_id,reference_id,body.get('region'),body.get('confirmed'))
        runtime.refresh_recognition()
        return {**row,**response_meta('user_reference_image',False)}

    @app.delete('/api/items/{item_id}/reference-images/{reference_id}')
    def delete_reference(item_id:str,reference_id:str):
        runtime.require('items',item_id)
        runtime.registration.delete_reference(item_id,reference_id)
        runtime.refresh_recognition()
        return {'success':True,**response_meta('admin_action')}

    @app.get('/api/items/{item_id}/profile')
    def recognition_profile(item_id:str):
        runtime.require('items',item_id)
        return {**runtime.registration.profile(item_id,dict(runtime.engines)),**response_meta('recognition_profile')}

    @app.post('/api/items/{item_id}/profile/build')
    def build_recognition_profile(item_id:str):
        runtime.require('items',item_id)
        runtime.registration.build(item_id)
        runtime.refresh_recognition(activate=True)
        return {**runtime.registration.profile(item_id,dict(runtime.engines)),**response_meta('recognition_profile')}

    @app.post('/api/items/{item_id}/reference-capture')
    def capture_reference(item_id:str,body:dict):
        runtime.require('items',item_id)
        camera_id=str(body.get('camera_id') or '')
        camera_before=runtime.require('cameras',camera_id)
        engine=runtime.engines.get(camera_id)
        if not engine:
            raise HTTPException(409,'请先在摄像头页面启动所选摄像头，再抓拍。')
        latest=engine.capture.preview_frames.latest(timeout=0)
        packet=latest[1] if latest else None
        if packet is None or time.monotonic()-packet.monotonic_time>2:
            raise HTTPException(503,'当前没有新鲜真实帧，未保存旧画面。')
        ok,encoded=cv2.imencode('.jpg',packet.frame,[cv2.IMWRITE_JPEG_QUALITY,95])
        if not ok:raise HTTPException(500,'抓拍编码失败，未保存照片。')
        with runtime.lock:
            camera=runtime.db.get('cameras',camera_id)
            if runtime.engines.get(camera_id) is not engine or camera != camera_before:
                raise HTTPException(409,'摄像头正在切换，未保存旧帧，请重新抓拍。')
            source_type,simulated=runtime.camera_provenance(camera)
            row=runtime.registration.add(item_id,encoded.tobytes(),{
                'camera_id':camera_id,'source_session_id':packet.source_session_id,'source_frame':packet.sequence,
                'source_timestamp':packet.wall_time,'runtime_mode':runtime.mode.value,'source_type':source_type,'is_simulated':simulated,
            })
        runtime.refresh_recognition()
        return {**row,**response_meta('user_reference_image',False)}

    @app.post('/api/items/{item_id}/recognition-test')
    def test_photo_recognition(item_id:str,body:dict):
        runtime.require('items',item_id)
        profile=runtime.registration.profile(item_id)
        if profile['registration_status']!='ready':
            raise HTTPException(409,'请先确认参考目标并成功建立识别档案。')
        camera_id=str(body.get('camera_id') or '')
        runtime.require('cameras',camera_id)
        engine=runtime.engines.get(camera_id)
        if not engine:raise HTTPException(409,'请先启动所选摄像头，测试使用它的真实帧，不使用注册照片自测。')
        if engine._active_detection_mode()!='experimental' or engine.loaded_profile_versions.get(item_id)!=profile['profile_version']:
            runtime.refresh_recognition(activate=True)
        deadline=time.monotonic()+6
        snapshot=None
        while time.monotonic()<deadline:
            snapshot=engine.recognition_snapshot()
            if snapshot and any(p['item_id']==item_id and p['profile_version']==profile['profile_version'] for p in snapshot['loaded_profiles']):
                break
            time.sleep(.05)
        if not snapshot or not any(p['item_id']==item_id and p['profile_version']==profile['profile_version'] for p in snapshot['loaded_profiles']):
            raise HTTPException(503,engine.health().get('recognition_error') or '识别档案尚未加载到实时摄像头；没有用旧结果冒充成功。')
        if runtime.engines.get(camera_id) is not engine:
            raise HTTPException(409,'摄像头正在切换，请重新测试。')
        jpeg=snapshot.pop('jpeg')
        snapshot.pop('created_at',None)
        return {**snapshot,'image_data_url':'data:image/jpeg;base64,'+base64.b64encode(jpeg).decode('ascii'),
                'latency_ms':engine.health().get('latency_ms'),'reference_is_test_input':False}

    @app.get('/api/items/{item_id}/last-location')
    def last_location(item_id:str):
        state=runtime.db.get('item_current_state',f'{runtime.mode.value}:{item_id}')
        state=runtime.verified_search_record(state)
        search_events=[runtime.verified_search_record(row) for row in runtime.db.list('events',{'item_id':item_id},limit=10000,order='timestamp_start')]
        result=location_result(item_result(runtime.require('items',item_id)),search_events,runtime.mode.value,state)
        result['current_track']=state
        if not state and not result.get('evidence'):
            from services.vision.detectors.appearance import normalize_category
            with runtime.lock:
                engines=dict(runtime.engines)
            profile=runtime.registration.profile(item_id,engines)
            category=normalize_category(result['item'].get('type'))
            # These are cached, source-bound diagnostic frames only. Never run
            # inference or activate/rebuild a profile from this read endpoint.
            loaded={camera_id:engine for camera_id,engine in engines.items()
                    if profile.get('profile_version',0)>0 and
                    getattr(engine,'loaded_profile_versions',{}).get(item_id)==profile['profile_version']}
            snapshots=[]
            stale_profile=False
            model_failed=False
            for camera_id,engine in loaded.items():
                reader=getattr(engine,'recognition_snapshot',None)
                snapshot=reader() if callable(reader) else None
                if not snapshot:
                    health=engine.health()
                    model_failed=model_failed or bool(health.get('recognition_error'))
                    continue
                if not any(p.get('item_id')==item_id and p.get('profile_version')==profile['profile_version']
                           for p in snapshot.get('loaded_profiles',[])):
                    stale_profile=True
                    continue
                model=snapshot.get('model') or {}
                if model.get('available') is not True or model.get('status')!='ready' or model.get('error'):
                    model_failed=True
                    continue
                snapshots.append((camera_id,snapshot))
            candidates=[candidate for _,snapshot in snapshots for candidate in snapshot.get('candidates',[])
                        if category is not None and normalize_category(candidate.get('category'))==category]
            accepted=[candidate for candidate in candidates
                      if candidate.get('accepted') is True and candidate.get('item_id')==item_id]
            small_reasons={'insufficient_detail','crop_too_small'}
            if profile.get('registration_status')!='ready':
                code,message='profile_not_ready','照片档案尚未就绪。请上传照片、确认目标并建立识别档案；尚未观察到这个物品。'
            elif not engines:
                code,message='no_live_source','没有正在输出新鲜识别帧的摄像头。请先到实时画面启动摄像头。'
            elif not loaded or (not snapshots and stale_profile):
                code,message='profile_not_loaded','运行中的摄像头尚未加载当前照片档案，请在物品管理中重新建立/加载档案。'
            elif not snapshots and model_failed:
                code,message='model_unavailable','当前档案对应的识别模型异常或尚未就绪；不能据此判断画面中没有物品。请查看摄像头与模型状态。'
            elif not snapshots:
                code,message='no_live_source','已加载这个物品档案的摄像头没有新鲜识别帧；请检查取流或离线状态。其他摄像头的帧不能替代它。'
            elif accepted:
                code,message='waiting_continuity','本帧已有身份接受候选，但连续观察或入库校验尚未形成位置记录。请保持物品清晰可见；这不是确认放下。'
            elif candidates and all((candidate.get('rejection_reason') or candidate.get('rejection')) in small_reasons for candidate in candidates):
                code,message='object_too_small','当前同类候选像素太少，尚未进入可靠身份匹配。请靠近摄像头或改善清晰度；没有把小目标猜成这个物品。'
            elif candidates and all(candidate.get('category_evidence')=='geometry_only' for candidate in candidates):
                code,message='shape_candidate_only','发现外形像手机的轮廓，但尚未可靠匹配为你的物品。黑色长方形也可能是其他东西；没有据此猜测位置或确认放下。'
            elif candidates:
                code,message='identity_uncertain','已检测到同类候选，但尚未可靠确认是这个物品。请查看候选相似度与拒绝原因；不是“肯定没有这个物品”。'
            else:
                code,message='no_target_candidate','当前已加载档案的摄像头尚未检出这个类别的候选框，因此还未进入该物品的身份匹配。请查看候选检测诊断；这不是确定物品不在场。'
            result['observation_hint']={'code':code,'message':message,'target_category':category,
                'profile_version':profile.get('profile_version'),'loaded_camera_count':len(loaded),
                'evaluated_camera_count':len(snapshots),'target_candidate_count':len(candidates),
                'accepted_candidate_count':len(accepted),
                'evaluated_frames':[{'camera_id':camera_id,'source_session_id':snapshot.get('source_session_id'),
                    'source_frame':snapshot.get('source_frame'),'source_timestamp':snapshot.get('source_timestamp')}
                    for camera_id,snapshot in snapshots[:16]]}
        return result

    @app.get('/api/items/{item_id}/events')
    def item_events(item_id:str):
        runtime.require('items',item_id)
        return [runtime.verified_search_record(row) for row in runtime.db.list('events',{'item_id':item_id},order='timestamp_start')]

    @app.get('/api/events')
    def events(item_id:str|None=None,camera_id:str|None=None,room_name:str|None=None,event_type:str|None=None,limit:int=Query(100,ge=1,le=10000)):
        return [runtime.verified_search_record(row) for row in runtime.db.list('events',locals(),limit=limit,order='timestamp_start')]

    @app.get('/api/events/{event_id}')
    def event_details(event_id:str):return runtime.verified_search_record(runtime.require('events',event_id))

    @app.post('/api/events/{event_id}/correct')
    def correct_event(event_id:str,body:CorrectionInput):
        old=runtime.require('events',event_id)
        zone=runtime.require('zones',body.zone_id) if body.zone_id else None
        if zone and zone['camera_id']!=old['camera_id']:raise HTTPException(422,'所选区域不属于这台摄像头。')
        corrected_zone=zone['name'] if zone else body.zone_name or old['zone_name']
        # Repeated submission of the same correction (double click, browser
        # retry, or a re-opened form) is idempotent while that correction is
        # already the item's current evidence.  A genuinely different target
        # still creates a new auditable human decision.
        with runtime.retention.lock:
            current=runtime.db.get('item_current_state',f'{runtime.mode.value}:{old["item_id"]}')
            current_event=runtime.db.get('events',str((current or {}).get('evidence_event_id') or '')) if current else None
            desired_room=body.room_name or old['room_name']
            old_public_id=str(old.get('event_id') or old.get('id'))
            if current_event and current and current.get('current_zone')==corrected_zone and current.get('current_room')==desired_room:
                current_public_id=str(current_event.get('event_id') or current_event.get('id'))
                current_reference=str(current_event.get('manual_reference_event_id') or '')
                if current_public_id==old_public_id or current_reference==old_public_id:
                    return runtime.verified_search_record(current_event)
            correction_at=now()
            event={**old,'id':uuid4().hex,'event_id':None,'movement_session_id':f'manual-{uuid4().hex}','event_type':'manual_correction','timestamp_start':correction_at,'timestamp_end':correction_at,'source_timestamp_start':correction_at,'source_timestamp_end':correction_at,'zone_id':body.zone_id,'zone_name':corrected_zone,'room_name':desired_room,'evidence_type':'human_input','evidence_status':'confirmed','human_review_status':'human_corrected','confidence':1.0,'notes':body.notes,'from_zone':old.get('to_zone') or old.get('zone_name'),'to_zone':corrected_zone,'manually_corrected':True,'manual_reference_event_id':old['id']}
            event['event_id']=event['id']
            try:return runtime.verified_search_record(runtime.event_service.record_manual_correction(event))
            except EvidenceRejected as exc:raise HTTPException(422,str(exc))

    @app.post('/api/events/{event_id}/pin')
    def pin_event(event_id:str,body:PinInput):
        with runtime.retention.lock:
            old=runtime.require('events',event_id)
            return runtime.verified_search_record(runtime.db.save('events',{'pinned':body.pinned},old['id']))

    def delete_media(url):
        if not url or not url.startswith('/media/'):return
        if url.startswith('/media/registered-items/'):
            root=(runtime.data/'registered-items').resolve()
            path=(root/Path(url).name).resolve()
        else:
            root=runtime.media.resolve()
            path=(root/url.removeprefix('/media/')).resolve()
        if path.is_relative_to(root) and path!=root:path.unlink(missing_ok=True)

    @app.delete('/api/events/{event_id}')
    def delete_event(event_id:str):
        runtime.require('events',event_id)
        result=runtime.retention.delete_event(event_id)
        if result.get('protected_dependency'):
            raise HTTPException(409,'这条原始事件仍被人工纠正引用，不能单独删除。')
        return {'success':True,**result,**response_meta('admin_action')}

    @app.post('/api/search')
    def search(body:SearchInput):
        result=[last_location(i['id']) for i in match_items(body.query,runtime.db.list('items'))]
        if '谁' in body.query:
            for item in result:item['answer']+=' 系统不识别人脸身份，不能判断具体是谁移动了它。'
        return {'query':body.query,'results':result,**response_meta('search_repository')}

    @app.websocket('/ws/companions/{item_id}')
    async def companion(ws:WebSocket,item_id:str):
        if not await runtime.sessions.websocket(ws,require_origin=True):return
        item=runtime.db.get('items',item_id)
        if not item or not item.get('ring_enabled'):
            await ws.close(4404);return
        runtime.companions.setdefault(item_id,set()).add(ws)
        runtime.db.save('companions',{'item_id':item_id,'last_seen':now(),'online':True},item_id)
        try:
            provenance=response_meta('companion_device')
            await ws.send_json({'type':'connected','item_id':item_id,**provenance})
            while True:
                message=await ws.receive_json()
                if message.get('type')=='ping':await ws.send_json({'type':'pong',**provenance})
                runtime.db.save('companions',{'last_seen':now()},item_id)
        except (WebSocketDisconnect,RuntimeError,ValueError):pass
        finally:
            runtime.companions.get(item_id,set()).discard(ws)
            runtime.db.save('companions',{'online':bool(runtime.companions.get(item_id))},item_id)

    async def broadcast_ring(item_id,kind):
        item=runtime.require('items',item_id)
        if not item.get('ring_enabled'):raise HTTPException(422,'这件物品还没有开启手机伴侣功能。')
        delivered=0
        for ws in list(runtime.companions.get(item_id,set())):
            try:await ws.send_json({'type':kind,'item_id':item_id,'timestamp':now(),**response_meta('companion_device')});delivered+=1
            except Exception:runtime.companions[item_id].discard(ws)
        return {'delivered':delivered,'message':'响铃指令已送达伴侣页面。' if delivered else '伴侣页面未连接，请在测试手机上打开伴侣页面并启用声音。',**response_meta('companion_device')}

    @app.post('/api/items/{item_id}/ring')
    async def ring(item_id:str):return await broadcast_ring(item_id,'ring')

    @app.post('/api/items/{item_id}/ring/stop')
    async def ring_stop(item_id:str):return await broadcast_ring(item_id,'stop')

    @app.get('/api/settings')
    def settings():return {**runtime.settings(),**response_meta('runtime_settings')}

    @app.patch('/api/settings')
    def change_settings(body:dict):
        # Compare effective values, not a legacy partial nested configuration
        # against its expanded equivalent: an overlay toggle must not reopen
        # the camera or reset a stable identity/movement baseline.
        previous=SettingsInput.model_validate(runtime.settings()).model_dump()
        values=SettingsInput.model_validate({**previous,**body}).model_dump()
        changed={key for key,value in values.items() if previous.get(key)!=value}
        runtime.db.save('settings',{'value':values},'main')
        runtime.retention.max_storage_mb=values['max_storage_mb']
        if 'inference_fps' in changed:
            for camera in runtime.db.list('cameras'):
                runtime.db.save('cameras',{'inference_fps':values['inference_fps']},camera['id'])
        # Hiding keypoints must not release the camera, erase the hand identity
        # baseline, or interrupt a movement. Full-form no-op saves are also inert.
        if changed-{'show_hands'}:
            for cid in list(runtime.engines):runtime.restart(cid)
        elif 'show_hands' in changed:
            for engine in list(runtime.engines.values()):
                engine.settings['show_hands']=values['show_hands']
        return {**values,**response_meta('runtime_settings')}

    @app.post('/api/system/demo-seed')
    def seed_demo():
        if runtime.mode is not RuntimeMode.DEMO:
            raise HTTPException(409,'演示种子只能写入 DEMO 数据库；请使用 Start-No-Hardware-Demo.bat 重启。')
        video=ROOT/'demo'/'sample-videos'/'object-memory-demo.mp4'
        if not video.exists():
            video=ROOT/'demo'/'sample-videos'/'object-memory-demo.avi'
        if not video.exists():
            raise HTTPException(409,'演示视频还没有生成，请先运行 bootstrap.ps1 或 generate-demo-assets.py。')
        presets=[('demo-phone','我的手机','phone',1,['手机','电话'],True),('demo-keys','我的钥匙','keys',2,['钥匙'],False),('demo-wallet','蓝色钱包','wallet',3,['钱包'],False)]
        for item_id,name,kind,marker,aliases,ring_enabled in presets:
            if not runtime.db.get('items',item_id):
                existing=runtime.db.list('items',{'aruco_id':marker},limit=1)
                if existing:continue
                runtime.db.save('items',ItemInput(name=name,type=kind,aruco_id=marker,aliases=aliases,ring_enabled=ring_enabled).model_dump(),item_id)
        camera_id='demo-camera'
        demo_source=str(video.relative_to(ROOT)).replace('\\','/')
        if not runtime.db.get('cameras',camera_id):
            runtime.db.save('cameras',CameraInput(name='客厅演示回放',room_name='客厅',installation='自动生成测试视频',source_type='video',source=demo_source,config={'loop':True}).model_dump(),camera_id)
            for zid,name,points in [('desk','桌面',[[0,.12],[.46,.12],[.46,.9],[0,.9]]),('sofa','沙发右侧',[[.5,.12],[1,.12],[1,.9],[.5,.9]]),('floor','地面',[[0,.91],[1,.91],[1,1],[0,1]])]:
                runtime.db.save('zones',{'camera_id':camera_id,'name':name,'points':points,'priority':1,'enabled':True},f'demo-{zid}')
        else:
            existing=runtime.db.get('cameras',camera_id)
            if existing and existing.get('source')!=demo_source:
                runtime.stop(camera_id)
                runtime.db.save('cameras',{'source':demo_source},camera_id)
        return {'camera_id':camera_id,'items':[i for i in runtime.db.list('items') if i.get('aruco_id') in {1,2,3}],'runtime_mode':'DEMO','source_type':'demo_seed','is_simulated':True,'message':'演示素材已写入独立 DEMO 数据库；事件不来自真实家庭摄像头。'}

    @app.post('/api/system/cleanup')
    def cleanup(body:dict|None=None):
        body=body or {}
        if body.get('all'):
            raise HTTPException(409,'普通清理不会删除全部真实记录；请使用独立的“删除所有真实记录”操作。')
        return runtime.retention.cleanup(trigger='legacy-api',dry_run=bool(body.get('dry_run')))

    @app.get('/api/storage/status')
    def storage_status():return runtime.retention.status()

    @app.post('/api/storage/scan')
    def storage_scan():return runtime.retention.scan()

    @app.post('/api/storage/preview')
    def storage_preview():return runtime.retention.preview()

    @app.post('/api/storage/cleanup')
    def storage_cleanup():return runtime.retention.cleanup(trigger='manual',dry_run=False)

    @app.patch('/api/storage/policy')
    def storage_policy(body:PolicyInput):
        runtime.retention.set_policy(body.policy)
        return runtime.retention.status()

    def clear_current_mode(mode:RuntimeMode):
        target_lease=runtime.mode_lease if runtime.mode is mode else RuntimeModeLease(runtime.data,mode.value)
        service=getattr(app.state,'firmware_service',None)
        clear_guard=(
            service.mode_clear_guard()
            if runtime.mode is mode and service is not None
            else contextlib.nullcontext()
        )
        try:
            if runtime.mode is not mode:
                # Keep this target lease through both the SQLite transaction and
                # file deletion.  It also prevents a target-mode process from
                # starting in the gap between those two phases.
                target_lease.acquire()
            with clear_guard:
                if runtime.mode is mode:
                    runtime.stop_all()
                    with contextlib.suppress(Exception):
                        if service:service.stop_virtual_device()
                with runtime.retention.lock:
                    result=RetentionService.clear_isolated_mode(runtime.data,mode.value,lease=target_lease)
                if runtime.mode is mode:
                    for folder in [runtime.media/'event-images',runtime.media/'event-clips',runtime.media/'thumbnails']:folder.mkdir(parents=True,exist_ok=True)
                    runtime.db.save('settings',{'value':SettingsInput().model_dump()},'main')
                return {**result,'runtime_mode':mode.value,'source_type':'storage_manager','is_simulated':mode is not RuntimeMode.REAL,'real_data_untouched':mode is not RuntimeMode.REAL,'reference_files_preserved':True}
        except RuntimeError as exc:raise HTTPException(409,str(exc))
        finally:
            if runtime.mode is not mode:
                target_lease.release()

    @app.post('/api/storage/clear-demo')
    def clear_demo():return clear_current_mode(RuntimeMode.DEMO)

    @app.post('/api/storage/clear-test')
    def clear_test():return clear_current_mode(RuntimeMode.TEST)

    @app.get('/api/storage/export-pinned')
    def export_pinned():
        events=[runtime.verified_search_record(event) for event in runtime.db.list('events',limit=100000) if event.get('pinned')]
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json',json.dumps({'runtime_mode':runtime.mode.value,'exported_at':now(),'events':events},ensure_ascii=False,indent=2))
            written=set()
            for event in events:
                for key in ['screenshot_path','clip_path']:
                    path=runtime.event_service.media_path(event.get(key))
                    if path and path.is_file() and path not in written:
                        archive.write(path,f'media/{path.parent.name}/{path.name}');written.add(path)
        output.seek(0)
        return StreamingResponse(output,media_type='application/zip',headers={'Content-Disposition':'attachment; filename="objectmemory-pinned.zip"','X-ObjectMemory-Runtime-Mode':runtime.mode.value,'X-ObjectMemory-Source-Type':'pinned_export','X-ObjectMemory-Is-Simulated':str(runtime.mode is not RuntimeMode.REAL).lower()})

    @app.post('/api/storage/delete-real')
    def delete_real(body:DeleteRealInput,request:Request):
        if body.confirmation!='DELETE REAL DATA':raise HTTPException(422,'确认文本必须完全等于 DELETE REAL DATA。')
        if runtime.mode is not RuntimeMode.REAL:raise HTTPException(409,'只能在 REAL 进程中删除真实事件。')
        if not runtime.sessions.local(request):raise HTTPException(403,'删除所有真实记录必须在本机操作。')
        runtime.stop_all();deleted=0
        for event in runtime.db.list('events',limit=100000):deleted+=runtime.retention.delete_event(event['id'],force=True)['deleted_events']
        for row in runtime.db.list('item_current_state',limit=100000):runtime.db.delete('item_current_state',row['id'])
        return {'deleted_events':deleted,'reference_images_preserved':True,'message':'真实事件和当前状态已删除；注册参考图片已保留。',**response_meta('admin_action',False)}

    @app.get('/api/system/diagnostics')
    def diagnostics():
        port=os.environ.get('OM_PORT','8018')
        return {'data_dir':str(runtime.data),'database_path':str(runtime.db.path),'media_dir':str(runtime.media),'python':os.sys.version.split()[0],'opencv':cv2.__version__,'lan_urls':[f'http://{h}:{port}' for h in lan_addresses()],'detection_modes':['aruco','experimental'],'hardware_verified':False,'runtime_mode':runtime.mode.value,'source_type':'service','is_simulated':runtime.mode is not RuntimeMode.REAL}

    @app.get('/api/demo/markers/{name}')
    def marker(name:str):
        path=(ROOT/'demo'/'markers'/name).resolve()
        if not path.is_relative_to(ROOT/'demo'/'markers') or not path.is_file():raise HTTPException(404,'标签文件不存在。')
        return FileResponse(path)

    # Device module is independent and performs its own token authentication on public device routes.
    try:
        from . import firmware
        # The outer factory owns the lease already.  Do not enter it as a
        # context manager here: __exit__ would release the running app's lock.
        app.state.firmware_service=firmware.setup(runtime.db.path,runtime.bind_device,runtime.data,runtime.stop,runtime_mode=runtime.mode.value,listener_info_provider=listener_info_provider)
        app.include_router(firmware.router)
        from .device_network import router as device_network_router
        app.include_router(device_network_router)
        app.state.sessions=runtime.sessions
    except ImportError:
        if not testing:
            print('设备模块尚未安装。')

    for folder in ['event-images','event-clips','scene-images']:
        app.mount('/media/'+folder,StaticFiles(directory=runtime.media/folder),name='media-'+folder)
    app.mount('/media/registered-items',StaticFiles(directory=runtime.data/'registered-items'),name='media-registered-items')
    web=ROOT/'apps'/'web'/'dist'
    if (web/'assets').exists():app.mount('/assets',StaticFiles(directory=web/'assets'),name='assets')

    @app.get('/{path:path}')
    def frontend(path:str):
        if path.startswith(('api/','ws/','media/')):raise HTTPException(404,'接口或文件不存在。')
        candidate=(web/path).resolve()
        if candidate.is_relative_to(web) and candidate.is_file():return FileResponse(candidate)
        if Path(path).suffix:raise HTTPException(404,'文件不存在。')
        if (web/'index.html').exists():return FileResponse(web/'index.html')
        return JSONResponse({'message':'API 已启动。请执行 scripts/build.ps1 构建前端，或使用 scripts/dev.ps1。'})

    return app
