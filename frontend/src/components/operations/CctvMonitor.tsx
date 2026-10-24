import { useEffect, useState } from 'react';
import { Camera, ExternalLink, RefreshCw } from 'lucide-react';
import { fetchCctvStatus, type CctvCamera, type CctvCameraStatus, type CctvMonitorStatus } from '../../services/cctv';
import './CctvMonitor.css';

const STATUS_LABELS: Record<CctvCameraStatus, string> = {
  live: 'Validated observation', estimated: 'Live estimate · Unvalidated', stale: 'Stale observation', unavailable: 'Unavailable',
  permission_pending: 'Permission pending', calibration_pending: 'Calibration pending', disabled: 'Disabled',
};
const COVERAGE_LABELS = [
  ['configured', 'Configured'], ['live', 'Validated'], ['estimated', 'Live estimates'], ['stale', 'Stale'], ['unavailable', 'Unavailable'],
  ['permission_pending', 'Permission pending'], ['calibration_pending', 'Calibration pending'],
] as const;
function localTime(value: string) {
  return `${new Intl.DateTimeFormat('en-SG', { dateStyle: 'medium', timeStyle: 'medium', timeZone: 'Asia/Jakarta' }).format(new Date(value))} WIB`;
}
function number(value: number | null, suffix = '') { return value === null ? 'Not measured' : `${value.toLocaleString('en-SG', { maximumFractionDigits: 1 })}${suffix}`; }
function expired(camera: CctvCamera, displayedAt: number) {
  return (camera.status === 'live' || camera.status === 'estimated') && camera.observed_at !== null
    && displayedAt >= Date.parse(camera.observed_at) + (camera.maximum_age_seconds ?? 15) * 1000;
}

function CameraObservation({ camera, current, displayedAt }: { camera: CctvCamera; current: boolean; displayedAt: number }) {
  const isExpired = expired(camera, displayedAt);
  const measured = current && !isExpired && (camera.status === 'live' || camera.status === 'estimated') && camera.observed_at !== null && camera.window_seconds !== null;
  const estimated = camera.status === 'estimated';
  return <li className="cctv-camera">
    <div className="cctv-camera__heading"><div><h4>{camera.name}</h4><p>{camera.road_name ?? 'Road view not calibrated'}</p></div>
      <span className={`badge ${measured && camera.operational_eligible ? 'badge-smooth' : 'badge-neutral'}`}>{current ? STATUS_LABELS[isExpired ? 'stale' : camera.status] : 'Status unavailable'}</span></div>
    <p className="cctv-camera__reason">{current ? isExpired ? 'The camera observation has expired. Fresh measurements are required.' : camera.reason : 'The latest source check failed. Previous observations are withheld.'}</p>
    {camera.observed_at ? <p className="cctv-camera__time">Last observation <time dateTime={camera.observed_at}>{localTime(camera.observed_at)}</time>{camera.window_seconds !== null ? ` · ${camera.window_seconds}-second window` : ''}</p> : null}
    {measured ? <>
      <dl className="cctv-camera__measurements">
        <div><dt>Vehicles in view</dt><dd>{number(camera.vehicle_count)}</dd></div>
        <div><dt>Moving vehicles</dt><dd>{number(camera.moving_vehicles)}</dd></div>
        <div><dt>Stopped vehicles</dt><dd>{number(camera.stopped_vehicles)}</dd></div>
        <div><dt>Road occupancy</dt><dd>{camera.occupancy_ratio === null ? 'Not measured' : number(camera.occupancy_ratio * 100, '%')}</dd></div>
        {camera.queue_length_m !== null ? <div><dt>Calibrated queue length</dt><dd>{number(camera.queue_length_m, ' m')}</dd></div> : null}
        {camera.operational_eligible || estimated ? <div><dt>{estimated ? 'Estimated congestion' : 'Camera congestion'}</dt><dd>{camera.severity}{camera.congestion_score !== null ? ` · ${number(camera.congestion_score, '%')}` : ''}</dd></div> : null}
      </dl>
      {estimated ? <p className="cctv-camera__pending">YOLO26 image estimate; accuracy and congestion thresholds are unvalidated. {camera.routing_enabled ? 'Fresh estimates influence the mapped road edge in Depart now routes.' : 'This view does not influence routing.'} {camera.timestamp_basis === 'stream_observation' ? 'Time records stream observation; source capture time is unverified.' : 'Time follows the source capture clock.'}</p> : !camera.operational_eligible ? <p className="cctv-camera__pending">Awaiting calibrated and validated congestion thresholds.</p> : null}
    </> : null}
    {camera.source_url ? <a className="cctv-camera__source" href={camera.source_url} target="_blank" rel="noreferrer">Official camera source <ExternalLink size={14} aria-hidden="true" /></a> : null}
  </li>;
}

