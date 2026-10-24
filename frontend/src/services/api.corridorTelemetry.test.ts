import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Corridor, CorridorObservedTraffic } from '../types';
import { CORRIDOR_CATALOG } from '../data/mockData';
import { corridorLiveView } from '../utils/corridorTelemetry';
import { fetchCorridors } from './api';

function respond(value: unknown): void {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(value))));
}

function freshObservation(score: number, at: number): CorridorObservedTraffic {
  const observedAt = new Date(at - 5_000).toISOString();
  const freshUntil = new Date(at + 60_000).toISOString();
  const pressure = Number(Math.min(100, score * 2.5).toFixed(1));
  return {
    status: 'available', source: 'tomtom_live', scope: 'representative_provider_point',
    condition: score <= 5 ? 'FREE_FLOW' : score < 20 ? 'SLOWING' : score < 40 ? 'HEAVY' : 'CRITICAL',
    condition_policy: 'observed_speed_loss_bands_v2', provider_confidence: 0.9,
    current_speed_kmh: 50 * (1 - score / 100), free_flow_speed_kmh: 50,
    checked_at: new Date(at).toISOString(), observed_at: observedAt, fresh_until: freshUntil,
    congestion_index: score, delay_mins: null, provider_point_count: 1, camera_count: 0,
    pressure_index: pressure, pressure_multiplier: 2.5, pressure_policy: 'congestion_pressure_v1',
    camera_congestion_index: null, camera_condition: null, camera_warning: false, camera_fresh_until: null,
    corridor_wide: false,
    forecast_30m: score, forecast_60m: score, forecast_method: 'persistence',
    pressure_forecast_30m: pressure, pressure_forecast_60m: pressure,
    forecast_observed_at: observedAt, forecast_fresh_until: freshUntil,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('corridor telemetry API evidence boundary', () => {
  it('retains orientation metadata during an outage without inventing live conditions', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));

    const result = await fetchCorridors();

    expect(result.source).toBe('offline');
    expect(result.data.length).toBeGreaterThan(0);
    expect(result.data.map(corridor => corridor.id)).toEqual(CORRIDOR_CATALOG.map(corridor => corridor.id));
    for (const corridor of result.data) {
      expect(corridor.name).not.toBe('');
      expect(corridor.distance_km).toBeGreaterThan(0);
      expect(corridor).toMatchObject({
        status: 'UNAVAILABLE', risk_level: 'UNAVAILABLE',
        live_congestion_score: null, delay_mins: null, forecast_30m: null, forecast_60m: null,
      });
      expect(corridorLiveView(corridor)).toMatchObject({
        available: false, score: null, delay: null, forecast30: null, forecast60: null,
      });
    }
  });

  it.each([[], null, { length: 5 }])('treats an empty or malformed corridor collection as unavailable: %j', async corridors => {
    respond({ data_source: 'live', generated_at: new Date().toISOString(), corridors });

    const result = await fetchCorridors();

    expect(result.source).toBe('offline');
    expect(result.data.length).toBeGreaterThan(0);
    expect(result.data.every(corridor => corridor.live_congestion_score === null)).toBe(true);
    expect(result.data.every(corridor => corridor.forecast_30m === null)).toBe(true);
    expect(result.data.every(corridor => !corridorLiveView(corridor).available)).toBe(true);
  });

  it('preserves a backend unavailable row instead of replacing it with a synthetic forecast', async () => {
    const now = Date.now();
    const observation: CorridorObservedTraffic = {
      ...freshObservation(0, now), status: 'unavailable', source: 'unavailable', scope: 'unavailable',
      condition: 'UNAVAILABLE', condition_policy: 'unavailable',
      provider_confidence: null, current_speed_kmh: null, free_flow_speed_kmh: null,
      observed_at: null, fresh_until: null, congestion_index: null, provider_point_count: 0,
      pressure_index: null, pressure_forecast_30m: null, pressure_forecast_60m: null,
      forecast_30m: null, forecast_60m: null, forecast_method: 'unavailable',
      forecast_observed_at: null, forecast_fresh_until: null,
    };
    const corridor = { ...CORRIDOR_CATALOG[0], observed_traffic: observation };
    respond({ data_source: 'live', generated_at: new Date(now).toISOString(), corridors: [corridor] });

    const result = await fetchCorridors();

    expect(result.data).toEqual([corridor]);
    expect(corridorLiveView(result.data[0], now)).toMatchObject({
      available: false, status: 'UNAVAILABLE', score: null, delay: null, forecastMethod: 'unavailable',
    });
  });

  it('passes genuine zero observations through even when legacy planning fields disagree', async () => {
    const now = Date.now();
    const corridor: Corridor = {
      ...CORRIDOR_CATALOG[0], live_congestion_score: 72, delay_mins: 8, status: 'CRITICAL',
      forecast_30m: 80, forecast_60m: 90, observed_traffic: freshObservation(0, now),
    };
    respond({ data_source: 'live', generated_at: new Date(now).toISOString(), corridors: [corridor] });

    const result = await fetchCorridors();

    expect(result.source).toBe('live');
    expect(result.data[0].observed_traffic).toEqual(corridor.observed_traffic);
    expect(corridorLiveView(result.data[0], now)).toMatchObject({
      available: true, status: 'FREE_FLOW', score: 0, pressureScore: 0, delay: null,
      forecast30: 0, forecast60: 0, pressureForecast30: 0, pressureForecast60: 0, forecastMethod: 'persistence',
    });
  });

  it('does not promote legacy live-labelled numeric payloads to measured conditions', async () => {
    const corridor: Corridor = {
      ...CORRIDOR_CATALOG[0], live_congestion_score: 72, delay_mins: 8, status: 'CRITICAL',
      forecast_30m: 80, forecast_60m: 90,
    };
    respond({ data_source: 'live', generated_at: new Date().toISOString(), corridors: [corridor] });

    const result = await fetchCorridors();

    expect(corridorLiveView(result.data[0])).toMatchObject({
      available: false, score: null, delay: null, forecast30: null, forecast60: null,
    });
  });

  it('preserves separate raw measurements and backend pressure through the API response', async () => {
    const now = Date.now();
    const evidence = freshObservation(15, now);
    const row = { ...CORRIDOR_CATALOG[0], observed_traffic: evidence };
    respond({ data_source: 'live', generated_at: new Date(now).toISOString(), corridors: [row] });
    const result = await fetchCorridors();
    expect(result.data[0].observed_traffic).toEqual(evidence);
    expect(corridorLiveView(result.data[0], now)).toMatchObject({
      available: true, score: 15, pressureScore: 37.5,
      forecast30: 15, forecast60: 15, pressureForecast30: 37.5, pressureForecast60: 37.5,
    });
  });
});
