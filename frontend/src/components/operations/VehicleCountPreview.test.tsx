/* @vitest-environment jsdom */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchVehicleCounterStatus, MAX_VEHICLE_IMAGE_BYTES, requestVehicleCountPreview } from '../../services/vehicleCounting';
import type { VehicleCountPreviewResult } from '../../services/vehicleCounting';
import { VehicleCountPreview } from './VehicleCountPreview';

vi.mock('../../services/vehicleCounting', () => ({
  fetchVehicleCounterStatus: vi.fn(), requestVehicleCountPreview: vi.fn(), MAX_VEHICLE_IMAGE_BYTES: 4 * 1024 * 1024,
}));
const status = vi.mocked(fetchVehicleCounterStatus);
const request = vi.mocked(requestVehicleCountPreview);
const createObjectURL = vi.fn(); const revokeObjectURL = vi.fn();
let root: Root | undefined;
let view: HTMLDivElement;
function result(count = 1): VehicleCountPreviewResult {
  return { vehicle_count: count, image: { width: 640, height: 480 },
    detections: Array.from({ length: count }, () => ({ class_name: 'car', score: 0.85, box: [0.1, 0.2, 0.5, 0.8] })),
    experimental: true, production_eligible: false, batam_validated: false, image_retained: false,
    note: 'Experimental object count; Batam validation pending.' };
}
function image(name = 'traffic.jpg', type = 'image/jpeg', bytes = 3) {
  return new File([new Uint8Array(bytes)], name, { type });
}
async function render(token: string | null = 'operator-session') {
  view = document.createElement('div'); document.body.append(view); root = createRoot(view);
  await act(async () => root!.render(<VehicleCountPreview accessToken={token} />));
  return view;
}
async function select(file: File | null) {
  const input = view.querySelector<HTMLInputElement>('input[type="file"]')!;
  Object.defineProperty(input, 'files', { configurable: true, value: file ? [file] : [] });
  await act(async () => input.dispatchEvent(new Event('change', { bubbles: true })));
}
async function count() { await act(async () => view.querySelector('button')!.click()); }
beforeEach(() => {
  vi.resetAllMocks();
  status.mockResolvedValue({ available: true, experimental: true, production_eligible: false, batam_validated: false, message: 'Preview ready' });
  request.mockResolvedValue(result()); createObjectURL.mockImplementation((file: File) => `blob:fixture-${file.name}`);
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = createObjectURL;
    static revokeObjectURL = revokeObjectURL;
  });
});
afterEach(() => {
  act(() => root?.unmount()); root = undefined; document.body.replaceChildren(); vi.unstubAllGlobals();
});

