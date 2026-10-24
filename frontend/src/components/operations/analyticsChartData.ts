import type { AnalyticsSimulation } from '../../services/api';
import type { OperationsSummary } from '../../types';
import { shortCorridorName } from '../../utils/format';

export type TrafficPoint = NonNullable<OperationsSummary['current_traffic_samples']>[number];
export const trafficBands = [
  { name: 'Smooth', range: '0–39', color: 'var(--accent-emerald)' },
  { name: 'Heavy', range: '40–69', color: 'var(--accent-amber)' },
  { name: 'Critical', range: '70–100', color: 'var(--accent-rose)' },
] as const;
export const trafficBand = (score: number) => score >= 70 ? 2 : score >= 40 ? 1 : 0;
export const forecastClock = (timestamp: string) => new Intl.DateTimeFormat('en-GB', {
  hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Jakarta',
}).format(new Date(timestamp));

export function measuredChartData(samples: TrafficPoint[]) {
  const speeds = samples.filter(point => [point.score, point.current_speed_kmh, point.free_flow_speed_kmh]
    .every(Number.isFinite) && point.score >= 0 && point.score <= 100
    && point.current_speed_kmh >= 0 && point.free_flow_speed_kmh > 0).map(point => ({
      id: point.id, name: shortCorridorName(point.name), measured: point.current_speed_kmh,
      freeFlow: point.free_flow_speed_kmh, score: point.score,
    }));
  const conditions = trafficBands.map((band, index) => ({ ...band,
    count: speeds.filter(point => trafficBand(point.score) === index).length,
  }));
  return { speeds, conditions };
}

export function corridorForecastData(simulation: AnalyticsSimulation) {
  const timestamps = simulation.hourly_forecast.map(point => point.timestamp);
  return simulation.corridors.map(corridor => {
    const byTime = new Map(corridor.hourly_forecast.map(point => [point.timestamp, point.mean_score]));
    const scores = timestamps.map(time => byTime.get(time) ?? null);
    const available = scores.filter((value): value is number => value !== null && Number.isFinite(value));
    return { id: corridor.corridor_id, name: shortCorridorName(corridor.corridor_name), scores,
      current: scores[0] ?? null, peak: available.length ? Math.max(...available) : null };
  });
}
