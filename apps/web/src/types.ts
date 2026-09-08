export type RuntimeMode = 'REAL' | 'DEMO' | 'TEST'
export type ResolvedRuntimeMode = RuntimeMode | 'UNKNOWN'
export type SourceType =
  | 'opencv_camera'
  | 'browser_camera'
  | 'rtsp'
  | 'onvif'
  | 'mjpeg'
  | 'esp32_real'
  | 'authorized_screen_capture'
  | 'video_file'
  | 'virtual_esp32'
  | 'demo_seed'
  | 'test_fixture'
  // Legacy camera values remain readable while existing databases are migrated.
  | 'webcam'
  | 'browser'
  | 'esp32'
  | 'screen'
  | 'video'
export type DetectionMode = 'aruco' | 'experimental'

export interface CameraHealth {
  status: string
  status_code?: string
  fps?: number
  capture_fps?: number
  preview_fps?: number
  inference_fps?: number
  preview_target_fps?: number
  inference_target_fps?: number
  preview_sequence?: number
  preview_frame_sequence?: number
  preview_source_session_id?: string | null
  preview_age_ms?: number | null
  preview_error?: string | null
  health_snapshot_stale?: boolean
  latency_ms?: number
  dropped_frames?: number
  reconnects?: number
  error?: string | null
  backend?: string | null
  backend_id?: number | null
  owner?: string | null
  thread_id?: number | null
  subscribers?: number
  width?: number
  height?: number
  detection_mode?: string
  fallback_mode?: string | null
}

export interface Camera {
  id: string
  name: string
  room_name: string
  installation?: string
  source_type: SourceType
  source_type_provenance?: SourceType | string
  source: string
  config?: Record<string, unknown>
  enabled: boolean
  inference_fps: number
  save_clips: boolean
  runtime_mode?: RuntimeMode | string
  is_simulated?: boolean
  source_session_id?: string | null
  hardware_type?: 'virtual' | 'physical' | string
  health?: CameraHealth
}

export interface Zone {
  id: string
  camera_id: string
  name: string
  points: [number, number][]
  priority: number
  enabled: boolean
}

export interface Item {
  id: string
  name: string
  type: string
  description?: string
  owner?: string
  color?: string
  features?: string
  aliases: string[]
  aruco_id: number | null
  ring_enabled: boolean
  reference_images?: ReferenceImage[]
  recognition_profile?: RecognitionProfile | null
}

export type ImageRegion = [number, number, number, number]

export type ScenePoint = [number, number]
export type SceneVector = [number, number, number]
export type SceneSurfaceType = 'table' | 'sofa' | 'bed' | 'shelf' | 'floor' | 'other'
export interface ScenePhysical {
  width: number; depth: number; height: number; thickness: number
  origin_x: number; origin_z: number; rotation_y: number; unit: 'relative' | 'm'
}
export interface SceneCalibration {
  image_points: ScenePoint[]
  plane_points: ScenePoint[]
  validation_points?: Array<{ image: ScenePoint; plane: ScenePoint }>
}
export interface SceneSurface {
  surface_id?: string; proposal_id?: string | null; name: string; surface_type: SceneSurfaceType
  image_polygon: ScenePoint[]; map_polygon?: ScenePoint[]; confirmed_by_user: boolean
  calibration_status?: string; confidence?: number; height_optional?: number | null
  physical?: ScenePhysical | null; calibration?: SceneCalibration | null
  physical_suggestion?: ScenePhysical | null
  surface_to_world?: number[][]; image_to_surface?: number[][]
  validation?: { status: string; independent_point_count?: number; max_error?: number | null }
}
export interface SceneWorldObject {
  id: string; surface_id: string; surface_type: SceneSurfaceType; part: string; shape: 'box'
  position: SceneVector; size: SceneVector; rotation_y: number
  source: 'user_confirmed_geometry' | 'model_proposal_estimate'; estimated: boolean
  confirmed_by_user?: boolean; eligible_for_mapping?: boolean
}
export interface SceneWorldGeometry {
  schema: 'scene3d_v1'; coordinate_system: 'right_handed_y_up'; unit: 'relative' | 'm' | null
  objects: SceneWorldObject[]
  status?: 'draft' | 'confirmed' | 'empty'
}
export interface SceneDocument {
  id?: string; scene_id?: string; camera_id: string; scene_version: number; snapshot_id: string
  source_session_id: string; source_frame: number; source_timestamp: string
  runtime_mode: string; source_type: string; is_simulated: boolean
  screenshot_path: string; screenshot_sha256: string
  geometry: { source_width: number; source_height: number; coordinate_space: string }
  calibration_status: string; invalid_reason?: string | null; proposal_error?: string | null
  proposals: SceneSurface[]; surfaces: SceneSurface[]; world_geometry?: SceneWorldGeometry | null
}
export interface SceneMarker {
  item_id?: string; person_track_id?: string; camera_id: string; scene_version: number | null; frame_id: number | null
  source_session_id: string | null; observation_timestamp: string | null; source_type: string; runtime_mode: string; is_simulated: boolean
  image_position?: ScenePoint | null; image_bbox?: ImageRegion | null; map_position: SceneVector | null
  region?: { surface_id: string; name: string; world_polygon: SceneVector[] | null; approximate: true } | null
  position_status: 'mapped' | 'mapped_unvalidated' | 'region_only' | 'held' | 'unknown' | 'stale'
  mapping_method: string; reason?: string | null
  evidence_reference?: { screenshot_path?: string | null; sha256?: string | null; frame_id?: number | null; source_session_id?: string | null } | null
  freshness?: 'live' | 'stale'; expires_at?: string
}
export interface SceneResponse {
  scene: SceneDocument | null; items: SearchResult[]; markers?: SceneMarker[]; persons?: SceneMarker[]
  runtime_mode: string; source_type: string; is_simulated: boolean; scene_stability?: { status: string } | null
}

