/* @vitest-environment jsdom */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { fetchRealTrafficForecast } from '../../services/api';
import { realTrafficFixture } from '../../test/fixtures/realTraffic';
import { RealTrafficForecast } from './RealTrafficForecast';

vi.mock('../../services/api', () => ({ fetchRealTrafficForecast: vi.fn() }));
const fetcher = vi.mocked(fetchRealTrafficForecast);
let root: Root;
let view: HTMLDivElement;
beforeEach(() => {
  vi.resetAllMocks(); fetcher.mockResolvedValue(realTrafficFixture());
  view = document.createElement('div'); document.body.append(view); root = createRoot(view);
});
afterEach(() => { act(() => root.unmount()); document.body.replaceChildren(); });
it('renders actual model, weather and road evidence without synthetic scores or invented missing tags', async () => {
  await act(async () => root.render(<RealTrafficForecast accessToken="operator-fixture" showTraining />));
  expect(fetcher).toHaveBeenCalledWith('operator-fixture');
  expect(view.textContent).toContain('1,234'); expect(view.textContent).toContain('Monitored roads');
  expect(view.textContent).toContain('27.3°C'); expect(view.textContent).toContain('0.5 m');
  expect(view.textContent).toContain('Not tagged'); expect(view.textContent).toContain('Mean error');
  expect(view.textContent).toContain('Interval estimates are withheld');
  expect(view.textContent).not.toContain('0.9490'); expect(view.textContent).not.toContain('4,000');
  expect(view.textContent).not.toContain('71 trees');
  expect(view.textContent).not.toContain('What the forests learned');
  expect(view.textContent).not.toContain('input features');
  expect(view.textContent).not.toContain('Forest MAE');
  expect(view.textContent).not.toContain('60-minute purges');
});
it('retains a previous genuine result as stale on failure and allows an explicit retry', async () => {
  await act(async () => root.render(<RealTrafficForecast accessToken="operator-fixture" />));
  fetcher.mockRejectedValueOnce(new Error('Observed feed unavailable'));
  await act(async () => view.querySelector('button')!.click());
  expect(view.querySelector('[role="alert"]')?.textContent).toContain('previous snapshot is stale');
  expect(view.querySelector('.real-traffic-actions .badge')?.textContent).toBe('Saved forecast · stale');
  expect(view.querySelector('.real-traffic-timestamp')?.textContent).toContain('Previous snapshot');
  expect(view.querySelector('time[datetime="2026-10-24T00:00:00Z"]')).not.toBeNull();
  expect(view.textContent).toContain('20.0 / 100');
  await act(async () => view.querySelector('button')!.click());
  expect(view.querySelector('[role="alert"]')).toBeNull();
  expect(view.querySelector('.real-traffic-actions .badge')?.textContent).toBe('Baseline · provisional');
  expect(view.querySelector('.real-traffic-timestamp')?.textContent).toContain('1 of 5 configured sampling points available');
});
it('separates a source-specific tolerance result from Batam field validation and pending forecasts', async () => {
  const value = realTrafficFixture();
  value.prospective_evaluation = { status: 'collecting_prospective_evidence', issued: 150, matched: 0, matched_days: 0 };
  value.historical_benchmarks = [{ id: 'guangzhou_speed', name: 'Guangzhou speed benchmark', geography: 'Guangzhou, China',
    source_rows: 1800000, source_days: 61, source_url: 'https://zenodo.org/records/1205229', scope: 'Does not establish Batam accuracy.',
    horizons: [{ horizon_minutes: 30, candidate: { samples: 120000, mae_kph: 2, rmse_kph: 3, within_5_kph: .84 },
      persistence: { samples: 120000, mae_kph: 3, rmse_kph: 4, within_5_kph: .75 }, mae_improvement_percent: 33.33, test_days: 14, improvement_ci95_kph: [.5, 1.5] }] }];
  fetcher.mockResolvedValueOnce(value);
  await act(async () => root.render(<RealTrafficForecast accessToken="operator-fixture" showTraining />));
  expect(view.textContent).toContain('150 forecasts saved');
  expect(view.textContent).toContain('0 matched to later measurements');
  expect(view.textContent).toContain('84.0%');
  expect(view.textContent).toContain('Does not establish Batam accuracy.');
  expect(view.textContent).toContain('not percentage accuracy');
});
it('discards prior-account results and late requests when the operator changes', async () => {
  await act(async () => root.render(<RealTrafficForecast accessToken="first-operator" showTraining />));
  let finish!: (data: ReturnType<typeof realTrafficFixture>) => void;
  fetcher.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  await act(async () => root.render(<RealTrafficForecast accessToken="second-operator" showTraining />));
  expect(view.textContent).not.toContain('1,234');
  await act(async () => finish(realTrafficFixture()));
  expect(view.textContent).toContain('1,234');
});

it('shows an unavailable state rather than connecting forever when the initial API request fails', async () => {
  fetcher.mockRejectedValueOnce(new Error('Authorized operator access is required.'));
  await act(async () => root.render(<RealTrafficForecast accessToken="expired-fixture" showTraining />));
  expect(view.querySelector('.real-traffic-actions .badge')?.textContent).toBe('Unavailable');
  expect(view.querySelector('[role="alert"]')?.textContent).toContain('Authorized operator access is required.');
  expect(view.querySelector('.real-traffic-kpis')).toBeNull();
  expect(view.querySelector('button')?.disabled).toBe(false);
});

it('labels the serving baseline and measurement timestamp instead of presenting flat forecasts as active ML', async () => {
  await act(async () => root.render(<RealTrafficForecast accessToken="operator-fixture" />));
  expect(view.querySelector('.real-traffic-actions .badge')?.textContent).toBe('Baseline · provisional');
  const cards = Array.from(view.querySelectorAll('.real-traffic-kpi'));
  expect(cards.slice(1).every(card => card.textContent?.includes('Latest-observation baseline'))).toBe(true);
  expect(view.querySelector('time[datetime="2026-10-23T23:59:00Z"]')).not.toBeNull();
});
