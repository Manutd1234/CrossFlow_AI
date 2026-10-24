import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  fetchVehicleCounterStatus, isVehicleCountPreview, MAX_VEHICLE_IMAGE_BYTES,
  requestVehicleCountPreview,
} from './vehicleCounting';

function result() {
  return {
    vehicle_count: 1, image: { width: 640, height: 480 },
    detections: [{ class_name: 'car', score: 0.85, box: [0.1, 0.2, 0.5, 0.8] }],
    experimental: true, production_eligible: false, batam_validated: false,
    image_retained: false, note: 'Experimental object count; Batam validation pending.',
  };
}
function image(type = 'image/jpeg', bytes = 3) {
  return new File([new Uint8Array(bytes)], 'traffic.jpg', { type });
}
function respond(value: unknown, status = 200) {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(value), {
    status, headers: { 'Content-Type': 'application/json' },
  }));
  vi.stubGlobal('fetch', fetcher); return fetcher;
}
afterEach(() => vi.unstubAllGlobals());

describe('experimental vehicle counting API boundary', () => {
  it('sends the original image as raw MIME bytes with operator bearer and no-store', async () => {
    const expected = result(); const fetcher = respond({ vehicle_count: expected }); const file = image();
    expect(await requestVehicleCountPreview(file, 'operator-session')).toEqual(expected);
    const [url, init] = fetcher.mock.calls[0];
    expect(url).toContain('/api/analytics/vehicle-count-preview');
    expect(init.method).toBe('POST'); expect(init.body).toBe(file);
    expect(init.headers).toEqual({ Authorization: 'Bearer operator-session', 'Content-Type': 'image/jpeg' });
    expect(init.cache).toBe('no-store'); expect(init.signal).toBeInstanceOf(AbortSignal);
    expect(typeof init.body).not.toBe('string');
  });
  it.each(['image/jpeg', 'image/png', 'image/webp'])('accepts authorized %s bodies without adding authentication when absent', async mime => {
    const fetcher = respond({ vehicle_count: result() }); const file = image(mime);
    await requestVehicleCountPreview(file, null);
    expect(fetcher.mock.calls[0][1].headers).toEqual({ 'Content-Type': mime });
    expect(fetcher.mock.calls[0][1].body).toBe(file);
  });
  it.each([
    ['unsupported MIME', 'image/svg+xml', 3],
    ['empty image', 'image/jpeg', 0],
    ['oversized image', 'image/png', MAX_VEHICLE_IMAGE_BYTES + 1],
  ])('rejects %s before any upload', async (_label, mime, bytes) => {
    const fetcher = respond({ vehicle_count: result() });
    await expect(requestVehicleCountPreview(image(String(mime), Number(bytes)), 'operator')).rejects.toThrow('no larger than 4 MiB');
    expect(fetcher).not.toHaveBeenCalled();
  });
  it('accepts the inclusive 4 MiB size limit', async () => {
    const fetcher = respond({ vehicle_count: result() });
    await requestVehicleCountPreview(image('image/jpeg', MAX_VEHICLE_IMAGE_BYTES), null);
    expect(fetcher).toHaveBeenCalledOnce();
  });
  it.each([401, 403, 429, 503])('preserves HTTP %s errors without substituting an empty count', async status => {
    respond({ detail: 'Preview unavailable for this request' }, status);
    await expect(requestVehicleCountPreview(image(), 'operator')).rejects.toThrow('Preview unavailable for this request');
  });
  it('provides an operator-specific rejection for an unstructured 403', async () => {
    respond({}, 403);
    await expect(requestVehicleCountPreview(image(), null)).rejects.toThrow('operator account');
  });
  it('keeps network failures and incomplete JSON distinct from a real zero result', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));
    await expect(requestVehicleCountPreview(image(), null)).rejects.toThrow('offline');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('not JSON', { status: 200 })));
    await expect(requestVehicleCountPreview(image(), null)).rejects.toThrow('incomplete response');
    const zero = { ...result(), vehicle_count: 0, detections: [] };
    respond({ vehicle_count: zero });
    expect((await requestVehicleCountPreview(image(), null)).vehicle_count).toBe(0);
  });
  it.each([
    ['missing result', {}],
    ['null result', { vehicle_count: null }],
    ['incomplete result', { vehicle_count: { vehicle_count: 0 } }],
    ['production claim', { vehicle_count: { ...result(), production_eligible: true } }],
    ['unverified Batam claim', { vehicle_count: { ...result(), batam_validated: true } }],
    ['retained image', { vehicle_count: { ...result(), image_retained: true } }],
    ['mismatched count', { vehicle_count: { ...result(), vehicle_count: 2 } }],
    ['invalid dimensions', { vehicle_count: { ...result(), image: { width: 0, height: 480 } } }],
    ['non-normalized box', { vehicle_count: { ...result(), detections: [{ class_name: 'car', score: 0.9, box: [0, 0, 640, 480] }] } }],
    ['reversed box', { vehicle_count: { ...result(), detections: [{ class_name: 'car', score: 0.9, box: [0.5, 0.2, 0.1, 0.8] }] } }],
  ])('rejects %s without manufacturing a fallback', async (_label, payload) => {
    respond(payload);
    await expect(requestVehicleCountPreview(image(), 'operator')).rejects.toThrow('No count was substituted');
  });
  it('rejects non-finite detector values and oversized detection sets', () => {
    expect(isVehicleCountPreview({ ...result(), detections: [{ class_name: 'car', score: NaN, box: [0, 0, 0.5, 0.5] }] })).toBe(false);
    const detections = Array.from({ length: 1001 }, () => result().detections[0]);
    expect(isVehicleCountPreview({ ...result(), vehicle_count: detections.length, detections })).toBe(false);
  });
  it('propagates caller cancellation to the upload fetch', async () => {
    const controller = new AbortController(); let signal: AbortSignal | undefined;
    vi.stubGlobal('fetch', vi.fn((_url, init: RequestInit) => new Promise((_resolve, reject) => {
      signal = init.signal ?? undefined;
      signal?.addEventListener('abort', () => reject(new DOMException('Request aborted', 'AbortError')));
    })));
    const pending = requestVehicleCountPreview(image(), 'operator', controller.signal);
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    expect(signal?.aborted).toBe(true);
  });
  it('fetches status without an image body and rejects unsafe production identities', async () => {
    const status = { available: true, experimental: true, production_eligible: false, batam_validated: false, message: 'Preview ready' };
    const fetcher = respond({ vehicle_counter: status });
    expect(await fetchVehicleCounterStatus('operator')).toEqual(status);
    const [url, init] = fetcher.mock.calls[0];
    expect(url).toContain('/vehicle-count-preview/status'); expect(init.body).toBeUndefined();
    expect(init.headers).toEqual({ Authorization: 'Bearer operator' }); expect(init.cache).toBe('no-store');
    respond({ vehicle_counter: { ...status, production_eligible: true } });
    await expect(fetchVehicleCounterStatus('operator')).rejects.toThrow('could not be verified');
  });
});
