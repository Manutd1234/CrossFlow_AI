/* @vitest-environment jsdom */

import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { browserModeledHistoricalProfile } from '../../data/offlineHistory';
import { BUNDLED_RF_VALIDATION, type ModelMetricsWithProvenance } from '../../data/modelManifest';
import { INITIAL_CORRIDORS, MOCK_OPERATIONS } from '../../data/mockData';
import { fetchAuthorityReports, fetchHistoricalCongestion, fetchModelStatus, requestAnalyticsSimulation, fetchRealTrafficForecast } from '../../services/api';
import type { Fetched, HistoricalProfile, ModelMetrics, OperationsSummary } from '../../types';
import { AIModelPanel } from './AIModelPanel';
import { CongestionHeatmap } from './CongestionHeatmap';
import { OperationsAnalytics } from './OperationsAnalytics';
import { observedReport } from '../../test/fixtures/observedBacktest';
import { realTrafficFixture } from '../../test/fixtures/realTraffic';

vi.mock('../../services/api', async () => {
  const actual = await vi.importActual<typeof import('../../services/api')>('../../services/api');
  return {
    ...actual,
    fetchHistoricalCongestion: vi.fn(),
    fetchModelStatus: vi.fn(),
    fetchAuthorityReports: vi.fn(),
    requestAnalyticsSimulation: vi.fn(),
    fetchRealTrafficForecast: vi.fn(),
  };
});

const historyMock = vi.mocked(fetchHistoricalCongestion);
const modelStatusMock = vi.mocked(fetchModelStatus);
const referenceTime = new Date('2026-08-10T05:00:00.000Z');

const browserHistoryResult = (): Fetched<HistoricalProfile> => ({
  data: browserModeledHistoricalProfile('corridor-1', 7, referenceTime),
  source: 'offline',
  fetchedAt: '2026-08-10T12:00:00.000+07:00',
});

const bundledModelResult: Fetched<ModelMetrics> = {
  data: BUNDLED_RF_VALIDATION,
  source: 'offline',
  fetchedAt: '2026-08-10T12:00:00.000+07:00',
};

let root: Root | undefined;

function renderIntoDom(element: ReactElement): HTMLElement {
  const container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
  act(() => root?.render(element));
  return container;
}

function staticText(element: ReactElement): string {
  const container = document.createElement('div');
  container.innerHTML = renderToStaticMarkup(element);
  return container.textContent ?? '';
}

