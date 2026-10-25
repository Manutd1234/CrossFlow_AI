import React, { useEffect, useRef, useState } from 'react';
import type { Corridor, Fetched, OperationsSummary } from '../../types';
import { ChartColumn, ChevronDown, Clock3, ExternalLink, Gauge, Info, Landmark, MapPinned, RefreshCw, ShieldAlert, Ship } from 'lucide-react';
import { ICON_SIZE } from '../../theme/iconSizes';
import { shortCorridorName } from '../../utils/format';
import { pressureBandView } from '../../utils/pressureBands';
import { CongestionHeatmap } from './CongestionHeatmap';
import { WorkspaceSubtabs } from '../shared/WorkspaceSubtabs';
import { PublishedOperatingRecords } from './PublishedOperatingRecords';
import { PublishedTrafficHistory } from './PublishedTrafficHistory';
import { ObservedTrafficCharts } from './AnalyticsCharts';
import { measuredChartData } from './analyticsChartData';
import { RoadCoverageSummary } from './RoadCoverageSummary';
import './OperationsAnalytics.css';

interface OperationsAnalyticsProps {
  operations: OperationsSummary;
  operationsSnapshot: Fetched<OperationsSummary> | null;
  corridors: Corridor[];
  ferryAndPortsContent?: React.ReactNode;
  checkedAt?: string | null;
  onRefresh?: () => Promise<void>;
  accessToken?: string | null;
}

type OperationsWorkspaceTab = 'overview' | 'ferry-ports' | 'history';
const OFFICIAL_SOURCE_LINKS = [
  { publisher: 'BP Batam Port Business Entity', url: 'https://batamport.bpbatam.go.id/pelabuhan-penumpang/' },
  { publisher: 'Batam City Transportation Agency', url: 'https://dishub.batam.go.id/wp-content/uploads/sites/3/2025/02/DISHUB_LAKIP_2024.pdf' },
];

function timestamp(value: string | null | undefined): string {
  if (!value || !Number.isFinite(Date.parse(value))) return 'Not available';
  return `${new Intl.DateTimeFormat('en-SG', {
    dateStyle: 'medium', timeStyle: 'medium', timeZone: 'Asia/Singapore',
  }).format(new Date(value))} SGT`;
}

function sampleClock(value: string): string {
  if (!Number.isFinite(Date.parse(value))) return 'Unavailable';
  return new Intl.DateTimeFormat('en-SG', {
    hour: 'numeric', minute: '2-digit', second: '2-digit', timeZone: 'Asia/Singapore',
  }).format(new Date(value));
}

function sampleAge(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return 'Sample age unavailable';
  return value < 60 ? `${Math.floor(value)}s old` : `${Math.floor(value / 60)}m ${Math.floor(value % 60)}s old`;
}

const PRESSURE_LABELS = { FREE_FLOW: 'Free flow', SLOWING: 'Slowing', HEAVY: 'Heavy', CRITICAL: 'Critical', UNAVAILABLE: 'Unavailable' };
const speedFormat = new Intl.NumberFormat('en-SG', { maximumFractionDigits: 1 });

