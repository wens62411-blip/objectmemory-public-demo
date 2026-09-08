from difflib import SequenceMatcher
from datetime import datetime, timezone
from heapq import nlargest
import re

REAL_CONFIRMED_SOURCES={'opencv_camera','esp32_real','authorized_screen_capture'}
TYPE_ALIASES={'phone':('手机','电话'),'keys':('钥匙',),'wallet':('钱包',)}


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
    latest=confirmed=current_evidence=None
    for event in events:
        instant=_evidence_instant(event.get('timestamp_end') or event.get('timestamp_start'))
        # Strict comparisons preserve the input order for equal/invalid times.
        if latest is None or instant>latest_instant:
            latest,latest_instant=event,instant
        if (confirmed is None or instant>confirmed_instant) and is_confirmed(event):
            confirmed,confirmed_instant=event,instant
        if (current_evidence_id and (event.get('event_id')==current_evidence_id or event.get('id')==current_evidence_id)
                and (current_evidence is None or instant>current_instant)):
            current_evidence,current_instant=event,instant
    if is_confirmed(current_evidence):confirmed=current_evidence
    evidence=seen=confirmed
    status=latest['event_type'] if latest else 'unknown'
    place=lambda e: f"{e.get('room_name') or '未命名房间'} · {e.get('zone_name') or '未定义区域'}"
    observed=(current_state or {}).get('last_observed') or {}
    state_time=(current_state or {}).get('last_seen_at')
    if observed and _evidence_instant(state_time)>_evidence_instant(observed.get('observed_at')):
        # A newly committed movement advances the authoritative current-state
        # coordinates but deliberately keeps the independent observation photo.
        # Its older photo clock must not hide a newer move, occlusion or exit.
        # Project the current location without rewriting the persisted snapshot
        # or assigning the old picture a new frame/time/identity proof.
        bound_event=current_evidence if (
            current_evidence
            and current_evidence.get('camera_id')==current_state.get('current_camera')
            and current_evidence.get('source_session_id')==current_state.get('source_session_id')
            and _evidence_instant(current_evidence.get('timestamp_end') or current_evidence.get('timestamp_start'))==_evidence_instant(state_time)
        ) else {}
        # Legacy proximity-only rows had no separate observation clock/frame.
        # Falling back to their state clock is not proof of a newer frame. Keep
        # that downgraded "nearby" hint only when no event or source change can
        # supersede it; never carry co-motion/release evidence across frames.
        legacy_proximity = (
            observed.get('holding_status') == 'nearby'
            and (observed.get('interaction') or {}).get('reason') == 'legacy_proximity_only'
            and not observed.get('observed_at') and observed.get('source_frame') is None
            and not current_evidence_id
            and all(not observed.get(old) or observed.get(old) == current_state.get(new)
                    for old, new in (('camera_id', 'current_camera'), ('source_session_id', 'source_session_id')))
        )
        observed={**observed,
            'observed_at':state_time,'camera_id':current_state.get('current_camera'),
            'camera_name':bound_event.get('camera_name'),
            'room_name':current_state.get('current_room'),'zone_name':current_state.get('current_zone'),
            'position':current_state.get('current_position'),'confidence':current_state.get('confidence'),
            'source_session_id':current_state.get('source_session_id'),
            'source_frame':bound_event.get('source_frame_end'),
            'detection_mode':bound_event.get('detection_mode'),'identity_evidence':None,
            'holding_status':'nearby' if legacy_proximity else 'not_established',
            'interaction':observed.get('interaction') if legacy_proximity else None,
            'image_status':'previous_observation' if observed.get('screenshot_path') else 'not_available',
        }
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
    elif confirmed:
        answer=f"{item['name']}最后一次确认放在{place(confirmed)}。"
    else:
        answer='暂时没有真实摄像头产生的位置记录。' if runtime_mode=='REAL' else f"当前 {runtime_mode} 模式还没有{item['name']}的位置记录。"
    evidence_provenance = {key:evidence.get(key) for key in (
        'validation_run_id','detection_mode','aruco_id','source_session_id','trajectory',
        'before_screenshot','before_screenshot_sha256','after_screenshot',
        'after_screenshot_sha256','clip_path','clip_sha256',
    )} if evidence else None
    return dict(
        item=item,last_confirmed=confirmed,last_seen=seen,last_picked_up=None,
        last_occluded=None,last_exited=None,status=status,answer=answer,
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
    candidates=[]
    exact=[]
    for item in items:
        names=[meaningful(n.lower()) for n in (
            item['name'],*(item.get('aliases') or []),*TYPE_ALIASES.get(item.get('type'),()),
        ) if n]
        if any(n and n in clean_query for n in names):
            exact.append(item)
            if len(exact)==5:return exact
        elif not exact:candidates.append((item,names))
    if exact:return exact
    scores=((max((SequenceMatcher(None,n,clean_query).ratio() for n in names if n),default=0),item)
            for item,names in candidates)
    return [item for score,item in nlargest(5,scores,key=lambda pair:pair[0]) if score>=0.5]
