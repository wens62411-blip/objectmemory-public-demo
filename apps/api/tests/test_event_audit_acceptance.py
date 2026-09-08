"""Real EventService/SQLite/media integration using explicitly generated tags."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import pytest

from apps.api.app.event_service import EventService
from apps.api.app.db import Database
from apps.api.app.runtime_mode import RuntimeMode
from apps.api.tests.test_physical_acceptance import (
    acceptance_clip_frames, acceptance_fixture, activate_run, vision_event_payload,
)
from scripts.event_audit import apply_cleanup, audit, cleanup_plan


def recorded_acceptance(tmp_path):
    db, media, item, camera, first, second, suite, acceptance = acceptance_fixture(tmp_path)
    run, started = activate_run(db, acceptance, suite, item, camera, first, second)
    service = EventService(db, RuntimeMode.REAL, media, None, lambda _: ("opencv_camera", False), acceptance)
    frames, _before, after = acceptance_clip_frames()
    event = service.record(vision_event_payload(run, started), after, frames)
    # Exercise classification and deletion eligibility without the current-state
    # protection hiding an incorrect no_evidence classification.
    db.delete("item_current_state", f"REAL:{item['id']}")
    return db, service, run, suite, event


def file_fingerprints(root):
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in root.rglob("*") if path.is_file()
    }


def test_real_p0_event_is_verified_by_readonly_audit_and_not_cleanup_target(tmp_path):
    db, service, _run, _suite, event = recorded_acceptance(tmp_path)
    before = file_fingerprints(tmp_path / "data")
    report = audit([db.path])
    assert report["read_only"] is True
    row = report["events"][0]
    assert row["protected"] is False
    assert row["classification"] == "real_verified", row
    assert row["flags"] == []
    assert row["provenance_contract"]["acceptance_v2"] is True
    assert set(row["media"]) == {"before", "image", "clip"}
    assert all(value["full_valid"] for value in row["media"].values())
    assert file_fingerprints(tmp_path / "data") == before
    plan = cleanup_plan(report)
    assert plan["delete_events"] == 0
    result = apply_cleanup(plan, tmp_path / "data")
    assert result["deleted_events"] == 0
    assert db.get("events", event["id"]) is not None
    assert all(service.media_path(event[field]).is_file() for field in ("before_screenshot", "after_screenshot", "clip_path"))


@pytest.mark.parametrize("damage", ["before_missing", "before_hash", "before_registry", "wrong_run", "failed_run", "missing_run", "wrong_suite", "duplicate_media_role"])
def test_v2_strings_do_not_override_missing_or_tampered_acceptance_evidence(tmp_path, damage):
    db, service, run, suite, event = recorded_acceptance(tmp_path)
    before_media = next(row for row in db.list("event_media", {"event_id": event["event_id"]}) if row["role"] == "before")
    if damage == "before_missing":
        service.media_path(event["before_screenshot"]).unlink()
    elif damage == "before_hash":
        service.media_path(event["before_screenshot"]).write_bytes(b"tampered")
    elif damage == "before_registry":
        db.delete("event_media", before_media["id"])
    elif damage == "wrong_run":
        db.save("acceptance_runs", {"event_id": "unrelated-event"}, run["id"])
    elif damage == "failed_run":
        db.save("acceptance_runs", {"status": "FAILED", "outcome": "FAILED"}, run["id"])
    elif damage == "missing_run":
        db.delete("acceptance_runs", run["id"])
    elif damage == "wrong_suite":
        db.save("acceptance_suites", {"camera_id": "unrelated-camera"}, suite["id"])
    else:
        db.save("event_media", {"role": "after"}, before_media["id"])
    before = file_fingerprints(tmp_path / "data")
    report = audit([db.path])
    row = report["events"][0]
    assert row["classification"] != "real_verified", row
    assert row["provenance_contract"]["acceptance_v2"] is False
    if damage.startswith("before") or damage == "duplicate_media_role":
        assert "no_evidence" in row["flags"]
    assert file_fingerprints(tmp_path / "data") == before


def four_recorded_acceptances(tmp_path):
    db, media, item, camera, first, second, suite, acceptance = acceptance_fixture(tmp_path)
    service = EventService(db, RuntimeMode.REAL, media, None, lambda _: ("opencv_camera", False), acceptance)
    events=[]
    for scenario_index in range(3,7):
        if scenario_index==3:
            run,started=activate_run(db,acceptance,suite,item,camera,first,second)
        else:
            run=acceptance.create({"suite_id":suite["id"],"scenario_index":scenario_index})
            started=datetime.now(timezone.utc)-timedelta(seconds=10)
            session_id=f"session-{run['id']}"
            db.save("source_sessions",{
                "camera_id":camera["id"],"runtime_mode":"REAL","source_type":"opencv_camera","is_simulated":False,
                "started_at":started.isoformat(),"first_frame":0,"last_frame":100,
                "last_frame_at":(started+timedelta(seconds=30)).isoformat(),"status":"streaming","continuity_ok":True,
            },session_id)
            run=acceptance.activate(run["id"],{"ready":True,"camera_id":camera["id"],"source_session_id":session_id,"reconnect_epoch":0})
            acceptance.observe_track({
                "camera_id":camera["id"],"item_id":item["id"],"runtime_mode":"REAL","source_type":"opencv_camera",
                "is_simulated":False,"source_session_id":session_id,"reconnect_epoch":0,"detector_backend":"aruco",
                "source_frame":9,"source_timestamp":started.isoformat(),"center":[0.8 if scenario_index%2==0 else 0.2,0.5],
            })
        payload=vision_event_payload(run,started)
        frames,_before,after=acceptance_clip_frames()
        if scenario_index%2==0:
            for key in ("from_position","to_position","final_position"):
                payload[key]=[1-payload[key][0],payload[key][1]]
            for point in payload["trajectory"]:
                point["center_norm"][0]=1-point["center_norm"][0]
                point["center"][0]=640-point["center"][0]
            # Translate a fresh marker, never mirror its code bits.
            size=32
            tag=cv2.cvtColor(cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50),0,size),cv2.COLOR_GRAY2BGR)
            for index,frame in enumerate(frames):
                frame[:]=235
                x_norm=0.8 if index<=25 else 0.2 if index>=35 else 0.8-0.6*(index-25)/10
                x,y=round(x_norm*frame.shape[1])-size//2,frame.shape[0]//2-size//2
                frame[y-3:y+size+3,x-3:x+size+3]=255
                frame[y:y+size,x:x+size]=tag
            after=frames[35]
        events.append(service.record(payload,after,frames))
    return db,service,acceptance,suite,events


def test_offline_cleanup_keeps_two_events_and_verified_policy_deleted_history(tmp_path):
    db,service,acceptance,suite,events=four_recorded_acceptances(tmp_path)
    before=file_fingerprints(tmp_path/"data")
    report=audit([db.path])
    assert all(row["classification"]=="real_verified" for row in report["events"])
    plan=cleanup_plan(report)
    assert plan["delete_events"]==2
    assert file_fingerprints(tmp_path/"data")==before
    result=apply_cleanup(plan,tmp_path/"data")
    assert result["success"] is True,result
    assert result["deleted_events"]==2
    assert db.count("events")==2 and db.count("event_media")==6
    assert {row["event_id"] for row in db.list("events")}=={event["event_id"] for event in events[-2:]}
    assert db.count("acceptance_retention_receipts",{"status":"completed"})==2
    summary=acceptance.suite_summary(suite["id"])
    assert summary["summary"]["movement_passed"]==4
    assert summary["summary"]["movement_policy_deleted"]==2
    assert summary["summary"]["movement_evidence_retained"]==2
    assert all(not service.media_path(event[field]).exists() for event in events[:2] for field in ("before_screenshot","after_screenshot","clip_path"))
    assert all(service.media_path(event[field]).exists() for event in events[-2:] for field in ("before_screenshot","after_screenshot","clip_path"))


def test_offline_partial_delete_receipt_is_pending_until_retry_finishes(tmp_path,monkeypatch):
    db,service,acceptance,suite,events=four_recorded_acceptances(tmp_path)
    victim=service.media_path(events[0]["before_screenshot"])
    original_unlink=Path.unlink

    def fail_one_file(path,*args,**kwargs):
        if path.resolve()==victim.resolve():
            raise PermissionError("controlled P0 deletion failure")
        return original_unlink(path,*args,**kwargs)

    monkeypatch.setattr(Path,"unlink",fail_one_file)
    result=apply_cleanup(cleanup_plan(audit([db.path])),tmp_path/"data")
    assert result["success"] is False
    assert db.count("events")==3
    assert db.count("acceptance_retention_receipts",{"status":"pending_delete"})==1
    assert acceptance.suite_summary(suite["id"])["summary"]["movement_passed"]==3
    monkeypatch.setattr(Path,"unlink",original_unlink)
    retried=apply_cleanup(cleanup_plan(audit([db.path])),tmp_path/"data")
    assert retried["success"] is True,retried
    assert db.count("events")==2 and db.count("event_media")==6
    assert db.count("acceptance_retention_receipts",{"status":"completed"})==2
    assert acceptance.suite_summary(suite["id"])["summary"]["movement_passed"]==4


def test_empty_cleanup_reports_existing_database_bytes_without_claiming_reclaimed_space(tmp_path):
    data=tmp_path/"data"
    db=Database(data/"database"/"objectmemory.sqlite","REAL")
    plan=cleanup_plan(audit([db.path]))
    before=file_fingerprints(data)
    assert plan["delete_events"]==0 and plan["targets"]==[] and plan["orphan_media"]==[]
    assert plan["managed_bytes_before"]==db.path.stat().st_size>0
    result=apply_cleanup(plan,data)
    assert result["apply_status"]=="no_changes"
    assert result["managed_bytes_after"]==result["managed_bytes_before"]==db.path.stat().st_size
    assert result["bytes_after"]==result["bytes_before"]
    assert result["deleted_events"]==result["deleted_screenshots"]==result["deleted_clips"]==0
    assert file_fingerprints(data)==before
    assert not (data/"backups").exists()