export interface CandidateProvenance {
  proposal_backend?: string | null
  category_evidence?: 'model' | 'geometry_only' | string
  geometry_score?: number | null
}

export interface ReferenceImage {
  id: string
  path?: string
  url?: string
  width?: number
  height?: number
  sha256?: string
  region?: ImageRegion | null
  region_confirmed?: boolean
  suggested_regions?: Array<{ bbox: ImageRegion; label: string; score: number } & CandidateProvenance>
  status?: string
  quality?: { suggestion_error?: string | null; target_confirmation_required?: boolean }
}

export interface ModelPreparation {
  status?: string
  downloaded_bytes?: number
  total_bytes?: number
  model_id?: string | null
  model_version?: string | null
  updated_at?: string | null
  error?: string | null
  error_code?: string | null
}

export interface ModelRuntime {
  status?: string
  available?: boolean
  model_id?: string | null
  model_version?: string | null
  error?: string | null
  error_code?: string | null
}

export interface RecognitionProfile {
  registration_status: string
  quality_warnings?: string[]
  profile_version: number
  model_id?: string | null
  model_version?: string | null
  reference_count: number
  ready_reference_count: number
  loaded_profile_version?: number | null
  loaded_camera_ids?: string[]
  error?: string | null
  model_preparation?: ModelPreparation | null
  model_runtime?: ModelRuntime | null
}

export interface RecognitionTest {
  image_data_url: string
  source_session_id: string
  source_frame: number
  source_timestamp: string
  runtime_mode?: RuntimeMode | string
  source_type?: SourceType
  is_simulated?: boolean
  model: string | { id?: string; name?: string; version?: string; model_id?: string; model_version?: string; status?: string; error?: string | null; candidate_backend?: string }
  latency_ms: number
  loaded_profiles: number | Array<{ item_id?: string; name?: string; profile_version?: number }>
  candidates: Array<{
    bbox: ImageRegion
    category: string
    entity_type?: 'person' | 'item'
    person_track_id?: string | null
    best_item_id?: string | null
    best_item_name?: string | null
    best_score: number | null
    second_item_name?: string | null
    second_score?: number | null
    margin?: number | null
    accepted: boolean
    rejection_reason?: string | null
    profile_version?: number | null
  } & CandidateProvenance>
  hand_count?: number
}

export type EventType = 'last_seen' | 'seen' | 'picked_up' | 'moved' | 'movement' | 'placed' | 'occluded' | 'lost' | 'offline' | 'exited_view' | 'reappeared' | 'manual_correction'

