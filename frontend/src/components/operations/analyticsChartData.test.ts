import { describe, expect, it } from 'vitest';
import type { AnalyticsSimulation } from '../../services/api';
import { corridorForecastData, forecastClock, measuredChartData, trafficBand, trafficBands, type TrafficPoint } from './analyticsChartData';
import { corridorColor } from '../../utils/format';

const sample = (id: string, score: number, speed = 60 * (1 - score / 100)): TrafficPoint => ({
  id, name: id, score, current_speed_kmh: speed, free_flow_speed_kmh: 60,
  as_of: '2026-10-23T10:00:00Z', age_seconds: 0, provider_confidence: 0.9,
  condition: score <= 5 ? 'FREE_FLOW' : score < 20 ? 'SLOWING' : score < 40 ? 'HEAVY' : 'CRITICAL',
  condition_policy: 'observed_speed_loss_bands_v2',
  pressure_index: Math.min(100, score * 2.5), pressure_multiplier: 2.5, pressure_policy: 'congestion_pressure_v1',
  pressure_condition: score * 2.5 <= 5 ? 'FREE_FLOW' : score * 2.5 < 70 ? 'SLOWING' : score * 2.5 < 80 ? 'HEAVY' : 'CRITICAL',
  pressure_condition_policy: 'congestion_pressure_bands_v1',
});
const point = (hour: number, score: number) => ({ timestamp: `2026-10-24T${String(hour).padStart(2, '0')}:00:00+07:00`,
  mean_score: score, p10_score: score - 1, p90_score: score + 1 });

describe('analytics chart data integrity', () => {
  it('uses declared pressure bands at 69.9, 70, 79.9 and 80 while retaining actual speeds', () => {
    const samples = [0, 5, 5.1, 69.9, 70, 79.9, 80, 100].map((pressure, index) => sample(String(index), pressure / 2.5));
    const data = measuredChartData(samples);
    expect(data.speeds.map(row => row.measured)).toEqual(samples.map(point => point.current_speed_kmh));
    expect(data.speeds.map(row => row.score)).toEqual(samples.map(point => point.score));
    expect(data.conditions.map(row => [row.name, row.count])).toEqual([
      ['Free flow', 2], ['Slowing', 2], ['Heavy', 2], ['Critical', 2], ['Unavailable', 0],
    ]);
    expect(data.pointCount).toBe(8);
    expect(data.conditions.find(row => row.name === 'Slowing')?.color).toBe(corridorColor('SMOOTH'));
    expect(data.conditions.find(row => row.name === 'Heavy')?.color).toBe(corridorColor('HEAVY'));
    expect(data.conditions.find(row => row.name === 'Critical')?.color).toBe(corridorColor('CRITICAL'));
  });
  it('does not invent observations for an empty or invalid sample', () => {
    expect(measuredChartData([]).conditions.map(row => row.count)).toEqual([0, 0, 0, 0, 0]);
    const result = measuredChartData([sample('nan', NaN), sample('negative', 10, -1), sample('bad-score', 101)]);
    expect(result.speeds).toEqual([]);
    expect(result.conditions.map(row => row.count)).toEqual([0, 0, 0, 0, 3]);
  });
  it.each([
    { condition: undefined }, { condition_policy: undefined }, { condition_policy: 'mapped_camera_proxy_v1' },
    { condition: 'CAMERA_ESTIMATE' },
    { provider_confidence: null }, { provider_confidence: 0.49 }, { provider_confidence: 1.01 },
    { current_speed_kmh: 60 }, { free_flow_speed_kmh: 0 },
  ] satisfies Partial<TrafficPoint>[])('keeps unqualified measurements neutral rather than green: %j', overrides => {
    const result = measuredChartData([{ ...sample('a', 15), ...overrides }]);
    expect(result.speeds).toEqual([]);
    expect(result.conditions.find(row => row.name === 'Unavailable')).toMatchObject({
      count: 1, color: 'var(--text-muted)',
    });
    expect(result.conditions.find(row => row.name === 'Free flow')?.count).toBe(0);
  });
  it.each([
    { pressure_condition: undefined }, { pressure_condition_policy: undefined },
    { pressure_condition: 'FREE_FLOW' }, { pressure_index: null }, { pressure_index: Number.NaN },
    { pressure_policy: undefined }, { pressure_multiplier: 3 }, { pressure_index: 100 },
  ] satisfies Partial<TrafficPoint>[])('keeps missing or incoherent pressure neutral while retaining verified speeds: %j', overrides => {
    const result = measuredChartData([{ ...sample('a', 15), ...overrides }]);
    expect(result.speeds).toHaveLength(1);
    expect(result.conditions.find(row => row.name === 'Unavailable')).toMatchObject({ count: 1, color: 'var(--text-muted)' });
    expect(result.classifiedCount).toBe(0);
  });
  it('never uses the old raw 20/40 warning label to display pressure', () => {
    const point = sample('raw20-pressure50', 20);
    expect(point.condition).toBe('HEAVY');
    const result = measuredChartData([point]);
    expect(result.conditions.find(row => row.name === 'Slowing')?.count).toBe(1);
    expect(result.conditions.find(row => row.name === 'Heavy')?.count).toBe(0);
    expect(result.speeds[0].score).toBe(20);
    expect(result.speeds[0].measured).toBe(48);
  });
  it('can show all genuinely clear measured points without forcing a congested category', () => {
    const result = measuredChartData([0, 1, 2, 3, 5].map((pressure, index) => sample(String(index), pressure / 2.5)));
    expect(result.conditions.map(row => row.count)).toEqual([5, 0, 0, 0, 0]);
    expect(result.speeds).toHaveLength(5);
  });
  it('leaves the offline and forecast traffic bands unchanged', () => {
    expect(trafficBands.map(band => band.name)).toEqual(['Smooth', 'Heavy', 'Critical']);
    expect(trafficBand(15)).toBe(0);
    expect(trafficBand(20)).toBe(0);
    expect(trafficBand(40)).toBe(1);
    expect(trafficBand(70)).toBe(2);
  });
  it('aligns corridor cells by timestamp and leaves missing hours unavailable', () => {
    const simulation = { hourly_forecast: [point(0, 20), point(1, 25), point(2, 30)],
      corridors: [{ corridor_id: 'a', corridor_name: 'A', hourly_forecast: [point(2, 90), point(0, 0)] },
        { corridor_id: 'b', corridor_name: 'B', hourly_forecast: [] }] } as AnalyticsSimulation;
    const original = JSON.stringify(simulation);
    const rows = corridorForecastData(simulation);
    expect(rows[0]).toMatchObject({ current: 0, peak: 90, scores: [0, null, 90] });
    expect(rows[1]).toMatchObject({ current: null, peak: null, scores: [null, null, null] });
    expect(JSON.stringify(simulation)).toBe(original);
  });
  it('formats forecast times in Batam rather than the browser timezone', () => {
    expect(forecastClock('2026-10-23T17:15:00Z')).toBe('00:15');
  });
});
