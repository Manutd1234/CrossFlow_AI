import type { Corridor, CorridorObservedCondition, CorridorObservedTraffic } from '../types';
import { corridorColor } from './format';

function timestamp(value: unknown): number | null {
  if (typeof value !== 'string' || !/(Z|[+-]\d{2}:\d{2})$/.test(value)) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function index(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100;
}

function count(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value > 0 && value <= 128;
}

export interface CorridorLiveView {
  available: boolean;
  score: number | null;
  status: CorridorObservedCondition;
  tone: 'smooth' | 'heavy' | 'critical' | 'neutral';
  cameraWarning: boolean;
  color: string;
  coverage: string;
  observedAt: string | null;
  delay: number | null;
  forecast30: number | null;
  forecast60: number | null;
  forecastMethod: CorridorObservedTraffic['forecast_method'];
}

/** Legacy planning numbers and bundled simulations are never live evidence. */
export function corridorLiveView(corridor: Corridor, nowMs = Date.now()): CorridorLiveView {
  const unknown: CorridorLiveView = {
    available: false, score: null, status: 'UNAVAILABLE', tone: 'neutral', cameraWarning: false,
    color: corridorColor('UNAVAILABLE'),
    coverage: 'No fresh measurements for this corridor.', observedAt: null,
    delay: null, forecast30: null, forecast60: null, forecastMethod: 'unavailable',
  };
  const evidence = corridor.observed_traffic;
  if (!evidence || evidence.status !== 'available' || !index(evidence.congestion_index)) return unknown;
  const observed = timestamp(evidence.observed_at);
  const checked = timestamp(evidence.checked_at);
  const until = timestamp(evidence.fresh_until);
  if (observed === null || checked === null || until === null || !Number.isFinite(nowMs)
    || observed > nowMs + 30_000 || checked > nowMs + 30_000
    || checked < observed - 30_000 || until <= nowMs || until <= observed) return unknown;
  const provider = evidence.source === 'tomtom_live'
    && evidence.scope === 'representative_provider_point' && count(evidence.provider_point_count);
  const camera = evidence.source === 'cctv_camera'
    && evidence.scope === 'mapped_camera_road_views' && count(evidence.camera_count);
  if ((!provider && !camera) || evidence.corridor_wide !== false) return unknown;
  // Match the maximum provider/camera source windows; a malformed distant
  // expiry cannot turn an old observation into fresh traffic.
  const maximumAgeMs = provider ? 180_000 : 120_000;
  if (nowMs - observed >= maximumAgeMs || until > observed + maximumAgeMs) return unknown;

  const score = evidence.congestion_index;
  if (provider) {
    const speed = evidence.current_speed_kmh;
    const freeFlow = evidence.free_flow_speed_kmh;
    const confidence = evidence.provider_confidence;
    const expectedCondition = score <= 5 ? 'FREE_FLOW' : score < 40 ? 'SLOWING' : score < 70 ? 'HEAVY' : 'CRITICAL';
    if (evidence.condition_policy !== 'observed_speed_loss_bands_v1'
      || evidence.condition !== expectedCondition
      || typeof confidence !== 'number' || !Number.isFinite(confidence) || confidence < 0.5 || confidence > 1
      || typeof speed !== 'number' || !Number.isFinite(speed) || speed < 0 || speed > 250
      || typeof freeFlow !== 'number' || !Number.isFinite(freeFlow) || freeFlow <= 0 || freeFlow > 250
      || speed / freeFlow > 1.5
      // Validate the backend's rounded bounded speed-loss index, not a second
      // congestion calculation or a substitute for missing evidence.
      || Math.abs(score - 100 * (1 - Math.min(1, Math.max(0.05, speed / freeFlow)))) > 0.051) return unknown;
  } else if (evidence.condition_policy !== 'mapped_camera_proxy_v1' || evidence.condition !== 'CAMERA_ESTIMATE') {
    return unknown;
  }
  const status = evidence.condition;
  const tone = status === 'FREE_FLOW' ? 'smooth' : status === 'CRITICAL' ? 'critical'
    : status === 'SLOWING' || status === 'HEAVY' ? 'heavy' : 'neutral';
  const color = corridorColor(tone === 'smooth' ? 'SMOOTH' : tone === 'heavy' ? 'HEAVY'
    : tone === 'critical' ? 'CRITICAL' : 'UNAVAILABLE');
  const cameraUntil = timestamp(evidence.camera_fresh_until);
  const cameraWarning = provider && evidence.camera_warning === true && score < 40
    && count(evidence.camera_count) && index(evidence.camera_congestion_index)
    && evidence.camera_congestion_index >= 40 && evidence.camera_condition === 'LOCAL_CONGESTION_ESTIMATE'
    && cameraUntil !== null && cameraUntil > nowMs && cameraUntil <= checked + 120_000;
  const forecastObserved = timestamp(evidence.forecast_observed_at);
  const forecastUntil = timestamp(evidence.forecast_fresh_until);
  const forecastAvailable = provider && index(evidence.forecast_30m) && index(evidence.forecast_60m)
    && ['persistence', 'random_forest', 'mixed'].includes(evidence.forecast_method)
    && forecastObserved !== null && forecastObserved <= nowMs + 30_000
    && forecastUntil !== null && Math.min(forecastUntil, until) > nowMs
    && forecastUntil > forecastObserved
    && forecastUntil <= forecastObserved + 180_000
    && (evidence.forecast_method !== 'persistence' || (forecastObserved === observed
      && evidence.forecast_30m === score && evidence.forecast_60m === score));
  return {
    available: true, score, status, tone, color, cameraWarning,
    coverage: provider
      ? `Partial coverage · ${evidence.provider_point_count} measured road point${evidence.provider_point_count === 1 ? '' : 's'}`
      : `Partial coverage · ${evidence.camera_count} mapped road view${evidence.camera_count === 1 ? '' : 's'} · Estimated`,
    observedAt: evidence.observed_at,
    // Neither a representative point nor image occupancy measures route delay.
    delay: null,
    forecast30: forecastAvailable ? evidence.forecast_30m : null,
    forecast60: forecastAvailable ? evidence.forecast_60m : null,
    forecastMethod: forecastAvailable ? evidence.forecast_method : 'unavailable',
  };
}

export function corridorObservationTime(value: string): string {
  return `${new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Jakarta', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).format(new Date(value))} WIB`;
}
