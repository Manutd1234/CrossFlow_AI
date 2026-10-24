/* @vitest-environment jsdom */

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { Fetched, LiveTrafficData, OperationsSummary } from './types';
import { RoadCoverageSummary } from './components/operations/RoadCoverageSummary';

const apiMocks = vi.hoisted(() => ({
  fetchCorridors: vi.fn(), fetchFerries: vi.fn(), fetchOperationsSummary: vi.fn(),
  fetchLiveTraffic: vi.fn(), fetchPortIntelligence: vi.fn(),
  fetchCorridorRoutes: vi.fn(), fetchRouteLocations: vi.fn(),
}));

vi.mock('./services/api', async importOriginal => ({
  ...await importOriginal<typeof import('./services/api')>(), ...apiMocks,
}));
vi.mock('./services/auth', async importOriginal => ({
  ...await importOriginal<typeof import('./services/auth')>(),
  supabaseConfigured: () => false,
  readStoredSession: () => null,
  hasOAuthCallback: () => false,
  fetchAuthStatus: async () => ({ enabled: false, configured: false, mode: 'disabled' }),
}));
vi.mock('./components/app-shell/Header', () => ({
  Header: ({ setActiveTab, dataSource }: { setActiveTab: (tab: 'analytics') => void; dataSource: string }) => (
    <header data-road-source={dataSource}><button onClick={() => setActiveTab('analytics')}>Operations</button></header>
  ),
}));
vi.mock('./components/corridor-map/MapView', () => ({ MapView: () => <div>Road map</div> }));
vi.mock('./components/operations/OperationsAnalytics', () => ({
    OperationsAnalytics: ({ operations, checkedAt }: { operations: OperationsSummary; checkedAt: string }) => (
      <div>
        <time data-testid="operations-checked-at">{checkedAt}</time>
        {operations.road_network_coverage ? <RoadCoverageSummary coverage={operations.road_network_coverage}
          estimates={operations.camera_traffic_summary} /> : null}
      </div>
    ),
}));

import { App } from './App';
import { INITIAL_CORRIDORS, INITIAL_FERRIES, PUBLISHED_FERRY_TIMETABLE_METADATA, ROUTE_LOCATIONS } from './data/mockData';
import { UNAVAILABLE_OPERATIONS } from './data/unavailableOperations';

const start = new Date('2026-10-24T00:00:00Z');
let root: Root;
let view: HTMLDivElement;

function fetched<T>(data: T): Fetched<T> {
  return { data, source: 'offline', fetchedAt: new Date().toISOString() };
}

/** The frame is already 15 seconds old when the summary reaches the app. */
function operationsAt(now = Date.now(), frameAgeMs = 15_000): Fetched<OperationsSummary> {
  return {
    data: { ...UNAVAILABLE_OPERATIONS,
      road_network_coverage: { source: 'OpenStreetMap', scope: 'bundled_batam_routing_graph',
        mapped_nodes: 115320, mapped_source_ways: 27018, directed_edges: 249745,
        live_camera_edge_count: 0, live_camera_edge_coverage_percent: 0, complete_live_coverage: false },
      camera_traffic_summary: { status: 'estimated', configured_cameras: 6, fresh_cameras: 4,
        validated_cameras: 0, estimated_cameras: 4, mean_congestion_index: 42.3, vehicle_detections: 89,
        latest_observed_at: new Date(now - frameAgeMs).toISOString(),
        oldest_observed_at: new Date(now - frameAgeMs).toISOString(), valid_until: new Date(now - frameAgeMs + 30_000).toISOString(),
        source: 'registered_camera_aggregates', scope: 'fresh_registered_camera_views_only',
        measured_road_speed: false, independently_validated: false },
    }, source: 'simulated', fetchedAt: new Date(now).toISOString(),
  };
}

async function openOverview() {
  await act(async () => root.render(<App />));
  await act(async () => view.querySelector<HTMLButtonElement>('button')!.click());
}

beforeEach(() => {
  vi.useFakeTimers(); vi.setSystemTime(start); vi.resetAllMocks();
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
  apiMocks.fetchCorridors.mockImplementation(async () => fetched(INITIAL_CORRIDORS));
  apiMocks.fetchFerries.mockImplementation(async () => ({ ...fetched(INITIAL_FERRIES), timetable: PUBLISHED_FERRY_TIMETABLE_METADATA }));
  apiMocks.fetchOperationsSummary.mockImplementation(async () => operationsAt());
  apiMocks.fetchLiveTraffic.mockImplementation(async () => fetched({ zones: [] }));
  apiMocks.fetchPortIntelligence.mockImplementation(async () => fetched([]));
  apiMocks.fetchCorridorRoutes.mockImplementation(async () => fetched([]));
  apiMocks.fetchRouteLocations.mockImplementation(async () => fetched(ROUTE_LOCATIONS));
  view = document.createElement('div'); document.body.append(view); root = createRoot(view);
});

afterEach(() => {
  act(() => root.unmount()); document.body.replaceChildren(); window.sessionStorage.clear();
  vi.restoreAllMocks(); vi.useRealTimers();
});

