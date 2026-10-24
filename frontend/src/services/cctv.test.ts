import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchCctvStatus, isCctvMonitorStatus, type CctvCamera, type CctvMonitorStatus } from './cctv';

function camera(): CctvCamera {
  return { camera_id: 'matanya-1', name: 'Simpang Batam', road_name: null, source_url: 'https://matanya.batam.go.id/',
    status: 'permission_pending', reason: 'Automated processing permission has not been verified.', observed_at: null, window_seconds: null,
    vehicle_count: null, moving_vehicles: null, stopped_vehicles: null, occupancy_ratio: null, motion_score: null,
    queue_length_m: null, congestion_score: null, severity: null, operational_eligible: false };
}
function status(): CctvMonitorStatus {
  return { status: 'awaiting_feeds', message: 'Awaiting authorized stream and calibrated road view.', updated_at: '2026-10-24T01:00:00Z',
    summary: { listed: 1, configured: 0, live: 0, estimated: 0, stale: 0, unavailable: 0, permission_pending: 1, calibration_pending: 0 }, cameras: [camera()] };
}
function respond(monitor: unknown, code = 200, source = 'cctv_traffic_observations') {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ camera_monitor: monitor, data_source: source }), {
    status: code, headers: { 'Content-Type': 'application/json' },
  }));
  vi.stubGlobal('fetch', fetcher); return fetcher;
}
afterEach(() => vi.unstubAllGlobals());