function selectWorkspaceTab(container: HTMLElement, label: string) {
  const tab = Array.from(
    container.querySelectorAll<HTMLButtonElement>('[role="tab"]'),
  ).find(candidate => candidate.textContent?.includes(label));

  expect(tab).toBeDefined();
  act(() => tab?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
}

beforeEach(() => {
  vi.mocked(fetchRealTrafficForecast).mockImplementation(() => new Promise(() => {}));
  historyMock.mockResolvedValue(browserHistoryResult());
  modelStatusMock.mockResolvedValue(bundledModelResult);
  vi.mocked(fetchAuthorityReports).mockResolvedValue({ status: 'unavailable', checked_at: null, fetched_at: null,
    report: null, stored_reports: [], storage: { durable: false, scope: 'test' }, error: null });
  vi.mocked(requestAnalyticsSimulation).mockRejectedValue(new Error('Model service unavailable'));
});

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = undefined;
  document.body.replaceChildren();
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe('operations provenance presentation', () => {
  it('keeps acknowledgement on an unchanged snapshot and resurfaces a later observation for the same road point', async () => {
    vi.useFakeTimers();
    const operationsAt = (timestamp: string, hasAlert = true): OperationsSummary => ({
      ...MOCK_OPERATIONS,
      alerts: hasAlert ? [{ id: 'flow-corridor-1', severity: 'CRITICAL', title: 'Measured road traffic pressure',
        message: 'Observed representative-point congestion.', timestamp }] : [],
      analytics_metadata: { generated_at: timestamp, as_of: timestamp, input_snapshot_at: timestamp,
        snapshot_age_seconds: 0, traffic_observed: true, traffic_source: 'tomtom_live',
        observed_corridor_count: 1, expected_corridor_count: 5, limitations: [] },
    });
    const viewFor = (operations: OperationsSummary) => <OperationsAnalytics operations={operations}
      operationsSnapshot={{ data: operations, source: 'live', fetchedAt: '2026-10-24T01:00:00Z' }}
      corridors={INITIAL_CORRIDORS} />;
    const first = operationsAt('2026-10-24T00:00:00Z');
    const container = renderIntoDom(viewFor(first));
    selectWorkspaceTab(container, 'Alerts');
    act(() => container.querySelector<HTMLButtonElement>('.operations-alert__action')!.click());
    await act(async () => vi.advanceTimersByTimeAsync(220));
    expect(container.querySelectorAll('.operations-alerts__list .operations-alert')).toHaveLength(0);
    expect(container.querySelectorAll('.operations-alerts__acknowledged-list .operations-alert')).toHaveLength(1);

    act(() => root!.render(viewFor({ ...first, alerts: first.alerts.map(alert => ({ ...alert })) })));
    expect(container.querySelectorAll('.operations-alerts__list .operations-alert')).toHaveLength(0);
    act(() => root!.render(viewFor(operationsAt('2026-10-24T00:01:00Z', false))));
    expect(container.textContent).toContain('No congestion alerts right now');
    act(() => root!.render(viewFor(operationsAt('2026-10-24T00:02:00Z'))));
    expect(container.querySelectorAll('.operations-alerts__list .operations-alert')).toHaveLength(1);
    expect(container.querySelectorAll('.operations-alerts__acknowledged-list .operations-alert')).toHaveLength(0);
  });

  it('does not let a pending acknowledgement animation dismiss a newer observation with the same ID', async () => {
    vi.useFakeTimers();
    const first: OperationsSummary = { ...MOCK_OPERATIONS,
      alerts: [{ id: 'flow-corridor-1', severity: 'WARNING', title: 'Measured road traffic pressure',
        message: 'Observed congestion.', timestamp: '2026-10-24T00:00:00Z' }],
      analytics_metadata: { generated_at: '2026-10-24T00:00:00Z', as_of: '2026-10-24T00:00:00Z',
        input_snapshot_at: '2026-10-24T00:00:00Z', snapshot_age_seconds: 0, traffic_observed: true,
        traffic_source: 'tomtom_live', observed_corridor_count: 1, expected_corridor_count: 5, limitations: [] },
    };
    const viewFor = (operations: OperationsSummary) => <OperationsAnalytics operations={operations}
      operationsSnapshot={{ data: operations, source: 'live', fetchedAt: '2026-10-24T00:00:00Z' }}
      corridors={INITIAL_CORRIDORS} />;
    const container = renderIntoDom(viewFor(first));
    selectWorkspaceTab(container, 'Alerts');
    act(() => container.querySelector<HTMLButtonElement>('.operations-alert__action')!.click());
    act(() => root!.render(viewFor({ ...first, alerts: [{ ...first.alerts[0], severity: 'CRITICAL',
      timestamp: '2026-10-24T00:01:00Z' }] })));
    expect(container.querySelector('.operations-alerts__item.is-dismissing')).toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync(220));
    expect(container.querySelectorAll('.operations-alerts__list .operations-alert')).toHaveLength(1);
    expect(container.querySelector('.operations-alerts__list')?.textContent).toContain('CRITICAL');
  });

  it.each([true, false])('distinguishes a healthy empty alert feed from missing observations (observed: %s)', async (observed) => {
    const onRefresh = vi.fn().mockResolvedValue(undefined);
    const operations: OperationsSummary = { ...MOCK_OPERATIONS, alerts: [], bottleneck_threshold: 60,
      current_traffic_samples: observed ? [{ id: 'point-a', name: 'Measured road', score: 2.5,
        current_speed_kmh: 39, free_flow_speed_kmh: 40, provider_confidence: 0.9,
        age_seconds: 0, as_of: '2026-10-24T00:00:00Z' }] : [],
      analytics_metadata: { generated_at: '2026-10-24T00:00:00Z', as_of: '2026-10-24T00:00:00Z',
        input_snapshot_at: observed ? '2026-10-24T00:00:00Z' : null, snapshot_age_seconds: observed ? 0 : null,
        traffic_observed: observed, traffic_source: observed ? 'tomtom_live' : 'unavailable',
        observed_corridor_count: observed ? 1 : 0, expected_corridor_count: 5, limitations: [] } };
    const view = renderIntoDom(<OperationsAnalytics operations={operations}
      operationsSnapshot={{ data: operations, source: 'live', fetchedAt: '2026-10-24T00:00:00Z' }}
      corridors={INITIAL_CORRIDORS} onRefresh={onRefresh} />);
    selectWorkspaceTab(view, 'Alerts');
    const alerts = view.querySelector('#operations-workspace-panel-3')!;
    if (observed) {
      expect(alerts.textContent).toContain('No congestion alerts right now');
      expect(alerts.textContent).toContain('Highest index 2.5 / 100');
      expect(alerts.textContent).toContain('>60 / 100');
    } else {
      expect(alerts.textContent).toContain('Traffic monitoring unavailable');
      expect(alerts.textContent).not.toContain('0 alerts');
    }
    expect(alerts.querySelectorAll('.operations-alert')).toHaveLength(0);
    await act(async () => Array.from(alerts.querySelectorAll<HTMLButtonElement>('button'))
      .find(button => button.textContent === 'Check traffic')!.click());
    expect(onRefresh).toHaveBeenCalledOnce();
  });

  it('labels browser operations and unitless planning charts without API or live claims', () => {
    const view = (
      <OperationsAnalytics
        operations={MOCK_OPERATIONS}
        operationsSnapshot={{
          data: MOCK_OPERATIONS,
          source: 'offline',
          fetchedAt: '2026-08-10T12:00:00.000+07:00',
        }}
        corridors={INITIAL_CORRIDORS}
      />
    );
    const text = staticText(view);
    const markup = renderToStaticMarkup(view);

    expect(markup).toContain('class="app-screen-layout operations-layout"');
    expect(markup).not.toContain('Operations planning data source:');
    expect(markup).not.toContain('workspace-subtabs__rail-accessory');
    expect(markup).not.toContain('operations-header');
    expect(text).not.toContain('OFFICIAL CONTEXT LOADED');
    expect(text).toContain('Batam Authority Operating Context');
    expect(text).not.toContain('2,671,134');
    expect(text).toContain('Batam City Transportation Agency');
    expect(text).not.toContain('38 reported');
    expect(text).not.toContain('32 ATCS and 6 non-ATCS intersections');
    expect(text).not.toContain('PDF p. 64 (report p. 54)');
    expect(text).toContain('Overview');
    expect(text).toContain('Trends');
    expect(text).toContain('Model');
    expect(text).toContain('Alerts');
    expect(text).toContain('History');
    expect(text).toContain('Observed Traffic Index');
    expect(markup).toContain('ui-card-shadow-hover official-evidence-panel');
    expect(text).toContain('Live operational measurements unavailable');
    expect(text).toContain('Measured Emissions Impact');
    expect(text).not.toContain('Modelled scenario to current hour');
    expect(text).toContain('Unavailable');
    expect(text).not.toContain('Model-Generated Dispatch Planning Alerts');
    expect(text).not.toContain('Illustrative Fleet Emissions-Pressure Share');
    expect(markup).toContain('aria-selected="true"');
    expect(text).not.toContain('MODELLED API');
    expect(text).not.toContain('STATIC DEMO');
    expect(text).not.toContain('kg CO2/hr');
    expect(text).not.toContain('City & Port Operator Dispatch Alerts');
    expect(text).not.toContain('TOMTOM');
    expect(text).not.toContain('CO2 Avoided');
  });

  it('labels fields supplied by the operations endpoint as modelled API scenarios', () => {
    const apiOperations: OperationsSummary = {
      ...MOCK_OPERATIONS,
      modeled_avoidable_emissions_opportunity_kg_today: 42,
      modeled_projected_full_day_avoidable_emissions_kg: 84,
      co2_by_vehicle_type: { 'Freight Trucks': 100 },
      hourly_co2_distribution: [
        { hour: '08:00', baseline_co2: 80, optimized_co2: 40 },
      ],
      live_co2_rate_kg_hr: 180,
    };
    const view = (
      <OperationsAnalytics
        operations={apiOperations}
        operationsSnapshot={{
          data: apiOperations,
          source: 'simulated',
          fetchedAt: '2026-08-10T12:00:00.000+07:00',
          provenance: {
            operations: 'CrossFlow modelled operations scenario; not observed or measured',
          },
        }}
        corridors={INITIAL_CORRIDORS}
      />
    );
    const text = staticText(view);
    const markup = renderToStaticMarkup(view);

    expect(markup).not.toContain('Operations planning data source:');
    expect(markup).not.toContain('operations-tabs__planning-badge');
    expect(text).toContain('Observed Traffic Index');
    expect(text).not.toContain('kg CO2/hr');
    expect(text).not.toContain('TOMTOM');
  });

  it('mounts only the active operations workspace panel while switching tabs', async () => {
    const observedOperations: OperationsSummary = { ...MOCK_OPERATIONS,
      alerts: [{ id: 'observed-alert', severity: 'WARNING', title: 'Observed test congestion',
        message: 'A measured test point is above the threshold.', timestamp: '2026-10-23T05:00:00Z' }],
      analytics_metadata: { generated_at: '2026-10-23T05:00:00Z', as_of: '2026-10-23T05:00:00Z',
        input_snapshot_at: '2026-10-23T05:00:00Z', snapshot_age_seconds: 0, traffic_observed: true,
        traffic_source: 'tomtom_live', observed_corridor_count: 1, expected_corridor_count: 5, limitations: [] } };
    const container = renderIntoDom(
      <OperationsAnalytics
        operations={observedOperations}
        operationsSnapshot={{
          data: observedOperations,
          source: 'live',
          fetchedAt: '2026-08-10T12:00:00.000+07:00',
        }}
        corridors={INITIAL_CORRIDORS}
        ferryAndPortsContent={<div>Ferry workspace content</div>}
      />,
    );

    const visiblePanels = () => container.querySelectorAll(
      '.operations-tabs > [role="tabpanel"]:not([hidden])',
    );

    expect(visiblePanels()).toHaveLength(1);
    expect(visiblePanels()[0]?.textContent).toContain('Batam Authority Operating Context');
    expect(container.textContent).not.toContain('Illustrative Fleet Emissions-Pressure Share');

    selectWorkspaceTab(container, 'Trends');
    expect(visiblePanels()).toHaveLength(1);
    expect(visiblePanels()[0]?.textContent).toContain('Live traffic forecasts');
    expect(visiblePanels()[0]?.querySelector('#fleet-chart-summary')).toBeNull();
    expect(visiblePanels()[0]?.querySelector('#emissions-chart-summary')).toBeNull();
    expect(visiblePanels()[0]?.querySelector('#scenario-chart-summary')).toBeNull();
    expect(visiblePanels()[0]?.querySelector('#corridor-carbon-summary')).toBeNull();
    expect(container.textContent).not.toContain('Batam authority operating context');

    selectWorkspaceTab(container, 'Alerts');
    expect(visiblePanels()).toHaveLength(1);
    expect(visiblePanels()[0]?.textContent).toContain('Observed Traffic Alerts');
    expect(visiblePanels()[0]?.textContent).toContain('1 alert');
    expect(visiblePanels()[0]?.querySelector('.operations-alerts__acknowledged-summary')?.classList)
      .toContain('ui-sand-interactive');
    expect(container.textContent).not.toContain('Illustrative Fleet Emissions-Pressure Share');

    const acknowledgeButton = visiblePanels()[0]?.querySelector<HTMLButtonElement>(
      '.operations-alert__action',
    );
    const acknowledgedTitle = acknowledgeButton
      ?.closest('.operations-alert')
      ?.querySelector('.operations-alert__title')
      ?.textContent;
    expect(acknowledgedTitle).toBeTruthy();

    act(() => acknowledgeButton?.click());

    expect(visiblePanels()[0]?.querySelector('.operations-alerts__item.is-dismissing'))
      .not.toBeNull();

    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 240));
    });

    expect(visiblePanels()[0]?.querySelector('.operations-alerts__list')?.textContent)
      .not.toContain(acknowledgedTitle);
    expect(visiblePanels()[0]?.querySelector('.operations-alerts__acknowledged-list')?.textContent)
      .toContain(acknowledgedTitle);
    expect(visiblePanels()[0]?.querySelector('.operations-alerts__acknowledged-count')?.textContent)
      .toBe('1');

    expect(container.querySelectorAll('[role="tab"]')).toHaveLength(6);
    expect(Array.from(container.querySelectorAll('[role="tab"]'))
      .some(tab => tab.textContent === 'Ports')).toBe(true);

    selectWorkspaceTab(container, 'Ports');
    expect(visiblePanels()).toHaveLength(1);
    expect(visiblePanels()[0]?.textContent).toContain('Ferry workspace content');

    selectWorkspaceTab(container, 'History');
    await act(async () => {
      await Promise.resolve();
    });
    expect(visiblePanels()).toHaveLength(1);
    expect(visiblePanels()[0]?.textContent).toContain('Historical Congestion Analysis');
    expect(visiblePanels()[0]?.textContent).not.toContain('Random Forest Congestion Forecasting Engine');
    expect(historyMock).toHaveBeenCalledTimes(1);
    expect(modelStatusMock).not.toHaveBeenCalled();

    selectWorkspaceTab(container, 'Model');
    await act(async () => {
      await Promise.resolve();
    });
    expect(visiblePanels()).toHaveLength(1);
    expect(visiblePanels()[0]?.textContent).toContain('Observed traffic engine');
    expect(visiblePanels()[0]?.textContent).not.toContain('Historical Congestion Analysis');
    expect(fetchRealTrafficForecast).toHaveBeenCalledTimes(2);
    expect(historyMock).toHaveBeenCalledTimes(1);

    selectWorkspaceTab(container, 'Overview');
    expect(visiblePanels()).toHaveLength(1);
    expect(visiblePanels()[0]?.textContent).toContain('Batam Authority Operating Context');
    expect(container.querySelector('#operations-workspace-panel-4')?.textContent)
      .toContain('Historical Congestion Analysis');
    expect(container.querySelector('#operations-workspace-panel-5')?.textContent)
      .toContain('Observed traffic engine');

    selectWorkspaceTab(container, 'History');
    selectWorkspaceTab(container, 'Model');
    expect(fetchRealTrafficForecast).toHaveBeenCalledTimes(2);
    expect(historyMock).toHaveBeenCalledTimes(1);
  });
});

