import { describe, expect, it } from 'vitest';
import type { AnalyticsSimulation } from '../../services/api';
import { corridorForecastData, forecastClock, measuredChartData, type TrafficPoint } from './analyticsChartData';

const sample = (id: string, score: number, speed = 42): TrafficPoint => ({
  id, name: id, score, current_speed_kmh: speed, free_flow_speed_kmh: 60,
  as_of: '2026-10-23T10:00:00Z', age_seconds: 0, provider_confidence: 0.9,
});
const point = (hour: number, score: number) => ({ timestamp: `2026-10-24T${String(hour).padStart(2, '0')}:00:00+07:00`,
  mean_score: score, p10_score: score - 1, p90_score: score + 1 });

describe('analytics chart data integrity', () => {
  it('uses actual speeds and counts score boundaries, including a stopped road', () => {
    const data = measuredChartData([sample('a', 0), sample('b', 39.9), sample('c', 40), sample('d', 69.9), sample('e', 70, 0)]);
    expect(data.speeds.map(row => row.measured)).toEqual([42, 42, 42, 42, 0]);
    expect(data.conditions.map(row => row.count)).toEqual([2, 2, 1]);
  });
  it('does not invent observations for an empty or invalid sample', () => {
    expect(measuredChartData([]).conditions.map(row => row.count)).toEqual([0, 0, 0]);
    expect(measuredChartData([sample('nan', NaN), sample('negative', 10, -1), sample('bad-score', 101)]).speeds).toEqual([]);
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