describe('CCTV status API boundary', () => {
  it('uses an operator GET with no cache or camera/image payload', async () => {
    const expected = status(); const fetcher = respond(expected);
    expect(await fetchCctvStatus('operator-session')).toEqual(expected);
    const [url, init] = fetcher.mock.calls[0];
    expect(url).toContain('/api/analytics/cctv-status');
    expect(init).toMatchObject({ method: 'GET', headers: { Authorization: 'Bearer operator-session' }, cache: 'no-store' });
    expect(init.body).toBeUndefined(); expect(init.signal).toBeInstanceOf(AbortSignal);
  });
  it('omits bearer credentials when the operator session is absent', async () => {
    const fetcher = respond(status()); await fetchCctvStatus(null);
    expect(fetcher.mock.calls[0][1].headers).toEqual({});
  });
  it('preserves an actually observed zero separately from missing observations', async () => {
    const expected = status(); expected.status = 'active';
    Object.assign(expected.cameras[0], { status: 'live', road_name: 'Jalan Gajah Mada', observed_at: expected.updated_at,
      window_seconds: 30, vehicle_count: 0, moving_vehicles: 0, stopped_vehicles: 0, occupancy_ratio: 0 });
    respond(expected); expect((await fetchCctvStatus('operator')).cameras[0].vehicle_count).toBe(0);
    respond(status()); expect((await fetchCctvStatus('operator')).cameras[0].vehicle_count).toBeNull();
  });
  it.each(['permission_pending', 'calibration_pending', 'disabled', 'live', 'stale', 'unavailable'] as const)('accepts explicit camera status %s', state => {
    const expected = status(); expected.cameras[0].status = state; expect(isCctvMonitorStatus(expected)).toBe(true);
  });
  it.each([401, 403])('presents HTTP %s as an operator access error without server details', async code => {
    respond(status(), code); await expect(fetchCctvStatus(null)).rejects.toThrow('operator account');
  });
  it.each([429, 500, 503])('preserves HTTP %s as unavailable without an empty count', async code => {
    respond(status(), code); await expect(fetchCctvStatus('operator')).rejects.toThrow('No measurements were substituted');
  });
  it('does not replace network errors or incomplete JSON with a fallback', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Network offline')));
    await expect(fetchCctvStatus('operator')).rejects.toThrow('Network offline');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('incomplete', { status: 200 })));
    await expect(fetchCctvStatus('operator')).rejects.toThrow('incomplete response');
  });
  it('propagates cancellation without exposing a camera URL', async () => {
    const controller = new AbortController(); let requestSignal: AbortSignal | null | undefined;
    vi.stubGlobal('fetch', vi.fn((_url, init: RequestInit) => new Promise((_resolve, reject) => {
      requestSignal = init.signal;
      requestSignal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    })));
    const pending = fetchCctvStatus('operator', controller.signal); controller.abort();
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' }); expect(requestSignal?.aborted).toBe(true);
  });
  it.each([
    ['empty monitor', {}],
    ['missing camera', { ...status(), cameras: [null] }],
    ['unknown monitor state', { ...status(), status: 'mock' }],
    ['invalid check time', { ...status(), updated_at: 'yesterday' }],
    ['negative coverage', { ...status(), summary: { ...status().summary, live: -1 } }],
    ['missing coverage', { ...status(), summary: {} }],
    ['duplicate camera', { ...status(), cameras: [camera(), camera()] }],
    ['unknown camera state', { ...status(), cameras: [{ ...camera(), status: 'unknown' }] }],
    ['unsafe source link', { ...status(), cameras: [{ ...camera(), source_url: 'javascript:alert(1)' }] }],
    ['credentials in source link', { ...status(), cameras: [{ ...camera(), source_url: 'https://user:password@example.com/' }] }],
    ['invalid observation time', { ...status(), cameras: [{ ...camera(), observed_at: 'tomorrow' }] }],
    ['zero window', { ...status(), cameras: [{ ...camera(), window_seconds: 0 }] }],
    ['negative vehicles', { ...status(), cameras: [{ ...camera(), vehicle_count: -1 }] }],
    ['fractional vehicles', { ...status(), cameras: [{ ...camera(), vehicle_count: 1.5 }] }],
    ['invalid occupancy', { ...status(), cameras: [{ ...camera(), occupancy_ratio: 1.1 }] }],
    ['invalid motion', { ...status(), cameras: [{ ...camera(), motion_score: -1 }] }],
    ['negative queue', { ...status(), cameras: [{ ...camera(), queue_length_m: -1 }] }],
    ['invalid congestion', { ...status(), cameras: [{ ...camera(), congestion_score: 101 }] }],
    ['operational claim without a live window', { ...status(), cameras: [{ ...camera(), operational_eligible: true, severity: 'low' }] }],
  ])('rejects %s without inventing congestion', async (_label, payload) => {
    respond(payload); await expect(fetchCctvStatus('operator')).rejects.toThrow('could not be verified');
  });
  it('rejects nonfinite values and unrelated data-source envelopes', async () => {
    expect(isCctvMonitorStatus({ ...status(), cameras: [{ ...camera(), vehicle_count: NaN }] })).toBe(false);
    expect(isCctvMonitorStatus({ ...status(), cameras: [{ ...camera(), occupancy_ratio: Infinity }] })).toBe(false);
    respond(status(), 200, 'generated_demo'); await expect(fetchCctvStatus('operator')).rejects.toThrow('could not be verified');
  });
  it('accepts calibrated severity without inventing a numeric index', () => {
    const expected = status(); Object.assign(expected.cameras[0], { status: 'live', observed_at: expected.updated_at,
      window_seconds: 30, operational_eligible: true, severity: 'moderate', congestion_score: null });
    expect(isCctvMonitorStatus(expected)).toBe(true);
  });
  it('requires estimated status and explicit model/clock provenance for provisional percentages', () => {
    const expected = status(); expected.summary.estimated = 1;
    Object.assign(expected.cameras[0], { status: 'estimated', observed_at: expected.updated_at,
      window_seconds: 30, congestion_score: 77, severity: 'high', validation_status: 'estimated',
      detector_id: 'yolo26-sha256-fixture', timestamp_basis: 'stream_observation', routing_enabled: true });
    expect(isCctvMonitorStatus(expected)).toBe(true);
    expected.cameras[0].operational_eligible = true;
    expect(isCctvMonitorStatus(expected)).toBe(false);
    expected.cameras[0].operational_eligible = false; expected.cameras[0].timestamp_basis = null;
    expect(isCctvMonitorStatus(expected)).toBe(false);
  });
  it.each([0, -1, 121, true, NaN])('rejects an invalid camera freshness limit: %s', maximum_age_seconds => {
    const expected = status(); Object.assign(expected.cameras[0], { maximum_age_seconds });
    expect(isCctvMonitorStatus(expected)).toBe(false);
  });
});
