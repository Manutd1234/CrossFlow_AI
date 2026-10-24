/* @vitest-environment jsdom */

import { act, type ReactElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import L from 'leaflet';
import { MapView } from './MapView';
import { corridorColor } from '../../utils/format';
import type { Corridor, CorridorRoute, Fetched, LiveTrafficData } from '../../types';

const leafletTestState = vi.hoisted(() => ({
  corridorClickHandlers: [] as Array<() => void>,
  corridorLayers: [] as Array<Record<string, ReturnType<typeof vi.fn>>>,
  hotspotClickHandlers: [] as Array<() => void>,
  map: vi.fn(),
  mapInstance: { fitBounds: vi.fn() } as { fitBounds: ReturnType<typeof vi.fn> },
}));

vi.mock('leaflet', () => {
  const makeLayer = (clickHandlers?: Array<() => void>) => {
    const layer = {
      addTo: vi.fn(),
      bindTooltip: vi.fn(),
      bringToFront: vi.fn(),
      getBounds: vi.fn(() => ({ pad: vi.fn(() => 'bounds') })),
      on: vi.fn(),
      openTooltip: vi.fn(),
      remove: vi.fn(),
      setStyle: vi.fn(),
    };
    layer.addTo.mockReturnValue(layer);
    layer.bindTooltip.mockReturnValue(layer);
    layer.on.mockImplementation((event: string, handler: () => void) => {
      if (event === 'click') clickHandlers?.push(handler);
      return layer;
    });
    return layer;
  };

  const map = {
    fitBounds: vi.fn(),
    getZoom: vi.fn(() => 11),
    remove: vi.fn(),
    scrollWheelZoom: {
      disable: vi.fn(),
      enable: vi.fn(),
    },
    setView: vi.fn(),
  };
  map.setView.mockReturnValue(map);
  leafletTestState.map.mockImplementation(() => map);
  leafletTestState.mapInstance = map;

  return {
    default: {
      circle: vi.fn(() => makeLayer(leafletTestState.hotspotClickHandlers)),
      circleMarker: vi.fn(() => makeLayer()),
      control: { zoom: vi.fn(() => makeLayer()) },
      map: leafletTestState.map,
      polyline: vi.fn(() => {
        const layer = makeLayer(leafletTestState.corridorClickHandlers);
        leafletTestState.corridorLayers.push(layer);
        return layer;
      }),
      tileLayer: vi.fn(() => makeLayer()),
    },
  };
});

let root: Root | undefined;

function observedTraffic(score: number, at = Date.now(), secondsUntilExpiry = 60): NonNullable<Corridor['observed_traffic']> {
  const observedAt = new Date(at - 5_000).toISOString();
  const freshUntil = new Date(at + secondsUntilExpiry * 1_000).toISOString();
  return {
    status: 'available', source: 'tomtom_live', scope: 'representative_provider_point',
    condition: score <= 5 ? 'FREE_FLOW' : score < 40 ? 'SLOWING' : score < 70 ? 'HEAVY' : 'CRITICAL',
    condition_policy: 'observed_speed_loss_bands_v1', provider_confidence: 0.9,
    current_speed_kmh: 50 * (1 - score / 100), free_flow_speed_kmh: 50,
    checked_at: new Date(at).toISOString(), observed_at: observedAt, fresh_until: freshUntil,
    congestion_index: score, delay_mins: null, provider_point_count: 1, camera_count: 0,
    camera_congestion_index: null, camera_condition: null, camera_warning: false, camera_fresh_until: null,
    corridor_wide: false,
    forecast_30m: score, forecast_60m: score, forecast_method: 'persistence',
    forecast_observed_at: observedAt, forecast_fresh_until: freshUntil,
  };
}

const TEST_CORRIDORS: Corridor[] = [
  {
    id: 'corridor-1',
    name: 'Mukakuning Industrial -> Batam Centre Terminal',
    distance_km: 9.91,
    base_time_mins: 18,
    live_congestion_score: 72,
    delay_mins: 8,
    status: 'CRITICAL',
    risk_level: 'HIGH',
    forecast_30m: 78,
    forecast_60m: 81,
    trend: 'UPWARD',
    key_checkpoints: ['Mukakuning Gate', 'Batam Centre Ferry'],
    observed_traffic: observedTraffic(72),
  },
];

const EMPTY_ROUTES: CorridorRoute[] = [];

function corridorView(corridors: Corridor[], onSelectCorridor = vi.fn(), canPlanRoutes = true): ReactElement {
  return <MapView corridors={corridors} routes={EMPTY_ROUTES} trafficSnapshot={null}
    onSelectCorridor={onSelectCorridor} canPlanRoutes={canPlanRoutes} />;
}

function showCorridorTab(container: HTMLElement): void {
  const tabs = container.querySelectorAll<HTMLButtonElement>('[aria-label="Corridor map sections"] [role="tab"]');
  act(() => tabs[1].click());
}

function latestCorridorStyle(): { color?: string; weight?: number } | undefined {
  return leafletTestState.corridorLayers[1]?.setStyle.mock.calls.slice(-1)[0]?.[0];
}

function latestCorridorTooltip(): string {
  return (leafletTestState.corridorLayers[1]?.bindTooltip.mock.calls.slice(-1)[0]?.[0] as HTMLElement | undefined)?.textContent ?? '';
}

function trafficSnapshot(selectionScore: number): Fetched<LiveTrafficData> {
  return {
    source: 'simulated',
    fetchedAt: '2026-08-13T12:00:00+07:00',
    data: {
      segments: [],
      zones: [{
        zone_id: 'zone-simpang-jam',
        name: 'Simpang Jam / Laluan Madani',
        category: 'Urban junction',
        lat: 1.122424,
        lng: 104.019539,
        radius_m: 650,
        congestion_index: 63.4,
        level: 'HEAVY',
        color: '#f59e0b',
        avoid_recommended: false,
        watch_priority: 'HEAVY',
        selection_rank: 1,
        selection_score: selectionScore,
        source: 'modelled_spatial_hotspot',
        modeled_emissions_pressure: {
          index: 40.2,
          queue_pressure_factor: 0.402,
          level: 'ELEVATED',
          metric: 'relative_queue_emissions_pressure',
          unit: 'index_0_100',
          observed: false,
        },
      }],
      overall_source: 'simulated',
      tomtom_key_configured: false,
    },
  };
}

function renderIntoDom(element: ReactElement): HTMLElement {
  const container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
  act(() => root?.render(element));
  return container;
}

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = undefined;
  leafletTestState.corridorClickHandlers.length = 0;
  leafletTestState.corridorLayers.length = 0;
  leafletTestState.hotspotClickHandlers.length = 0;
  leafletTestState.map.mockClear();
  leafletTestState.mapInstance.fitBounds.mockClear();
  vi.mocked(L.polyline).mockClear();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  document.body.replaceChildren();
});