describe('explicit experimental vehicle-count preview', () => {
  it('loads metadata without uploading an image until the operator clicks', async () => {
    await render();
    expect(status).toHaveBeenCalledWith('operator-session', expect.any(AbortSignal));
    expect(request).not.toHaveBeenCalled(); expect(view.querySelector('button')!.disabled).toBe(true);
    const file = image(); await select(file);
    await act(async () => window.dispatchEvent(new Event('focus')));
    expect(request).not.toHaveBeenCalled(); expect(view.querySelector('button')!.disabled).toBe(false);
    expect(view.querySelector('img')?.getAttribute('src')).toBe('blob:fixture-traffic.jpg');
    await count();
    expect(request).toHaveBeenCalledWith(file, 'operator-session', expect.any(AbortSignal));
  });
  it('presents accessible experimental results and converts normalized boxes to image dimensions', async () => {
    await render(); await select(image()); await count();
    const section = view.querySelector('section')!;
    expect(section.getAttribute('aria-labelledby')).toBe('vehicle-count-title');
    expect(view.textContent).toContain('Experimental · Batam validation pending');
    expect(view.querySelector('[aria-live="polite"]')?.textContent).toContain('1 vehicle detected');
    expect(view.textContent).toContain('confidence is not a measured accuracy score');
    expect(view.textContent).toContain('do not update road congestion or routing');
    expect(view.querySelector('img')?.getAttribute('alt')).toContain('Selected traffic image');
    const overlay = view.querySelector('.vehicle-count-preview__image svg')!;
    expect(overlay.getAttribute('viewBox')).toBe('0 0 640 480');
    expect(overlay.getAttribute('aria-hidden')).toBe('true');
    const box = view.querySelector('.vehicle-count-preview__image rect')!;
    expect(Number(box.getAttribute('x'))).toBeCloseTo(64);
    expect(Number(box.getAttribute('y'))).toBeCloseTo(96);
    expect(Number(box.getAttribute('width'))).toBeCloseTo(256);
    expect(Number(box.getAttribute('height'))).toBeCloseTo(288);
    expect(view.textContent).toContain('car · detector confidence 85%');
  });
  it('deduplicates rapid clicks before React has committed the busy state', async () => {
    let finish!: (value: VehicleCountPreviewResult) => void;
    request.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    await render(); await select(image());
    await act(async () => { view.querySelector('button')!.click(); view.querySelector('button')!.click(); });
    expect(request).toHaveBeenCalledOnce(); expect(view.querySelector('button')!.disabled).toBe(true);
    expect(view.querySelector('section')!.getAttribute('aria-busy')).toBe('true');
    await act(async () => finish(result()));
    expect(view.querySelector('button')!.disabled).toBe(false);
  });
  it('aborts a replaced file and discards an old response after the new file finishes', async () => {
    let finishOld!: (value: VehicleCountPreviewResult) => void;
    request.mockReturnValueOnce(new Promise(resolve => { finishOld = resolve; }));
    await render(); await select(image('old.jpg')); await count();
    const oldSignal = request.mock.calls[0][2]!;
    await select(image('new.jpg'));
    expect(oldSignal.aborted).toBe(true); expect(revokeObjectURL).toHaveBeenCalledWith('blob:fixture-old.jpg');
    expect(view.querySelector('[aria-live="polite"]')).toBeNull();
    request.mockResolvedValueOnce(result(2)); await count();
    expect(view.textContent).toContain('2 vehicles detected');
    await act(async () => finishOld(result(5)));
    expect(view.textContent).toContain('2 vehicles detected'); expect(view.textContent).not.toContain('5 vehicles detected');
    expect(view.querySelector('img')?.getAttribute('src')).toBe('blob:fixture-new.jpg');
  });
  it('aborts pending processing and revokes its object URL on unmount', async () => {
    request.mockReturnValueOnce(new Promise(() => {}));
    await render(); await select(image()); await count();
    const signal = request.mock.calls[0][2]!;
    await act(async () => root!.unmount()); root = undefined;
    expect(signal.aborted).toBe(true); expect(revokeObjectURL).toHaveBeenCalledWith('blob:fixture-traffic.jpg');
    expect(status.mock.calls[0][1]?.aborted).toBe(true);
  });
  it('drops file, result and pending response on the parent session-key remount', async () => {
    let finishOld!: (value: VehicleCountPreviewResult) => void;
    request.mockReturnValueOnce(new Promise(resolve => { finishOld = resolve; }));
    await render('old-session'); await select(image()); await count();
    const oldSignal = request.mock.calls[0][2]!;
    await act(async () => root!.render(<VehicleCountPreview key="new-session" accessToken="new-session" />));
    expect(oldSignal.aborted).toBe(true); expect(view.querySelector('img')).toBeNull();
    expect(view.querySelector('button')!.disabled).toBe(true);
    expect(status).toHaveBeenLastCalledWith('new-session', expect.any(AbortSignal));
    await act(async () => finishOld(result(7)));
    expect(view.textContent).not.toContain('7 vehicles detected');
    await select(image('new-account.jpg')); await count();
    expect(request).toHaveBeenLastCalledWith(expect.any(File), 'new-session', expect.any(AbortSignal));
  });
  it('also resets private state on a direct accessToken prop change without relying on a parent key', async () => {
    await render('old-session'); await select(image('private.jpg')); await count();
    expect(view.textContent).toContain('1 vehicle detected');
    await act(async () => root!.render(<VehicleCountPreview accessToken="new-session" />));
    expect(view.querySelector('img')).toBeNull(); expect(view.querySelector('[aria-live="polite"]')).toBeNull();
    expect(view.querySelector('button')!.disabled).toBe(true);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:fixture-private.jpg');
    await select(image('replacement.jpg')); await count();
    expect(request).toHaveBeenLastCalledWith(expect.any(File), 'new-session', expect.any(AbortSignal));
  });
  it('discards late upload and status promises after a direct session change', async () => {
    let finishOldStatus!: (value: Awaited<ReturnType<typeof fetchVehicleCounterStatus>>) => void;
    let finishOldUpload!: (value: VehicleCountPreviewResult) => void;
    status.mockReturnValueOnce(new Promise(resolve => { finishOldStatus = resolve; }));
    request.mockReturnValueOnce(new Promise(resolve => { finishOldUpload = resolve; }));
    await render('old-session'); await select(image('private.jpg')); await count();
    const oldUploadSignal = request.mock.calls[0][2]!;
    await act(async () => root!.render(<VehicleCountPreview accessToken="replacement-session" />));
    expect(oldUploadSignal.aborted).toBe(true);
    await act(async () => {
      finishOldUpload(result(9));
      finishOldStatus({ available: false, experimental: true, production_eligible: false, batam_validated: false, message: 'Old account status' });
    });
    expect(view.textContent).not.toContain('9 vehicles detected');
    expect(view.textContent).not.toContain('Old account status');
    expect(view.querySelector('img')).toBeNull();
  });
  it('sizes the image using its natural ratio before inference and returned dimensions afterward', async () => {
    await render(); await select(image());
    const selected = view.querySelector('img')!;
    Object.defineProperties(selected, { naturalWidth: { configurable: true, value: 200 }, naturalHeight: { configurable: true, value: 1000 } });
    await act(async () => selected.dispatchEvent(new Event('load')));
    const wrapper = view.querySelector<HTMLDivElement>('.vehicle-count-preview__image')!;
    expect(wrapper.style.width).toBe('min(100%, 112px)');
    await count();
    expect(wrapper.style.width).toBe(`min(100%, ${560 * 640 / 480}px)`);
    expect(view.querySelector('.vehicle-count-preview__image svg')?.getAttribute('viewBox')).toBe('0 0 640 480');
  });
  it('shows an authoritative processing failure without presenting zero vehicles and permits retry', async () => {
    request.mockRejectedValueOnce(new Error('Detector unavailable; preview not produced.'));
    await render(); await select(image()); await count();
    expect(view.querySelector('[role="alert"]')?.textContent).toContain('preview not produced');
    expect(view.textContent).not.toContain('0 vehicles detected'); expect(view.querySelector('[aria-live="polite"]')).toBeNull();
    await count(); expect(view.querySelector('[role="alert"]')).toBeNull(); expect(view.textContent).toContain('1 vehicle detected');
  });
  it.each([
    ['unsupported MIME', 'image/svg+xml', 3],
    ['empty image', 'image/jpeg', 0],
    ['oversized image', 'image/webp', MAX_VEHICLE_IMAGE_BYTES + 1],
  ])('rejects %s locally without upload or object URL creation', async (_label, mime, bytes) => {
    await render(); await select(image('invalid', String(mime), Number(bytes)));
    expect(view.querySelector('[role="alert"]')?.textContent).toContain('no larger than 4 MiB');
    expect(view.querySelector('button')!.disabled).toBe(true); expect(request).not.toHaveBeenCalled(); expect(createObjectURL).not.toHaveBeenCalled();
  });
  it('limits drawn boxes while keeping the returned full detection count visible', async () => {
    request.mockResolvedValueOnce(result(101)); await render(); await select(image()); await count();
    expect(view.querySelectorAll('.vehicle-count-preview__image rect')).toHaveLength(100);
    expect(view.textContent).toContain('101 vehicles detected');
    expect(view.textContent).toContain('count includes all returned detections');
  });
});