export interface EventRecord {
  id: string | null
  event_id?: string | null
  item_id: string
  item_name: string
  event_type: EventType
  camera_id: string
  camera_name: string
  room_name: string
  zone_id?: string | null
  zone_name?: string | null
  timestamp_start: string
  timestamp_end?: string | null
  confidence: number
  evidence_type?: string
  previous_zone?: string | null
  new_zone?: string | null
  from_zone?: string | null
  to_zone?: string | null
  final_position?: Record<string, unknown> | number[] | null
  screenshot_path?: string | null
  clip_path?: string | null
  detection_mode: string
  human_review_status?: string
  notes?: string | null
  movement_session_id?: string | null
  runtime_mode: RuntimeMode | string
  source_type: SourceType | string
  source_type_provenance?: SourceType | string
  is_simulated: boolean
  source_session_id?: string | null
  source_frame_start?: number | null
  source_frame_end?: number | null
  source_timestamp_start?: string | null
  source_timestamp_end?: string | null
  ingested_at?: string | null
  detector_backend?: string | null
  tracker_backend?: string | null
  evidence_status: string | null
  screenshot_sha256?: string | null
  clip_sha256?: string | null
  created_by?: string | null
  pinned?: boolean
  manually_corrected?: boolean
  started_at?: string | null
  ended_at?: string | null
  pickup_evidence?: Record<string, unknown> | null
  placement_evidence?: Record<string, unknown> | null
  before_screenshot?: string | null
  after_screenshot?: string | null
  final_status?: string | null
  source_frame?: number | null
  screenshot_source_frame?: number | null
  screenshot_observed_at?: string | null
  screenshot_source_session_id?: string | null
  image_status?: string | null
  image_error?: string | null
  holding_status?: string | null
  identity_evidence?: {
    accepted?: boolean
    best_score?: number | null
    second_score?: number | null
    gap?: number | null
    score_kind?: string
    model_id?: string
    model_version?: string
    profile_version?: number
    rejection?: string | null
  } | null
}

export interface LocationHypothesis {
  label: string
  reason: string
  evidence_type: 'inferred'
  source_session_id: string
  source_frame?: number | null
  observed_at?: string | null
  zone_id?: string | null
  position?: Record<string, unknown> | number[] | null
}

export interface SearchResult {
  item: Item
  last_confirmed: EventRecord | null
  last_seen: EventRecord | null
  last_picked_up: EventRecord | null
  last_occluded: EventRecord | null
  last_exited: EventRecord | null
  status: string
  answer: string
  evidence?: EventRecord | null
  last_observed?: EventRecord | null
  last_confirmed_placement?: EventRecord | null
  location_hypotheses?: LocationHypothesis[]
  observation_hint?: { code: string; message: string } | null
  runtime_mode?: RuntimeMode | string
  source_type?: SourceType | string
  is_simulated?: boolean
}

export interface Settings {
  detection_mode: DetectionMode
  inference_fps: number
  static_seconds: number
  pre_seconds: number
  post_seconds: number
  retention_days: number | null
  save_clips: boolean
  hand_detection_enabled?: boolean
  show_hands: boolean
  privacy_mode: boolean
  record_events: boolean
}

export interface HandAction {
  hand_id: string | number
  handedness?: string | null
  handedness_score?: number | null
  posture: string
  posture_label: string
  motion: string
  motion_label: string
  stable: boolean
  evidence_type?: 'landmark_geometry_only' | string
  grasp_established?: boolean
}

export interface HandInteraction {
  item_id: string
  item_name?: string
  state: string
  holding_status: string
  release_observed: boolean
  reason: string
}

export interface HandActionSnapshot {
  camera_id: string
  source_session_id: string | null
  source_frame: number | null
  timestamp: string | null
  fresh: boolean
  hand_status: { enabled: boolean; available: boolean; status: string; error?: string | null }
  hands: HandAction[]
  interactions: HandInteraction[]
  profile_count: number
  reason?: string | null
}

export interface HealthResponse {
  status: string
  version?: string
  stats?: { cameras?: number; online_cameras?: number; items?: number; events?: number; today_events?: number; fps?: number; latency_ms?: number }
}

