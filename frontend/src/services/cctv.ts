/** Operator-only CCTV observations. A failed request never becomes an empty road. */
const API_BASE = import.meta.env.VITE_API_BASE_URL?.trim().replace(/\/$/, '') ?? '';
const API_ENABLED = import.meta.env.VITE_API_ENABLED !== 'false';

export type CctvCameraStatus = 'permission_pending' | 'calibration_pending' | 'disabled' | 'live' | 'estimated' | 'stale' | 'unavailable';
export type CctvCamera = {
  camera_id: string; name: string; road_name: string | null; source_url: string | null;
  status: CctvCameraStatus; reason: string; observed_at: string | null; window_seconds: number | null;
  vehicle_count: number | null; moving_vehicles: number | null; stopped_vehicles: number | null;
  occupancy_ratio: number | null; motion_score: number | null; queue_length_m: number | null;
  congestion_score: number | null; severity: string | null; operational_eligible: boolean;
  validation_status?: 'unavailable' | 'estimated' | 'validated';
  timestamp_basis?: 'source_clock' | 'stream_observation' | null;
  detector_id?: string | null; routing_enabled?: boolean;
  maximum_age_seconds?: number;
};
export type CctvMonitorStatus = {
  status: 'awaiting_feeds' | 'active' | 'degraded' | 'unavailable'; message: string; updated_at: string;
  summary: { listed: number; configured: number; live: number; estimated: number; stale: number; unavailable: number; permission_pending: number; calibration_pending: number };
  cameras: CctvCamera[];
};
const CAMERA_STATUSES = new Set<CctvCameraStatus>(['permission_pending', 'calibration_pending', 'disabled', 'live', 'estimated', 'stale', 'unavailable']);
const SUMMARY_KEYS = ['listed', 'configured', 'live', 'estimated', 'stale', 'unavailable', 'permission_pending', 'calibration_pending'] as const;
function record(value: unknown): value is Record<string, unknown> { return value !== null && typeof value === 'object' && !Array.isArray(value); }
function text(value: unknown, max = 4096): value is string { return typeof value === 'string' && value.length > 0 && value.length <= max; }
function timestamp(value: unknown): value is string { return text(value, 64) && Number.isFinite(Date.parse(value)); }
function bounded(value: unknown, max: number, integer = false): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= max && (!integer || Number.isInteger(value));
}
function nullableNumber(value: unknown, max: number, integer = false): boolean { return value === null || bounded(value, max, integer); }
function sourceUrl(value: unknown): value is string | null {
  if (value === null) return true;
  if (!text(value, 2048)) return false;
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password; } catch { return false; }
}
function camera(value: unknown): value is CctvCamera {
  if (!record(value) || !text(value.camera_id, 128) || !text(value.name, 256)
    || !(value.road_name === null || text(value.road_name, 256)) || !sourceUrl(value.source_url)
    || !CAMERA_STATUSES.has(value.status as CctvCameraStatus) || !text(value.reason)
    || !(value.observed_at === null || timestamp(value.observed_at))
    || !(value.window_seconds === null || bounded(value.window_seconds, 86_400) && value.window_seconds > 0)
    || !['vehicle_count', 'moving_vehicles', 'stopped_vehicles'].every(key => nullableNumber(value[key], 1_000_000, true))
    || !['occupancy_ratio', 'motion_score'].every(key => nullableNumber(value[key], 1))
    || !nullableNumber(value.queue_length_m, 100_000) || !nullableNumber(value.congestion_score, 100)
    || !(value.severity === null || text(value.severity, 64)) || typeof value.operational_eligible !== 'boolean') return false;
  if (value.operational_eligible && (value.status !== 'live' || value.observed_at === null
    || value.window_seconds === null || value.severity === null)) return false;
  if (value.validation_status !== undefined && !['unavailable', 'estimated', 'validated'].includes(String(value.validation_status))) return false;
  if (value.timestamp_basis !== undefined && value.timestamp_basis !== null && !['source_clock', 'stream_observation'].includes(String(value.timestamp_basis))) return false;
  if (value.detector_id !== undefined && value.detector_id !== null && !text(value.detector_id, 256)) return false;
  if (value.routing_enabled !== undefined && typeof value.routing_enabled !== 'boolean') return false;
  if (value.maximum_age_seconds !== undefined && (!bounded(value.maximum_age_seconds, 120) || value.maximum_age_seconds < 1)) return false;
  if (value.status === 'estimated' && (value.operational_eligible || value.validation_status !== 'estimated'
    || value.observed_at === null || value.window_seconds === null || value.congestion_score === null
    || value.severity === null || !value.timestamp_basis || !value.detector_id)) return false;
  return true;
}
export function isCctvMonitorStatus(value: unknown): value is CctvMonitorStatus {
  if (!record(value) || !['awaiting_feeds', 'active', 'degraded', 'unavailable'].includes(String(value.status))
    || !text(value.message) || !timestamp(value.updated_at) || !record(value.summary)
    || !SUMMARY_KEYS.every(key => bounded((value.summary as Record<string, unknown>)[key], 1_000_000, true))
    || !Array.isArray(value.cameras) || value.cameras.length > 2_000 || !value.cameras.every(camera)) return false;
  return new Set(value.cameras.map(item => item.camera_id)).size === value.cameras.length;
}
export async function fetchCctvStatus(token: string | null, signal?: AbortSignal): Promise<CctvMonitorStatus> {
  if (!API_ENABLED) throw new Error('Connect the backend to check CCTV observations.');
  const response = await fetch(`${API_BASE}/api/analytics/cctv-status`, {
    method: 'GET', headers: token ? { Authorization: `Bearer ${token}` } : {}, cache: 'no-store',
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(20_000)]) : AbortSignal.timeout(20_000),
  });
  let body: unknown;
  try { body = await response.json(); } catch { throw new Error('CCTV status returned an incomplete response. No measurements were substituted.'); }
  if (!response.ok) {
    if (response.status === 401 || response.status === 403) throw new Error('Sign in with an operator account to view CCTV observations.');
    throw new Error('CCTV status is unavailable. No measurements were substituted.');
  }
  if (!record(body) || body.data_source !== 'cctv_traffic_observations' || !isCctvMonitorStatus(body.camera_monitor)) {
    throw new Error('CCTV observations could not be verified. No measurements were substituted.');
  }
  return body.camera_monitor;
}
