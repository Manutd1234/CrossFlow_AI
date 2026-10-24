/* @vitest-environment jsdom */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { requestObservedBacktest } from '../../services/api';
import { observedResult } from '../../test/fixtures/observedBacktest';
import type { Fetched, ObservedBacktestResult } from '../../types';
import { ObservedBacktest } from './ObservedBacktest';

vi.mock('../../services/api', () => ({ requestObservedBacktest: vi.fn() }));
const request = vi.mocked(requestObservedBacktest);
let root: Root | undefined;
async function render(token = 'operator-session') {
  const container = document.createElement('div'); document.body.append(container); root = createRoot(container);
  await act(async () => root!.render(<ObservedBacktest accessToken={token} />)); return container;
}
async function run(container: HTMLElement) { await act(async () => container.querySelector('button')!.click()); }
beforeEach(() => { vi.resetAllMocks(); request.mockResolvedValue(observedResult()); });
afterEach(() => { act(() => root?.unmount()); root = undefined; document.body.replaceChildren(); });

describe('observed-only backtest presentation', () => {
  it('runs only on explicit request and forwards the operator session', async () => {
    const view = await render();
    await act(async () => window.dispatchEvent(new Event('focus')));
    expect(request).not.toHaveBeenCalled();
    expect(view.textContent).toContain('not the currently running forest');
    await run(view);
    expect(request).toHaveBeenCalledWith('operator-session');
    expect(view.textContent).toContain('Insufficient observed history');
    expect(view.textContent).toContain('60'); expect(view.textContent).toContain('0.37 hours');
    expect(view.textContent).toContain('observed span below required minimum');
    expect(view.textContent).toContain('No forecast-error or interval-coverage scores');
    expect(view.textContent).toContain('The running model was not replaced');
    expect(view.querySelector('time[datetime="2026-10-23T07:38:00Z"]')).not.toBeNull();
    expect(view.textContent).not.toContain('0.000');
  });
  it('shows the observed candidate, chronological partitions, baselines and empirical coverage without claiming activation', async () => {
    request.mockResolvedValue(observedResult(true));
    const view = await render(); await run(view);
    expect(view.textContent).toContain('Observed candidate evaluated');
    expect(view.textContent).toContain('192.00 hours'); expect(view.textContent).toContain('5,000');
    expect(view.textContent).toContain('Observed candidate MAE'); expect(view.textContent).toContain('Persistence MAE');
    expect(view.textContent).toContain('Current/history blend MAE'); expect(view.textContent).toContain('4.250');
    expect(view.textContent).toContain('3.125'); expect(view.textContent).toContain('62.5%'); expect(view.textContent).toContain('-1.250');
    expect(view.textContent).toContain('candidate does not beat persistence');
    expect(view.textContent).toContain('Equal timestamps stay in the same partition');
    expect(view.textContent).toContain('Purge gap: 60 minutes');
    expect(view.textContent).toContain('60 distinct rows and 180 prior-origin forecast pairs');
    expect(view.textContent).toContain('weather response is not validated');
    expect(view.textContent).toContain('published-timetable adjustment is not validated');
    expect(view.textContent).not.toContain('Observed candidate activated');
  });
  it('does not present zero-sample metrics as a perfect forecast', async () => {
    const response = observedResult(true);
    response.data.backtest.horizons[0] = { horizon_minutes: 15, status: 'insufficient_current_observations',
      serving_model: { samples: 0, mae_points: null, rmse_points: null, bias_points: null },
      persistence: { samples: 0, mae_points: null, rmse_points: null, bias_points: null },
      current_history_blend: { samples: 0, mae_points: null, rmse_points: null, bias_points: null }, p10_p90_coverage: null };
    request.mockResolvedValue(response); const view = await render(); await run(view);
    const row = [...view.querySelectorAll('tr')].find(item => item.textContent?.startsWith('15 min0'));
    expect(row?.textContent).toContain('Insufficient samples'); expect(row?.textContent).not.toContain('0.000');
  });
  it('prevents duplicate evaluations while an explicit request is pending', async () => {
    let finish!: (value: Fetched<ObservedBacktestResult>) => void;
    request.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    const view = await render();
    await act(async () => { view.querySelector('button')!.click(); view.querySelector('button')!.click(); });
    expect(request).toHaveBeenCalledOnce(); expect(view.querySelector('button')!.disabled).toBe(true);
    await act(async () => finish(observedResult())); expect(view.querySelector('button')!.disabled).toBe(false);
  });
  it('discards a late result after the operator account/session changes', async () => {
    let finishOld!: (value: Fetched<ObservedBacktestResult>) => void;
    request.mockReturnValueOnce(new Promise(resolve => { finishOld = resolve; }));
    const view = await render(); await run(view);
    await act(async () => root!.render(<ObservedBacktest accessToken="replacement-session" />));
    expect(view.querySelector('button')!.disabled).toBe(false);
    await run(view);
    await act(async () => finishOld(observedResult(true)));
    expect(request).toHaveBeenLastCalledWith('replacement-session');
    expect(view.textContent).toContain('Insufficient observed history'); expect(view.textContent).not.toContain('5,000');
  });
  it('retains a prior actual report as stale after a service error and supports retry', async () => {
    const view = await render(); await run(view);
    request.mockRejectedValueOnce(new Error('Evaluation busy. Try again in 5 seconds.'));
    await run(view);
    expect(view.querySelector('[role="alert"]')?.textContent).toContain('Try again in 5 seconds');
    expect(view.textContent).toContain('previous report is retained and may be stale');
    request.mockResolvedValueOnce(observedResult(true)); await run(view);
    expect(view.querySelector('[role="alert"]')).toBeNull(); expect(view.textContent).toContain('Observed candidate evaluated');
    await act(async () => root!.render(<ObservedBacktest accessToken="other-account" />));
    expect(view.textContent).not.toContain('5,000'); expect(view.textContent).toContain('No observed backtest has been requested');
  });
});
