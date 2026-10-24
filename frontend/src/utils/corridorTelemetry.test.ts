import { describe, expect, it } from 'vitest';
import type { Corridor, CorridorObservedTraffic } from '../types';
import { corridorLiveView, corridorObservationTime } from './corridorTelemetry';

const NOW = Date.parse('2026-10-24T08:00:00Z');
const iso = (offsetMs: number) => new Date(NOW + offsetMs).toISOString();

function observedTraffic(overrides: Record<string, unknown> = {}): CorridorObservedTraffic {
  return {
    status: 'available', source: 'tomtom_live', scope: 'representative_provider_point',
    checked_at: iso(0), observed_at: iso(-5_000), fresh_until: iso(60_000),
    congestion_index: 72, delay_mins: null, provider_point_count: 1, camera_count: 0,
    camera_congestion_index: null, corridor_wide: false,
    forecast_30m: 72, forecast_60m: 72, forecast_method: 'persistence',
    forecast_observed_at: iso(-5_000), forecast_fresh_until: iso(60_000),
    ...overrides,
  } as CorridorObservedTraffic;
}

function corridor(observed?: CorridorObservedTraffic): Corridor {
  return {
    id: 'corridor-1', name: 'Batamindo Industrial Park -> Batam Centre Ferry Terminal',
    distance_km: 9.91, base_time_mins: 18, live_congestion_score: 99,
    delay_mins: 20, status: 'CRITICAL', risk_level: 'HIGH',
    forecast_30m: 99, forecast_60m: 99, key_checkpoints: [], observed_traffic: observed,
  };
}

function view(overrides: Record<string, unknown> = {}, now = NOW) {
  return corridorLiveView(corridor(observedTraffic(overrides)), now);
}

describe('current corridor observation contract', () => {
  it('keeps genuine zero independent of disagreeing legacy estimates', () => {
    expect(view({ congestion_index: 0, forecast_30m: 0, forecast_60m: 0 })).toMatchObject({
      available: true, score: 0, status: 'SMOOTH', delay: null,
      forecast30: 0, forecast60: 0, forecastMethod: 'persistence',
      coverage: 'Partial coverage · 1 measured road point',
    });
  });

  it('withholds legacy estimates without a current evidence block', () => {
    expect(corridorLiveView(corridor(), NOW)).toMatchObject({
      available: false, score: null, status: 'UNAVAILABLE', delay: null,
      forecast30: null, forecast60: null, forecastMethod: 'unavailable',
    });
  });

  it.each([
    ['negative score', { congestion_index: -1 }],
    ['over-range score', { congestion_index: 101 }],
    ['missing score', { congestion_index: null }],
    ['NaN score', { congestion_index: Number.NaN }],
    ['infinite score', { congestion_index: Number.POSITIVE_INFINITY }],
    ['string score', { congestion_index: '0' }],
    ['unavailable status', { status: 'unavailable' }],
    ['simulation source', { source: 'simulated' }],
    ['mismatched scope', { scope: 'mapped_camera_road_views' }],
    ['unsupported complete-coverage claim', { corridor_wide: true }],
    ['zero provider count', { provider_point_count: 0 }],
    ['fractional provider count', { provider_point_count: 1.5 }],
    ['unbounded provider count', { provider_point_count: 129 }],
    ['string provider count', { provider_point_count: '1' }],
  ])('rejects malformed or unsupported evidence: %s', (_name, overrides) => {
    expect(view(overrides)).toMatchObject({
      available: false, score: null, delay: null, forecast30: null, forecast60: null,
    });
  });

  it.each([
    ['invalid observation timestamp', { observed_at: 'not-a-date' }],
    ['observation without timezone', { observed_at: '2026-10-24T08:00:00' }],
    ['missing observation timestamp', { observed_at: null }],
    ['invalid checked timestamp', { checked_at: 'not-a-date' }],
    ['expiry without timezone', { fresh_until: '2026-10-24T08:01:00' }],
    ['already expired', { fresh_until: iso(-1) }],
    ['expiry at the current instant', { fresh_until: iso(0) }],
    ['expiry before observation', { fresh_until: iso(-6_000) }],
    ['future observation beyond clock skew', { observed_at: iso(31_000) }],
    ['future check beyond clock skew', { checked_at: iso(31_000) }],
    ['check preceding capture', { checked_at: iso(-36_000) }],
    ['ancient source with renewed expiry', { observed_at: iso(-86_400_000) }],
    ['provider expiry beyond maximum lifetime', { fresh_until: iso(180_000) }],
    ['far-future expiry', { fresh_until: iso(86_400_000) }],
  ])('withdraws stale or inconsistent timestamps: %s', (_name, overrides) => {
    expect(view(overrides).available).toBe(false);
  });

  it('rejects an invalid consumer clock instead of making age comparisons succeed', () => {
    expect(view({}, Number.NaN).available).toBe(false);
  });

  it('withdraws exactly at the declared source deadline', () => {
    expect(view({}, NOW + 59_999).available).toBe(true);
    expect(view({}, NOW + 60_000).available).toBe(false);
  });

  it('never derives physical route delay or a speed forecast from camera occupancy', () => {
    expect(view({
      source: 'cctv_camera', scope: 'mapped_camera_road_views',
      provider_point_count: 0, camera_count: 2, congestion_index: 40,
      camera_congestion_index: 40, delay_mins: 17,
      forecast_30m: 99, forecast_60m: 99,
    })).toMatchObject({
      available: true, score: 40, status: 'HEAVY', delay: null,
      forecast30: null, forecast60: null, forecastMethod: 'unavailable',
      coverage: 'Partial coverage · 2 mapped road views · Estimated',
    });
  });

  it('enforces the shorter camera maximum lifetime even if its declared expiry is later', () => {
    expect(view({
      source: 'cctv_camera', scope: 'mapped_camera_road_views',
      provider_point_count: 0, camera_count: 1, fresh_until: iso(120_000),
    }).available).toBe(false);
  });

  it('does not show whole-route delay inferred from a representative provider point', () => {
    expect(view({ delay_mins: 12 }).delay).toBeNull();
  });

  it.each([
    ['expired forecast', { forecast_fresh_until: iso(0) }],
    ['malformed forecast time', { forecast_observed_at: 'not-a-date' }],
    ['forecast without timezone', { forecast_fresh_until: '2026-10-24T08:01:00' }],
    ['persistence from a different observation', { forecast_observed_at: iso(-10_000) }],
    ['future forecast source', { forecast_observed_at: iso(31_000) }],
    ['invalid forecast value', { forecast_30m: Number.NaN }],
    ['missing forecast value', { forecast_60m: null }],
    ['unsupported method', { forecast_method: 'synthetic' }],
    ['far-future forecast expiry', { forecast_fresh_until: iso(86_400_000) }],
  ])('withholds an unsupported outlook without hiding its valid current observation: %s', (_name, overrides) => {
    expect(view(overrides)).toMatchObject({
      available: true, score: 72, forecast30: null, forecast60: null, forecastMethod: 'unavailable',
    });
  });

  it('formats the original observation time in Batam time', () => {
    expect(corridorObservationTime('2026-10-24T08:00:00Z')).toBe('15:00:00 WIB');
  });
});
