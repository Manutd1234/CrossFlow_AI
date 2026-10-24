import React, { useCallback, useEffect, useRef, useState } from 'react';
import type { AlertItem, Corridor, Fetched, OperationsSummary } from '../../types';
import { ChartColumn, Check, ChevronDown, ExternalLink, Info, Landmark, Leaf, RefreshCw, ShieldAlert, ShieldCheck, Ship } from 'lucide-react';
import { ICON_SIZE } from '../../theme/iconSizes';
import { formatTime, shortCorridorName } from '../../utils/format';
import { CongestionHeatmap } from './CongestionHeatmap';
import { RealTrafficForecast } from './RealTrafficForecast';
import { WorkspaceSubtabs } from '../shared/WorkspaceSubtabs';
import { PublishedOperatingRecords } from './PublishedOperatingRecords';
import { PublishedTrafficHistory } from './PublishedTrafficHistory';
import { ObservedTrafficCharts } from './AnalyticsCharts';
import { RoadCoverageSummary } from './RoadCoverageSummary';
import './OperationsAnalytics.css';

interface OperationsAlertCardProps {
  alert: AlertItem;
  acknowledged?: boolean;
  dismissing?: boolean;
  onAcknowledge?: () => void;
}

const OperationsAlertCard: React.FC<OperationsAlertCardProps> = ({
  alert,
  acknowledged = false,
  dismissing = false,
  onAcknowledge,
}) => (
  <article
    className={`operations-alert operations-alert--${alert.severity.toLowerCase()}${acknowledged ? ' is-acknowledged' : ''}`}
  >
    <div className="operations-alert__content">
      <div className="operations-alert__heading">
        <strong className="operations-alert__title">{alert.title}</strong>
        <span className="operations-alert__metadata">
          <span className="operations-alert__timestamp">{formatTime(alert.timestamp)}</span>
          <span className={`operations-alert__severity operations-alert__severity--${alert.severity.toLowerCase()}`}>
            {alert.severity}
          </span>
        </span>
      </div>
      <p className="operations-alert__message">{alert.message}</p>
    </div>

    {acknowledged ? (
      <span className="operations-alert__acknowledged-status">
        <Check aria-hidden="true" size={ICON_SIZE.big} /> Acknowledged
      </span>
    ) : (
      <button
        type="button"
        className="glass-button operations-alert__action"
        aria-label={`Acknowledge planning alert: ${alert.title}`}
        disabled={dismissing}
        onClick={onAcknowledge}
      >
        {dismissing ? 'Acknowledging…' : 'Acknowledge'}
      </button>
    )}
  </article>
);

interface OperationsAnalyticsProps {
  operations: OperationsSummary;
  operationsSnapshot: Fetched<OperationsSummary> | null;
  corridors: Corridor[];
  ferryAndPortsContent?: React.ReactNode;
  checkedAt?: string | null;
  onRefresh?: () => Promise<void>;
  accessToken?: string | null;
}

type OperationsWorkspaceTab = 'overview' | 'ferry-ports' | 'trends' | 'alerts' | 'history' | 'model';
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

// Providers reuse a road-point ID across observations. Acknowledging one
// snapshot must not suppress a later observation or a changed severity.
function alertObservationKey(alert: AlertItem): string {
  return JSON.stringify([alert.id, alert.timestamp, alert.severity]);
}

