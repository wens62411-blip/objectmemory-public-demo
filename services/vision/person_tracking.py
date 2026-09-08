"""Bounded anonymous same-source IoU association, not human identification."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from uuid import uuid4

from .detectors.person_pose import PersonPoseConfig, normalized_box


@dataclass(frozen=True)
class PersonTrackingSettings:
    min_iou: float = .3
    ambiguity_margin: float = .1
    max_gap_seconds: float = .75
    snapshot_max_age_seconds: float = 3.

    def __post_init__(self):
        ranges = {"min_iou":(.05,.95),"ambiguity_margin":(.01,.5),"max_gap_seconds":(.1,3.),
                  "snapshot_max_age_seconds":(.2,6.)}
        for key,(low,high) in ranges.items():
            value = getattr(self,key)
            if type(value) not in (int,float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"Invalid person tracking setting: {key}")


def person_configuration(value=None):
    if value is None:
        value = {}
    if not isinstance(value,dict) or set(value)-set(PersonPoseConfig.__dataclass_fields__)-{"tracking"}:
        raise ValueError("Unsupported person pose configuration")
    tracking = PersonTrackingSettings(**value.get("tracking",{}))
    pose = PersonPoseConfig(**{key:item for key,item in value.items() if key != "tracking"})
    return {**asdict(pose),"tracking":asdict(tracking)}


def iou(first,second):
    x,y,w,h = first
    xx,yy,ww,hh = second
    intersection = max(0,min(x+w,xx+ww)-max(x,xx))*max(0,min(y+h,yy+hh)-max(y,yy))
    return intersection/max(1e-12,w*h+ww*hh-intersection)


class AnonymousPersonTracker:
    def __init__(self, *, max_people=2, settings=None):
        if type(max_people) is not int or not 1 <= max_people <= 4:
            raise ValueError("Person tracks must be bounded to 1..4")
        self.max_people = max_people
        self.settings = settings if isinstance(settings,PersonTrackingSettings) else PersonTrackingSettings(**(settings or {}))
        self.reset()

    def reset(self):
        self._session = None
        self._frame = None
        self._timestamp = None
        self._tracks = []

    def update(self, people, source_session_id, source_frame, timestamp):
        if (not isinstance(source_session_id,str) or not source_session_id or type(source_frame) is not int or source_frame < 0
                or type(timestamp) not in (int,float) or not math.isfinite(timestamp)
                or not isinstance(people,list) or len(people)>self.max_people):
            self.reset()
            raise ValueError("Invalid or unbounded person frame metadata")
        try:
            rows = [{**row,"bbox":normalized_box(row.get("bbox"))} for row in people]
        except (TypeError, ValueError, AttributeError):
            self.reset()
            raise ValueError("Invalid person observation geometry") from None
        same_source = source_session_id == self._session
        if same_source and (source_frame <= self._frame or timestamp <= self._timestamp):
            # Replayed frames are not a new observation and must not extend TTL.
            return []
        previous = self._tracks if same_source and timestamp-self._timestamp <= self.settings.max_gap_seconds else []
        scores = [[iou(row["bbox"],old["bbox"]) for old in previous] for row in rows]
        output = []
        for index,row in enumerate(rows):
            rank = sorted(range(len(previous)),key=lambda j:scores[index][j],reverse=True)
            match = None
            if rank and scores[index][rank[0]] >= self.settings.min_iou:
                best = rank[0]
                forward_unique = len(rank)<2 or scores[index][best]-scores[index][rank[1]] >= self.settings.ambiguity_margin
                reverse = sorted((scores[i][best] for i in range(len(rows))),reverse=True)
                reverse_unique = len(reverse)<2 or reverse[0]-reverse[1] >= self.settings.ambiguity_margin
                if forward_unique and reverse_unique and scores[index][best] == reverse[0]:
                    match = previous[best]
            output.append({**row,"person_track_id":match["person_track_id"] if match else "person-"+uuid4().hex[:16],
                "tracking_stable":match is not None,"identity_kind":"anonymous_short_lived_iou_track",
                "association_status":"continued" if match else "new_or_ambiguous"})
        self._tracks = [{"person_track_id":row["person_track_id"],"bbox":row["bbox"]} for row in output]
        self._session,self._frame,self._timestamp = source_session_id,source_frame,float(timestamp)
        return output
