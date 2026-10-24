import type { Corridor, CorridorObservedTraffic } from '../types';
import { classifyStatus } from '../services/localForecast';
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
  status: Corridor['status'];
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
    available: false, score: null, status: 'UNAVAILABLE', color: corridorColor('UNAVAILABLE'),
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

  const status = classifyStatus(evidence.congestion_index);
  const forecastObserved = timestamp(evidence.forecast_observed_at);
  const forecastUntil = timestamp(evidence.forecast_fresh_until);
  const forecastAvailable = provider && index(evidence.forecast_30m) && index(evidence.forecast_60m)
    && ['persistence', 'random_forest', 'mixed'].includes(evidence.forecast_method)
    && forecastObserved !== null && forecastObserved <= nowMs + 30_000
    && forecastUntil !== null && Math.min(forecastUntil, until) > nowMs
    && forecastUntil > forecastObserved
    && forecastUntil <= forecastObserved + 180_000
    && (evidence.forecast_method !== 'persistence' || forecastObserved === observed);
  return {
    available: true, score: evidence.congestion_index, status, color: corridorColor(status),
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
