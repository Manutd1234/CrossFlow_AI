/* @vitest-environment jsdom */

import { act } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { ApiRequestError, fetchRouteRunHistory } from '../../services/api';
import type { RouteRunHistory, RouteRunSummary } from '../../types';
import { RouteRunHistoryPanel } from './RouteRunHistoryPanel';

vi.mock('../../services/api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../services/api')>(),
  fetchRouteRunHistory: vi.fn(),
}));

const fetchHistory = vi.mocked(fetchRouteRunHistory);
let root: Root | undefined;
let container: HTMLDivElement;

const run = (code: string, origin: string): RouteRunSummary => ({
  route_id: code, route_code: code, origin_name: origin, destination_name: 'Batam Centre',
  route_kind: 'optimize-free-route', vehicle_type: 'COMMUTER', created_by: 'user',
  created_at: '2026-10-23T05:00:00Z', updated_at: '2026-10-23T05:00:00Z',
});

async function render(token = 'session-a', activeRouteCode: string | null = null, onSelectRun = vi.fn()) {
  if (!root) {
    container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
  }
  await act(async () => {
    root!.render(<RouteRunHistoryPanel accessToken={token} activeRouteCode={activeRouteCode} onSelectRun={onSelectRun} />);
  });
  return container;
}

afterEach(() => {
  if (root) act(() => root!.unmount());
  root = undefined;
  document.body.replaceChildren();
  vi.resetAllMocks();
});

describe('route history workspace', () => {
  it('loads, selects, refreshes, and updates after a newly planned route', async () => {
    fetchHistory.mockResolvedValue({ runs: [run('4KF9X2P', 'Nagoya')], scope: 'all_accounts',
      history_storage: { source: 'local_sqlite', shared: false, durable: true,
        durability: 'persistent_local_file', scope: 'single_host' } });
    const select = vi.fn();
    const panel = await render('session-a', '4KF9X2P', select);
    expect(panel.textContent).toContain('Every account’s runs');
    expect(panel.textContent).toContain('History is stored on this server only.');
    const item = panel.querySelector<HTMLButtonElement>('.route-run-history__item')!;
    expect(item.getAttribute('aria-current')).toBe('true');
    act(() => item.click());
    expect(select).toHaveBeenCalledWith('4KF9X2P');
    await act(async () => panel.querySelector<HTMLButtonElement>('[aria-label="Refresh route history"]')!.click());
    expect(fetchHistory).toHaveBeenCalledTimes(2);

    fetchHistory.mockResolvedValue({ runs: [run('7KQ4MNP', 'Sekupang')], scope: 'all_accounts' });
    await render('session-a', '7KQ4MNP', select);
    expect(fetchHistory).toHaveBeenCalledTimes(3);
    expect(panel.textContent).toContain('Sekupang');
    expect(panel.textContent).not.toContain('Nagoya');
  });

  it('hides old account rows immediately and ignores its late response', async () => {
    let resolveFirst: (history: RouteRunHistory) => void = () => undefined;
    fetchHistory.mockImplementationOnce(() => new Promise(resolve => { resolveFirst = resolve; }));
    const panel = await render();
    expect(panel.textContent).toContain('Loading route history');
    fetchHistory.mockResolvedValueOnce({ runs: [run('7KQ4MNP', 'New account')], scope: 'own_account' });
    await render('session-b');
    await act(async () => resolveFirst({ runs: [run('4KF9X2P', 'Old account')], scope: 'all_accounts' }));
    expect(panel.textContent).toContain('New account');
    expect(panel.textContent).not.toContain('Old account');
    expect(panel.textContent).toContain('Your own runs');
  });

  it('surfaces server errors and recovers to the empty state on refresh', async () => {
    fetchHistory.mockRejectedValueOnce(new ApiRequestError('Route history requires durable storage.', 503));
    const panel = await render();
    expect(panel.querySelector('[role="alert"]')?.textContent).toContain('Route history requires durable storage.');
    fetchHistory.mockResolvedValueOnce({ runs: [], scope: 'own_account' });
    await act(async () => panel.querySelector<HTMLButtonElement>('[aria-label="Refresh route history"]')!.click());
    expect(panel.querySelector('[role="alert"]')).toBeNull();
    expect(panel.textContent).toContain('No runs recorded yet');
  });
});