describe('analytics tab continuity', () => {
  it('keeps all six panels navigable when protected forecasts reject an expired session', async () => {
    vi.mocked(fetchRealTrafficForecast).mockRejectedValue(new Error('Authorized operator access is required.'));
    const view = renderIntoDom(<OperationsAnalytics operations={MOCK_OPERATIONS}
      operationsSnapshot={{ data: MOCK_OPERATIONS, source: 'simulated', fetchedAt: '2026-10-24T00:00:00Z' }}
      corridors={INITIAL_CORRIDORS} accessToken="expired-operator-fixture"
      ferryAndPortsContent={<p>Published ferry schedule</p>} />);
    for (const label of ['Overview', 'Ports', 'Trends', 'Alerts', 'History', 'Model']) {
      selectWorkspaceTab(view, label);
      await act(async () => { await Promise.resolve(); });
      const visible = view.querySelectorAll('.operations-tabs > [role="tabpanel"]:not([hidden])');
      expect(visible).toHaveLength(1);
      expect(view.querySelector('.operations-tabs > .workspace-subtabs__rail [aria-selected="true"]')?.textContent).toBe(label);
      expect(visible[0].textContent?.trim().length).toBeGreaterThan(0);
      if (label === 'Trends' || label === 'Model') {
        expect(visible[0].querySelector('[role="alert"]')?.textContent).toContain('Authorized operator access is required.');
        expect(visible[0].querySelector('.real-traffic-actions .badge')?.textContent).toBe('Unavailable');
        expect(visible[0].querySelector('.real-traffic-kpis')).toBeNull();
      }
    }
    expect(fetchRealTrafficForecast).toHaveBeenCalledTimes(2);
  });

  it('retains port selection and a real forecast snapshot across other workspace tabs', async () => {
    vi.mocked(fetchRealTrafficForecast).mockResolvedValue(realTrafficFixture());
    const view = renderIntoDom(<OperationsAnalytics operations={MOCK_OPERATIONS}
      operationsSnapshot={{ data: MOCK_OPERATIONS, source: 'simulated', fetchedAt: '2026-10-24T00:00:00Z' }}
      corridors={INITIAL_CORRIDORS} accessToken="operator-fixture"
      ferryAndPortsContent={<label>Port filter<input defaultValue="All ports" /></label>} />);
    selectWorkspaceTab(view, 'Ports');
    const portFilter = view.querySelector<HTMLInputElement>('input')!;
    portFilter.value = 'Batam Centre';
    selectWorkspaceTab(view, 'Trends');
    await act(async () => { await Promise.resolve(); });
    const forecast = view.querySelector('#operations-workspace-panel-2 .real-traffic-kpis');
    expect(forecast?.textContent).toContain('20.0 / 100');
    selectWorkspaceTab(view, 'Overview');
    selectWorkspaceTab(view, 'Trends');
    expect(view.querySelector('#operations-workspace-panel-2 .real-traffic-kpis')).toBe(forecast);
    expect(fetchRealTrafficForecast).toHaveBeenCalledOnce();
    selectWorkspaceTab(view, 'Ports');
    expect(view.querySelector('input')).toBe(portFilter);
    expect(portFilter.value).toBe('Batam Centre');
    expect(view.querySelectorAll('.operations-tabs > [role="tabpanel"]:not([hidden])')).toHaveLength(1);
  });

  it('provides a reconnect state when port data has not been supplied', () => {
    const view = renderIntoDom(<OperationsAnalytics operations={MOCK_OPERATIONS} operationsSnapshot={null} corridors={[]} />);
    selectWorkspaceTab(view, 'Ports');
    expect(view.querySelector('#operations-workspace-panel-1 [role="status"]')?.textContent)
      .toContain('Ferry and port data is unavailable');
  });
});

