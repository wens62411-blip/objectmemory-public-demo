from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict


class StrictModel(BaseModel):
    model_config=ConfigDict(extra='forbid')


class CameraInput(StrictModel):
    name: str = Field(min_length=1,max_length=80)
    room_name: str = Field(default='客厅',max_length=80)
    installation: str = Field(default='',max_length=200)
    source_type: Literal['webcam','browser','esp32','rtsp','onvif','mjpeg','screen','video']
    source: str = Field(default='0',max_length=2048)
    config: dict = Field(default_factory=dict)
    enabled: bool = True
    inference_fps: float = Field(default=5,ge=1,le=20)
    save_clips: bool = True


class CameraDiagnosticInput(StrictModel):
    indices: str = Field(default='0', min_length=1, max_length=80)
    frames: int = Field(default=30, ge=30, le=120)
    timeout_seconds: float = Field(default=8, ge=3, le=30)
    overall_timeout_seconds: float = Field(default=45, ge=5, le=120)
    profile: Literal['quick', 'full'] = 'quick'


class ZoneInput(StrictModel):
    name: str=Field(min_length=1,max_length=80)
    points: list[list[float]]=Field(min_length=3,max_length=64)
    priority: int=Field(default=0,ge=-100,le=100)
    enabled: bool=True

    @field_validator('points')
    @classmethod
    def normalized(cls,value):
        if any(len(p)!=2 or any(not 0<=x<=1 for x in p) for p in value):
            raise ValueError('区域坐标必须在画面范围内')
        area=abs(sum(value[i][0]*value[(i+1)%len(value)][1]-value[(i+1)%len(value)][0]*value[i][1] for i in range(len(value))))/2
        if area<0.0001:
            raise ValueError('区域需要至少三个不共线的点')
        return value


class ItemInput(StrictModel):
    name: str=Field(min_length=1,max_length=80)
    type: str=Field(default='other',max_length=50)
    description: str=Field(default='',max_length=1000)
    owner: str=Field(default='',max_length=80)
    color: str=Field(default='',max_length=80)
    features: str=Field(default='',max_length=1000)
    aliases: list[str]=Field(default_factory=list,max_length=20)
    aruco_id: int|None=Field(default=None,ge=0,le=49)
    ring_enabled: bool=False


