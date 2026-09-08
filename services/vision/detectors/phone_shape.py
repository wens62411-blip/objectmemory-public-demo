"""Bounded geometry-only phone-front proposals, never identity decisions.

The ``cell phone`` label routes an untrusted candidate to appearance matching;
it is NOT a COCO classifier verdict. Black rectangles/remotes can be proposed.
Only the caller's independent identity/continuity gates may admit observations.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import heapq
import math
import time
from typing import Any

import cv2
import numpy as np

from .base import Detection
from .appearance import normalize_category


@dataclass(frozen=True)
class PhoneShapeSettings:
    max_width: int = 640
    max_candidates: int = 3
    max_contours: int = 300
    max_input_pixels: int = 16_777_216
    dark_threshold: int = 85
    canny_low: int = 50
    canny_high: int = 150
    min_area_fraction: float = .007
    max_area_fraction: float = .8
    min_short_pixels: int = 12
    min_long_pixels: int = 32
    min_aspect: float = 1.4
    max_aspect: float = 3.3
    min_rectangularity: float = .8
    polygon_epsilon: float = .025
    min_dark_boundary_fraction: float = .22
    nms_iou: float = .45
    boundary_band_pixels: int = 5
    bbox_padding_pixels: int = 2

    def __post_init__(self):
        integers = {'max_width':(32,640),'max_candidates':(1,3),'max_contours':(1,300),
            'max_input_pixels':(1,16_777_216),'dark_threshold':(0,255),'canny_low':(0,254),
            'canny_high':(1,255),'min_short_pixels':(2,640),'min_long_pixels':(2,640),
            'boundary_band_pixels':(1,9),'bbox_padding_pixels':(0,8)}
        ranges = {'min_area_fraction':(.00001,1),'max_area_fraction':(.00001,1),
            'min_aspect':(1,10),'max_aspect':(1,10),'min_rectangularity':(.1,1),
            'polygon_epsilon':(.001,.1),'min_dark_boundary_fraction':(0,1),'nms_iou':(.01,1)}
        for name,(low,high) in integers.items():
            value=getattr(self,name)
            if type(value) is not int or not low<=value<=high:
                raise ValueError(f'phone_shape.{name} must be an integer in [{low}, {high}]')
        for name,(low,high) in ranges.items():
            value=getattr(self,name)
            if type(value) not in (int,float) or not math.isfinite(value) or not low<=value<=high:
                raise ValueError(f'phone_shape.{name} must be finite in [{low}, {high}]')
        if self.canny_low>=self.canny_high:
            raise ValueError('phone_shape.canny_low must be smaller than canny_high')
        if self.min_area_fraction>self.max_area_fraction or self.min_aspect>self.max_aspect:
            raise ValueError('phone_shape minimum must not exceed maximum')
        if self.min_short_pixels>self.min_long_pixels or self.min_long_pixels>self.max_width:
            raise ValueError('phone_shape size thresholds must fit the bounded processing image')

    def to_dict(self):
        return asdict(self)


def _iou(a,b):
    ax,ay,aw,ah=a;bx,by,bw,bh=b
    intersection=max(0,min(ax+aw,bx+bw)-max(ax,bx))*max(0,min(ay+ah,by+bh)-max(ay,by))
    return intersection/max(1,aw*ah+bw*bh-intersection)


class PhoneShapeProposer:
    name='phone_shape_proposal'

    def __init__(self,settings: dict[str,Any] | PhoneShapeSettings | None=None):
        if settings is None:
            self.settings=PhoneShapeSettings()
        elif isinstance(settings,PhoneShapeSettings):
            self.settings=settings
        elif isinstance(settings,dict):
            try:self.settings=PhoneShapeSettings(**settings)
            except TypeError as exc:raise ValueError('Unknown phone_shape configuration field') from exc
        else:
            raise ValueError('phone_shape settings must be a mapping or PhoneShapeSettings')
        self._last_stats={}

    def health(self):
        return {'name':self.name,'available':True,'category_evidence':'geometry_only',
                'certainty':'candidate','config':self.settings.to_dict(),**self._last_stats}

    def detect(self,frame: np.ndarray) -> list[Detection]:
        started=time.perf_counter()
        config=self.settings
        if not isinstance(frame,np.ndarray) or frame.dtype!=np.uint8 or frame.ndim!=3 or frame.shape[2]!=3:
            raise ValueError('phone_shape frame must be uint8 BGR HWC')
        h,w=frame.shape[:2]
        if min(h,w)<1 or h*w>config.max_input_pixels:
            raise ValueError('phone_shape frame is empty or exceeds the pixel limit')
        scale=min(1.,config.max_width/max(h,w))
        small=cv2.resize(frame,(max(1,round(w*scale)),max(1,round(h*scale))),interpolation=cv2.INTER_AREA)
        sh,sw=small.shape[:2]
        gray=cv2.cvtColor(small,cv2.COLOR_BGR2GRAY)
        blur=cv2.GaussianBlur(gray,(3,3),0)
        dark=cv2.inRange(blur,0,config.dark_threshold)
        edges=cv2.Canny(blur,config.canny_low,config.canny_high)
        edges=cv2.morphologyEx(edges,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8))
        contours=[]
        for origin,mask in (('dark_boundary',dark),('edge_boundary',edges)):
            found,_=cv2.findContours(mask,cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)
            contours.extend((origin,c) for c in found)
        # findContours is itself bounded by <=640x640 pixels; retain and inspect
        # at most 300 contours even on adversarial texture/noise.
        examined=heapq.nlargest(config.max_contours,contours,key=lambda pair:cv2.contourArea(pair[1]))
        raw=[]
        for origin,contour in examined:
            area=float(cv2.contourArea(contour))
            if not config.min_area_fraction<=area/(sw*sh)<=config.max_area_fraction:continue
            perimeter=cv2.arcLength(contour,True)
            polygon=cv2.approxPolyDP(contour,config.polygon_epsilon*perimeter,True)
            if len(polygon)!=4 or not cv2.isContourConvex(polygon):continue
            short,long=sorted(cv2.minAreaRect(polygon)[1])
            if short<config.min_short_pixels or long<config.min_long_pixels:continue
            aspect=long/max(short,1e-9)
            if not config.min_aspect<=aspect<=config.max_aspect:continue
            rectangularity=area/max(short*long,1)
            if rectangularity<config.min_rectangularity:continue
            band=np.zeros_like(gray)
            cv2.polylines(band,[polygon],True,255,config.boundary_band_pixels)
            boundary=gray[band>0]
            darkness=float(np.mean(boundary<=config.dark_threshold)) if len(boundary) else 0.
            if darkness<config.min_dark_boundary_fraction:continue
            x,y,bw,bh=cv2.boundingRect(polygon)
            pad=config.bbox_padding_pixels
            # Use actual rounded resize dimensions for inverse coordinates.
            x0=max(0,round((x-pad)*w/sw));y0=max(0,round((y-pad)*h/sh))
            x1=min(w,round((x+bw+pad)*w/sw));y1=min(h,round((y+bh+pad)*h/sh))
            if x1<=x0 or y1<=y0:continue
            score=float(min(1,rectangularity)*.65+darkness*.35)
            source_polygon=polygon[:,0,:].astype(np.float64)*np.asarray([w/sw,h/sh])
            raw.append({'bbox':(x0,y0,x1-x0,y1-y0),'geometry_score':score,
                'polygon':source_polygon.round(2).tolist(),'rectangularity':round(rectangularity,6),
                'dark_boundary_fraction':round(darkness,6),'aspect':round(aspect,6),'origin':origin})
        selected=[]
        for candidate in sorted(raw,key=lambda row:row['geometry_score'],reverse=True):
            if any(_iou(candidate['bbox'],kept['bbox'])>config.nms_iou for kept in selected):continue
            selected.append(candidate)
            if len(selected)>=config.max_candidates:break
        self._last_stats={'contours_examined':len(examined),'raw_candidates':len(raw),
            'candidate_count':len(selected),'processing_size':[sw,sh],
            'last_ms':round((time.perf_counter()-started)*1000,3)}
        results=[]
        for index,candidate in enumerate(selected):
            x,y,bw,bh=candidate['bbox']
            results.append(Detection(identity=f'generic:phone_shape:{index}',label='cell phone',
                bbox=(x,y,bw,bh),center=(x+bw/2,y+bh/2),confidence=candidate['geometry_score'],
                detection_mode='experimental',metadata={**candidate,'backend':self.name,
                    'category_evidence':'geometry_only','certainty':'candidate',
                    'score_kind':'geometry_score_not_probability','display_label':'外形像手机 · 待确认'}))
        return results

    def supplement(self,frame: np.ndarray,detections: list[Detection]) -> list[Detection]:
        if any(normalize_category(detection.label)=='phone' for detection in detections):
            return detections
        additions=[candidate for candidate in self.detect(frame)
                   if not any(_iou(candidate.bbox,existing.bbox)>self.settings.nms_iou for existing in detections)]
        return [*detections,*additions]
