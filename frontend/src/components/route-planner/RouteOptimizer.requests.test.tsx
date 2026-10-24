/* @vitest-environment jsdom */

import { act } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { ROUTE_LOCATIONS } from '../../data/mockData';
import { ApiRequestError, requestFreeRouteOptimization, requestPersistedRoute } from '../../services/api';
import type { Fetched, RouteOptimizationResult } from '../../types';
import { RouteOptimizer } from './RouteOptimizer';

vi.mock('../../services/api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../services/api')>(),
  requestFreeRouteOptimization: vi.fn(), requestPersistedRoute: vi.fn(),
}));
vi.mock('./RoutePreviewMap', () => ({ RoutePreviewMap: () => null }));
vi.mock('./RouteRunHistoryPanel', () => ({
  RouteRunHistoryPanel: ({ onSelectRun }: { onSelectRun: (code: string) => void }) => (
    <div>
      <button onClick={() => onSelectRun('4KF9X2P')}>Open first history run</button>
      <button onClick={() => onSelectRun('7KQ4MNP')}>Open second history run</button>
    </div>
  ),
}));

let root: Root | undefined;
const setResult = vi.fn();
const setSource = vi.fn();

function render(driverAccess = false, selectedHour = 14) {
  const container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
  act(() => root!.render(<RouteOptimizer
    locations={ROUTE_LOCATIONS} originId="mukakuning" destinationId="batam_centre"
    setOriginId={vi.fn()} setDestinationId={vi.fn()} result={null}
    setResult={setResult} resultSource="offline" setResultSource={setSource}
    vehicleType="COMMUTER" setVehicleType={vi.fn()} weather={1} setWeather={vi.fn()}
    hour={selectedHour} setHour={vi.fn()} accessToken="signed-in-token"
    driverAccess={driverAccess}
  />));
  return container;
}

function response(code: string): Fetched<RouteOptimizationResult> {
  return { source: 'simulated', fetchedAt: '2026-10-23T05:00:00Z', data: {
    route_code: code, corridor: { id: code, name: 'Saved journey', distance_km: 5, base_time_mins: 10 },
    vehicle_type: 'COMMUTER', estimated_travel_time_mins: 10, customs_buffer_mins: 0,
    total_eta_mins: 10, co2_emissions_kg: 1, co2_saved_kg: 0,
    congestion_prediction: { current_score: 20, predicted_30min: 20, predicted_60min: 20,
      estimated_delay_mins: 0, risk_level: 'LOW', trend: 'STABLE' },
    optimal_departure: { recommended: 'DEPART_NOW', reason: 'Saved plan', time_saved_mins: 0 },
    next_matching_ferries: [],
  } };
}

function clickByText(container: HTMLElement, text: string) {
  act(() => Array.from(container.querySelectorAll('button')).find(button => button.textContent?.trim() === text)!.click());
}

function changeDriverCode(container: HTMLElement, value: string) {
  const input = container.querySelector<HTMLInputElement>('#driver-route-code')!;
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  return input;
}