export interface RuntimeConfig {
  frontend_url?: string
  api_base_url?: string
  websocket_base_url?: string
  backend_listen_url?: string
  backend_healthy?: boolean
  backend_version?: string
  runtime_mode?: RuntimeMode | string
  mode?: 'normal' | 'no_hardware_demo' | RuntimeMode | string
  no_hardware_demo?: boolean
  generated_at?: string
}

export type StoragePolicy = 'MINIMAL' | 'BALANCED' | 'FORENSIC'

export interface StorageItemCount {
  item_id: string
  item_name?: string
  event_count: number
  pinned_count?: number
}

export interface StorageStatus {
  runtime_mode?: RuntimeMode | string
  policy?: StoragePolicy
  storage_mode?: StoragePolicy
  retention_policy?: StoragePolicy
  database_bytes?: number
  database_size?: number
  screenshots_bytes?: number
  screenshot_size?: number
  clips_bytes?: number
  recordings_bytes?: number
  clip_size?: number
  logs_bytes?: number
  logs_size?: number
  firmware_bytes?: number
  firmware_artifacts_size?: number
  total_bytes?: number
  total_managed_bytes?: number
  max_storage_mb?: number
  pinned_event_count?: number
  pinned_events?: number
  orphan_file_count?: number
  orphan_files?: number
  next_cleanup_at?: string | null
  item_event_counts?: StorageItemCount[] | Record<string, number>
  events_by_item?: Record<string, number>
  sizes?: Partial<Record<'database' | 'screenshots' | 'clips' | 'recordings' | 'logs' | 'firmware' | 'total', number>>
  last_cleanup_report?: Record<string, unknown> | null
  message?: string
}

export interface StorageOperationResult {
  message?: string
  scanned_at?: string
  reclaimable_bytes?: number
  deleted_bytes?: number
  orphan_file_count?: number
  deleted_events?: number
  deleted_images?: number
  deleted_clips?: number
  bytes_before?: number
  bytes_after?: number
  event_ids?: string[]
  orphan_media?: string[]
  temporary_files?: string[]
  candidates?: Array<{ kind?: string; path?: string; bytes?: number; reason?: string }>
  [key: string]: unknown
}

export interface CameraDiagnosticResult {
  device_index: number
  backend: string
  backend_id?: number
  opened: boolean
  frame_read_success: boolean
  first_frame_ms?: number | null
  successful_frames: number
  failed_frames: number
  width?: number
  height?: number
  actual_fps?: number
  error?: string | null
  status_code?: string
}

export interface CameraDiagnosticReport {
  generated_at?: string
  python_version?: string
  opencv_version?: string
  available_backends?: Array<string | { id?: number; name?: string }>
  windows_devices?: Array<Record<string, unknown> | string>
  results?: CameraDiagnosticResult[]
  summary?: Record<string, unknown>
  screenshot_path?: string | null
  report_path?: string
  logs?: string[]
  manager?: Record<string, unknown> | Array<Record<string, unknown>>
  status?: string
  message?: string
}

export interface FirmwareArtifact {
  path?: string
  size?: number
  sha256?: string
}

export interface FirmwareManifest {
  firmware_version?: string
  build_time?: string
  generated_at?: string
  platformio_version?: string
  arduino_esp32_version?: string
  target_board?: string
  ram_usage_percent?: number
  flash_usage_percent?: number
  files?: Record<string, FirmwareArtifact>
  physical_board_detected?: boolean
  physical_flash_performed?: boolean
  serial_provision_performed?: boolean
  real_video_verified?: boolean
}

export interface FirmwarePort {
  device: string
  description?: string
  hwid?: string
  vid?: number | string | null
  pid?: number | string | null
  serial_number?: string | null
  eligible?: boolean
  rejection_reason?: string | null
}

export interface FirmwareJob {
  id: string
  job_id?: string
  kind?: string
  job_type?: string
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | string
  stage?: string
  progress?: number
  logs?: string[] | string
  error?: string | null
  result?: Record<string, unknown>
  created_at?: string
  updated_at?: string
}

