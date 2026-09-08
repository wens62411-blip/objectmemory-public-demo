from difflib import SequenceMatcher
from datetime import datetime, timezone
import re

REAL_CONFIRMED_SOURCES={'opencv_camera','esp32_real','authorized_screen_capture'}


def _evidence_instant(value):
    """Return a comparable UTC instant for heterogeneous ISO-8601 evidence.

    Lexicographic ordering is incorrect when two equivalent/ordered instants use
    different offsets (for example ``+08:00`` and ``Z``).  Invalid legacy values
    sort before valid timestamps and remain readable without being promoted.
    """
    if not value:
        return (0, datetime.min.replace(tzinfo=timezone.utc))
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (1, parsed.astimezone(timezone.utc))
    except (TypeError, ValueError):
        return (0, datetime.min.replace(tzinfo=timezone.utc))


def location_result(item,events,runtime_mode='REAL',current_state=None):
    legacy=(current_state or {}).get('last_observed') or {}
    if legacy.get('holding_status')=='possibly_held':
        # Old versions used this label for proximity alone. Do not retroactively
        # claim the new continuous co-motion proof for existing observations.
        current_state={**current_state,'last_observed':{**legacy,'holding_status':'nearby',
            'interaction':{**(legacy.get('interaction') or {}),'holding_status':'nearby',
                           'release_observed':False,'reason':'legacy_proximity_only'}}}
    events=sorted(events,key=lambda e:_evidence_instant(e.get('timestamp_end') or e.get('timestamp_start')),reverse=True)
    def first(predicate):
        return next((e for e in events if predicate(e)),None)
    def is_confirmed(event):
        return (
            bool(event)
            and event.get('evidence_status')=='confirmed'
            and event.get('final_status')=='confirmed_placed'
            and (
                runtime_mode!='REAL'
                or (
                    event.get('runtime_mode')=='REAL'
                    and event.get('is_simulated') is False
                    and event.get('source_type') in REAL_CONFIRMED_SOURCES
                    and (
                        event.get('source_type')!='esp32_real'
                        or bool(event.get('source_attestation_id'))
                        )
                )
            )
        )
    # The current-state row is the single source of truth for the latest
    # accepted placement.  In particular, a human correction ingested now must
    # not be hidden by an older video episode whose source end timestamp happens
    # to lie later (clock skew and delayed ingestion are both possible).
    current_evidence_id=(current_state or {}).get('evidence_event_id')
    current_evidence=first(lambda e: current_evidence_id and (e.get('event_id')==current_evidence_id or e.get('id')==current_evidence_id))
    confirmed=current_evidence if is_confirmed(current_evidence) else first(is_confirmed)
    seen=confirmed
    picked=None
    occluded=None
    exited=None
    latest=events[0] if events else None
    evidence=confirmed or seen or picked or occluded or exited
    status=latest['event_type'] if latest else 'unknown'
    place=lambda e: f"{e.get('room_name') or '未命名房间'} · {e.get('zone_name') or '未定义区域'}"
    observed=(current_state or {}).get('last_observed') or {}
    current_time=observed.get('observed_at') or (current_state or {}).get('last_seen_at')
    confirmed_time=(confirmed or {}).get('timestamp_end') or (confirmed or {}).get('timestamp_start')
    current_status=str((current_state or {}).get('status') or '').lower()
    missing_status=current_status in {'occluded','lost','offline','exited_view'}
    current_is_newer=bool(current_state and (not confirmed or _evidence_instant(current_time)>_evidence_instant(confirmed_time)
                        or (missing_status and _evidence_instant(current_time)==_evidence_instant(confirmed_time))))
    current_place=f"{observed.get('room_name') or (current_state or {}).get('current_room') or '未命名房间'} · {observed.get('zone_name') or (current_state or {}).get('current_zone') or '未定义区域'}"
    observation=None
    if current_state:
        observation={
            'id':None,'event_id':None,'event_type':current_status if missing_status else 'last_seen',
            'item_id':item['id'],'item_name':item['name'],
            'camera_id':observed.get('camera_id') or current_state.get('current_camera'),
            'camera_name':observed.get('camera_name'),
            'room_name':observed.get('room_name') or current_state.get('current_room'),
            'zone_name':observed.get('zone_name') or current_state.get('current_zone'),
            'final_position':observed.get('position') or current_state.get('current_position'),
            'timestamp_start':current_time,'timestamp_end':current_time,'confidence':observed.get('confidence',current_state.get('confidence')),
            'evidence_status':'observation_only','runtime_mode':current_state.get('runtime_mode'),
            'source_type':current_state.get('source_type'),'is_simulated':current_state.get('is_simulated'),
            'source_session_id':observed.get('source_session_id') or current_state.get('source_session_id'),
            'source_attestation_id':current_state.get('source_attestation_id'),
            'source_frame':observed.get('source_frame'),'detection_mode':observed.get('detection_mode'),
            'identity_evidence':observed.get('identity_evidence'),'screenshot_path':observed.get('screenshot_path'),
            'holding_status':((observed.get('current_interaction') or {}).get('holding_status','not_established')
                              if missing_status else observed.get('holding_status','not_established')),
            'interaction':observed.get('current_interaction') if missing_status else observed.get('interaction'),
            'evidence_type':'observed',
            'screenshot_sha256':observed.get('screenshot_sha256'),
            'screenshot_source_frame':observed.get('screenshot_source_frame'),
            'screenshot_observed_at':observed.get('screenshot_observed_at'),
            'screenshot_source_session_id':observed.get('screenshot_source_session_id'),
            'image_status':observed.get('image_status','not_available'),'image_error':observed.get('image_error'),
            'clip_path':None,'notes':'最后看到不是确认放下；截图时间和帧号单独标明，观察不要求录像。',
        }
        if current_is_newer:
            evidence=seen=observation
    if current_is_newer and current_status=='occluded':
        status='occluded'
        answer=f"{item['name']}最后在{current_place}被看到，随后被遮挡；当前具体位置未确认。"
        if confirmed:answer+=f" 上一次确认放置在{place(confirmed)}。"
    elif current_is_newer and current_status in {'exited_view','offline','lost'}:
        status=current_status
        answer=f"{item['name']}最后在{current_place}被看到，之后离开视野或视频源中断；当前去向未确认。"
        if confirmed:answer+=f" 上一次确认放置在{place(confirmed)}。"
    elif current_is_newer:
        status='last_seen'
        answer=f"{item['name']}最后在{current_place}被看到，尚未有新的确认放置证据。"
        if confirmed:answer+=f" 上一次确认放置在{place(confirmed)}。"
    elif not evidence and not current_state:
        answer='暂时没有真实摄像头产生的位置记录。' if runtime_mode=='REAL' else f"当前 {runtime_mode} 模式还没有{item['name']}的位置记录。"
    elif status=='occluded':
        evidence=occluded
        answer=f"{item['name']}最后出现在{place(occluded)}，随后被遮挡，当前具体位置未完全确认。"
    elif status=='exited_view':
        evidence=exited
        answer=f"{item['name']}从{place(exited)}离开摄像头视野；当前去向未确认。"
    elif confirmed:
        answer=f"{item['name']}最后一次确认放在{place(confirmed)}。"
    elif current_state:
        answer=f"{item['name']}最后在{current_state.get('current_room') or '未命名房间'} · {current_state.get('current_zone') or '未定义区域'}被看到，尚未确认已放稳。"
    else:
        answer=f"{item['name']}最后出现在{place(seen or evidence)}，尚未确认已放稳。"
    evidence_provenance = None
    if evidence:
        evidence_provenance = {
            'validation_run_id': evidence.get('validation_run_id'),
            'detection_mode': evidence.get('detection_mode'),
            'aruco_id': evidence.get('aruco_id'),
            'source_session_id': evidence.get('source_session_id'),
            'trajectory': evidence.get('trajectory'),
            'before_screenshot': evidence.get('before_screenshot'),
            'before_screenshot_sha256': evidence.get('before_screenshot_sha256'),
            'after_screenshot': evidence.get('after_screenshot'),
            'after_screenshot_sha256': evidence.get('after_screenshot_sha256'),
            'clip_path': evidence.get('clip_path'),
            'clip_sha256': evidence.get('clip_sha256'),
        }
    return dict(
        item=item,last_confirmed=confirmed,last_seen=seen,last_picked_up=picked,
        last_occluded=occluded,last_exited=exited,status=status,answer=answer,
        evidence=evidence,evidence_provenance=evidence_provenance,
        current_state=current_state,runtime_mode=runtime_mode,
        last_observed=observation,last_confirmed_placement=confirmed,
        location_hypotheses=(current_state or {}).get('location_hypotheses') or [],
        source_type=(evidence or current_state or {}).get('source_type'),
        is_simulated=bool((evidence or current_state or {}).get('is_simulated')),
        authenticity_label=(
            '人工纠正' if evidence and evidence.get('manually_corrected')
            else '真实验收确认' if evidence and evidence.get('validation_run_id')
            else '最后看到' if current_state else '暂无真实记录'
        ),
    )


def match_items(query,items):
    query=query.lower().replace(' ','')
    stop_words=['最后一次','最后出现','最近','哪里','在哪','放哪','放在','找一下','帮我找','我的','是谁','移动了','移动','谁','被','了','吗']
    def meaningful(text):
        for word in stop_words:text=text.replace(word,'')
        return re.sub(r'[？?。！!，,\s]','',text)
    clean_query=meaningful(query)
    ranked=[]
    exact=[]
    for item in items:
        names=[item['name'],*(item.get('aliases') or [])]
        item_type=item.get('type','')
        names += {'phone':['手机','电话'],'keys':['钥匙'],'wallet':['钱包']}.get(item_type,[])
        names=[meaningful(n.lower()) for n in names if n]
        if any(n and n in clean_query for n in names):
            exact.append(item)
            continue
        score=max((SequenceMatcher(None,n,clean_query).ratio() for n in names if n),default=0)
        if score>=0.5:
            ranked.append((score,item))
    if exact:return exact[:5]
    return [i for _,i in sorted(ranked,key=lambda p:p[0],reverse=True)][:5]
