import { afterEach, expect, it, vi } from 'vitest';
import { fetchRealTrafficForecast } from './api';
import { realTrafficFixture } from '../test/fixtures/realTraffic';
afterEach(() => vi.unstubAllGlobals());
function respond(forecast: unknown) {
  const request = vi.fn().mockResolvedValue(new Response(JSON.stringify({ forecast }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
  vi.stubGlobal('fetch', request); return request;
}
it('forwards operator authentication and retains real evidence including negative R² and unavailable intervals', async () => {
  const value = realTrafficFixture(); const request = respond(value);
  expect(await fetchRealTrafficForecast('operator-fixture')).toEqual(value);
  expect(request.mock.calls[0][1].headers).toMatchObject({ Authorization: 'Bearer operator-fixture' });
});
it('rejects synthetic or malformed forecasts without a fallback', async () => {
  for (const change of ['synthetic', 'interval', 'number']) {
    const value = realTrafficFixture();
    if (change === 'synthetic') Object.assign(value.model.report, { synthetic_rows: 4000 });
    if (change === 'interval') Object.assign(value.network[0], { lower: 30, upper: 10 });
    if (change === 'number') value.points[0].current_index = 101;
    respond(value);
    await expect(fetchRealTrafficForecast('operator-fixture')).rejects.toThrow('No synthetic values were substituted');
  }
});
it('rejects impossible prospective counts and invalid benchmark tolerance fractions', async () => {
  const value = realTrafficFixture();
  value.prospective_evaluation = { status: 'collecting', issued: 2, matched: 3, matched_days: 1 };
  respond(value);
  await expect(fetchRealTrafficForecast('operator-fixture')).rejects.toThrow('evaluation evidence failed validation');
  value.prospective_evaluation.matched = 1;
  value.historical_benchmarks = [{ id: 'guangzhou_speed', name: 'Benchmark', geography: 'Guangzhou, China', source_rows: 100, source_days: 61,
    source_url: 'https://zenodo.org/records/1205229', scope: 'Not Batam', horizons: [{ horizon_minutes: 30,
      candidate: { samples: 10, mae_kph: 1, rmse_kph: 2, within_5_kph: 1.2 }, persistence: { samples: 10, mae_kph: 2, rmse_kph: 3, within_5_kph: .5 },
      mae_improvement_percent: 50, test_days: 14, improvement_ci95_kph: null }] }];
  respond(value);
  await expect(fetchRealTrafficForecast('operator-fixture')).rejects.toThrow('evaluation evidence failed validation');
});

it('accepts the declared configured sampling coverage instead of limiting every model to five points', async () => {
  const value = realTrafficFixture();
  value.coverage = { monitored_points: 6, expected_points: 8, max_horizon_minutes: 60,
    configured_point_ids: Array.from({ length: 8 }, (_, i) => `sample-${i + 1}`), sampling_scope: 'representative_provider_points' };
  value.points = Array.from({ length: 6 }, (_, i) => ({ ...value.points[0], corridor_id: `sample-${i + 1}` }));
  value.network = value.network.map(row => ({ ...row, strategy: 'persistence' }));
  respond(value);
  expect((await fetchRealTrafficForecast('operator-fixture')).coverage.monitored_points).toBe(6);
  value.coverage.configured_point_ids![0] = 'unexpected-id'; respond(value);
  await expect(fetchRealTrafficForecast('operator-fixture')).rejects.toThrow('No synthetic values were substituted');
});
it('rejects a network strategy that contradicts the per-point serving strategy', async () => {
  const value = realTrafficFixture(); value.network[0].strategy = 'random_forest'; respond(value);
  await expect(fetchRealTrafficForecast('operator-fixture')).rejects.toThrow('No synthetic values were substituted');
});

it('rejects a network mean that does not match the returned point forecasts', async () => {
  const value = realTrafficFixture(); value.network[0].mean_score = 55; respond(value);
  await expect(fetchRealTrafficForecast('operator-fixture')).rejects.toThrow('No synthetic values were substituted');
});