describe('history continuity presentation', () => {
  it('shows a loading state before resolving to the neutral browser baseline', async () => {
    let resolveHistory: ((result: Fetched<HistoricalProfile>) => void) | undefined;
    historyMock.mockReturnValueOnce(new Promise(resolve => {
      resolveHistory = resolve;
    }));

    const container = renderIntoDom(<CongestionHeatmap corridors={INITIAL_CORRIDORS} />);
    expect(container.textContent).toContain('LOADING HISTORY');
    expect(container.textContent).not.toContain('OFFLINE');

    await act(async () => {
      resolveHistory?.(browserHistoryResult());
      await Promise.resolve();
    });

    expect(container.textContent).toContain('BROWSER-MODELLED BASELINE');
    expect(container.textContent).toContain('Browser model');
    expect(container.textContent).toContain('0 observed samples');
    expect(container.querySelector('[aria-label="History provenance"]')).toBeNull();
    expect(container.querySelector('.badge-critical')).toBeNull();

    const corridorFilter = container.querySelector('[aria-label="Corridor history"]');
    const timeFilter = container.querySelector('[aria-label="History window"]');
    expect(corridorFilter?.parentElement).toBe(timeFilter?.parentElement);
    expect(corridorFilter?.parentElement).toHaveProperty(
      'className',
      'operations-history__filters',
    );
    expect(corridorFilter?.nextElementSibling).toBe(timeFilter);
    expect(timeFilter?.querySelectorAll('.operations-history__window-button')).toHaveLength(4);
    expect(timeFilter?.querySelectorAll('.operations-history__window-button.ui-sand-interactive')).toHaveLength(4);
    expect(corridorFilter?.querySelectorAll('.operations-history__corridor-button.ui-sand-interactive').length)
      .toBeGreaterThan(0);
    expect(container.querySelector('.operations-history__charts')?.children).toHaveLength(2);

    const chartCards = container.querySelectorAll('.operations-history-chart-card');
    expect(chartCards).toHaveLength(2);
    expect(chartCards[0]?.querySelector('.operations-history__chart-title')).not.toBeNull();
    expect(chartCards[0]?.querySelector('.operations-history-bars__axis')).not.toBeNull();
    expect(chartCards[0]?.querySelector('.operations-history-legend')).not.toBeNull();
    expect(chartCards[1]?.querySelector('.operations-history__chart-title')).not.toBeNull();
    expect(chartCards[1]?.querySelector('.operations-history-trend__labels')).not.toBeNull();
    expect(chartCards[1]?.querySelector('.operations-history-legend')).not.toBeNull();
    expect(container.querySelectorAll('.operations-history-chart-plot')).toHaveLength(2);

    const hourLabels = container.querySelectorAll('.operations-history-bars__axis span');
    expect(hourLabels).toHaveLength(24);
    expect(hourLabels[0]?.textContent).toBe('12am');
    expect(hourLabels[3]?.textContent).toBe('3am');
    expect(hourLabels[12]?.textContent).toBe('12pm');
    expect(hourLabels[15]?.textContent).toBe('3pm');

    const barHeights = Array.from(
      container.querySelectorAll<HTMLElement>('.operations-history-bars__bar'),
      bar => Number.parseFloat(bar.style.height),
    );
    expect(Math.max(...barHeights)).toBe(160);

    const trendSvg = container.querySelector('.operations-history-trend__svg');
    expect(trendSvg?.getAttribute('viewBox')).toBe('0 0 100 100');
    expect(trendSvg?.querySelector('linearGradient')?.getAttribute('gradientUnits'))
      .toBe('userSpaceOnUse');
    expect(trendSvg?.querySelectorAll('path')).toHaveLength(2);
    expect(trendSvg?.querySelectorAll('path')[1]?.getAttribute('d')).toContain('20.00');
    expect(container.querySelector('[aria-label="Daily trend legend"]')?.textContent)
      .toContain('Daily average congestion index');

    const firstHourlyColumn = container.querySelector('.operations-history-bars__column');
    act(() => firstHourlyColumn?.dispatchEvent(new MouseEvent('mouseover', {
      bubbles: true,
      clientX: 120,
      clientY: 160,
    })));
    expect(document.querySelector('.operations-history-chart-tooltip')?.textContent)
      .toContain('12am');
    expect(document.querySelector('.operations-history-chart-tooltip')?.textContent)
      .toContain('Average congestion index:');
    expect(document.querySelector('.operations-history-chart-tooltip .operations-chart-tooltip__title'))
      .not.toBeNull();
    expect(document.querySelector('.operations-history-chart-tooltip .operations-chart-tooltip__body'))
      .not.toBeNull();

    act(() => firstHourlyColumn?.dispatchEvent(new MouseEvent('mouseout', {
      bubbles: true,
      relatedTarget: container,
    })));
    expect(document.querySelector('.operations-history-chart-tooltip')).toBeNull();
  });

  it('uses metadata to identify a mixed observed and modelled API window', async () => {
    historyMock.mockResolvedValueOnce({
      source: 'simulated',
      fetchedAt: '2026-08-10T12:00:00+07:00',
      data: {
        corridor_id: 'corridor-1',
        days_requested: 7,
        hourly_profile: Array.from({ length: 24 }, (_, hour) => ({
          hour,
          avg_score: 35 + hour,
          sample_count: 7,
        })),
        weekly_trend: [{ date: '2026-08-10', avg_score: 47, sample_count: 170 }],
        history_metadata: {
          window_days: 7,
          observed: false,
          contains_observed_samples: true,
          source_counts: { synthetic: 168, tomtom_live: 2 },
          sources: {
            synthetic: { sample_count: 168, observed: false },
            tomtom_live: { sample_count: 2, observed: true },
          },
          latest_sample_at: '2026-08-10T12:00:00+07:00',
          latest_sample_age_seconds: 0,
          freshness: 'current',
          freshness_basis: 'latest sample in the requested history window',
          storage: {
            engine: 'sqlite',
            durability: 'ephemeral_instance_file',
            durable: false,
            shared_across_instances: false,
            fallback_to_memory: false,
          },
          synthetic_seed: {
            source: 'synthetic',
            version: 3,
            days: 14,
            timezone: 'WIB (UTC+07:00)',
            generated_for_date: '2026-08-10',
            observed: false,
          },
        },
      },
    });

    const container = renderIntoDom(<CongestionHeatmap corridors={INITIAL_CORRIDORS} />);
    await act(async () => {
      await Promise.resolve();
    });

    expect(container.textContent).toContain('MIXED HISTORY');
    expect(container.textContent).toContain('2 observed of 170 stored samples');
    expect(container.querySelector('.operations-history-stat__value--cyan')?.textContent).toBe('Mixed');
  });

  it('limits the daily trend to the selected history window', async () => {
    const thirtyDayTrend = Array.from({ length: 30 }, (_, index) => ({
      date: `2026-07-${String(index + 1).padStart(2, '0')}`,
      avg_score: 20 + index,
      sample_count: 24,
    }));
    const historyForDays = (days: number): Fetched<HistoricalProfile> => ({
      source: 'simulated',
      fetchedAt: '2026-08-10T12:00:00+07:00',
      data: {
        corridor_id: 'corridor-1',
        days_requested: days,
        hourly_profile: Array.from({ length: 24 }, (_, hour) => ({
          hour,
          avg_score: 30 + hour,
          sample_count: days,
        })),
        // Mirrors the current API behavior: it returns 30 trend days even
        // when the requested hourly/profile window is shorter.
        weekly_trend: thirtyDayTrend,
        history_metadata: {
          ...browserModeledHistoricalProfile('corridor-1', days, referenceTime)
            .history_metadata,
        },
      },
    });
    historyMock.mockImplementation(async (_corridorId, days = 7) => historyForDays(days));

    const container = renderIntoDom(<CongestionHeatmap corridors={INITIAL_CORRIDORS} />);
    await act(async () => {
      await Promise.resolve();
    });

    const firstTrendPath = container.querySelectorAll(
      '.operations-history-trend__svg path',
    )[1]?.getAttribute('d');
    expect(container.querySelector('.operations-history-trend__labels')?.firstElementChild?.textContent)
      .toBe('07-24');

    const fourteenDayButton = container.querySelector<HTMLButtonElement>(
      '.operations-history__window-button[aria-label="Show the last 14 days"]',
    );
    await act(async () => {
      fourteenDayButton?.click();
      await Promise.resolve();
    });

    const secondTrendPath = container.querySelectorAll(
      '.operations-history-trend__svg path',
    )[1]?.getAttribute('d');
    expect(historyMock).toHaveBeenLastCalledWith('corridor-1', 14, false);
    expect(container.querySelector('.operations-history-trend__labels')?.firstElementChild?.textContent)
      .toBe('07-17');
    expect(secondTrendPath).not.toBe(firstTrendPath);
  });

  it('keeps the current history layout mounted while a filter refresh loads', async () => {
    let resolveRefresh: ((result: Fetched<HistoricalProfile>) => void) | undefined;
    historyMock
      .mockResolvedValueOnce(browserHistoryResult())
      .mockReturnValueOnce(new Promise(resolve => {
        resolveRefresh = resolve;
      }));

    const container = renderIntoDom(<CongestionHeatmap corridors={INITIAL_CORRIDORS} />);
    await act(async () => {
      await Promise.resolve();
    });

    const initialChartCount = container.querySelectorAll('.operations-history-chart-card').length;
    const initialTrendPath = container.querySelectorAll(
      '.operations-history-trend__svg path',
    )[1]?.getAttribute('d');
    const todayButton = container.querySelector<HTMLButtonElement>(
      '.operations-history__window-button[aria-label="Show today"]',
    );

    act(() => todayButton?.click());

    expect(container.querySelector('.operations-history')?.getAttribute('aria-busy')).toBe('true');
    expect(container.querySelectorAll('.operations-history-chart-card')).toHaveLength(initialChartCount);
    expect(container.querySelectorAll('.operations-history-trend__svg path')[1]?.getAttribute('d'))
      .toBe(initialTrendPath);
    expect(container.querySelector('.operations-history__summary')).not.toBeNull();
    expect(container.querySelector('[aria-label="History provenance"]')).toBeNull();

    await act(async () => {
      resolveRefresh?.({
        ...browserHistoryResult(),
        data: browserModeledHistoricalProfile('corridor-1', 1, referenceTime),
      });
      await Promise.resolve();
    });

    expect(container.querySelector('.operations-history')?.getAttribute('aria-busy')).toBe('false');
  });
});