export const OperationsAnalytics: React.FC<OperationsAnalyticsProps> = ({
  operations, operationsSnapshot, corridors, ferryAndPortsContent, checkedAt, onRefresh,
}) => {
  const [activeWorkspaceTab, setActiveWorkspaceTab] = useState<OperationsWorkspaceTab>('overview');
  const [visitedTabs, setVisitedTabs] = useState<Set<OperationsWorkspaceTab>>(() => new Set(['overview']));
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const refreshInFlight = useRef(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const handleWorkspaceTabChange = (tab: OperationsWorkspaceTab) => {
    setActiveWorkspaceTab(tab);
    setVisitedTabs(previous => previous.has(tab) ? previous : new Set(previous).add(tab));
  };

  const refresh = async () => {
    if (!onRefresh || refreshInFlight.current) return;
    refreshInFlight.current = true;
    setRefreshing(true);
    setRefreshError(null);
    try { await onRefresh(); }
    catch (error) {
      if (mounted.current) setRefreshError(error instanceof Error ? error.message : 'Operational sources could not be checked.');
    } finally {
      refreshInFlight.current = false;
      if (mounted.current) setRefreshing(false);
    }
  };

  const metadata = operations.analytics_metadata;
  const apiAvailable = Boolean(operationsSnapshot && operationsSnapshot.source !== 'offline');
  const observedTraffic = apiAvailable && metadata?.traffic_observed === true;
  const pressureValue = observedTraffic
    && operations.pressure_policy === 'congestion_pressure_v1'
    && operations.pressure_multiplier === 2.5
    && typeof operations.average_congestion_pressure === 'number'
    && Number.isFinite(operations.average_congestion_pressure)
    && operations.average_congestion_pressure >= 0
    && operations.average_congestion_pressure <= 100
    ? operations.average_congestion_pressure : null;
  const pressure = pressureValue === null ? null : pressureBandView({
    pressure_index: pressureValue, pressure_condition: operations.pressure_condition,
    pressure_condition_policy: operations.pressure_condition_policy,
  });
  const average = pressure ? pressureValue : null;
  const bottlenecks = observedTraffic ? operations.active_bottlenecks : null;
  const departures = apiAvailable ? operations.scheduled_ferry_departures_next_12h : null;
  const bottleneckNames = operations.bottleneck_corridors?.map(item => shortCorridorName(item.name)).join(' & ');
  const samples = observedTraffic ? operations.current_traffic_samples ?? [] : [];
  const measuredSpeeds = measuredChartData(samples).speeds.map(point => point.measured);
  const lowestSpeed = measuredSpeeds.length ? Math.min(...measuredSpeeds) : null;
  const highestSpeed = measuredSpeeds.length ? Math.max(...measuredSpeeds) : null;
  const speedRange = lowestSpeed === null || highestSpeed === null ? null
    : lowestSpeed === highestSpeed ? speedFormat.format(lowestSpeed) : `${speedFormat.format(lowestSpeed)}–${speedFormat.format(highestSpeed)}`;
  const locations = samples.map(point => {
    const qualified = measuredChartData([point]);
    const locationPressure = qualified.classifiedCount ? pressureBandView(point) : null;
    return {
      point,
      measured: qualified.speeds.length > 0,
      pressure: locationPressure,
      pressureValue: locationPressure ? point.pressure_index : null,
    };
  }).sort((left, right) => (right.pressureValue ?? -1) - (left.pressureValue ?? -1));
  const highestPressure = locations.find(location => location.pressure);

  return (
    <div className="app-screen-layout operations-layout">
      <section className="glass-panel operations-data-status" aria-label="Operational data freshness">
        <div className="operations-data-status__row">
          <div className="operations-data-status__copy">
            <strong><span className={`operations-data-status__dot${observedTraffic ? ' is-observed' : ''}`} aria-hidden="true" />{observedTraffic ? 'Observed traffic snapshot' : 'Live operational measurements unavailable'}</strong>
            <p>Last checked <time dateTime={checkedAt ?? operationsSnapshot?.fetchedAt}>{timestamp(checkedAt ?? operationsSnapshot?.fetchedAt)}</time></p>
          </div>
          {onRefresh ? <button type="button" className="glass-button" disabled={refreshing} onClick={() => void refresh()}>
            <RefreshCw aria-hidden="true" size={ICON_SIZE.medium} /> {refreshing ? 'Checking sources…' : 'Refresh data'}
          </button> : null}
        </div>
        <details className="operations-data-status__details"><summary><Info aria-hidden="true" size={ICON_SIZE.medium} /> Sources, coverage and refresh policy</summary>
          <div className="operations-data-status__provenance">
          <p>{observedTraffic
            ? `Provider: ${metadata?.traffic_source}. Coverage: ${metadata?.observed_corridor_count} of ${metadata?.expected_corridor_count} corridor points; this is not whole-network coverage.`
            : 'No fresh observed traffic feed is available. Missing measurements stay unavailable; no demo values or modelled operating totals are substituted.'}</p>
          <p>Traffic snapshot: <time dateTime={metadata?.input_snapshot_at ?? undefined}>{timestamp(metadata?.input_snapshot_at)}</time>.</p>
          <p>Summaries refresh about every 10 seconds while this window is visible, with earlier checks near an estimate’s expiry. Road and schedule checks run every 30 seconds. Returning to this window also checks the sources. Values change only when their source changes.</p>
          {metadata?.limitations?.map(item => <p key={item}>{item}</p>)}
          </div>
        </details>
        {refreshError ? <p className="operations-data-status__error" role="alert">{refreshError}</p> : null}
      </section>
      <WorkspaceSubtabs activeTab={activeWorkspaceTab} onActiveTabChange={handleWorkspaceTabChange}
        ariaLabel="Operations analytics sections" className="operations-tabs" idPrefix="operations-workspace" tabs={[
          { id: 'overview', label: 'Overview', content: visitedTabs.has('overview') ? (
            <div className="operations-section-stack">
              <section className="operations-metrics" aria-label="Operations headline metrics">
                <article className="glass-panel operations-metric-card">
                  <div className="operations-metric-card__label"><ChartColumn aria-hidden="true" size={ICON_SIZE.medium} /> Congestion pressure</div>
                  <div className="operations-metric-card__value" style={{ color: pressure?.color ?? 'var(--text-muted)' }}>{average ?? 'Unavailable'}{average !== null ? <span className="operations-metric-card__unit"> / 100</span> : null}</div>
                  <p className="operations-metric-card__detail">{observedTraffic ? `${metadata?.observed_corridor_count ?? 0} fresh road points` : 'Awaiting fresh observations'}</p>
                </article>
                <article className="glass-panel operations-metric-card">
                  <div className="operations-metric-card__label"><ShieldAlert aria-hidden="true" size={ICON_SIZE.medium} /> Observed Bottlenecks</div>
                  <div className="operations-metric-card__value">{bottlenecks ?? 'Unavailable'}</div>
                  <p className="operations-metric-card__detail">{bottlenecks === null ? 'No measured congestion coverage.' : bottleneckNames || 'No measured points above threshold.'}</p>
                </article>
                <article className="glass-panel operations-metric-card">
                  <div className="operations-metric-card__label"><Gauge aria-hidden="true" size={ICON_SIZE.medium} /> Measured speeds</div>
                  <div className="operations-metric-card__value">{speedRange ?? 'Unavailable'}{speedRange !== null ? <span className="operations-metric-card__unit"> km/h</span> : null}</div>
                  <p className="operations-metric-card__detail">{measuredSpeeds.length ? `${measuredSpeeds.length} measured road ${measuredSpeeds.length === 1 ? 'point' : 'points'}` : 'Awaiting fresh speed measurements'}</p>
                </article>
                <article className="glass-panel operations-metric-card">
                  <div className="operations-metric-card__label"><Ship aria-hidden="true" size={ICON_SIZE.medium} /> Published Departures</div>
                  <div className="operations-metric-card__value">{departures ?? 'Unavailable'}</div>
                  <p className="operations-metric-card__detail">Published timetable · next 12 hours</p>
                </article>
              </section>
              <section className="glass-panel operations-live-roads" aria-labelledby="operations-live-roads-title">
                <header className="operations-live-roads__header">
                  <div><h3 id="operations-live-roads-title"><MapPinned aria-hidden="true" size={ICON_SIZE.medium} /> Live road conditions</h3>
                    <p>Measured locations, highest pressure first.</p></div>
                  <div className="operations-live-roads__coverage"><strong>{samples.length}{metadata?.expected_corridor_count ? ` of ${metadata.expected_corridor_count}` : ''} sampled road points</strong>
                    <span><Clock3 aria-hidden="true" size={ICON_SIZE.small} />{observedTraffic ? `Oldest sample · ${sampleAge(metadata?.snapshot_age_seconds)}` : 'Awaiting fresh observations'}</span></div>
                </header>
                {highestPressure?.pressure ? <div className="operations-live-roads__highlight" style={{ borderColor: highestPressure.pressure.color }}>
                  <span>Highest sampled pressure</span><strong>{shortCorridorName(highestPressure.point.name)}</strong>
                  <span style={{ color: highestPressure.pressure.color }}>{highestPressure.pressureValue} / 100 · {PRESSURE_LABELS[highestPressure.pressure.condition]}</span>
                </div> : null}
                {locations.length ? <div className="operations-live-roads__table-wrap"><table className="operations-live-roads__table">
                  <thead><tr><th scope="col">Road point</th><th scope="col">Congestion pressure</th><th scope="col">Measured speed</th><th scope="col">Observed (SGT)</th></tr></thead>
                  <tbody>{locations.map(({ point, measured, pressure: locationPressure }) => <tr key={point.id}>
                    <th scope="row">{shortCorridorName(point.name)}</th>
                    <td><div className="operations-live-roads__pressure" style={{ color: locationPressure?.color ?? 'var(--text-muted)' }}>
                      <strong>{locationPressure ? `${point.pressure_index} / 100` : 'Unavailable'}</strong><span>{locationPressure ? PRESSURE_LABELS[locationPressure.condition] : 'Unverified'}</span>
                      <div className="operations-live-roads__track" aria-hidden="true"><span style={{ width: `${locationPressure ? point.pressure_index : 0}%`, background: locationPressure?.color }} /></div>
                    </div></td>
                    <td>{measured ? <><strong>{speedFormat.format(point.current_speed_kmh)} <small>km/h</small></strong><span className="operations-live-roads__muted">{speedFormat.format(point.free_flow_speed_kmh)} km/h free flow</span></> : <span className="operations-live-roads__muted">Unavailable</span>}</td>
                    <td><time dateTime={point.as_of}>{sampleClock(point.as_of)}</time><span className="operations-live-roads__muted">{sampleAge(point.age_seconds)}</span></td>
                  </tr>)}</tbody>
                </table></div> : <p className="operations-live-roads__empty" role="status">Live road conditions are unavailable. Refresh data to check the sources.</p>}
                <p className="operations-live-roads__scope">Sampled locations only · below 70 green · 70–79 amber · 80+ critical</p>
              </section>
              <ObservedTrafficCharts samples={samples} expectedCount={metadata?.expected_corridor_count} />
              {samples.length ? (
                <details className="glass-panel operations-chart-card operations-disclosure operations-source-readings">
                  <summary>Source measurements <ChevronDown aria-hidden="true" size={ICON_SIZE.medium} /></summary>
                  <div className="operations-observed-table"><table><thead><tr><th>Corridor point</th><th>Measured speed</th><th>Free-flow speed</th><th>Measured congestion index</th><th>Snapshot (SGT)</th></tr></thead><tbody>
                    {samples.map(point => <tr key={point.id}><th>{shortCorridorName(point.name)}</th><td>{point.current_speed_kmh} km/h</td><td>{point.free_flow_speed_kmh} km/h</td><td>{point.score} / 100</td><td><time dateTime={point.as_of}>{timestamp(point.as_of)}</time></td></tr>)}
                  </tbody></table></div>
                </details>
              ) : null}
              <details className="operations-context operations-disclosure">
                <summary className="glass-panel"><div><strong>Coverage & published context</strong><span>Map coverage, dated reports and official sources</span></div><ChevronDown aria-hidden="true" size={ICON_SIZE.medium} /></summary>
                <div className="operations-section-stack">
                  {apiAvailable && operations.road_network_coverage ? <RoadCoverageSummary
                    coverage={operations.road_network_coverage} estimates={operations.camera_traffic_summary} /> : null}
                  <PublishedOperatingRecords />
                  <section className="ui-card-shadow-hover official-evidence-panel" aria-labelledby="official-evidence-title">
                    <div className="official-evidence-heading"><h3 id="official-evidence-title">Batam Authority Operating Context</h3><span><Landmark aria-hidden="true" size={ICON_SIZE.medium} /> Primary-source references</span></div>
                    <p className="operations-reference-note">Open the source for its reporting period and latest published totals. Historical report figures are not copied into current operational measurements.</p>
                    <div className="official-evidence-grid">{OFFICIAL_SOURCE_LINKS.map(source => (
                      <article key={source.url}>
                        <div className="operations-alert__content"><a href={source.url} target="_blank" rel="noreferrer">{source.publisher} <ExternalLink aria-hidden="true" size={ICON_SIZE.medium} /></a></div>
                      </article>
                    ))}</div>
                    <p className="operations-reference-note">Measured emissions impact: unavailable.</p>
                  </section>
                </div>
              </details>
            </div>
          ) : null },
          { id: 'ferry-ports', label: 'Ports', content: visitedTabs.has('ferry-ports') ? ferryAndPortsContent ?? <p role="status">Ferry and port data is unavailable. Refresh data to reconnect.</p> : null },
          { id: 'history', label: 'History', content: visitedTabs.has('history') ? (
            <PublishedTrafficHistory modelledContent={corridors.length
              ? <div className="glass-panel operations-history-panel"><CongestionHeatmap corridors={corridors} mode="modelled" /></div>
              : <div role="status">Traffic-flow analysis is unavailable without corridor definitions.</div>} />
          ) : null },
        ]} />
    </div>
  );
};
