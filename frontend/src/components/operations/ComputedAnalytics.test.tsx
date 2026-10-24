/* @vitest-environment jsdom */
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { INITIAL_CORRIDORS } from '../../data/mockData';
import { fetchAuthorityReports, requestAnalyticsSimulation, type AnalyticsSimulation, type AuthorityReports } from '../../services/api';
import type { Fetched } from '../../types';
import { OperationsSimulation } from './OperationsSimulation';
import { PublishedOperatingRecords } from './PublishedOperatingRecords';

vi.mock('../../services/api', () => ({ fetchAuthorityReports: vi.fn(), requestAnalyticsSimulation: vi.fn() }));
const simulate = vi.mocked(requestAnalyticsSimulation);
const reports = vi.mocked(fetchAuthorityReports);
let root: Root | undefined;
const NOW = new Date('2026-10-23T08:00:00Z');
const forecast = (weather = 0, mean = 41): Fetched<AnalyticsSimulation> => ({
  source: 'simulated', fetchedAt: '2026-10-23T07:59:00Z', data: {
    horizon_hours: 24, step_minutes: 60, weather, timezone: 'Asia/Jakarta',
    hourly_forecast: [
      { timestamp: '2026-10-23T08:00:00Z', mean_score: mean, p10_score: 31, p90_score: 51 },
      { timestamp: '2026-10-23T09:00:00Z', mean_score: mean + 7, p10_score: 38, p90_score: 58 },
    ], corridors: [], weather_comparison: [{ weather: 0, label: 'Clear', mean_score: 41, p10_score: 31, p90_score: 51 }],
    model: { training_data_source: 'test_backend_training', observed_training_rows: 0 }, limitations: ['Synthetic training does not establish real-world forecast accuracy.'],
  },
});
const official = (): AuthorityReports => ({
  status: 'fresh', checked_at: '2026-10-23T07:58:00Z', fetched_at: '2026-10-23T07:55:00Z',
  report: { publisher: 'BP Batam', source_url: 'https://batamport.bpbatam.go.id/pelabuhan-penumpang/',
    period_label: 'Source period A', period_start: '2026-01', period_end: '2026-08',
    metrics: [{ id: 'calls', label: 'Source vessel calls', value: 12345, unit: 'calls' }], content_sha256: 'a' },
  stored_reports: [], storage: { durable: true, scope: 'single_server' }, error: null,
});
async function render(element: ReactElement) {
  const container = document.createElement('div'); document.body.append(container); root = createRoot(container);
  await act(async () => root?.render(element)); return container;
}
function button(container: HTMLElement, label: string) {
  const found = [...container.querySelectorAll('button')].find(item => item.textContent?.includes(label));
  expect(found).toBeDefined(); return found!;
}
async function change(container: HTMLElement, label: string, value: string) {
  const select = container.querySelector<HTMLSelectElement>(`[aria-label="${label}"]`)!;
  await act(async () => { select.value = value; select.dispatchEvent(new Event('change', { bubbles: true })); });
}
beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(NOW); vi.resetAllMocks(); simulate.mockResolvedValue(forecast()); reports.mockResolvedValue(official()); });
afterEach(() => { act(() => root?.unmount()); root = undefined; document.body.replaceChildren(); vi.useRealTimers(); });