export interface FirmwareDevice {
  id?: string
  device_id: string
  camera_id?: string
  device_name?: string
  name?: string
  room_name?: string
  online?: boolean
  status?: string
  last_heartbeat?: string
  ip_address?: string
  mac_address?: string
  firmware_version?: string
  wifi_rssi?: number
  free_heap?: number
  camera_status?: string
  stream_status?: string
  fps?: number
  current_fps?: number
  last_error?: string | null
  recent_error?: string | null
  stream_url?: string
  capabilities?: string[]
  token_revoked?: boolean
  simulated?: boolean
  hardware_type?: 'virtual' | 'physical' | string
  source_type?: 'esp32_real' | 'virtual_esp32' | string
  display_label?: string
}

export type AcceptanceRegionKey = 'A' | 'B'

export interface AcceptanceRegion {
  key: AcceptanceRegionKey
  name: string
  x: number
  y: number
  width: number
  height: number
}

export interface AcceptanceCamera {
  id: string
  name?: string
  room_name?: string
  source_type?: SourceType | string
  runtime_mode?: RuntimeMode | string
  is_simulated?: boolean
  status?: string
  status_code?: string
  online?: boolean
  detector_backend?: string | null
  frame_url?: string | null
}

export interface AcceptancePreflight {
  ready?: boolean
  runtime_mode?: RuntimeMode | string
  status?: string
  message?: string
  blockers?: string[]
  camera?: AcceptanceCamera | null
  cameras?: AcceptanceCamera[]
  camera_id?: string | null
  camera_name?: string | null
  source_type?: SourceType | string
  is_simulated?: boolean
  detector_backend?: string | null
  companion_url?: string | null
  lan_url?: string | null
  companion_qr_url?: string | null
  qr_code_url?: string | null
  refreshed_at?: string | null
}

export interface AcceptanceMarkerStatus {
  item_id: string
  validation_run_id?: string | null
  item_name?: string
  marker_id?: number | null
  aruco_id?: number | null
  bound?: boolean
  recognized?: boolean
  seen?: boolean
  state?: string
  recognition_status?: string
  pipeline_status?: string
  runtime_mode?: RuntimeMode | string
  camera_id?: string | null
  camera_name?: string | null
  source_type?: SourceType | string
  is_simulated?: boolean
  detector_backend?: string | null
  detection_mode?: string | null
  confidence?: number | null
  last_seen_at?: string | null
  source_frame?: number | null
  frame?: number | null
  track_id?: string | number | null
  source_session_id?: string | null
  server_observed?: boolean
  last_observed_at?: string | null
  stability?: {
    state?: string | null
    stable_remaining_seconds?: number | null
    speed?: number | null
    diagnostics?: {
      state?: string | null
      baseline?: unknown
      [key: string]: unknown
    } | null
  } | null
  message?: string
}

export interface AcceptanceMarkerBinding {
  id?: string
  item_id?: string
  marker_id?: number | null
  aruco_id?: number | null
  bound?: boolean
  marker_url?: string | null
  message?: string
}

export interface AcceptanceCheck {
  id?: string
  label?: string
  name?: string
  passed?: boolean | null
  status?: string
  message?: string
  detail?: string
}

export interface AcceptanceEvidence {
  event_id?: string | null
  movement_session_id?: string | null
  from_zone?: string | null
  to_zone?: string | null
  screenshot_path?: string | null
  clip_path?: string | null
  screenshot_sha256?: string | null
  clip_sha256?: string | null
}

export interface AcceptanceResult {
  passed?: boolean
  matched_expected_transition?: boolean
  movement_event_count?: number
  event_count?: number
  actual_from?: string | null
  actual_to?: string | null
  final_status?: string | null
  message?: string
  evidence?: AcceptanceEvidence | null
  event?: AcceptanceEvidence | null
}

export interface AcceptanceEngineStatus {
  validation_run_id?: string
  item_id?: string
  camera_id?: string
  state?: string
  marker_status?: string
  terminal?: boolean
  ring_seconds?: number
  required_pre_seconds?: number
  required_post_seconds?: number
  detected_frames?: number
  stable_remaining_seconds?: number
  observed_duration_seconds?: number
  trial_remaining_seconds?: number
  event_count?: number
  baseline?: Record<string, unknown> | null
  current_zone?: { id?: string; name?: string } | null
  [key: string]: unknown
}

