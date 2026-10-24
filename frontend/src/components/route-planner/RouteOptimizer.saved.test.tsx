/* @vitest-environment jsdom */

import { act, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ROUTE_LOCATIONS } from '../../data/mockData';
import {
  ApiRequestError, fetchRouteRunHistory, requestFreeRouteOptimization,
  requestPersistedRoute,
} from '../../services/api';
import type { DataSource, Fetched, FreeLocation, RouteOptimizationResult, RouteRunHistory, RouteStopSnapshot } from '../../types';
import { RouteOptimizer } from './RouteOptimizer';

vi.mock('../../services/api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../services/api')>(),
  fetchRouteRunHistory: vi.fn(), requestPersistedRoute: vi.fn(),
  requestFreeRouteOptimization: vi.fn(), googleRouteBenchmarkEnabled: () => false,
}));
vi.mock('./RoutePreviewMap', () => ({
  RoutePreviewMap: ({ stops }: { stops?: RouteStopSnapshot[] }) => (
    <div data-testid="saved-journey-map" data-stops={JSON.stringify(stops)} />
  ),
}));
vi.mock('./LocationSearch', () => ({
  LocationSearch: ({ id, label, value, onChange }: {
    id: string; label: string; value: FreeLocation | null;
    onChange: (location: FreeLocation) => void;
  }) => <label htmlFor={id}>{label}<input id={id} value={value?.display_name ?? ''}
    onChange={event => onChange({ lat: 1.15, lng: 104.04, display_name: event.target.value })} /></label>,
}));

const history: RouteRunHistory = {
  scope: 'own_account',
  runs: [{ route_id: '4KF9X2P', route_code: '4KF9X2P', origin_name: 'Nagoya', destination_name: 'Batam Centre',
    route_kind: 'optimize-free-route', vehicle_type: 'COMMUTER', created_by: 'user',
    created_at: '2026-10-24T02:00:00Z', updated_at: '2026-10-24T02:00:00Z' }],
};
const saved: Fetched<RouteOptimizationResult> = {
  source: 'simulated', fetchedAt: '2026-10-24T02:00:00Z',
  data: {
    route_code: '4KF9X2P', corridor: { id: 'free:saved', name: 'Saved journey', distance_km: 5, base_time_mins: 10 },
    requested_origin: { lat: 1.15, lng: 104.01, display_name: 'Loaded origin' },
    requested_destination: { lat: 1.13, lng: 104.05, display_name: 'Loaded destination' },
    vehicle_type: 'COMMUTER', estimated_travel_time_mins: 10, customs_buffer_mins: 0,
    total_eta_mins: 10, co2_emissions_kg: 1, co2_saved_kg: 0,
    congestion_prediction: { current_score: 20, predicted_30min: 20, predicted_60min: 20,
      estimated_delay_mins: 0, risk_level: 'LOW', trend: 'STABLE' },
    optimal_departure: { recommended: 'DEPART_NOW', reason: 'Saved plan', time_saved_mins: 0 },
    next_matching_ferries: [],
  },
};

let root: Root | undefined;
const changes = vi.fn();

function Planner({ accessToken }: { accessToken: string | null }) {
  const [result, setResult] = useState<RouteOptimizationResult | null>(null);
  const [source, setSource] = useState<DataSource>('offline');
  return <RouteOptimizer locations={ROUTE_LOCATIONS} originId="mukakuning" destinationId="batam_centre"
    setOriginId={vi.fn()} setDestinationId={vi.fn()} result={result}
    setResult={value => { changes(value); setResult(value); }} resultSource={source} setResultSource={setSource}
    vehicleType="COMMUTER" setVehicleType={vi.fn()} weather={0} setWeather={vi.fn()}
    hour={14} setHour={vi.fn()} accessToken={accessToken} />;
}

async function render(accessToken: string | null = 'signed-in-token') {
  const container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root!.render(<Planner accessToken={accessToken} />));
  return container;
}

async function click(container: HTMLElement, text: string) {
  const button = [...container.querySelectorAll('button')].find(item => item.textContent?.trim() === text);
  expect(button).toBeDefined();
  await act(async () => button!.click());
}

async function selectHistoryRun(container: HTMLElement) {
  const item = container.querySelector<HTMLButtonElement>('.route-run-history__item');
  expect(item).not.toBeNull();
  await act(async () => item!.click());
}

beforeEach(() => {
  vi.mocked(fetchRouteRunHistory).mockResolvedValue(history);
  vi.mocked(requestPersistedRoute).mockResolvedValue(saved);
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  root = undefined;
  document.body.replaceChildren();
  vi.resetAllMocks();
});

