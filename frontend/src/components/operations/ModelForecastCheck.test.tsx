/* @vitest-environment jsdom */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { requestAnalyticsSimulation, type AnalyticsSimulation } from '../../services/api';
import type { Fetched } from '../../types';
import { ModelForecastCheck } from './ModelForecastCheck';

vi.mock('../../services/api', () => ({ requestAnalyticsSimulation: vi.fn() }));
const compute = vi.mocked(requestAnalyticsSimulation);
let root: Root;
let view: HTMLDivElement;
function result(mean = 31): Fetched<AnalyticsSimulation> {
  return { source: 'simulated', fetchedAt: '2026-10-24T02:30:00Z', data: {
    horizon_hours: 24, step_minutes: 60, weather: 0, timezone: 'Asia/Jakarta', prediction_count: 135,
    engine: { algorithm: 'RandomForestRegressor', estimator_count: 87, feature_count: 23, is_trained: true, last_trained_at: '2026-10-23T01:00:00Z' },
    hourly_forecast: [{ timestamp: '2026-10-24T02:30:00Z', mean_score: mean, p10_score: 20, p90_score: 40 },
      { timestamp: '2026-10-24T03:30:00Z', mean_score: 45, p10_score: 35, p90_score: 55 }],
    corridors: [{ corridor_id: 'corridor-1', corridor_name: 'Test corridor', hourly_forecast: [] }],
    weather_comparison: [], model: { training_data_source: 'test_training_source' }, limitations: [],
  } };
}
beforeEach(async () => {
  vi.resetAllMocks(); view = document.createElement('div'); document.body.append(view); root = createRoot(view);
  await act(async () => root.render(<ModelForecastCheck accessToken="operator-test" />));
});
afterEach(() => { act(() => root.unmount()); document.body.replaceChildren(); });
it('runs protected inference on demand and displays actual runtime metadata and forecast values', async () => {
  expect(compute).not.toHaveBeenCalled();
  compute.mockResolvedValue(result());
  await act(async () => view.querySelector('button')!.click());
  expect(compute).toHaveBeenCalledWith(0, undefined, 'operator-test');
  expect(view.textContent).toContain('31.0'); expect(view.textContent).toContain('45.0');
  expect(view.textContent).toContain('135'); expect(view.textContent).toContain('87 fitted trees · 23 input features');
  expect(view.textContent).toContain('test training source');
  expect(view.querySelector('time')?.dateTime).toBe('2026-10-24T02:30:00Z');
});
it('discards an in-flight response after scenario or credential changes', async () => {
  let resolve!: (value: Fetched<AnalyticsSimulation>) => void;
  compute.mockReturnValueOnce(new Promise(done => { resolve = done; }));
  await act(async () => view.querySelector('button')!.click());
  await act(async () => {
    view.querySelector('select')!.value = '2';
    view.querySelector('select')!.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await act(async () => resolve(result(17)));
  expect(view.textContent).not.toContain('17.0');
  compute.mockResolvedValue(result(66));
  await act(async () => view.querySelector('button')!.click());
  expect(compute).toHaveBeenLastCalledWith(2, undefined, 'operator-test');
  expect(view.textContent).toContain('66.0');
  await act(async () => root.render(<ModelForecastCheck accessToken="different-operator" />));
  expect(view.textContent).not.toContain('66.0');
});
it('reports failures without substituting a curve or a successful engine status', async () => {
  compute.mockRejectedValue(new Error('Model service unavailable'));
  await act(async () => view.querySelector('button')!.click());
  expect(view.querySelector('[role="alert"]')?.textContent).toBe('Model service unavailable');
  expect(view.querySelector('svg[role="img"]')).toBeNull();
  expect(view.textContent).not.toContain('fitted trees');
});