async function submit(container: HTMLElement) {
  await act(async () => container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
}

afterEach(() => {
  if (root) act(() => root!.unmount());
  root = undefined;
  document.body.replaceChildren();
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe('planner request lifecycle', () => {
  it('normalizes a padded lowercase code before applying the controlled length limit', async () => {
    vi.mocked(requestPersistedRoute).mockResolvedValueOnce(response('4KF9X2P'));
    const container = render(true);
    const input = changeDriverCode(container, ' 4kf9x2p ');
    // Native maxlength would truncate a real paste before onChange can trim it.
    expect(input.hasAttribute('maxlength')).toBe(false);
    expect(input.value).toBe('4KF9X2P');
    await submit(container);
    expect(requestPersistedRoute).toHaveBeenCalledExactlyOnceWith('4KF9X2P', 'signed-in-token');
    expect(setResult).toHaveBeenLastCalledWith(response('4KF9X2P').data);
  });

  it.each(['success', 'failure'])('ignores an old driver-code %s after editing and loading a newer assignment', async (outcome) => {
    let finish!: (value: Fetched<RouteOptimizationResult>) => void;
    let fail!: (error: Error) => void;
    vi.mocked(requestPersistedRoute)
      .mockImplementationOnce(() => new Promise((resolve, reject) => { finish = resolve; fail = reject; }))
      .mockResolvedValueOnce(response('7KQ4MNP'));
    const container = render(true);
    changeDriverCode(container, '4KF9X2P');
    await submit(container);
    setResult.mockClear();
    changeDriverCode(container, '7KQ4MNP');
    expect(setResult).toHaveBeenCalledExactlyOnceWith(null);
    expect(container.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(false);
    expect(container.textContent).not.toContain('Loading your assigned route');
    await submit(container);
    setResult.mockClear();
    await act(async () => outcome === 'success'
      ? finish(response('4KF9X2P')) : fail(new ApiRequestError('Old code not found', 404)));
    expect(setResult).not.toHaveBeenCalled();
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(requestPersistedRoute).toHaveBeenLastCalledWith('7KQ4MNP', 'signed-in-token');
  });

  it('clears the loaded assignment when its code is edited and blocks incomplete replacement codes', async () => {
    vi.mocked(requestPersistedRoute).mockResolvedValueOnce(response('4KF9X2P'));
    const container = render(true);
    changeDriverCode(container, '4KF9X2P');
    await submit(container);
    setResult.mockClear();
    changeDriverCode(container, '7KQ');
    expect(setResult).toHaveBeenCalledExactlyOnceWith(null);
    expect(container.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled).toBe(true);
    await submit(container);
    expect(requestPersistedRoute).toHaveBeenCalledOnce();
  });

  it('retries the assigned code after a driver lookup fails instead of planning another journey', async () => {
    let rejectFirst!: (error: Error) => void;
    vi.mocked(requestPersistedRoute).mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectFirst = reject; }));
    const container = render(true);
    const input = container.querySelector<HTMLInputElement>('#driver-route-code')!;
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '4KF9X2P');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await act(async () => container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
    expect(container.textContent).toContain('Loading your assigned route');
    expect(container.textContent).not.toContain('Matching local roads');
    await act(async () => rejectFirst(new ApiRequestError('No assigned route matches that code.', 404)));
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('No assigned route matches that code.');
    vi.mocked(requestPersistedRoute).mockResolvedValueOnce(response('4KF9X2P'));
    await act(async () => Array.from(container.querySelectorAll('button'))
      .find(button => button.textContent?.trim() === 'Try again')!.click());
    expect(requestPersistedRoute).toHaveBeenCalledTimes(2);
    expect(requestPersistedRoute).toHaveBeenLastCalledWith('4KF9X2P', 'signed-in-token');
    expect(requestFreeRouteOptimization).not.toHaveBeenCalled();
    expect(setResult).toHaveBeenLastCalledWith(response('4KF9X2P').data);
  });

  it('sends the signed-in session with the actual free-route form submission', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-10-24T15:23:45Z'));
    vi.mocked(requestFreeRouteOptimization).mockResolvedValue(response('4KF9X2P'));
    const container = render();
    await act(async () => container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
    expect(requestFreeRouteOptimization).toHaveBeenCalledWith(
      expect.objectContaining({ display_name: 'Batamindo Industrial Park' }),
      expect.objectContaining({ display_name: 'Batam Centre Ferry Terminal' }),
      'COMMUTER', 14, 1, 'BALANCED',
      expect.objectContaining({ departure_at: expect.stringMatching(/Z$/) }), 'signed-in-token',
    );
    expect(setResult).toHaveBeenCalledWith(expect.objectContaining({ route_code: '4KF9X2P' }));
  });

  it('keeps the original two time controls while the current selected minute departs immediately', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-10-24T15:23:45Z'));
    vi.mocked(requestFreeRouteOptimization).mockResolvedValue(response('4KF9X2P'));
    const localHour = new Date().getHours();
    const container = render(false, localHour);
    const radios = Array.from(container.querySelectorAll<HTMLElement>('[role="radio"]'));
    expect(radios).toHaveLength(2);
    expect(radios.find(item => item.textContent?.includes('Departure time'))?.getAttribute('aria-checked')).toBe('true');
    expect(container.textContent).not.toMatch(/CCTV|camera|surveillance/i);
    await submit(container);
    const departureCalls = vi.mocked(requestFreeRouteOptimization).mock.calls;
    const departure = departureCalls[departureCalls.length - 1];
    expect(departure[3]).toBe(localHour);
    expect(departure[6]).toHaveProperty('departure_at');
    expect(departure[6]?.departure_at).toBe('2026-10-24T15:23:45.000Z');
    act(() => radios.find(item => item.textContent?.includes('Arrival time'))!.click());
    await submit(container);
    const arrivalCalls = vi.mocked(requestFreeRouteOptimization).mock.calls;
    const arrival = arrivalCalls[arrivalCalls.length - 1];
    expect(arrival[6]).toHaveProperty('arrive_by');
    expect(arrival[6]).not.toHaveProperty('departure_at');
  });

  it('keeps the latest selected history run when earlier requests finish later', async () => {
    let resolveFirst: (value: Fetched<RouteOptimizationResult>) => void = () => undefined;
    let resolveSecond: (value: Fetched<RouteOptimizationResult>) => void = () => undefined;
    vi.mocked(requestPersistedRoute)
      .mockImplementationOnce(() => new Promise(resolve => { resolveFirst = resolve; }))
      .mockImplementationOnce(() => new Promise(resolve => { resolveSecond = resolve; }));
    const container = render();
    clickByText(container, 'Saved');
    setResult.mockClear();
    clickByText(container, 'Open first history run');
    clickByText(container, 'Open second history run');
    await act(async () => resolveSecond(response('7KQ4MNP')));
    await act(async () => resolveFirst(response('4KF9X2P')));
    expect(setResult).toHaveBeenCalledExactlyOnceWith(response('7KQ4MNP').data);
  });

  it('ignores a pending history response after leaving the planner', async () => {
    let resolve: (value: Fetched<RouteOptimizationResult>) => void = () => undefined;
    vi.mocked(requestPersistedRoute).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    const container = render();
    clickByText(container, 'Saved');
    clickByText(container, 'Open first history run');
    act(() => root!.unmount());
    root = undefined;
    setResult.mockClear();
    await act(async () => resolve(response('4KF9X2P')));
    expect(setResult).not.toHaveBeenCalled();
  });
});