describe('computed analytics sources', () => {
  it('renders backend-computed forecasts, quantile scope and actual source timestamps', async () => {
    const view = await render(<OperationsSimulation corridors={INITIAL_CORRIDORS} accessToken="operator-session" />);
    expect(simulate).toHaveBeenCalledWith(0, undefined, 'operator-session');
    expect(view.querySelector('[aria-label="Model forecast charts"]')).not.toBeNull();
    expect(view.querySelectorAll('.operations-observed-table tbody tr')).toHaveLength(2);
    expect(view.textContent).toContain('41.0'); expect(view.textContent).toContain('48.0');
    expect(view.textContent).toContain('not a calibrated joint interval');
    expect(view.textContent).toContain('test_backend_training');
    expect(view.querySelector('time[datetime="2026-10-23T07:59:00Z"]')).not.toBeNull();
  });
  it('changes assumptions and rejects a late forecast for the previous selection', async () => {
    let resolveOld: (value: Fetched<AnalyticsSimulation>) => void = () => {};
    simulate.mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve; }));
    const view = await render(<OperationsSimulation corridors={INITIAL_CORRIDORS} accessToken="operator-session" />);
    simulate.mockResolvedValue(forecast(1, 62));
    await change(view, 'Simulation weather', '1');
    await change(view, 'Simulation corridor', INITIAL_CORRIDORS[0].id);
    expect(simulate).toHaveBeenLastCalledWith(1, [INITIAL_CORRIDORS[0].id], 'operator-session');
    await act(async () => resolveOld(forecast(0, 13)));
    expect(view.textContent).toContain('62.0'); expect(view.textContent).not.toContain('13.0');
    expect(view.querySelector<HTMLSelectElement>('[aria-label="Simulation weather"]')?.value).toBe('1');
  });
  it('keeps the last actual forecast stale on failure and refreshes on focus and polling', async () => {
    const view = await render(<OperationsSimulation corridors={INITIAL_CORRIDORS} accessToken={null} />);
    simulate.mockRejectedValue(new Error('Backend temporarily unavailable'));
    await act(async () => window.dispatchEvent(new Event('focus')));
    expect(view.textContent).toContain('Showing the previous computed snapshot; it is stale');
    expect(view.querySelector('[aria-label="Model forecast charts"]')).not.toBeNull();
    await act(async () => vi.advanceTimersByTime(60_000));
    expect(simulate).toHaveBeenCalledTimes(3);
    await act(async () => button(view, 'Recompute forecast').click());
    expect(simulate).toHaveBeenCalledTimes(4);
  });
  it('shows a recoverable unavailable state without an invented forecast', async () => {
    simulate.mockRejectedValue(new Error('Operator access required'));
    const view = await render(<OperationsSimulation corridors={INITIAL_CORRIDORS} accessToken={null} />);
    expect(view.querySelector('[aria-label="Model forecast charts"]')).toBeNull(); expect(view.textContent).toContain('Operator access required');
    simulate.mockResolvedValue(forecast());
    await act(async () => button(view, 'Recompute forecast').click());
    expect(view.querySelector('[aria-label="Model forecast charts"]')).not.toBeNull();
  });
  it('renders only scraped period figures and separates source time from browser checks', async () => {
    const payload = official(); payload.stored_reports = [{ ...payload.report!, period_label: 'Older source period', content_sha256: 'b' }];
    reports.mockResolvedValue(payload);
    const view = await render(<PublishedOperatingRecords />);
    expect(view.textContent).toContain('12,345 calls'); expect(view.textContent).toContain('Source period A');
    expect(view.textContent).toContain('Older source period'); expect(view.textContent).toContain('not live terminal activity');
    expect(view.querySelector('a')?.getAttribute('href')).toBe(payload.report!.source_url);
    expect([...view.querySelectorAll('time')].map(item => item.dateTime)).toEqual([
      payload.checked_at,
    ]);
    reports.mockRejectedValue(new Error('Source timed out'));
    await act(async () => button(view, 'Check official report').click());
    expect(view.querySelector('[aria-label="Report status: stale"]')).not.toBeNull(); expect(view.textContent).toContain('12,345 calls');
    expect(view.textContent).toContain('Last known published report retained');
  });
  it('does not substitute report totals when no validated source is available', async () => {
    reports.mockResolvedValue({ ...official(), status: 'unavailable', report: null, error: 'Source parser rejected the page' });
    const view = await render(<PublishedOperatingRecords />);
    expect(view.querySelector('dl')).toBeNull(); expect(view.textContent).toContain('Source parser rejected the page');
    expect(view.textContent).toContain('No validated official report is available');
  });

  it('computes passenger shares from people only and keeps vessel calls separate', async () => {
    const payload = official();
    payload.report!.metrics = [
      { id: 'vessels', label: 'Vessel calls', value: 1000, unit: 'calls' },
      { id: 'domestic', label: 'Domestic movements', value: 100, unit: 'people' },
      { id: 'international', label: 'International movements', value: 300, unit: 'people' },
    ];
    reports.mockResolvedValue(payload);
    const view = await render(<PublishedOperatingRecords />);
    const graphic = view.querySelector('.maritime-passenger-bar');
    expect(graphic?.getAttribute('aria-label')).toContain('Domestic movements: 100, 25.0%');
    expect(graphic?.getAttribute('aria-label')).toContain('International movements: 300, 75.0%');
    expect(graphic?.getAttribute('aria-label')).not.toContain('Vessel');
    expect(view.querySelectorAll('.maritime-report-metric')).toHaveLength(3);
    expect(view.querySelector('details')).toBeNull();
  });
});