export function CctvMonitor({ accessToken }: { accessToken: string | null }) {
  return <CctvSession key={accessToken ?? 'anonymous-cctv'} accessToken={accessToken} />;
}
function CctvSession({ accessToken }: { accessToken: string | null }) {
  const [snapshot, setSnapshot] = useState<CctvMonitorStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [displayedAt, setDisplayedAt] = useState(() => Date.now());
  useEffect(() => {
    const controller = new AbortController();
    let inFlight = false;
    let failures = 0;
    let nextCheck = 0;
    let pollInterval = 30_000;
    const load = async (force = false) => {
      if (inFlight || controller.signal.aborted || !force && Date.now() < nextCheck) return;
      inFlight = true; setLoading(true);
      try {
        const status = await fetchCctvStatus(accessToken, controller.signal);
        if (controller.signal.aborted) return;
        failures = 0; pollInterval = status.summary.configured > 0 ? 10_000 : 30_000;
        setSnapshot(status); setError(null); setDisplayedAt(Date.now());
      } catch (cause) {
        if (controller.signal.aborted) return;
        failures += 1; setError(cause instanceof Error ? cause.message : 'CCTV observations are unavailable.');
      } finally {
        inFlight = false;
        nextCheck = Date.now() + Math.min(120_000, pollInterval * 2 ** Math.min(failures, 4));
        if (!controller.signal.aborted) setLoading(false);
      }
    };
    const checkVisible = () => { if (document.visibilityState !== 'hidden') { setDisplayedAt(Date.now()); void load(); } };
    void load(true);
    const timer = window.setInterval(checkVisible, 10_000);
    window.addEventListener('focus', checkVisible);
    document.addEventListener('visibilitychange', checkVisible);
    return () => {
      controller.abort(); window.clearInterval(timer);
      window.removeEventListener('focus', checkVisible); document.removeEventListener('visibilitychange', checkVisible);
    };
  }, [accessToken, revision]);
  const hasObservation = Boolean(snapshot?.cameras.some(camera => camera.status === 'live' || camera.status === 'estimated'));
  useEffect(() => {
    if (!hasObservation) return;
    const timer = window.setInterval(() => { if (document.visibilityState !== 'hidden') setDisplayedAt(Date.now()); }, 1000);
    return () => window.clearInterval(timer);
  }, [hasObservation]);
  const current = snapshot !== null && error === null;
  const coverage = snapshot ? { ...snapshot.summary } : null;
  let expiredCount = 0;
  if (coverage && snapshot) for (const camera of snapshot.cameras) {
    if (expired(camera, displayedAt)) {
      const key = camera.status === 'live' ? 'live' : 'estimated';
      coverage[key] = Math.max(0, coverage[key] - 1); coverage.stale += 1; expiredCount += 1;
    }
  }
  return <section className="glass-panel cctv-monitor" aria-label="CCTV observations" aria-busy={loading}>
    <div className="cctv-monitor__header"><div><h3><Camera size={22} aria-hidden="true" />CCTV observations</h3><p>Automatic road observations from authorized surveillance feeds.</p></div>
      <button type="button" className="glass-button" disabled={loading} onClick={() => setRevision(value => value + 1)}><RefreshCw size={16} aria-hidden="true" className={loading ? 'operations-spinner' : undefined} />{loading ? 'Checking cameras…' : 'Refresh cameras'}</button></div>
    {error ? <p role="alert" className="cctv-monitor__error">{error}</p> : null}
    {current ? <>
      <p className="cctv-monitor__message" role="status">{expiredCount ? `${coverage!.live + coverage!.estimated} camera views have current observations; expired measurements are withheld.` : snapshot.message}</p>
      <dl className="cctv-monitor__coverage" aria-label="Camera coverage">{COVERAGE_LABELS.map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{coverage![key]}</dd></div>)}</dl>
      {coverage!.live === 0 && coverage!.estimated === 0 ? <p className="cctv-monitor__awaiting">Awaiting authorized stream and calibrated road view. Congestion is unavailable.</p> : null}
      <p className="cctv-monitor__checked">{snapshot.summary.listed} listed cameras · Checked <time dateTime={snapshot.updated_at}>{localTime(snapshot.updated_at)}</time></p>
    </> : !snapshot && !error ? <p role="status">Checking camera coverage. Congestion is unavailable until observations are verified.</p> : null}
    {snapshot?.cameras.length ? <details className="cctv-monitor__details"><summary>Camera coverage and observations</summary>
      <ul className="cctv-monitor__cameras">{snapshot.cameras.map(camera => <CameraObservation key={camera.camera_id} camera={camera} current={current} displayedAt={displayedAt} />)}</ul>
    </details> : null}
    <p className="cctv-monitor__scope">Counts describe the visible road area. Fresh camera estimates can influence explicitly mapped road edges in Depart now routes. These image estimates do not measure road speed or travel time. Missing feeds stay unavailable.</p>
  </section>;
}