class SettingsInput(StrictModel):
    detection_mode: Literal['aruco','experimental']='aruco'
    inference_fps: float=Field(default=5,ge=1,le=20)
    static_seconds: float=Field(default=1.8,ge=0.5,le=10)
    pre_seconds: float=Field(default=5,ge=0,le=10)
    post_seconds: float=Field(default=5,ge=0,le=10)
    # retention_days is kept for older clients, but policy controls event/media
    # retention. MINIMAL is intentionally the default.
    retention_days: Literal[0,1,7,30]=7
    retention_policy: Literal['MINIMAL','BALANCED','FORENSIC']='MINIMAL'
    max_storage_mb: int=Field(default=500,ge=50,le=10240)
    save_clips: bool=True
    show_hands: bool=True
    # Rendering landmarks is not permission to stop temporal hand analysis.
    # Legacy show_hands=False now hides only the overlay.
    hand_detection_enabled: bool=True
    person_pose: dict=Field(default_factory=dict,validate_default=True)
    privacy_mode: Literal[True]=True
    record_events: bool=True
    min_detection_frames: int=Field(default=3,ge=2,le=60)
    min_stable_frames: int=Field(default=5,ge=2,le=120)
    max_frame_gap_seconds: float=Field(default=.75,ge=.05,le=10)
    min_move_distance: float=Field(default=.04,ge=.001,le=1)
    same_zone_move_distance: float=Field(default=.10,ge=.001,le=1)
    min_confidence: float=Field(default=.55,ge=0,le=1)
    stable_speed: float=Field(default=.025,ge=0,le=1)
    stable_position_jitter: float=Field(default=.018,ge=.001,le=.5)
    min_stable_seconds: float=Field(default=1.8,ge=0,le=30)
    movement_speed: float=Field(default=.035,ge=.001,le=1)
    occluded_seconds: float=Field(default=.6,ge=0,le=60)
    lost_seconds: float=Field(default=8,ge=.1,le=600)
    track_recovery_seconds: float=Field(default=12,ge=.1,le=600)
    capture_queue_size: int=Field(default=2,ge=1,le=4)
    evidence_buffer_max_mb: int=Field(default=64,ge=4,le=512)
    evidence_max_width: int=Field(default=640,ge=160,le=3840)
    max_pending_media_jobs: int=Field(default=2,ge=1,le=8)
    max_pending_event_clips: int=Field(default=4,ge=1,le=16)
    experimental_confidence: float=Field(default=.35,ge=.05,le=.99)
    phone_shape_enabled: bool=True
    phone_shape: dict=Field(default_factory=dict,validate_default=True)
    preview_tracking: dict=Field(default_factory=dict,validate_default=True)
    reference_match_threshold: float=Field(default=.82,ge=.1,le=1)
    reference_match_margin: float=Field(default=.06,ge=.01,le=.5)
    observation_min_frames: int=Field(default=3,ge=2,le=30)
    observation_max_speed: float=Field(default=1.5,ge=.05,le=5)
    observation_snapshot_interval_seconds: float=Field(default=30,ge=30,le=3600)
    observation_snapshot_move_distance: float=Field(default=.04,ge=.001,le=1)
    hand_object_margin: float=Field(default=.02,ge=.005,le=.1)
    max_hands: int=Field(default=2,ge=1,le=2)
    hand_detection_confidence: float=Field(default=.55,ge=.1,le=.99)
    hand_presence_confidence: float=Field(default=.5,ge=.1,le=.99)
    hand_tracking_confidence: float=Field(default=.5,ge=.1,le=.99)
    hand_action_max_hands: int=Field(default=2,ge=1,le=2)
    hand_action_max_gap_seconds: float=Field(default=.75,ge=.01,le=5)
    hand_action_min_stable_frames: int=Field(default=3,ge=2,le=30)
    hand_action_min_stable_seconds: float=Field(default=.2,ge=.01,le=5)
    hand_action_tracking_min_frames: int=Field(default=3,ge=2,le=30)
    hand_action_tracking_min_seconds: float=Field(default=.2,ge=.01,le=5)
    hand_action_history_size: int=Field(default=12,ge=3,le=60)
    hand_action_motion_window_seconds: float=Field(default=.75,ge=.05,le=5)
    hand_action_motion_min_frames: int=Field(default=3,ge=2,le=30)
    hand_action_motion_min_seconds: float=Field(default=.2,ge=.01,le=5)
    hand_action_motion_min_distance: float=Field(default=.025,ge=.001,le=1)
    hand_action_still_distance: float=Field(default=.012,ge=.0001,le=.5)
    hand_action_axis_dominance: float=Field(default=1.5,ge=1.01,le=10)
    hand_action_motion_directness: float=Field(default=.75,ge=.5,le=1)
    hand_action_match_max_distance: float=Field(default=.15,ge=.001,le=1)
    hand_action_match_max_speed: float=Field(default=1.5,ge=.01,le=10)
    hand_action_match_jitter: float=Field(default=.02,ge=0,le=.25)
    hand_action_match_margin: float=Field(default=.025,ge=.001,le=.5)
    hand_action_match_scale_ratio: float=Field(default=2,ge=1.01,le=4)
    hand_action_handedness_min_score: float=Field(default=.7,ge=.5,le=1)
    hand_action_min_palm_diagonal_ratio: float=Field(default=.015,ge=.0001,le=.5)
    hand_action_extended_angle: float=Field(default=150,ge=120,le=179)
    hand_action_curled_angle: float=Field(default=105,ge=30,le=119)
    hand_action_extension_ratio: float=Field(default=.15,ge=.01,le=1)
    hand_action_curl_distance_ratio: float=Field(default=.15,ge=0,le=1)
    hand_action_pinch_ratio: float=Field(default=.28,ge=.01,le=.5)
    hand_action_pinch_other_extended: int=Field(default=2,ge=1,le=3)
    hand_action_thumb_open_ratio: float=Field(default=.7,ge=.1,le=2)
    hand_action_thumb_folded_ratio: float=Field(default=1.15,ge=.1,le=3)
    hand_interaction_near_distance: float=Field(default=.04,ge=.005,le=.2)
    hand_interaction_separation_distance: float=Field(default=.07,ge=.01,le=.4)
    hand_interaction_min_motion_frames: int=Field(default=4,ge=3,le=60)
    hand_interaction_min_motion_seconds: float=Field(default=.45,ge=.2,le=5)
    hand_interaction_min_object_step: float=Field(default=.004,ge=.001,le=.1)
    hand_interaction_min_hand_step: float=Field(default=.004,ge=.001,le=.1)
    hand_interaction_min_total_distance: float=Field(default=.035,ge=.01,le=.5)
    hand_interaction_min_direction_cosine: float=Field(default=.8,ge=.5,le=1)
    hand_interaction_max_vector_error: float=Field(default=.02,ge=.001,le=.1)
    hand_interaction_max_relative_drift: float=Field(default=.04,ge=.001,le=.2)
    hand_interaction_release_stable_frames: int=Field(default=4,ge=3,le=60)
    hand_interaction_release_stable_seconds: float=Field(default=5,ge=5,le=10)
    hand_interaction_separation_min_seconds: float=Field(default=.6,ge=.2,le=2)
    hand_interaction_release_max_object_speed: float=Field(default=.025,ge=.001,le=.1)
    hand_interaction_release_position_jitter: float=Field(default=.018,ge=.001,le=.1)
    hand_interaction_release_min_distance_increase: float=Field(default=.01,ge=.001,le=.1)
    hand_interaction_release_valid_seconds: float=Field(default=3,ge=1,le=10)
    hand_interaction_max_gap_seconds: float=Field(default=.75,ge=.1,le=2)
    hand_interaction_state_ttl_seconds: float=Field(default=20,ge=1,le=60)
    hand_interaction_max_items: int=Field(default=128,ge=1,le=1024)
    hand_interaction_max_hands: int=Field(default=8,ge=1,le=8)
    scene_check_interval_seconds: float=Field(default=3,ge=1,le=60)
    scene_shift_threshold: float=Field(default=.04,ge=.01,le=.3)
    identity_min_crop_pixels: int=Field(default=32,ge=16,le=256)
    reference_patch_enabled: bool=True
    reference_patch: dict=Field(default_factory=dict,validate_default=True)
    reference_patch_fusion_iou: float=Field(default=.5,ge=.5,le=.9)
    reference_patch_fusion_containment: float=Field(default=.9,ge=.85,le=1)
    reference_patch_fusion_min_area_ratio: float=Field(default=.2,ge=.2,le=1)
    diagnostic_mode: bool=False

    @field_validator('reference_patch')
    @classmethod
    def bounded_reference_patch(cls,value):
        from dataclasses import asdict
        from services.vision.detectors.reference_patch import ReferencePatchSettings
        try: return asdict(ReferencePatchSettings(**value))
        except (TypeError,ValueError) as exc:
            raise ValueError(f'注册照片局部匹配配置无效：{exc}') from exc

    @field_validator('phone_shape')
    @classmethod
    def bounded_phone_shape(cls,value):
        from services.vision.detectors.phone_shape import PhoneShapeSettings
        try:
            return PhoneShapeSettings(**value).to_dict()
        except (TypeError,ValueError) as exc:
            raise ValueError(f'手机外形候选配置无效：{exc}') from exc

    @field_validator('person_pose')
    @classmethod
    def bounded_person_pose(cls,value):
        from services.vision.person_tracking import person_configuration
        try:
            return person_configuration(value)
        except (TypeError,ValueError) as exc:
            raise ValueError(f'人物姿态配置无效：{exc}') from exc

    @field_validator('preview_tracking')
    @classmethod
    def bounded_preview_tracking(cls, value):
        from dataclasses import asdict
        from services.vision.preview_tracking import PreviewTrackingSettings
        try:
            return asdict(PreviewTrackingSettings(**value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f'连续跟踪配置无效：{exc}') from exc

    @model_validator(mode='after')
    def coherent_thresholds(self):
        if self.same_zone_move_distance<self.min_move_distance:
            raise ValueError('同区域移动阈值不得小于跨区域移动阈值。')
        if self.lost_seconds<self.occluded_seconds:
            raise ValueError('丢失阈值不得小于遮挡阈值。')
        if self.hand_interaction_separation_distance<=self.hand_interaction_near_distance:
            raise ValueError('手物分离距离必须大于接近距离。')
        if (self.hand_action_history_size<self.hand_action_motion_min_frames
                or self.hand_action_motion_window_seconds<self.hand_action_motion_min_seconds
                or self.hand_action_still_distance>=self.hand_action_motion_min_distance):
            raise ValueError('手部历史窗口或静止/移动阈值不一致。')
        return self


class SearchInput(StrictModel):
    query:str=Field(min_length=1,max_length=200)


class CorrectionInput(StrictModel):
    zone_id:str|None=None
    zone_name:str|None=Field(default=None,max_length=80)
    room_name:str|None=Field(default=None,max_length=80)
    notes:str=Field(default='用户手动纠正',max_length=1000)


class PinInput(StrictModel):
    pinned: bool


class PolicyInput(StrictModel):
    policy: Literal['MINIMAL','BALANCED','FORENSIC']


class DeleteRealInput(StrictModel):
    confirmation: str=Field(min_length=1,max_length=64)
