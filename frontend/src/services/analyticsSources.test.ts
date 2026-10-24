import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchAuthorityReports, requestAnalyticsSimulation } from './api';

const point = { timestamp: '2026-10-23T16:00:00+07:00', mean_score: 43, p10_score: 35, p90_score: 52 };
const simulation = () => ({ generated_at: point.timestamp, data_source: 'simulation', observed: false,
  simulation: { horizon_hours: 24, step_minutes: 60, weather: 1, timezone: 'Asia/Jakarta',
    hourly_forecast: [point], corridors: [{ corridor_id: 'corridor-1', corridor_name: 'Terminal road', hourly_forecast: [point] }],
    weather_comparison: [{ weather: 0, label: 'Clear', mean_score: 40, p10_score: 31, p90_score: 49 }],
    model: { training_data_source: 'synthetic_baseline', observed_training_rows: 0 }, limitations: ['Modelled values only'] } });
const authority = () => ({ status: 'fresh', checked_at: point.timestamp, fetched_at: point.timestamp,
  report: { publisher: 'BP Batam', source_url: 'https://batamport.bpbatam.go.id/pelabuhan-penumpang/',
    period_label: 'Period supplied by source', period_start: '2026-01', period_end: '2026-08',
    metrics: [{ id: 'calls', label: 'Vessel calls', value: 12345, unit: 'calls' }], content_sha256: 'source-hash' },
  stored_reports: [], storage: { durable: true, scope: 'single_server' }, error: null });
function respond(value: unknown, status = 200) {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } }));
  vi.stubGlobal('fetch', fetcher); return fetcher;
}
afterEach(() => vi.unstubAllGlobals());

describe('computed and published analytics API boundaries', () => {
  it('forwards the operator token and requested assumptions without replacing server predictions', async () => {
    const payload = simulation(); const fetcher = respond(payload);
    const result = await requestAnalyticsSimulation(1, ['corridor-1'], 'operator-session');
    expect(result.data).toEqual(payload.simulation); expect(result.fetchedAt).toBe(payload.generated_at);
    const [url, init] = fetcher.mock.calls[0]; expect(url).toContain('/api/analytics/simulation');
    expect(init.headers).toMatchObject({ Authorization: 'Bearer operator-session' });
    expect(JSON.parse(init.body)).toEqual({ weather: 1, corridor_ids: ['corridor-1'] });
  });
  it.each([401, 403])('propagates operator access rejection %s without a fallback forecast', async status => {
    respond({ detail: 'Operator access required' }, status);
    await expect(requestAnalyticsSimulation(0, undefined, null)).rejects.toMatchObject({ status });
  });
  it('rejects malformed or missing model curves instead of inventing one', async () => {
    const payload = simulation(); payload.simulation.hourly_forecast[0] = { ...point, mean_score: 120 }; respond(payload);
    await expect(requestAnalyticsSimulation(0, undefined, null)).rejects.toThrow('No illustrative curve was substituted');
    respond({}); await expect(requestAnalyticsSimulation(0, undefined, null)).rejects.toThrow('No illustrative curve was substituted');
  });
  it('preserves actual fitted-engine metadata and rejects malformed runtime evidence', async () => {
    const payload = { ...simulation(), simulation: { ...simulation().simulation, prediction_count: 135,
      engine: { algorithm: 'RandomForestRegressor', estimator_count: 87, feature_count: 26, is_trained: true, last_trained_at: point.timestamp } } };
    respond(payload);
    const result = await requestAnalyticsSimulation(1, undefined, 'operator-session');
    expect(result.data.engine?.estimator_count).toBe(87);
    expect(result.data.prediction_count).toBe(135);
    for (const invalid of [
      { ...payload.simulation, prediction_count: -1 },
      { ...payload.simulation, engine: { ...payload.simulation.engine, estimator_count: '100' } },
      { ...payload.simulation, engine: { ...payload.simulation.engine, last_trained_at: 'invalid' } },
    ]) {
      respond({ ...payload, simulation: invalid });
      await expect(requestAnalyticsSimulation(1, undefined, 'operator-session')).rejects.toThrow('No illustrative curve was substituted');
    }
  });
  it('retains the exact scraped period, totals, timestamps and stale state', async () => {
    const payload = { ...authority(), status: 'stale', error: 'Upstream source unavailable' }; respond(payload);
    expect(await fetchAuthorityReports()).toEqual(payload);
  });
  it('accepts a legitimate unavailable report without supplying totals', async () => {
    const payload = { ...authority(), status: 'unavailable', report: null, error: 'Parser rejected source' }; respond(payload);
    expect((await fetchAuthorityReports()).report).toBeNull();
  });
  it('rejects malformed official-report numbers and network errors without fallback totals', async () => {
    const payload = authority(); payload.report.metrics[0].value = -1; respond(payload);
    await expect(fetchAuthorityReports()).rejects.toThrow('No fixed totals were substituted');
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));
    await expect(fetchAuthorityReports()).rejects.toThrow('No fixed totals were substituted');
  });
});