describe('forecast model continuity presentation', () => {
  it('shows raw-forest validation and feature metrics supplied by the current backend', async () => {
    modelStatusMock.mockResolvedValueOnce({ ...bundledModelResult, source: 'simulated' });
    const container = renderIntoDom(<AIModelPanel />);
    await act(async () => {
      await Promise.resolve();
    });

    expect(container.textContent).toContain('BACKEND RF · SYNTHETIC');
    expect(container.textContent).toContain('API MODEL STATUS');
    expect(container.querySelectorAll('.operations-model-panel__badges .badge')).toHaveLength(2);
    expect(container.querySelector('.operations-model-panel__description')).toBeNull();
    expect(container.textContent).toContain('synthetic profile generator: 4,000');
    expect(container.textContent).not.toContain('Admin retraining');
    expect(container.textContent).toContain('0.9490');
    expect(container.textContent).toContain('66.13% on the declared holdout (target 80%)');
    expect(container.textContent).toContain('Empirical ensemble CRPS: 2.223');
    expect(container.textContent).toContain('Official ATCS inventory');
    const modelProgressBars = Array.from(container.querySelectorAll<HTMLElement>('[role="progressbar"]'));
    expect(modelProgressBars).toHaveLength(9);
    expect(container.textContent).toContain('Ferry Surge Feature (Training Forest)');
    expect(container.querySelector('[aria-label="Road Class (One-hot) importance"]')?.getAttribute('aria-valuenow')).toBe('0.3');
    expect(modelProgressBars.every(bar => bar.classList.contains('ui-progress-track'))).toBe(true);
    expect(modelProgressBars.every(bar => bar.firstElementChild?.classList.contains('ui-progress-fill'))).toBe(true);
    expect(container.querySelector('.operations-model-panel__provenance a')?.textContent).toContain('Official ATCS inventory');
    expect(container.querySelector('[aria-label="Observed-data chronological backtest"]')).toBeNull();
    expect(container.textContent).not.toContain('UNAVAILABLE');
    expect(container.querySelector('.badge-critical')).toBeNull();
  });

  it('uses backend training metadata for a mixed-history retrain', async () => {
    const mixedHistoryMetrics: ModelMetricsWithProvenance = {
      ...BUNDLED_RF_VALIDATION,
      retraining_enabled: true,
      total_samples: 120,
      last_trained_at: '2026-08-10T05:00:00.000Z',
      training_data_source: 'history_store_mixed',
      validation_scope: 'history_holdout_mixed',
      training_source_counts: { synthetic: 100, tomtom_live: 20 },
      observed_training_rows: 20,
    };
    modelStatusMock.mockResolvedValueOnce({
      data: mixedHistoryMetrics,
      source: 'simulated',
      fetchedAt: '2026-08-10T12:00:00.000+07:00',
    });

    const container = renderIntoDom(<AIModelPanel />);
    await act(async () => {
      await Promise.resolve();
    });

    expect(container.textContent).toContain('BACKEND RF · MIXED HISTORY');
    expect(container.textContent).toContain('20 of 120 rows are tagged observed');
    expect(container.textContent).toContain('synthetic: 100');
    expect(container.textContent).toContain('tomtom live: 20');
    expect(container.textContent).toContain('Mixed-history holdout R²');
    expect(container.textContent).not.toContain('Admin retraining');
    expect(container.textContent).not.toContain('These metrics measure fit to the deterministic synthetic profile generator');
  });

  it.each([
    ['validated_spatial_observed', 'chronological_spatial_holdout_observed', 'OBSERVED HISTORY', 'Observed-history', 120],
    ['validated_spatial_mixed', 'chronological_spatial_holdout_mixed', 'MIXED HISTORY', 'Mixed-history', 20],
    ['validated_spatial_modelled', 'chronological_spatial_holdout_modelled', 'NON-OBSERVED HISTORY', 'Non-observed history', 0],
  ] as const)('renders current spatial training provenance %s and the API metrics', async (
    trainingDataSource, validationScope, badge, label, observedRows,
  ) => {
    const metrics: ModelMetricsWithProvenance = {
      ...BUNDLED_RF_VALIDATION,
      r2_score: 0.71, mae: 4.25, rmse: 5.12, total_samples: 120,
      training_data_source: trainingDataSource, validation_scope: validationScope,
      observed_training_rows: observedRows,
      training_source_counts: { probe_gps: observedRows, synthetic: 120 - observedRows },
      calibration: { interval_coverage_target: 0.8, interval_coverage_observed: 0.612,
        empirical_crps: 3.125, holdout_samples: 24 },
    };
    modelStatusMock.mockResolvedValueOnce({ data: metrics, source: 'simulated',
      fetchedAt: '2026-10-23T05:00:00Z' });
    const container = renderIntoDom(<AIModelPanel />);
    await act(async () => { await Promise.resolve(); });
    expect(container.textContent).toContain(`BACKEND RF · ${badge}`);
    expect(container.textContent).toContain(`${label} holdout R²`);
    expect(container.textContent).toContain(`${observedRows} of 120 rows are tagged observed`);
    expect(container.textContent).toContain('0.7100');
    expect(container.textContent).toContain('61.20% on the declared holdout (target 80%)');
    expect(container.textContent).not.toContain('0.9490');
    expect(container.textContent).not.toContain('PROVENANCE UNDECLARED');
    expect(container.textContent).not.toContain('fit to the deterministic synthetic profile generator');
  });

  it('treats mismatched source and validation metadata as unverified', async () => {
    modelStatusMock.mockResolvedValueOnce({ data: {
      ...BUNDLED_RF_VALIDATION, training_data_source: 'validated_spatial_observed',
      validation_scope: 'synthetic_holdout',
    } as ModelMetricsWithProvenance, source: 'simulated', fetchedAt: '2026-10-23T05:00:00Z' });
    const container = renderIntoDom(<AIModelPanel />);
    await act(async () => { await Promise.resolve(); });
    expect(container.textContent).toContain('BACKEND RF · PROVENANCE UNDECLARED');
    expect(container.textContent).not.toContain('BACKEND RF · OBSERVED HISTORY');
    expect(container.textContent).not.toContain('interval coverage');
  });

  it('recognizes the purged observed serving model and separates fit, calibration and final evaluation rows', async () => {
    const trainingReport = observedReport(true);
    trainingReport.status = 'activated'; trainingReport.model_replaced = true;
    trainingReport.activation = { ...trainingReport.activation, eligible: true, reasons: [] };
    const metrics: ModelMetricsWithProvenance = {
      ...BUNDLED_RF_VALIDATION, total_samples: 300, r2_score: 0.48, mae: 4.25, rmse: 5.125,
      training_data_source: 'validated_spatial_observed', validation_scope: 'purged_chronological_observed_holdout',
      evaluation_scope: 'observed_serving_pipeline_weather_clear_ferry_unknown',
      observed_training_rows: 180, observed_fit_rows: 120, observed_calibration_rows: 60, observed_evaluation_rows: 120,
      observed_evaluation_horizon_minutes: 30,
      training_source_counts: { tomtom_live: 180 },
      observed_training: trainingReport,
      calibration: { interval_coverage_target: 0.8, interval_coverage_observed: 0.625, holdout_samples: 120,
        calibration_rows: 60, calibration_forecast_pairs: 180, coverage_guaranteed: false },
    };
    modelStatusMock.mockResolvedValueOnce({ data: metrics, source: 'simulated', fetchedAt: '2026-10-23T05:00:00Z' });
    const container = renderIntoDom(<AIModelPanel accessToken="operator-session" />);
    await act(async () => { await Promise.resolve(); });
    expect(container.textContent).toContain('BACKEND RF · OBSERVED HISTORY');
    expect(container.textContent).toContain('Purged observed holdout R²');
    expect(container.textContent).toContain('180 observed rows for fit/calibration; 120 evaluation forecast pairs');
    expect(container.textContent).toContain('Fit rows: 120; calibration rows: 60; final evaluation forecast pairs: 120');
    expect(container.textContent).toContain('R², MAE and RMSE use 30-minute observed targets');
    expect(container.textContent).toContain('180 calibration forecast pairs');
    expect(container.textContent).toContain('Holdout: 120 observed targets');
    expect(container.textContent).toContain('unknown ferry proximity');
    expect(container.textContent).toContain('Temporal dependence prevents a coverage guarantee');
    expect(container.textContent).toContain('Serving model’s recorded training window');
    expect(container.querySelector('time[datetime="2026-10-15T08:00:00Z"]')).not.toBeNull();
    expect(container.textContent).not.toContain('BACKEND RF · SYNTHETIC');
    expect(container.textContent).not.toContain('180 of 300 rows are tagged observed');
  });

  it('does not guess whether a legacy backend model is synthetic or observed', async () => {
    const undeclaredMetrics: ModelMetrics = {
      ...BUNDLED_RF_VALIDATION,
      last_trained_at: '2026-08-10T05:00:00.000Z',
    };
    delete (undeclaredMetrics as ModelMetricsWithProvenance).training_data_source;
    delete (undeclaredMetrics as ModelMetricsWithProvenance).validation_scope;
    delete (undeclaredMetrics as ModelMetricsWithProvenance).training_source_counts;
    delete (undeclaredMetrics as ModelMetricsWithProvenance).observed_training_rows;
    modelStatusMock.mockResolvedValueOnce({
      data: undeclaredMetrics,
      source: 'simulated',
      fetchedAt: '2026-08-10T12:00:00.000+07:00',
    });

    const container = renderIntoDom(<AIModelPanel />);
    await act(async () => {
      await Promise.resolve();
    });

    expect(container.textContent).toContain('BACKEND RF · PROVENANCE UNDECLARED');
    expect(container.textContent).toContain('treated as unverified rather than assumed synthetic or observed');
    expect(container.textContent).not.toContain('BACKEND RF · SYNTHETIC');
  });
});