describe('saved route sidebar', () => {
  it('forwards all saved resolved stops to the journey map without replacing them with editable endpoints', async () => {
    const stops: RouteStopSnapshot[] = [
      { sequence: 1, name: 'Saved Batam Centre', lat: 1.1318, lng: 104.0554, role: 'ORIGIN' },
      { sequence: 2, name: 'Saved Sekupang', lat: 1.125, lng: 103.928, role: 'WAYPOINT' },
      { sequence: 3, name: 'Saved Batu Besar', lat: 1.152, lng: 104.135, role: 'WAYPOINT' },
      { sequence: 4, name: 'Saved Batamindo', lat: 1.06, lng: 104.0303, role: 'DESTINATION' },
    ];
    vi.mocked(requestPersistedRoute).mockResolvedValueOnce({ ...saved, data: {
      ...saved.data, stops, route_geometry: stops.map(stop => [stop.lat, stop.lng]),
    } });
    const container = await render();
    await click(container, 'Saved');
    await selectHistoryRun(container);
    await click(container, 'Journey map');
    expect(JSON.parse(container.querySelector('[data-testid="saved-journey-map"]')!.getAttribute('data-stops')!))
      .toEqual(stops);
    expect(requestFreeRouteOptimization).not.toHaveBeenCalled();
  });

  it('replaces planning controls with real history, opens a run and preserves its result tab across view switches', async () => {
    const container = await render();
    expect(container.querySelector('form[aria-label="Route planning controls"]')).not.toBeNull();
    await click(container, 'Saved');
    expect(container.querySelector('form')).toBeNull();
    expect(container.querySelector('.route-planner-layout--saved > .route-run-history')).not.toBeNull();
    expect(container.textContent).not.toContain('Plan Journey');
    expect(fetchRouteRunHistory).toHaveBeenCalledWith('signed-in-token', { limit: 20 });
    await selectHistoryRun(container);
    expect(requestPersistedRoute).toHaveBeenCalledExactlyOnceWith('4KF9X2P', 'signed-in-token');
    expect(container.querySelector('.route-result-title')?.textContent).toContain('Loaded origin');
    await click(container, 'Directions');
    await click(container, 'Search');
    expect(container.querySelector('form[aria-label="Route planning controls"]')).not.toBeNull();
    expect(container.querySelector<HTMLInputElement>('#route-origin')?.value).toBe('Batamindo Industrial Park');
    expect(container.querySelector('.route-result-title')?.textContent).toContain('Loaded origin');
    expect(container.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toBe('Directions');
    await click(container, 'Saved');
    expect(container.querySelector('form')).toBeNull();
    expect(container.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toBe('Directions');
    expect(changes).toHaveBeenCalledExactlyOnceWith(saved.data);
    expect(requestFreeRouteOptimization).not.toHaveBeenCalled();
  });

  it('requires sign-in for history and restores search controls without requesting private data', async () => {
    const container = await render(null);
    await click(container, 'Saved');
    expect(container.querySelector('form')).toBeNull();
    expect(container.querySelector('[aria-label="Route solver history"]')?.textContent).toContain('Sign in to view recent runs.');
    expect(fetchRouteRunHistory).not.toHaveBeenCalled();
    await click(container, 'Search');
    expect(container.querySelector('form[aria-label="Route planning controls"]')).not.toBeNull();
    expect(container.querySelector('[aria-label="Route solver history"]')).toBeNull();
    expect(changes).not.toHaveBeenCalled();
  });

  it('retries the selected history code after a lookup failure instead of calculating a new route', async () => {
    vi.mocked(requestPersistedRoute).mockRejectedValueOnce(new ApiRequestError('Saved route temporarily unavailable.', 503));
    const container = await render();
    await click(container, 'Saved');
    await selectHistoryRun(container);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Saved route temporarily unavailable.');
    await click(container, 'Try again');
    expect(requestPersistedRoute).toHaveBeenCalledTimes(2);
    expect(requestPersistedRoute).toHaveBeenLastCalledWith('4KF9X2P', 'signed-in-token');
    expect(container.querySelector('.route-result-title')?.textContent).toContain('Loaded origin');
    expect(requestFreeRouteOptimization).not.toHaveBeenCalled();
  });

  it('allows a pending history lookup to complete after switching views but clears it on an endpoint edit', async () => {
    let finish!: (value: Fetched<RouteOptimizationResult>) => void;
    vi.mocked(requestPersistedRoute).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const container = await render();
    await click(container, 'Saved');
    await selectHistoryRun(container);
    await click(container, 'Search');
    await act(async () => finish(saved));
    expect(container.querySelector('.route-result-title')?.textContent).toContain('Loaded origin');
    const origin = container.querySelector<HTMLInputElement>('#route-origin')!;
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(origin, 'Another origin');
      origin.dispatchEvent(new Event('input', { bubbles: true }));
    });
    expect(changes).toHaveBeenLastCalledWith(null);
    expect(container.querySelector('.route-result-title')).toBeNull();
    expect(container.querySelector('.route-empty-state-heading')).not.toBeNull();
  });
});