describe('MapView traffic overlay controls', () => {
  it('does not render fallback corridors while backend route geometry is loading', () => {
    const container = renderIntoDom(
      <MapView
        corridors={TEST_CORRIDORS}
        routes={[]}
        routesLoading
        trafficSnapshot={null}
        onSelectCorridor={vi.fn()}
      />,
    );

    // No corridor casing or endpoint fallback is added until the backend
    // request settles.
    expect(L.polyline).not.toHaveBeenCalled();

    act(() => root?.render(
      <MapView
        corridors={TEST_CORRIDORS}
        routes={[]}
        routesLoading={false}
        trafficSnapshot={null}
        onSelectCorridor={vi.fn()}
      />,
    ));

    expect(container).not.toBeNull();
    expect(L.polyline).toHaveBeenCalledTimes(10);
    expect(vi.mocked(L.polyline).mock.calls[1][1]).toMatchObject({ dashArray: '8, 6' });
  });

  it('disables wheel zoom in the small-width layout', () => {
    const mediaQuery = {
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    };
    vi.stubGlobal('matchMedia', vi.fn(() => mediaQuery));

    renderIntoDom(
      <MapView
        corridors={[]}
        routes={[]}
        trafficSnapshot={null}
        onSelectCorridor={vi.fn()}
      />,
    );

    expect(leafletTestState.map).toHaveBeenCalledWith(
      expect.any(HTMLElement),
      expect.objectContaining({ scrollWheelZoom: false }),
    );
    expect(mediaQuery.addEventListener).toHaveBeenCalledWith(
      'change',
      expect.any(Function),
    );
  });

  it('names and reports the combined planning-watch and traffic overlays', () => {
    const container = renderIntoDom(
      <MapView
        corridors={[]}
        routes={[]}
        trafficSnapshot={null}
        onSelectCorridor={vi.fn()}
      />,
    );

    const mapPanel = container.querySelector<HTMLElement>('.map-canvas-panel');
    const mapLayout = container.querySelector('.map-view-layout');
    expect(mapLayout?.classList.contains(
      'app-screen-layout',
    )).toBe(true);
    expect(mapLayout?.querySelector(':scope > .workspace-subtabs__rail')).not.toBeNull();
    expect(mapLayout?.querySelector(':scope > .workspace-subtabs__panel')).not.toBeNull();
    expect(container.querySelector('.map-view-sidebar')).toBeNull();
    expect(mapPanel?.style.height).toBe('');
    expect(mapPanel?.closest('[role="tabpanel"]')).toBeNull();
    expect(container.querySelectorAll('.hotspot-card')).toHaveLength(30);
    expect(container.querySelectorAll('.hotspot-card.ui-sand-interactive')).toHaveLength(30);
    expect(container.querySelectorAll('.hotspot-card__credit-meta')).toHaveLength(30);
    expect(container.querySelectorAll('.hotspot-card__credit-meta a')).toHaveLength(60);
    expect(container.querySelector<HTMLElement>('.hotspot-watch__list')?.tabIndex).toBe(0);
    expect(container.querySelector('.hotspot-watch__list')?.getAttribute('aria-label'))
      .toContain('Scrollable');
    expect(container.textContent).toContain('30-Area Congestion Watch');
    expect(container.textContent).toContain('20 critical');
    expect(container.textContent).toContain('10 heavy');
    expect(L.polyline).toHaveBeenCalledTimes(10);
    expect(leafletTestState.corridorClickHandlers).toHaveLength(5);

    const tabs = Array.from(
      container.querySelectorAll<HTMLButtonElement>('[aria-label="Corridor map sections"] [role="tab"]'),
    );
    act(() => leafletTestState.corridorClickHandlers[0]?.());
    expect(tabs[1].getAttribute('aria-selected')).toBe('true');
    expect(document.activeElement).toBe(tabs[1]);

    const toggle = container.querySelector<HTMLButtonElement>('.map-traffic-toggle');
    expect(toggle?.getAttribute('aria-pressed')).toBe('true');
    expect(toggle?.hasAttribute('aria-controls')).toBe(false);
    expect(toggle?.getAttribute('aria-label'))
      .toBe('Hide planning-watch overlays');
    expect(toggle?.textContent).toBe('');
    expect(toggle?.querySelector('svg')?.getAttribute('width')).toBe('18');
    expect(container.querySelector('.map-radar-card .map-traffic-toggle')).toBeNull();

    expect(container.querySelector('#traffic-overlay-layer-status')).toBeNull();

    act(() => toggle?.click());
    expect(toggle?.getAttribute('aria-pressed')).toBe('false');
    expect(toggle?.getAttribute('aria-label'))
      .toBe('Show planning-watch overlays');
    expect(toggle?.textContent).toBe('');
  });

  it('keeps the map visible while corridor details and feeds share one tab', () => {
    const container = renderIntoDom(
      <MapView
        corridors={TEST_CORRIDORS}
        routes={[]}
        trafficSnapshot={null}
        onSelectCorridor={vi.fn()}
      />,
    );

    const tabs = Array.from(
      container.querySelectorAll<HTMLButtonElement>('[aria-label="Corridor map sections"] [role="tab"]'),
    );
    const mapPanel = container.querySelector<HTMLElement>('.map-canvas-panel');

    expect(tabs.map(tab => tab.textContent)).toEqual([
      'Hotspots',
      'Corridor',
    ]);
    expect(tabs[0].getAttribute('aria-selected')).toBe('true');
    expect(mapPanel?.closest('[role="tabpanel"]')).toBeNull();

    act(() => tabs[1].click());
    const feedButton = container.querySelector<HTMLButtonElement>(
      '[aria-label^="Select Mukakuning Industrial"]',
    );
    expect(feedButton?.classList.contains('ui-sand-interactive')).toBe(true);
    act(() => feedButton?.click());

    expect(tabs[1].getAttribute('aria-selected')).toBe('true');
    expect(document.activeElement).toBe(tabs[1]);
    expect(container.querySelector('#selected-corridor-title')?.textContent)
      .toContain('Mukakuning Industrial');
    expect(container.querySelector('#corridor-feed-title')).not.toBeNull();
    expect(container.querySelector('.map-corridor-stack')?.children).toHaveLength(2);
    expect(container.querySelector<HTMLElement>('.corridor-feed-list')?.tabIndex).toBe(0);
    expect(container.querySelector('.corridor-feed-list')?.getAttribute('aria-label'))
      .toContain('Scrollable');
    expect(mapPanel?.closest('[hidden]')).toBeNull();

    act(() => leafletTestState.hotspotClickHandlers[0]?.());

    expect(tabs[0].getAttribute('aria-selected')).toBe('true');
    expect(container.querySelector('.hotspot-card.is-focused')?.id)
      .toBe('hotspot-card-zone-simpang-jam');
  });

  it('keeps corridor road geometry coloured by congestion across both subtabs', () => {
    const secondCorridor: Corridor = {
      id: 'corridor-2',
      name: 'Batu Ampar Freight Port -> Batam Centre Ferry',
      distance_km: 8.4,
      base_time_mins: 16,
      live_congestion_score: 31,
      delay_mins: 2,
      status: 'SMOOTH',
      risk_level: 'LOW',
      forecast_30m: 29,
      forecast_60m: 27,
      trend: 'STABLE',
      key_checkpoints: ['Batu Ampar Gate 2', 'Batam Centre'],
      observed_traffic: observedTraffic(31),
    };
    const container = renderIntoDom(
      <MapView
        corridors={[...TEST_CORRIDORS, secondCorridor]}
        routes={[{
          id: 'corridor-1',
          name: 'Mukakuning Industrial -> Batam Centre Terminal',
          distance_km: 9.91,
          straight_line_km: 7.2,
          detour_ratio: 1.38,
          geometry: [[1.0605, 104.0303], [1.1, 104.04], [1.1318, 104.0554]],
        }]}
        trafficSnapshot={null}
        onSelectCorridor={vi.fn()}
      />,
    );

    // One casing plus one line for each corridor is already present while the
    // Hotspots tab is active.
    expect(L.polyline).toHaveBeenCalledTimes(10);

    const tabs = Array.from(
      container.querySelectorAll<HTMLButtonElement>('[aria-label="Corridor map sections"] [role="tab"]'),
    );
    act(() => tabs[1].click());

    // Switching tabs retains the same layers instead of rebuilding them.
    expect(L.polyline).toHaveBeenCalledTimes(10);
    const calls = vi.mocked(L.polyline).mock.calls;
    const geometry = [[1.0605, 104.0303], [1.1, 104.04], [1.1318, 104.0554]];
    // corridor-1 has real A* geometry: casing and stroke both trace the road.
    expect(calls[0][0]).toEqual(geometry);
    expect(calls[1][0]).toEqual(geometry);
    expect(calls[1][1]).not.toHaveProperty('dashArray', expect.any(String));
    // corridor-2 has none yet, so it falls back to a two-point placeholder and
    // is dashed rather than passed off as a surveyed road path.
    expect(calls[3][0]).toHaveLength(2);
    expect(calls[3][1]).toMatchObject({ dashArray: '8, 6' });

    // corridor-1 is selected, so it is emphasised and carries its own status
    // colour rather than a single flat stroke for the whole network.
    const styles = leafletTestState.corridorLayers.flatMap(
      layer => vi.mocked(layer.setStyle).mock.calls.map(call => call[0]),
    );
    const selected = styles.find(style => style.weight === 7);
    expect(selected?.color).toBe(corridorColor('CRITICAL'));
    expect(selected?.opacity).toBe(1);
    // The unselected corridor is subdued and carries its own status colour, so
    // the map cannot disagree with the badge in the feed beneath it.
    const unselected = styles.find(style => style.weight === 4);
    // The provider reports speed loss even below the old HEAVY threshold:
    // SLOWING is amber rather than the legacy SMOOTH/green label.
    expect(unselected?.color).toBe(corridorColor('HEAVY'));
    expect(unselected?.opacity).toBe(0.75);

    // Clicking the feed frames the corridor the panel is describing.
    act(() => container.querySelector<HTMLButtonElement>(
      '[aria-label^="Select Batu Ampar"]',
    )?.click());
    expect(leafletTestState.mapInstance.fitBounds).toHaveBeenCalled();
  });

  it('uses backend hotspot weights while retaining photo metadata', () => {
    const onSelectCorridor = vi.fn();
    const container = renderIntoDom(
      <MapView
        corridors={TEST_CORRIDORS}
        routes={[]}
        trafficSnapshot={trafficSnapshot(71.3)}
        onSelectCorridor={onSelectCorridor}
      />,
    );
    const card = container.querySelector('#hotspot-card-zone-simpang-jam');

    expect(card?.querySelector('.hotspot-card__score')?.getAttribute('aria-label'))
      .toBe('Watch score 71.3 out of 100');
    expect(card?.querySelector('.hotspot-card__priority')?.textContent).toContain('Heavy');
    expect(container.textContent).not.toMatch(/weighted|backend-weighted|formula/i);
    expect(card?.querySelector('.hotspot-card__score')?.textContent).not.toContain('63.4');
    expect(card?.querySelector('img')?.getAttribute('src')).toContain('wikimedia.org');
    expect(container.querySelectorAll('.hotspot-card')).toHaveLength(30);

    act(() => root?.render(
      <MapView
        corridors={TEST_CORRIDORS}
        routes={[]}
        trafficSnapshot={trafficSnapshot(77.8)}
        onSelectCorridor={onSelectCorridor}
      />,
    ));

    expect(container.querySelector('#hotspot-card-zone-simpang-jam .hotspot-card__score')?.getAttribute('aria-label'))
      .toBe('Watch score 77.8 out of 100');
    expect(leafletTestState.map).toHaveBeenCalledTimes(1);
  });

  it('withdraws unsupported legacy numbers while keeping the route controls available', () => {
    const legacy = { ...TEST_CORRIDORS[0] };
    delete legacy.observed_traffic;
    const onSelectCorridor = vi.fn();
    const container = renderIntoDom(corridorView([legacy], onSelectCorridor));
    showCorridorTab(container);

    const panel = container.querySelector('.selected-corridor-panel');
    const metrics = panel?.querySelectorAll('.selected-corridor-panel__metric-value');
    expect(metrics?.[0].textContent).toContain('Unavailable');
    expect(metrics?.[0].textContent).not.toContain('72');
    expect(metrics?.[1].textContent).toContain('Unavailable');
    expect(metrics?.[1].textContent).not.toMatch(/\+0|\+8/);
    expect(panel?.textContent).toContain('Forecast unavailable');
    expect(panel?.textContent).not.toContain('AI 30-Min');
    const feed = container.querySelector('.corridor-feed-item');
    expect(feed?.textContent).toContain('Unavailable');
    expect(feed?.textContent).not.toMatch(/Idx: 72|Delay: \+8/);
    expect(latestCorridorStyle()?.color).toEqual(expect.any(String));
    expect(latestCorridorStyle()?.color).not.toBe(corridorColor('SMOOTH'));
    expect(latestCorridorStyle()?.color).not.toBe(corridorColor('CRITICAL'));
    expect(latestCorridorTooltip()).toMatch(/unavailable/i);
    expect(latestCorridorTooltip()).not.toContain('72');
    // Geometry also includes corridors absent from this response. They have
    // no evidence either and must not retain a default green traffic stroke.
    const missingCorridorStyle = vi.mocked(L.polyline).mock.calls[3]?.[1];
    expect(missingCorridorStyle?.color).toEqual(expect.any(String));
    expect(missingCorridorStyle?.color).not.toBe(corridorColor('SMOOTH'));

    act(() => container.querySelector<HTMLButtonElement>('.selected-corridor-panel__solve')?.click());
    expect(onSelectCorridor).toHaveBeenCalledWith('corridor-1');
  });

  it('updates the selected corridor from a fresh busy observation to a genuine zero on refresh', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-24T08:00:00Z'));
    const busy = { ...TEST_CORRIDORS[0], observed_traffic: observedTraffic(72) };
    const container = renderIntoDom(corridorView([busy]));
    showCorridorTab(container);
    expect(container.querySelector('.selected-corridor-panel__metric-value')?.textContent).toMatch(/72\s*\/ 100/);
    expect(latestCorridorStyle()?.color).toBe(corridorColor('CRITICAL'));

    act(() => vi.advanceTimersByTime(1_000));
    // Deliberately leave the old legacy fields at 72/CRITICAL/+8: the fresh
    // backend observation, including numeric zero, is the authoritative input.
    const clear = { ...busy, observed_traffic: observedTraffic(0) };
    act(() => root?.render(corridorView([clear])));

    expect(container.querySelector('#selected-corridor-title')?.textContent).toContain('Mukakuning');
    expect(container.querySelector('.selected-corridor-panel__metric-value')?.textContent).toMatch(/^0\s*\/ 100/);
    expect(container.querySelector('.corridor-feed-item')?.textContent).not.toContain('Idx: 72');
    expect(container.querySelector('.corridor-feed-item')?.getAttribute('aria-pressed')).toBe('true');
    expect(container.querySelector('.corridor-feed-item')?.textContent).toMatch(/free.flow/i);
    expect(container.querySelector('.corridor-feed-item')?.textContent).not.toContain('SMOOTH');
    expect(latestCorridorStyle()?.color).toBe(corridorColor('SMOOTH'));
    expect(latestCorridorTooltip()).toMatch(/index 0\b/i);
    expect(latestCorridorTooltip()).not.toMatch(/index 72\b/i);
    expect(leafletTestState.map).toHaveBeenCalledTimes(1);
  });

  it('expires the card, feed and map tooltip without waiting for another server response', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-24T08:00:00Z'));
    const corridor = { ...TEST_CORRIDORS[0], observed_traffic: observedTraffic(72, Date.now(), 2) };
    const container = renderIntoDom(corridorView([corridor]));
    showCorridorTab(container);
    expect(container.querySelector('.selected-corridor-panel__metric-value')?.textContent).toContain('72');
    expect(latestCorridorTooltip()).toMatch(/index 72\b/i);

    act(() => vi.advanceTimersByTime(3_000));

    const panel = container.querySelector('.selected-corridor-panel');
    expect(panel?.querySelector('.selected-corridor-panel__metric-value')?.textContent).toContain('Unavailable');
    expect(panel?.textContent).toContain('Forecast unavailable');
    expect(container.querySelector('.corridor-feed-item')?.textContent).toContain('Unavailable');
    expect(latestCorridorTooltip()).toMatch(/unavailable/i);
    expect(latestCorridorTooltip()).not.toMatch(/index 72\b/i);
    expect(latestCorridorStyle()?.color).not.toBe(corridorColor('CRITICAL'));
    expect(latestCorridorStyle()?.color).not.toBe(corridorColor('SMOOTH'));
    expect(leafletTestState.map).toHaveBeenCalledTimes(1);
  });

  it('withdraws a previously observed road when its corridor disappears from the refreshed response', () => {
    const corridor = { ...TEST_CORRIDORS[0], observed_traffic: observedTraffic(72) };
    const container = renderIntoDom(corridorView([corridor]));
    showCorridorTab(container);
    expect(latestCorridorStyle()?.color).toBe(corridorColor('CRITICAL'));
    expect(latestCorridorTooltip()).toMatch(/index 72\b/i);

    act(() => root?.render(corridorView([])));

    expect(container.querySelector('.selected-corridor-panel')).toBeNull();
    expect(container.querySelectorAll('.corridor-feed-item')).toHaveLength(0);
    expect(latestCorridorStyle()?.color).not.toBe(corridorColor('CRITICAL'));
    expect(latestCorridorStyle()?.color).not.toBe(corridorColor('SMOOTH'));
    expect(latestCorridorTooltip()).toMatch(/unavailable/i);
    expect(latestCorridorTooltip()).not.toMatch(/index 72\b/i);
    expect(leafletTestState.map).toHaveBeenCalledTimes(1);
  });

  it('labels persistence as a partial-coverage baseline and preserves the driver workspace boundary', () => {
    const corridor = { ...TEST_CORRIDORS[0], observed_traffic: observedTraffic(22) };
    const onSelectCorridor = vi.fn();
    const container = renderIntoDom(corridorView([corridor], onSelectCorridor, false));
    showCorridorTab(container);

    const panel = container.querySelector('.selected-corridor-panel');
    expect(panel?.textContent).toContain('30 / 60 min outlook');
    expect(panel?.textContent).toContain('Latest-observation baseline');
    expect(panel?.textContent).toContain('Partial coverage');
    expect(panel?.textContent).toMatch(/1 measured road point/);
    expect(panel?.textContent).not.toMatch(/AI 30-Min|Predicted to reach|Modelled corridor estimates/);
    expect(panel?.querySelectorAll('video, iframe, canvas')).toHaveLength(0);
    const action = panel?.querySelector<HTMLButtonElement>('.selected-corridor-panel__solve');
    expect(action?.textContent).toContain('View Assigned Routes');
    expect(action?.getAttribute('aria-label')).toBe('View assigned routes');
    act(() => action?.click());
    expect(onSelectCorridor).toHaveBeenCalledWith('corridor-1');
  });

  it('shows moderate observed speed loss as slowing with amber road styling', () => {
    const corridor = { ...TEST_CORRIDORS[0], observed_traffic: observedTraffic(20) };
    const container = renderIntoDom(corridorView([corridor]));
    showCorridorTab(container);
    expect(container.querySelector('.selected-corridor-panel')?.textContent).toMatch(/slowing/i);
    expect(container.querySelector('.corridor-feed-item')?.textContent).toMatch(/slowing/i);
    expect(container.querySelector('.selected-corridor-panel')?.textContent).not.toContain('SMOOTH');
    expect(latestCorridorStyle()?.color).toBe(corridorColor('HEAVY'));
  });

  it('withholds a low-confidence provider observation instead of showing legacy smooth flow', () => {
    const corridor = { ...TEST_CORRIDORS[0], observed_traffic: { ...observedTraffic(0), provider_confidence: 0.49 } };
    const container = renderIntoDom(corridorView([corridor]));
    showCorridorTab(container);
    expect(container.querySelector('.selected-corridor-panel__metric-value')?.textContent).toContain('Unavailable');
    expect(container.querySelector('.corridor-feed-item')?.textContent).toContain('Unavailable');
    expect(latestCorridorStyle()?.color).not.toBe(corridorColor('SMOOTH'));
  });

  it('keeps a zero camera proxy labelled as an estimate rather than free-flow evidence', () => {
    const observed = observedTraffic(0);
    const corridor: Corridor = {
      ...TEST_CORRIDORS[0],
      observed_traffic: {
        ...observed, source: 'cctv_camera', scope: 'mapped_camera_road_views',
        condition: 'CAMERA_ESTIMATE', condition_policy: 'mapped_camera_proxy_v1',
        provider_confidence: null, current_speed_kmh: null, free_flow_speed_kmh: null,
        provider_point_count: 0, camera_count: 1, camera_congestion_index: 0,
        camera_condition: 'LOW_PRESSURE_ESTIMATE', camera_warning: false, camera_fresh_until: observed.fresh_until,
        forecast_30m: null, forecast_60m: null, forecast_method: 'unavailable',
        forecast_observed_at: null, forecast_fresh_until: null,
      },
    };
    const container = renderIntoDom(corridorView([corridor]));
    showCorridorTab(container);
    const panel = container.querySelector('.selected-corridor-panel');
    expect(panel?.textContent).toMatch(/camera.estimate/i);
    expect(panel?.textContent).not.toMatch(/SMOOTH|FREE.FLOW/i);
    expect(panel?.textContent).toContain('Forecast unavailable');
    expect(latestCorridorStyle()?.color).not.toBe(corridorColor('SMOOTH'));
  });

  it('expires the local camera warning while retaining the fresh free-flow provider point', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-24T08:00:00Z'));
    const corridor: Corridor = {
      ...TEST_CORRIDORS[0],
      observed_traffic: {
        ...observedTraffic(0), camera_count: 1, camera_congestion_index: 75,
        camera_condition: 'LOCAL_CONGESTION_ESTIMATE', camera_warning: true,
        camera_fresh_until: new Date(Date.now() + 2_000).toISOString(),
      },
    };
    const container = renderIntoDom(corridorView([corridor]));
    showCorridorTab(container);
    expect(container.querySelector('.selected-corridor-panel')?.textContent).toContain('Local congestion estimated');
    expect(container.querySelector('.selected-corridor-panel__metric-value')?.textContent).toMatch(/^0\s*\/ 100/);

    act(() => vi.advanceTimersByTime(3_000));

    const panel = container.querySelector('.selected-corridor-panel');
    expect(panel?.textContent).not.toContain('Local congestion estimated');
    expect(panel?.textContent).toMatch(/free.flow/i);
    expect(panel?.querySelector('.selected-corridor-panel__metric-value')?.textContent).toMatch(/^0\s*\/ 100/);
    expect(latestCorridorStyle()?.color).toBe(corridorColor('SMOOTH'));
    expect(leafletTestState.map).toHaveBeenCalledTimes(1);
  });
});
