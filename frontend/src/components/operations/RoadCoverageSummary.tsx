import { useEffect, useState } from 'react';
import { MapPinned } from 'lucide-react';
import type { OperationsRoadCoverage, OperationsTrafficEstimates } from '../../types';

function count(value: number | null) {
  return value === null ? 'Unavailable' : value.toLocaleString('en-SG');
}

/** Summaries only: source imagery and the congestion calculation stay on the server. */
interface RoadCoverageProps {
  coverage: OperationsRoadCoverage;
  estimates?: OperationsTrafficEstimates;
}

export function RoadCoverageSummary({ coverage, estimates }: RoadCoverageProps) {
  return <RoadCoverageSnapshot key={estimates?.valid_until ?? 'map-only'} coverage={coverage} estimates={estimates} />;
}

function RoadCoverageSnapshot({ coverage, estimates }: RoadCoverageProps) {
  const [clock, setClock] = useState(() => Date.now());
  const expiresAt = estimates?.valid_until ? Date.parse(estimates.valid_until) : null;
  useEffect(() => {
    if (expiresAt === null || expiresAt <= Date.now()) return;
    const updateClock = () => {
      if (document.visibilityState !== 'hidden') setClock(Date.now());
    };
    const timer = window.setTimeout(() => setClock(Date.now()), expiresAt - Date.now() + 1);
    // Recheck immediately on return even if the browser suspended the expiry
    // timer. An in-flight refresh must not keep an old observation visible.
    window.addEventListener('focus', updateClock);
    document.addEventListener('visibilitychange', updateClock);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener('focus', updateClock);
      document.removeEventListener('visibilitychange', updateClock);
    };
  }, [expiresAt]);
  const current = estimates !== undefined && estimates.fresh_cameras > 0
    && expiresAt !== null && clock < expiresAt;
  const liveLinks = current ? coverage.live_camera_edge_count : null;

  return <section className="glass-panel operations-road-coverage" aria-label="Road network coverage">
    <div className="operations-road-coverage__heading">
      <h3><MapPinned size={20} aria-hidden="true" />Road coverage</h3>
      <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a>
    </div>
    <dl className="operations-road-coverage__metrics">
      <div><dt>Mapped roads & paths</dt><dd>{count(coverage.mapped_source_ways)}</dd></div>
      <div><dt>Map nodes</dt><dd>{count(coverage.mapped_nodes)}</dd></div>
      <div><dt>Directed road links</dt><dd>{count(coverage.directed_edges)}</dd></div>
      <div><dt>Links with current traffic estimates</dt><dd>{liveLinks === null ? 'Unavailable' : count(liveLinks)}</dd>
        {current && coverage.live_camera_edge_coverage_percent !== null ? <small>{coverage.live_camera_edge_coverage_percent > 0 && coverage.live_camera_edge_coverage_percent < 0.01 ? '<0.01' : coverage.live_camera_edge_coverage_percent.toLocaleString('en-SG', { maximumFractionDigits: 2 })}% of mapped links</small> : null}
      </div>
    </dl>
    <p className="operations-road-coverage__scope">Mapped roads include paths. Live measurements cover selected locations.</p>
    {estimates ? <div className="operations-road-coverage__traffic" aria-label="Local traffic estimate">
      <div><strong>Local congestion estimate</strong><span className="badge badge-neutral">{current ? estimates.independently_validated ? 'Validated views' : 'Provisional' : 'Awaiting fresh observations'}</span></div>
      <p>{current && estimates.mean_congestion_index !== null ? <><strong>{estimates.mean_congestion_index.toFixed(1)} / 100</strong> across {count(estimates.fresh_cameras)} current road views</> : 'A current local estimate is unavailable.'}</p>
      {current && estimates.vehicle_detections !== null ? <p>{count(estimates.vehicle_detections)} vehicle detections in current views; overlapping views may include the same vehicle.</p> : null}
      {current && estimates.latest_observed_at ? <time dateTime={estimates.latest_observed_at}>Observed {new Date(estimates.latest_observed_at).toLocaleTimeString('en-SG', { timeZone: 'Asia/Singapore', hour: 'numeric', minute: '2-digit', second: '2-digit' })} SGT</time> : null}
    </div> : null}
  </section>;
}
