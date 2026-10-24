import React, { useEffect, useRef, useState } from 'react';
import type { Corridor, Fetched, OperationsSummary } from '../../types';
import { ChartColumn, ExternalLink, Info, Landmark, Leaf, RefreshCw, ShieldAlert, Ship } from 'lucide-react';
import { ICON_SIZE } from '../../theme/iconSizes';
import { shortCorridorName } from '../../utils/format';
import { pressureBandView } from '../../utils/pressureBands';
import { CongestionHeatmap } from './CongestionHeatmap';
import { WorkspaceSubtabs } from '../shared/WorkspaceSubtabs';
import { PublishedOperatingRecords } from './PublishedOperatingRecords';
import { PublishedTrafficHistory } from './PublishedTrafficHistory';
import { ObservedTrafficCharts } from './AnalyticsCharts';
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
                  <p className="operations-metric-card__detail">{metadata?.observed_corridor_count ?? 0} fresh road points</p>
                </article>
                <article className="glass-panel operations-metric-card">
                  <div className="operations-metric-card__label"><ShieldAlert aria-hidden="true" size={ICON_SIZE.medium} /> Observed Bottlenecks</div>
                  <div className="operations-metric-card__value">{bottlenecks ?? 'Unavailable'}</div>
                  <p className="operations-metric-card__detail">{bottlenecks === null ? 'No measured congestion coverage.' : bottleneckNames || 'No measured points above threshold.'}</p>
                </article>
                <article className="glass-panel operations-metric-card">
                  <div className="operations-metric-card__label"><Ship aria-hidden="true" size={ICON_SIZE.medium} /> Published Departures</div>
                  <div className="operations-metric-card__value">{departures ?? 'Unavailable'}</div>
                  <p className="operations-metric-card__detail">Published timetable · next 12 hours</p>
                </article>
                <article className="glass-panel operations-metric-card">
                  <div className="operations-metric-card__label"><Leaf aria-hidden="true" size={ICON_SIZE.medium} /> Measured Emissions Impact</div>
                  <div className="operations-metric-card__value">Unavailable</div>
                  <p className="operations-metric-card__detail">Awaiting measured emissions data</p>
                </article>
              </section>
              {apiAvailable && operations.road_network_coverage ? <RoadCoverageSummary
                coverage={operations.road_network_coverage} estimates={operations.camera_traffic_summary} /> : null}
              <ObservedTrafficCharts samples={observedTraffic ? operations.current_traffic_samples ?? [] : []}
                expectedCount={metadata?.expected_corridor_count} />
              {observedTraffic && operations.current_traffic_samples?.length ? (
                <section className="glass-panel operations-chart-card">
                  <h3>Current observed corridor points</h3>
                  <div className="operations-observed-table"><table><thead><tr><th>Corridor point</th><th>Measured speed</th><th>Free-flow speed</th><th>Measured congestion index</th><th>Snapshot (SGT)</th></tr></thead><tbody>
                    {operations.current_traffic_samples.map(point => <tr key={point.id}><th>{shortCorridorName(point.name)}</th><td>{point.current_speed_kmh} km/h</td><td>{point.free_flow_speed_kmh} km/h</td><td>{point.score} / 100</td><td><time dateTime={point.as_of}>{timestamp(point.as_of)}</time></td></tr>)}
                  </tbody></table></div>
                </section>
              ) : null}
              <PublishedOperatingRecords />
              <section className="ui-card-shadow-hover official-evidence-panel" aria-labelledby="official-evidence-title">
                <div className="official-evidence-heading"><h3 id="official-evidence-title">Batam Authority Operating Context</h3><span><Landmark aria-hidden="true" size={ICON_SIZE.medium} /> Primary-source references</span></div>
                <p className="operations-reference-note">Open the source for its reporting period and latest published totals. Historical report figures are not copied into current operational measurements.</p>
                <div className="official-evidence-grid">{OFFICIAL_SOURCE_LINKS.map(source => (
                  <article key={source.url}>
                    <div className="operations-alert__content"><a href={source.url} target="_blank" rel="noreferrer">{source.publisher} <ExternalLink aria-hidden="true" size={ICON_SIZE.medium} /></a></div>
                  </article>
                ))}</div>
              </section>
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
