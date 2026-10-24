import { describe, expect, it } from 'vitest';
import type { Corridor, CorridorObservedTraffic } from '../types';
import { corridorLiveView, corridorObservationTime } from './corridorTelemetry';
import { corridorColor } from './format';

const NOW = Date.parse('2026-10-24T08:00:00Z');
const iso = (offsetMs: number) => new Date(NOW + offsetMs).toISOString();

function observedTraffic(overrides: Record<string, unknown> = {}): CorridorObservedTraffic {
  const score = typeof overrides.congestion_index === 'number' ? overrides.congestion_index : 72;
  const camera = overrides.source === 'cctv_camera';
  return {
    status: 'available', source: 'tomtom_live', scope: 'representative_provider_point',
    condition: camera ? 'CAMERA_ESTIMATE' : score <= 5 ? 'FREE_FLOW' : score < 40 ? 'SLOWING' : score < 70 ? 'HEAVY' : 'CRITICAL',
    condition_policy: camera ? 'mapped_camera_proxy_v1' : 'observed_speed_loss_bands_v1',
    provider_confidence: camera ? null : 0.9,
    current_speed_kmh: camera ? null : 50 * (1 - score / 100), free_flow_speed_kmh: camera ? null : 50,
    checked_at: iso(0), observed_at: iso(-5_000), fresh_until: iso(60_000),
    congestion_index: score, delay_mins: null, provider_point_count: 1, camera_count: 0,
    camera_congestion_index: camera ? score : null, corridor_wide: false,
    camera_condition: camera ? score >= 40 ? 'LOCAL_CONGESTION_ESTIMATE' : 'LOW_PRESSURE_ESTIMATE' : null,
    camera_warning: false, camera_fresh_until: camera ? iso(60_000) : null,
    forecast_30m: score, forecast_60m: score, forecast_method: 'persistence',
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
      available: true, score: 0, status: 'FREE_FLOW', delay: null,
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
      available: true, score: 40, status: 'CAMERA_ESTIMATE', delay: null,
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
    ['persistence with a changed 30-minute value', { forecast_30m: 30 }],
    ['persistence with a changed 60-minute value', { forecast_60m: 30 }],
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

  it.each([
    [0, 'FREE_FLOW'], [5, 'FREE_FLOW'], [5.1, 'SLOWING'], [20, 'SLOWING'],
    [39.9, 'SLOWING'], [40, 'HEAVY'], [69.9, 'HEAVY'], [70, 'CRITICAL'], [95, 'CRITICAL'],
  ] as const)('uses the explicit observed speed-loss condition at index %s: %s', (score, status) => {
    const result = view({ congestion_index: score });
    expect(result).toMatchObject({ available: true, score, status });
    if (status === 'SLOWING') expect(result.color).toBe(corridorColor('HEAVY'));
    expect(result.status).not.toBe('SMOOTH');
  });

  it.each([
    ['condition missing', { condition: undefined }],
    ['legacy smooth condition', { condition: 'SMOOTH' }],
    ['incoherent free-flow claim', { condition: 'FREE_FLOW' }],
    ['camera condition for provider speed', { condition: 'CAMERA_ESTIMATE' }],
    ['condition policy missing', { condition_policy: undefined }],
    ['wrong condition policy', { condition_policy: 'mapped_camera_proxy_v1' }],
    ['confidence missing', { provider_confidence: null }],
    ['confidence below minimum', { provider_confidence: 0.49 }],
    ['non-finite confidence', { provider_confidence: Number.NaN }],
    ['out-of-range confidence', { provider_confidence: 1.01 }],
    ['string confidence', { provider_confidence: '0.9' }],
    ['current speed missing', { current_speed_kmh: null }],
    ['negative current speed', { current_speed_kmh: -1 }],
    ['free-flow speed missing', { free_flow_speed_kmh: null }],
    ['zero free-flow speed', { free_flow_speed_kmh: 0 }],
    ['non-finite free-flow speed', { free_flow_speed_kmh: Number.POSITIVE_INFINITY }],
    ['speed inconsistent with score', { current_speed_kmh: 50 }],
    ['unbounded current speed', { current_speed_kmh: 251 }],
    ['unbounded free-flow speed', { free_flow_speed_kmh: 251, current_speed_kmh: 70.28 }],
    ['implausible speed ratio', { congestion_index: 0, current_speed_kmh: 76 }],
    ['provider score beyond the stopped-speed cap', { congestion_index: 100, current_speed_kmh: 0 }],
  ])('withholds unverified provider conditions: %s', (_name, overrides) => {
    expect(view(overrides)).toMatchObject({ available: false, score: null, status: 'UNAVAILABLE' });
  });

  it('accepts the minimum supported provider confidence without substituting a legacy condition', () => {
    expect(view({ congestion_index: 20, provider_confidence: 0.5 })).toMatchObject({
      available: true, score: 20, status: 'SLOWING',
    });
  });

  it('preserves a validated stopped provider point at the shared 95-index cap', () => {
    expect(view({ congestion_index: 95, current_speed_kmh: 0 })).toMatchObject({
      available: true, score: 95, status: 'CRITICAL',
    });
  });

  it('never turns an empty camera view into measured free flow', () => {
    const result = view({
      source: 'cctv_camera', scope: 'mapped_camera_road_views', congestion_index: 0,
      provider_point_count: 0, camera_count: 1,
    });
    expect(result).toMatchObject({
      available: true, score: 0, status: 'CAMERA_ESTIMATE', delay: null,
      forecast30: null, forecast60: null,
    });
    expect(result.color).not.toBe(corridorColor('SMOOTH'));
  });

  it('withdraws only the local camera warning at its separate deadline', () => {
    const evidence = {
      congestion_index: 0, camera_count: 1, camera_congestion_index: 75,
      camera_condition: 'LOCAL_CONGESTION_ESTIMATE', camera_warning: true,
      camera_fresh_until: iso(2_000),
    };
    expect(view(evidence)).toMatchObject({ available: true, score: 0, status: 'FREE_FLOW', cameraWarning: true });
    expect(view(evidence, NOW + 2_000)).toMatchObject({
      available: true, score: 0, status: 'FREE_FLOW', cameraWarning: false,
    });
  });

  it.each([
    ['missing camera score', { camera_congestion_index: null }],
    ['low camera pressure', { camera_congestion_index: 10, camera_condition: 'LOW_PRESSURE_ESTIMATE' }],
    ['no mapped camera', { camera_count: 0 }],
    ['expired camera', { camera_fresh_until: iso(0) }],
    ['unparseable camera expiry', { camera_fresh_until: 'not-a-date' }],
  ])('keeps valid provider free flow without an unsupported local warning: %s', (_name, overrides) => {
    expect(view({
      congestion_index: 0, camera_count: 1, camera_congestion_index: 75,
      camera_condition: 'LOCAL_CONGESTION_ESTIMATE', camera_warning: true,
      camera_fresh_until: iso(2_000), ...overrides,
    })).toMatchObject({ available: true, score: 0, status: 'FREE_FLOW', cameraWarning: false });
  });
});