it('replaces an aging numeric summary before expiry without repolling roads or ferry providers', async () => {
  await openOverview();
  expect(view.textContent).toContain('42.3 / 100');
  const baseline = Object.fromEntries(['fetchCorridors', 'fetchFerries', 'fetchLiveTraffic'].map(name =>
    [name, apiMocks[name as keyof typeof apiMocks].mock.calls.length]));
  const initialChecks = apiMocks.fetchOperationsSummary.mock.calls.length;
  await act(async () => vi.advanceTimersByTimeAsync(16_000));
  expect(view.textContent).toContain('42.3 / 100');
  expect(view.textContent).not.toContain('Awaiting fresh observations');
  expect(apiMocks.fetchOperationsSummary.mock.calls.length).toBeGreaterThan(initialChecks);
  expect(view.querySelector('[data-testid="operations-checked-at"]')?.textContent).toBe('2026-10-24T00:00:10.000Z');
  for (const name of ['fetchCorridors', 'fetchFerries', 'fetchLiveTraffic'] as const) {
    expect(apiMocks[name]).toHaveBeenCalledTimes(baseline[name]);
  }
  expect(view.querySelector('header')?.getAttribute('data-road-source')).toBe('offline');
});

it('withdraws an unchanged observation at its original deadline despite successful summary polls', async () => {
  apiMocks.fetchOperationsSummary.mockResolvedValue(operationsAt(start.getTime()));
  await openOverview();
  await act(async () => vi.advanceTimersByTimeAsync(15_001));
  expect(apiMocks.fetchOperationsSummary.mock.calls.length).toBeGreaterThan(1);
  expect(view.textContent).toContain('Awaiting fresh observations');
  expect(view.textContent).not.toContain('42.3 / 100');
  expect(view.textContent).toContain('115,320');
  const checksAtExpiry = apiMocks.fetchOperationsSummary.mock.calls.length;
  await act(async () => vi.advanceTimersByTimeAsync(10_000));
  expect(apiMocks.fetchOperationsSummary.mock.calls.length - checksAtExpiry).toBeLessThanOrEqual(1);
});

it('checks before a nearer expiry instead of waiting for the normal ten-second tick', async () => {
  apiMocks.fetchOperationsSummary.mockImplementation(async () => operationsAt(Date.now(), 25_000));
  await openOverview();
  const initialChecks = apiMocks.fetchOperationsSummary.mock.calls.length;
  await act(async () => vi.advanceTimersByTimeAsync(3_000));
  expect(apiMocks.fetchOperationsSummary).toHaveBeenCalledTimes(initialChecks + 1);
  await act(async () => vi.advanceTimersByTimeAsync(2_001));
  expect(view.textContent).toContain('42.3 / 100');
  expect(view.textContent).not.toContain('Awaiting fresh observations');
});

it('shows fresh local numeric estimates while an unrelated road request remains slow', async () => {
  let finishRoads!: (value: Fetched<LiveTrafficData>) => void;
  apiMocks.fetchLiveTraffic.mockReturnValue(new Promise(resolve => { finishRoads = resolve; }));
  await openOverview();
  expect(view.textContent).toContain('42.3 / 100');
  await act(async () => finishRoads(fetched({ zones: [] } as unknown as LiveTrafficData)));
});

it('rechecks summaries on foreground return and expires throttled background observations immediately', async () => {
  await openOverview();
  const original = operationsAt(start.getTime());
  let finishSummary!: (value: Fetched<OperationsSummary>) => void;
  apiMocks.fetchOperationsSummary.mockReturnValue(new Promise(resolve => { finishSummary = resolve; }));
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
  await act(async () => document.dispatchEvent(new Event('visibilitychange')));
  const checksBeforeReturn = apiMocks.fetchOperationsSummary.mock.calls.length;
  await act(async () => vi.advanceTimersByTimeAsync(10_000));
  expect(apiMocks.fetchOperationsSummary).toHaveBeenCalledTimes(checksBeforeReturn);
  // Suspended browser timers need not fire while a tab is in the background.
  vi.setSystemTime(new Date(start.getTime() + 20_000));
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
  await act(async () => document.dispatchEvent(new Event('visibilitychange')));
  await act(async () => window.dispatchEvent(new Event('focus')));
  expect(apiMocks.fetchOperationsSummary).toHaveBeenCalledTimes(checksBeforeReturn + 1);
  expect(view.textContent).toContain('Awaiting fresh observations');
  expect(view.textContent).not.toContain('42.3 / 100');
  const checksBeforeTimer = apiMocks.fetchOperationsSummary.mock.calls.length;
  await act(async () => vi.advanceTimersByTimeAsync(10_000));
  expect(apiMocks.fetchOperationsSummary).toHaveBeenCalledTimes(checksBeforeTimer);
  await act(async () => finishSummary(original));
  expect(view.textContent).toContain('Awaiting fresh observations');
  expect(view.textContent).not.toContain('42.3 / 100');
});

it('cancels automatic numeric checks when the app unmounts', async () => {
  await openOverview();
  const initialChecks = apiMocks.fetchOperationsSummary.mock.calls.length;
  await act(async () => root.render(null));
  await act(async () => vi.advanceTimersByTimeAsync(30_000));
  expect(apiMocks.fetchOperationsSummary).toHaveBeenCalledTimes(initialChecks);
});
