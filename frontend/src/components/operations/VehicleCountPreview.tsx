import { useEffect, useRef, useState } from 'react';
import { Camera, RefreshCw } from 'lucide-react';
import { fetchVehicleCounterStatus, MAX_VEHICLE_IMAGE_BYTES, requestVehicleCountPreview } from '../../services/vehicleCounting';
import type { VehicleCounterStatus, VehicleCountPreviewResult } from '../../services/vehicleCounting';
import './VehicleCountPreview.css';

export function VehicleCountPreview({ accessToken }: { accessToken: string | null }) {
  return <VehicleCountSession key={accessToken ?? 'anonymous-vehicle-preview'} accessToken={accessToken} />;
}

function VehicleCountSession({ accessToken }: { accessToken: string | null }) {
  const [status, setStatus] = useState<VehicleCounterStatus | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [imageSize, setImageSize] = useState<{ width: number; height: number } | null>(null);
  const [result, setResult] = useState<VehicleCountPreviewResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const request = useRef<AbortController | null>(null);
  const previewUrl = useRef<string | null>(null);
  const sequence = useRef(0);
  useEffect(() => {
    const controller = new AbortController();
    void fetchVehicleCounterStatus(accessToken, controller.signal).then(value => { if (!controller.signal.aborted) setStatus(value); }).catch(error => {
      if (!controller.signal.aborted) setError(error instanceof Error ? error.message : 'Detector status is unavailable.');
    });
    return () => { controller.abort(); request.current?.abort(); sequence.current += 1; };
  }, [accessToken]);
  useEffect(() => () => {
    if (previewUrl.current) URL.revokeObjectURL(previewUrl.current);
  }, []);
  function selectFile(next: File | null) {
    request.current?.abort(); sequence.current += 1; setRunning(false); setResult(null); setError(null); setPreview(null); setImageSize(null);
    if (previewUrl.current) URL.revokeObjectURL(previewUrl.current);
    previewUrl.current = null;
    if (next && (!['image/jpeg', 'image/png', 'image/webp'].includes(next.type) || next.size < 1 || next.size > MAX_VEHICLE_IMAGE_BYTES)) {
      setFile(null); setError('Choose a JPEG, PNG or WebP image no larger than 4 MiB.'); return;
    }
    setFile(next);
    if (next) {
      previewUrl.current = URL.createObjectURL(next);
      setPreview(previewUrl.current);
    }
  }
  async function count() {
    if (!file || running || request.current && !request.current.signal.aborted) return;
    const controller = new AbortController(); request.current = controller;
    const id = ++sequence.current; setRunning(true); setResult(null); setError(null);
    try {
      const value = await requestVehicleCountPreview(file, accessToken, controller.signal);
      if (!controller.signal.aborted && id === sequence.current) setResult(value);
    } catch (error) {
      if (!controller.signal.aborted && id === sequence.current) setError(error instanceof Error ? error.message : 'The image could not be counted.');
    } finally {
      if (id === sequence.current) { setRunning(false); request.current = null; }
    }
  }
  const dimensions = result?.image ?? imageSize;
  const imageStyle = dimensions ? { width: `min(100%, ${560 * dimensions.width / dimensions.height}px)` } : undefined;
  return <section className="vehicle-count-preview" aria-labelledby="vehicle-count-title" aria-busy={running}>
    <div className="vehicle-count-preview__header"><h4 id="vehicle-count-title"><Camera size={20} aria-hidden="true" /> Vehicle counting preview</h4>
      <span className="badge badge-neutral">Experimental · Batam validation pending</span></div>
    <p>Choose a traffic image to inspect detected vehicle boxes. Uploaded images are processed for this preview and are not saved.</p>
    <div className="vehicle-count-preview__controls">
      <label htmlFor="vehicle-count-file">Traffic image <span>JPEG, PNG or WebP · up to 4 MiB</span></label>
      <input id="vehicle-count-file" type="file" accept="image/jpeg,image/png,image/webp" onChange={event => selectFile(event.target.files?.[0] ?? null)} />
      <button type="button" className="glass-button" disabled={!file || running} onClick={() => void count()}>
        {running ? <RefreshCw size={16} className="operations-spinner" aria-hidden="true" /> : null}{running ? 'Counting vehicles…' : 'Count vehicles'}
      </button>
    </div>
    {status && !status.available ? <p role="status">{status.message}</p> : null}
    {error ? <p role="alert" className="vehicle-count-preview__error">{error}</p> : null}
    {preview ? <div className="vehicle-count-preview__image" style={imageStyle}>
      <img src={preview} onLoad={event => setImageSize({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })} alt="Selected traffic image for vehicle-count review" />
      {result ? <svg viewBox={`0 0 ${result.image.width} ${result.image.height}`} aria-hidden="true" preserveAspectRatio="none">
        {result.detections.slice(0, 100).map((detection, index) => {
          const [x1, y1, x2, y2] = detection.box;
          return <rect key={index} x={x1 * result.image.width} y={y1 * result.image.height}
            width={(x2 - x1) * result.image.width} height={(y2 - y1) * result.image.height} />;
        })}
      </svg> : null}
    </div> : null}
    {result ? <div aria-live="polite" className="vehicle-count-preview__result">
      <strong>{result.vehicle_count} {result.vehicle_count === 1 ? 'vehicle' : 'vehicles'} detected</strong>
      <p>{result.note} Detector confidence is not a measured accuracy score.</p>
      {result.detections.length > 100 ? <p>Showing the first 100 boxes. The count includes all returned detections.</p> : null}
      {result.detections.length ? <details><summary>Inspect detected vehicles</summary><ul>
        {result.detections.slice(0, 100).map((detection, index) => <li key={index}>{index + 1}. {detection.class_name} · detector confidence {(detection.score * 100).toFixed(0)}%</li>)}
      </ul></details> : null}
    </div> : null}
    <p className="vehicle-count-preview__limit">Preview counts do not update road congestion or routing. Flow, queue length and speed need additional measurements.</p>
  </section>;
}
