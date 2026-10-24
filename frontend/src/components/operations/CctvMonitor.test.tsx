/* @vitest-environment jsdom */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchCctvStatus, type CctvMonitorStatus } from '../../services/cctv';
import { CctvMonitor } from './CctvMonitor';
import { OperationsAnalytics } from './OperationsAnalytics';
import { INITIAL_CORRIDORS, MOCK_OPERATIONS } from '../../data/mockData';

vi.mock('../../services/cctv', () => ({ fetchCctvStatus: vi.fn() }));
const fetchStatus = vi.mocked(fetchCctvStatus);
let root: Root | undefined; let view: HTMLDivElement;
function status(): CctvMonitorStatus {
  return { status: 'awaiting_feeds', message: 'Automatic processing permission has not been verified.', updated_at: '2026-10-24T01:00:00Z',
    summary: { listed: 28, configured: 0, live: 0, estimated: 0, stale: 0, unavailable: 0, permission_pending: 28, calibration_pending: 0 },
    cameras: [{ camera_id: 'camera-1', name: 'Simpang Batam', road_name: null, source_url: 'https://matanya.batam.go.id/',
      status: 'permission_pending', reason: 'Awaiting verified processing permission.', observed_at: null, window_seconds: null,
      vehicle_count: null, moving_vehicles: null, stopped_vehicles: null, occupancy_ratio: null, motion_score: null,
      queue_length_m: null, congestion_score: null, severity: null, operational_eligible: false }] };
}
function live(): CctvMonitorStatus {
  const expected = status(); expected.status = 'active'; expected.message = 'Receiving observations.';
  expected.summary = { listed: 1, configured: 1, live: 1, estimated: 0, stale: 0, unavailable: 0, permission_pending: 0, calibration_pending: 0 };
  Object.assign(expected.cameras[0], { status: 'live', road_name: 'Jalan Gajah Mada', observed_at: expected.updated_at, window_seconds: 30,
    reason: 'Fresh observation; calibration evidence pending.', vehicle_count: 7, moving_vehicles: 4, stopped_vehicles: 3, occupancy_ratio: 0.25 });
  return expected;
}
async function render(token: string | null = 'operator-session') {
  view = document.createElement('div'); document.body.append(view); root = createRoot(view);
  await act(async () => root!.render(<CctvMonitor accessToken={token} />)); return view;
}
beforeEach(() => { vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(new Date('2026-10-24T01:00:00Z')); vi.resetAllMocks(); fetchStatus.mockResolvedValue(status()); });
afterEach(() => { act(() => root?.unmount()); root = undefined; document.body.replaceChildren(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('automatic CCTV observations', () => {
  it('keeps camera processing in the backend without CCTV or upload controls in the operations overview', () => {
    const content = renderToStaticMarkup(<OperationsAnalytics operations={MOCK_OPERATIONS} operationsSnapshot={null} corridors={INITIAL_CORRIDORS} />);
    const page = document.createElement('div'); page.innerHTML = content;
    expect(page.querySelector('[aria-label="CCTV observations"]')).toBeNull();
    expect(page.textContent).not.toMatch(/CCTV|surveillance|camera feed/i);
    expect(page.querySelector('input[type="file"]')).toBeNull(); expect(page.textContent).not.toContain('Vehicle counting preview');
  });
  it('shows explicit camera coverage with no image upload or stream URL entry', async () => {
    await render(); expect(fetchStatus).toHaveBeenCalledWith('operator-session', expect.any(AbortSignal));
    expect(view.querySelector('section')?.getAttribute('aria-label')).toBe('CCTV observations');
    expect(view.textContent).toContain('28 listed cameras'); expect(view.textContent).toContain('Permission pending');
    expect(view.textContent).toContain('Awaiting authorized stream and calibrated road view. Congestion is unavailable.');
    expect(view.querySelectorAll('input')).toHaveLength(0); expect(view.querySelector('video')).toBeNull(); expect(view.querySelector('img')).toBeNull();
    expect(view.textContent).not.toContain('0 vehicles'); expect(view.textContent).not.toContain('0 / 100');
  });
  it('keeps never-observed measurements absent and links to the official source', async () => {
    await render(); expect(view.querySelector('dl.cctv-camera__measurements')).toBeNull();
    expect(view.textContent).toContain('Road view not calibrated'); expect(view.textContent).toContain('Awaiting verified processing permission');
    expect(view.querySelector('a')?.getAttribute('href')).toBe('https://matanya.batam.go.id/');
    expect(view.querySelector('a')?.getAttribute('rel')).toBe('noreferrer'); expect(view.querySelector('time')?.getAttribute('datetime')).toBe(status().updated_at);
  });
  it('shows actual window counts and occupancy without unmeasured speed, queue or congestion', async () => {
    fetchStatus.mockResolvedValue(live()); await render();
    expect(view.textContent).toContain('Jalan Gajah Mada'); expect(view.textContent).toContain('30-second window');
    const measurements = view.querySelector('.cctv-camera__measurements')!;
    expect(measurements.textContent).toContain('Vehicles in view7'); expect(measurements.textContent).toContain('Moving vehicles4');
    expect(measurements.textContent).toContain('Stopped vehicles3'); expect(measurements.textContent).toContain('Road occupancy25%');
    expect(measurements.textContent).not.toContain('km/h'); expect(measurements.textContent).not.toContain('queue');
    expect(measurements.textContent).not.toContain('/ 100'); expect(view.textContent).toContain('Awaiting calibrated and validated congestion thresholds');
  });
  it('presents genuine measured zero while keeping null measurements unknown', async () => {
    const expected = live(); expected.cameras[0].vehicle_count = 0; expected.cameras[0].moving_vehicles = null;
    fetchStatus.mockResolvedValue(expected); await render();
    expect(view.querySelector('.cctv-camera__measurements')?.textContent).toContain('Vehicles in view0');
    expect(view.querySelector('.cctv-camera__measurements')?.textContent).toContain('Moving vehiclesNot measured');
  });
  it('shows calibrated congestion and physical queue only when those observations are provided', async () => {
    const expected = live(); Object.assign(expected.cameras[0], { operational_eligible: true, severity: 'moderate', congestion_score: null, queue_length_m: 24.5 });
    fetchStatus.mockResolvedValue(expected); await render();
    expect(view.textContent).toContain('Camera congestionmoderate'); expect(view.textContent).toContain('Calibrated queue length24.5 m');
    expect(view.textContent).not.toContain('/ 100');
  });
  it('withholds a provisional numeric congestion score from operational display', async () => {
    const expected = live(); Object.assign(expected.cameras[0], { congestion_score: 77, severity: 'high' });
    fetchStatus.mockResolvedValue(expected); await render();
    expect(view.querySelector('.cctv-camera__measurements')?.textContent).not.toContain('77');
    expect(view.textContent).not.toContain('Camera congestion');
  });
  it('shows live YOLO26 estimates with their provisional status and mapped routing scope', async () => {
    const expected = live(); expected.summary.live = 0; expected.summary.estimated = 1;
    Object.assign(expected.cameras[0], { status: 'estimated', congestion_score: 77, severity: 'high',
      validation_status: 'estimated', timestamp_basis: 'stream_observation', detector_id: 'yolo26-fixture', routing_enabled: true });
    fetchStatus.mockResolvedValue(expected); await render();
    expect(view.textContent).toContain('Live estimate · Unvalidated');
    expect(view.textContent).toContain('Estimated congestionhigh · 77%');
    expect(view.textContent).toContain('Fresh estimates influence the mapped road edge in Depart now routes');
    expect(view.textContent).toContain('source capture time is unverified');
    expect(view.textContent).not.toContain('Congestion is unavailable');
    expect(view.textContent).not.toContain('km/h');
    expect(view.querySelector('.badge-smooth')).toBeNull();
  });
  it.each([5, undefined])('expires observations using the camera freshness limit before a later poll completes (%s seconds)', async maximumAge => {
    vi.useFakeTimers();
    const expected = live(); expected.cameras[0].maximum_age_seconds = maximumAge;
    fetchStatus.mockResolvedValueOnce(expected).mockReturnValue(new Promise(() => {}));
    await render(); expect(view.querySelector('.cctv-camera__measurements')).not.toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync((maximumAge ?? 15) * 1000 + 1));
    expect(view.querySelector('.cctv-camera__measurements')).toBeNull();
    expect(view.textContent).toContain('Stale observation');
    expect(view.textContent).toContain('The camera observation has expired');
    expect(view.textContent).toContain('expired measurements are withheld');
    expect(view.textContent).not.toContain('Vehicles in view0');
  });
  it('checks configured camera aggregates every ten seconds', async () => {
    vi.useFakeTimers(); fetchStatus.mockResolvedValue(live());
    await render(); await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(fetchStatus).toHaveBeenCalledTimes(2);
  });
  it.each(['stale', 'unavailable', 'permission_pending', 'calibration_pending', 'disabled'] as const)('does not show old traffic counts as current for %s', async state => {
    const expected = live(); expected.cameras[0].status = state; expected.cameras[0].operational_eligible = false;
    fetchStatus.mockResolvedValue(expected); await render();
    expect(view.querySelector('.cctv-camera__measurements')).toBeNull(); expect(view.textContent).toContain('Last observation');
  });
  it('keeps source errors distinct from an empty road and allows a manual retry', async () => {
    fetchStatus.mockRejectedValueOnce(new Error('CCTV source check failed.')); await render();
    expect(view.querySelector('[role="alert"]')?.textContent).toContain('source check failed');
    expect(view.querySelector('.cctv-monitor__coverage')).toBeNull(); expect(view.textContent).not.toContain('0 vehicles');
    await act(async () => view.querySelector('button')!.click());
    expect(fetchStatus).toHaveBeenCalledTimes(2); expect(view.querySelector('[role="alert"]')).toBeNull();
  });
  it('withholds previous measurements after a later refresh fails', async () => {
    fetchStatus.mockResolvedValueOnce(live()).mockRejectedValueOnce(new Error('Latest check unavailable.')); await render();
    expect(view.querySelector('.cctv-camera__measurements')).not.toBeNull();
    await act(async () => view.querySelector('button')!.click());
    expect(view.querySelector('.cctv-camera__measurements')).toBeNull(); expect(view.querySelector('.cctv-monitor__coverage')).toBeNull();
    expect(view.textContent).toContain('Previous observations are withheld');
  });
  it('deduplicates timer and focus checks while a request is pending', async () => {
    vi.useFakeTimers(); fetchStatus.mockReturnValue(new Promise(() => {})); await render();
    expect(view.querySelector('button')!.disabled).toBe(true);
    await act(async () => { window.dispatchEvent(new Event('focus')); await vi.advanceTimersByTimeAsync(90_000); });
    expect(fetchStatus).toHaveBeenCalledTimes(1);
  });
  it('polls visible sources every thirty seconds and backs off failed requests', async () => {
    vi.useFakeTimers(); fetchStatus.mockResolvedValueOnce(status()).mockRejectedValueOnce(new Error('Offline')).mockResolvedValue(status());
    await render(); await act(async () => vi.advanceTimersByTimeAsync(30_000)); expect(fetchStatus).toHaveBeenCalledTimes(2);
    await act(async () => vi.advanceTimersByTimeAsync(30_000)); expect(fetchStatus).toHaveBeenCalledTimes(2);
    await act(async () => vi.advanceTimersByTimeAsync(30_000)); expect(fetchStatus).toHaveBeenCalledTimes(3);
  });
  it('does not poll a hidden document', async () => {
    vi.useFakeTimers(); await render();
    const original = Object.getOwnPropertyDescriptor(document, 'visibilityState');
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
    try { await act(async () => vi.advanceTimersByTimeAsync(90_000)); expect(fetchStatus).toHaveBeenCalledTimes(1); }
    finally { if (original) Object.defineProperty(document, 'visibilityState', original); else delete (document as unknown as Record<string, unknown>).visibilityState; }
  });
  it('aborts pending requests and removes polling on unmount', async () => {
    vi.useFakeTimers(); fetchStatus.mockReturnValue(new Promise(() => {})); await render();
    const signal = fetchStatus.mock.calls[0][1]!;
    await act(async () => root!.unmount()); root = undefined;
    expect(signal.aborted).toBe(true); await act(async () => vi.advanceTimersByTimeAsync(120_000)); expect(fetchStatus).toHaveBeenCalledTimes(1);
  });
  it('drops private camera data and ignores late results after a session change', async () => {
    let finish!: (value: CctvMonitorStatus) => void;
    fetchStatus.mockReturnValueOnce(new Promise(resolve => { finish = resolve; })); await render('old-session');
    const signal = fetchStatus.mock.calls[0][1]!;
    await act(async () => root!.render(<CctvMonitor accessToken="new-session" />));
    expect(signal.aborted).toBe(true); expect(fetchStatus).toHaveBeenLastCalledWith('new-session', expect.any(AbortSignal));
    const old = live(); old.cameras[0].name = 'Old private camera';
    await act(async () => finish(old)); expect(view.textContent).not.toContain('Old private camera'); expect(view.textContent).toContain('28 listed cameras');
  });
});