export type AcceptanceRunPhase =
  | 'created'
  | 'active'
  | 'calibrating'
  | 'stable_a'
  | 'moving'
  | 'stable_b'
  | 'evaluating'
  | 'passed'
  | 'camera_interrupted'
  | 'failed'
  | 'cancelled'

export interface AcceptanceRun {
  id?: string
  run_id?: string
  validation_run_id?: string
  suite_id?: string
  contract_version?: string
  score_eligible?: boolean
  evidence_integrity_status?: 'verified' | 'policy_deleted' | 'missing_or_tampered' | 'not_applicable' | 'not_passed'
  evidence_retained?: boolean
  retained_by_policy?: boolean
  retention_receipt_id?: string | null
  evidence_integrity_reason?: string | null
  status?: string
  phase?: AcceptanceRunPhase | string
  current_phase?: AcceptanceRunPhase | string
  message?: string
  instruction?: string
  aruco_id?: number | null
  trial_kind?: AcceptanceScenarioKind | string
  scenario_index?: number
  minimum_duration_seconds?: number | null
  outcome?: string | null
  event_id?: string | null
  failure_reason?: string | null
  expected_from_zone?: string | null
  expected_to_zone?: string | null
  origin_zone_id?: string | null
  destination_zone_id?: string | null
  origin_zone?: { id?: string; name?: string; points?: number[][] } | null
  destination_zone?: { id?: string; name?: string; points?: number[][] } | null
  runtime_mode?: RuntimeMode | string
  item_id?: string
  item_name?: string
  camera_id?: string
  camera_name?: string
  source_type?: SourceType | string
  is_simulated?: boolean
  detector_backend?: string | null
  regions?: AcceptanceRegion[]
  marker_status?: AcceptanceMarkerStatus | null
  stable_frames?: number
  required_stable_frames?: number
  observed_frames?: number
  engine_status?: AcceptanceEngineStatus | null
  progress?: {
    phase?: string
    controlled_disconnect?: boolean
    controlled_reconnect?: boolean
    [key: string]: unknown
  } | null
  result?: AcceptanceResult | null
  evidence?: AcceptanceEvidence | null
  negative_checks?: AcceptanceCheck[]
  negative_samples?: AcceptanceCheck[]
  companion_url?: string | null
  lan_url?: string | null
  companion_qr_url?: string | null
  qr_code_url?: string | null
  created_at?: string | null
  started_at?: string | null
  updated_at?: string | null
}

export type AcceptanceScenarioKind =
  | 'stationary'
  | 'minor_adjustment'
  | 'movement'
  | 'occlusion'
  | 'disconnect_reconnect'

export interface AcceptanceSuiteScenario {
  scenario_index: number
  trial_kind: AcceptanceScenarioKind
  from_zone: AcceptanceRegionKey
  to_zone: AcceptanceRegionKey
  minimum_duration_seconds: number
}

export interface AcceptanceSuiteSummary {
  contract_complete: boolean
  created_runs: number
  terminal_runs: number
  movement_total: number
  movement_passed: number
  movement_required: number
  movement_evidence_retained?: number
  movement_policy_deleted?: number
  movement_evidence_invalid?: number
  negative_total: number
  negative_passed: number
  negative_required: number
  next_scenario_index: number | null
}

export interface AcceptanceSuite {
  id: string
  suite_id: string
  contract_version: string
  contract: AcceptanceSuiteScenario[]
  runtime_mode: RuntimeMode | string
  suite_status: 'READY' | 'IN_PROGRESS' | 'PASSED' | 'FAILED' | string
  overall_passed: boolean
  item_id: string
  camera_id: string
  aruco_id: number
  zone_a_id: string
  zone_b_id: string
  zone_a: { id: string; name: string; points: number[][] }
  zone_b: { id: string; name: string; points: number[][] }
  thresholds: Record<string, number>
  source_type: SourceType | string
  is_simulated: boolean
  summary: AcceptanceSuiteSummary
  runs: AcceptanceRun[]
  created_at?: string | null
  updated_at?: string | null
}

export interface CreateAcceptanceSuiteRequest {
  item_id: string
  camera_id: string
  zone_a_id: string
  zone_b_id: string
}

export interface CreateAcceptanceRunRequest {
  suite_id: string
  scenario_index: number
}
