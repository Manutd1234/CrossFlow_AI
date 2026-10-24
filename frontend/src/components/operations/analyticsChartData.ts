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
const measuredConditions = [
  { condition: 'FREE_FLOW', name: 'Free flow', color: 'var(--accent-emerald)' },
  { condition: 'SLOWING', name: 'Slowing', color: 'var(--accent-amber)' },
  { condition: 'HEAVY', name: 'Heavy', color: 'var(--accent-amber)' },
  { condition: 'CRITICAL', name: 'Critical', color: 'var(--accent-rose)' },
  { condition: 'UNAVAILABLE', name: 'Unavailable', color: 'var(--text-muted)' },
] as const;

function qualifiedMeasuredPoint(point: TrafficPoint): boolean {
  const confidence = point.provider_confidence;
  if (![point.score, point.current_speed_kmh, point.free_flow_speed_kmh].every(Number.isFinite)
    || point.score < 0 || point.score > 95
    || point.current_speed_kmh < 0 || point.current_speed_kmh > 250
    || point.free_flow_speed_kmh <= 0 || point.free_flow_speed_kmh > 250
    || point.current_speed_kmh / point.free_flow_speed_kmh > 1.5
    || typeof confidence !== 'number' || !Number.isFinite(confidence) || confidence < 0.5 || confidence > 1
    || point.condition_policy !== 'observed_speed_loss_bands_v2') return false;
  // Validate the declared rounded index against the serving contract; never
  // infer a condition when the backend's declaration is absent or incoherent.
  if (Math.abs(point.score - 100 * (1 - Math.min(1, Math.max(0.05,
    point.current_speed_kmh / point.free_flow_speed_kmh)))) > 0.051) return false;
  const expected = point.score <= 5 ? 'FREE_FLOW' : point.score < 20 ? 'SLOWING'
    : point.score < 40 ? 'HEAVY' : 'CRITICAL';
  return point.condition === expected;
}

export const forecastClock = (timestamp: string) => new Intl.DateTimeFormat('en-GB', {
  hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Jakarta',
}).format(new Date(timestamp));

export function measuredChartData(samples: TrafficPoint[]) {
  const points = samples.map(point => ({ point, qualified: qualifiedMeasuredPoint(point) }));
  const speeds = points.filter(({ qualified }) => qualified).map(({ point }) => ({
      id: point.id, name: shortCorridorName(point.name), measured: point.current_speed_kmh,
      freeFlow: point.free_flow_speed_kmh, score: point.score,
    }));
  const conditions = measuredConditions.map(band => ({ ...band,
    count: points.filter(({ point, qualified }) => (qualified ? point.condition : 'UNAVAILABLE') === band.condition).length,
  }));
  return { speeds, conditions, pointCount: points.length };
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