export const OperationsAnalytics: React.FC<OperationsAnalyticsProps> = ({
  operations, operationsSnapshot, corridors, ferryAndPortsContent, checkedAt, onRefresh, accessToken = null,
}) => {
  const [acknowledged, setAcknowledged] = useState<Set<string>>(new Set());
  const [activeWorkspaceTab, setActiveWorkspaceTab] = useState<OperationsWorkspaceTab>('overview');
  const [visitedTabs, setVisitedTabs] = useState<Set<OperationsWorkspaceTab>>(() => new Set(['overview']));
  const [dismissingAlertKeys, setDismissingAlertKeys] = useState<Set<string>>(new Set());
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const refreshInFlight = useRef(false);
  const mounted = useRef(true);
  const acknowledgementTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  useEffect(() => {
    const timers = acknowledgementTimersRef.current;
    mounted.current = true;
    return () => {
      mounted.current = false;
      timers.forEach(timer => clearTimeout(timer));
      timers.clear();
    };
  }, []);

  const beginAcknowledge = useCallback((alertKey: string) => {
    if (acknowledgementTimersRef.current.has(alertKey)) return;
    setDismissingAlertKeys(prev => new Set(prev).add(alertKey));
    const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
    const timer = setTimeout(() => {
      setAcknowledged(prev => new Set(prev).add(alertKey));
      setDismissingAlertKeys(prev => { const next = new Set(prev); next.delete(alertKey); return next; });
      acknowledgementTimersRef.current.delete(alertKey);
    }, reducedMotion ? 0 : 220);
    acknowledgementTimersRef.current.set(alertKey, timer);
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
  const average = observedTraffic ? operations.average_congestion_index : null;
  const bottlenecks = observedTraffic ? operations.active_bottlenecks : null;
  const departures = apiAvailable ? operations.scheduled_ferry_departures_next_12h : null;
  const visibleAlerts = observedTraffic ? operations.alerts : [];
  const activeAlerts = visibleAlerts.filter(alert => !acknowledged.has(alertObservationKey(alert)));
  const acknowledgedAlerts = visibleAlerts.filter(alert => acknowledged.has(alertObservationKey(alert)));
  const bottleneckNames = operations.bottleneck_corridors?.map(item => shortCorridorName(item.name)).join(' & ');
  const peakObservedScore = observedTraffic && operations.current_traffic_samples?.length
    ? Math.max(...operations.current_traffic_samples.map(point => point.score)) : null;
  const alertThreshold = typeof operations.bottleneck_threshold === 'number' ? operations.bottleneck_threshold : null;

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
                  <div className="operations-metric-card__label"><ChartColumn aria-hidden="true" size={ICON_SIZE.medium} /> Observed Traffic Index</div>
                  <div className="operations-metric-card__value">{average ?? 'Unavailable'}{average !== null ? <span className="operations-metric-card__unit"> / 100</span> : null}</div>
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
                  <div className="operations-observed-table"><table><thead><tr><th>Corridor point</th><th>Measured speed</th><th>Free-flow speed</th><th>Congestion index</th><th>Snapshot (SGT)</th></tr></thead><tbody>
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
          { id: 'trends', label: 'Trends', content: visitedTabs.has('trends') ? (
            <div className="operations-section-stack">
              <RealTrafficForecast accessToken={accessToken} />
              <button type="button" className="glass-button operations-history-link" onClick={() => handleWorkspaceTabChange('history')}>Open observed history</button>
            </div>
          ) : null },
          { id: 'alerts', label: 'Alerts', content: activeWorkspaceTab === 'alerts' ? (
            <section className="glass-panel operations-alerts" aria-labelledby="operator-alerts-title">
              <div className="operations-alerts-heading">
                <h3 id="operator-alerts-title" className="operations-alerts__title"><ShieldAlert aria-hidden="true" size={ICON_SIZE.large} /> Observed Traffic Alerts</h3>
                <span className="badge badge-neutral">{observedTraffic ? `${activeAlerts.length} ${activeAlerts.length === 1 ? 'alert' : 'alerts'}` : 'Unavailable'}</span>
                {onRefresh ? <button type="button" className="glass-button" disabled={refreshing} onClick={() => void refresh()}><RefreshCw size={16} aria-hidden="true" />{refreshing ? 'Checking traffic…' : 'Check traffic'}</button> : null}
              </div>
              {observedTraffic ? <div className="operations-alert-monitoring" aria-label="Alert monitoring status">
                <span><strong>{metadata?.observed_corridor_count}</strong> road points monitored</span>
                {alertThreshold !== null ? <span>Alert threshold <strong>&gt;{alertThreshold} / 100</strong></span> : null}
                {peakObservedScore !== null ? <span>Highest index <strong>{peakObservedScore} / 100</strong></span> : null}
              </div> : null}
              <div className="operations-alerts__scroll-region"><div className="operations-alerts__list" aria-label="Active planning alerts">
                {activeAlerts.map(alert => <div key={alertObservationKey(alert)} className={`operations-alerts__item${dismissingAlertKeys.has(alertObservationKey(alert)) ? ' is-dismissing' : ''}`}><div className="operations-alerts__item-inner"><OperationsAlertCard alert={alert} dismissing={dismissingAlertKeys.has(alertObservationKey(alert))} onAcknowledge={() => beginAcknowledge(alertObservationKey(alert))} /></div></div>)}
                {!activeAlerts.length ? <div role="status" className="operations-alerts__empty operations-alerts-empty-state">
                  {observedTraffic ? <ShieldCheck size={30} aria-hidden="true" /> : <Info size={30} aria-hidden="true" />}
                  <strong>{!observedTraffic ? 'Traffic monitoring unavailable' : acknowledgedAlerts.length ? 'All current alerts acknowledged' : 'No congestion alerts right now'}</strong>
                  <p>{observedTraffic ? 'Monitoring is active. None of the fresh measured points currently needs your attention.' : 'No fresh observed traffic feed is available. Check traffic again to reconnect.'}</p>
                  {metadata?.input_snapshot_at && observedTraffic ? <time dateTime={metadata.input_snapshot_at}>Traffic snapshot {timestamp(metadata.input_snapshot_at)}</time> : null}
                </div> : null}
              </div><details className="operations-alerts__acknowledged-menu"><summary className="ui-sand-interactive operations-alerts__acknowledged-summary"><span>Acknowledged alerts</span><span className="operations-alerts__acknowledged-count">{acknowledgedAlerts.length}</span><ChevronDown aria-hidden="true" size={ICON_SIZE.medium} /></summary><div className="operations-alerts__acknowledged-list">{acknowledgedAlerts.length ? acknowledgedAlerts.map(alert => <OperationsAlertCard key={alert.id} alert={alert} acknowledged />) : <p>No alerts have been acknowledged.</p>}</div></details></div>
            </section>
          ) : null },
          { id: 'history', label: 'History', content: visitedTabs.has('history') ? (
            <PublishedTrafficHistory observedContent={corridors.length
              ? <div className="glass-panel operations-history-panel"><CongestionHeatmap corridors={corridors} /></div>
              : <div role="status">Historical congestion is unavailable without corridor definitions.</div>} />
          ) : null },
          { id: 'model', label: 'Model', content: visitedTabs.has('model') ? <RealTrafficForecast accessToken={accessToken} showTraining /> : null },
        ]} />
    </div>
  );
};
