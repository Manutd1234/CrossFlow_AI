/** Experimental image counts never fall back to generated observations. */
const API_BASE = import.meta.env.VITE_API_BASE_URL?.trim().replace(/\/$/, '') ?? '';
const API_ENABLED = import.meta.env.VITE_API_ENABLED !== 'false';
export const MAX_VEHICLE_IMAGE_BYTES = 4 * 1024 * 1024;
export type VehicleDetection = { class_name: string; score: number; box: [number, number, number, number] };
export type VehicleCountPreviewResult = {
  vehicle_count: number; image: { width: number; height: number }; detections: VehicleDetection[];
  experimental: true; production_eligible: false; batam_validated: false; image_retained: false;
  model_id?: string; latency_ms?: number; note: string;
};
export type VehicleCounterStatus = {
  available: boolean; experimental: true; production_eligible: false; batam_validated: false; message: string;
};
function record(value: unknown): value is Record<string, unknown> { return value !== null && typeof value === 'object' && !Array.isArray(value); }
function probability(value: unknown): value is number { return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1; }
export function isVehicleCountPreview(value: unknown): value is VehicleCountPreviewResult {
  if (!record(value) || !record(value.image)) return false;
  const image = value.image;
  if (!record(value) || value.experimental !== true || value.production_eligible !== false || value.batam_validated !== false
    || value.image_retained !== false || typeof value.note !== 'string' || !record(value.image)
    || !['width', 'height'].every(key => typeof image[key] === 'number' && Number.isInteger(image[key])
      && image[key] >= 1 && image[key] <= 20_000)
    || typeof value.vehicle_count !== 'number' || !Number.isInteger(value.vehicle_count) || value.vehicle_count < 0
    || !Array.isArray(value.detections) || value.detections.length > 1000 || value.vehicle_count !== value.detections.length) return false;
  return value.detections.every(item => record(item) && typeof item.class_name === 'string' && item.class_name.length <= 64
    && probability(item.score) && Array.isArray(item.box) && item.box.length === 4 && item.box.every(probability)
    && item.box[0] < item.box[2] && item.box[1] < item.box[3]);
}
async function request(path: string, token: string | null, init: RequestInit = {}, signal?: AbortSignal): Promise<unknown> {
  if (!API_ENABLED) throw new Error('Connect the backend to preview vehicle counts.');
  const response = await fetch(`${API_BASE}${path}`, {
    ...init, headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...init.headers },
    cache: 'no-store', signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(60_000)]) : AbortSignal.timeout(60_000),
  });
  let data: unknown;
  try { data = await response.json(); } catch { throw new Error('The detector returned an incomplete response.'); }
  if (!response.ok) {
    const message = record(data) && typeof data.detail === 'string' ? data.detail : response.status === 403
      ? 'Sign in with an operator account to preview vehicle counts.' : 'The vehicle detector is unavailable. Try again shortly.';
    throw new Error(message);
  }
  return data;
}
export async function fetchVehicleCounterStatus(token: string | null, signal?: AbortSignal): Promise<VehicleCounterStatus> {
  const data = await request('/api/analytics/vehicle-count-preview/status', token, {}, signal);
  const status = record(data) ? data.vehicle_counter : null;
  if (!record(status) || typeof status.available !== 'boolean' || status.experimental !== true
    || status.production_eligible !== false || status.batam_validated !== false || typeof status.message !== 'string') {
    throw new Error('Vehicle-counter status could not be verified.');
  }
  return status as VehicleCounterStatus;
}
export async function requestVehicleCountPreview(file: File, token: string | null, signal?: AbortSignal): Promise<VehicleCountPreviewResult> {
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type) || file.size < 1 || file.size > MAX_VEHICLE_IMAGE_BYTES) {
    throw new Error('Choose a JPEG, PNG or WebP image no larger than 4 MiB.');
  }
  const data = await request('/api/analytics/vehicle-count-preview', token, { method: 'POST', headers: { 'Content-Type': file.type }, body: file }, signal);
  if (!record(data) || !isVehicleCountPreview(data.vehicle_count)) throw new Error('The detector did not return a verified preview. No count was substituted.');
  return data.vehicle_count;
}
