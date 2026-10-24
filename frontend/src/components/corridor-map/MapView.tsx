import React, { useEffect, useMemo, useRef, useState } from 'react';
import L from 'leaflet';
import { Corridor, CorridorRoute, Fetched, LiveTrafficData } from '../../types';
import { CORRIDOR_ENDPOINTS, MAP_NODES } from '../../data/mockData';
import {
  ArrowUpRight,
  Camera,
  Clock,
  ExternalLink,
  Gauge,
  ImageOff,
  MapPin,
  Radio,
  Sparkles,
} from 'lucide-react';
import { corridorColor, prettyArrow, shortCorridorName } from '../../utils/format';
import { corridorLiveView, corridorObservationTime } from '../../utils/corridorTelemetry';
import { ICON_SIZE } from '../../theme/iconSizes';
import { MAP_PALETTE } from '../../theme/mapPalette';
import { MAP_TILE_ATTRIBUTION, MAP_TILE_URL } from '../../theme/mapTiles';
import {
  BATAM_HOTSPOT_REFERENCES,
} from '../../data/batamHotspots';
import type { HotspotWatchPriority } from '../../data/batamHotspots';
import './HotspotCards.css';
import { WorkspaceSubtabs } from '../shared/WorkspaceSubtabs';

type MapWorkspaceTab = 'hotspots' | 'corridor';

const BATAM_MAP_MIN_ZOOM = 10;

interface MapViewProps {
  corridors: Corridor[];
  routes: CorridorRoute[];
  routesLoading?: boolean;
  trafficSnapshot: Fetched<LiveTrafficData> | null;
  canPlanRoutes?: boolean;
  onSelectCorridor: (corridorId: string) => void;
}

function linesTooltip(lines: Array<{ text: string; strong?: boolean; italic?: boolean; color?: string }>): HTMLDivElement {
  const container = document.createElement('div');
  lines.forEach((line, index) => {
    if (index > 0) container.append(document.createElement('br'));
    const element = document.createElement(line.strong ? 'strong' : line.italic ? 'i' : 'span');
    element.textContent = line.text;
    if (line.color) element.style.color = line.color;
    container.append(element);
  });
  return container;
}

function hotspotPriorityColor(priority: HotspotWatchPriority): string {
  return priority === 'CRITICAL'
    ? MAP_PALETTE.traffic.critical
    : MAP_PALETTE.traffic.heavy;
}

function hotspotPriorityLabel(priority: HotspotWatchPriority): string {
  return priority === 'CRITICAL' ? 'High priority' : 'Watch priority';
}

export const MapView: React.FC<MapViewProps> = ({
  corridors,
  routes,
  routesLoading = false,
  trafficSnapshot,
  canPlanRoutes = true,
  onSelectCorridor,
}) => {
  const mapContainerRef = useRef<HTMLDivElement>(null);
  const mapInstanceRef = useRef<L.Map | null>(null);
  const corridorCasingRefs = useRef<Map<string, L.Polyline>>(new Map());
  const corridorLineRefs = useRef<Map<string, L.Polyline>>(new Map());

  /**
   * Selection is tracked by id and the corridor re-derived on every render.
   *
   * It used to be a state snapshot of the corridor object, taken once on mount
   * and only replaced on click — so the detail card froze at whatever the
   * numbers were then, while the feed list directly beneath it refreshed every
   * on each poll. Both sat on screen disagreeing about the same corridor.
   */
  const [selectedId, setSelectedId] = useState<string | null>(corridors[0]?.id ?? null);
  const activeSelectedId = corridors.some(corridor => corridor.id === selectedId)
    ? selectedId
    : corridors[0]?.id ?? null;
  const selectedCorridor =
    corridors.find(c => c.id === activeSelectedId) ?? null;
  const [nowMs, setNowMs] = useState(Date.now);
  useEffect(() => {
    const updateClock = () => setNowMs(Date.now());
    const timer = window.setInterval(updateClock, 1000);
    window.addEventListener('focus', updateClock);
    document.addEventListener('visibilitychange', updateClock);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('focus', updateClock);
      document.removeEventListener('visibilitychange', updateClock);
    };
  }, []);
  const corridorViews = useMemo(() => new Map(
    corridors.map(corridor => [corridor.id, corridorLiveView(corridor, nowMs)]),
  ), [corridors, nowMs]);
  const selectedTraffic = selectedCorridor ? corridorViews.get(selectedCorridor.id) : undefined;

  // One control intentionally governs both the planning-watch catalogue and
  // the current/provider-or-model traffic markers drawn over the base map.
  const [showTrafficOverlays, setShowTrafficOverlays] = useState(true);
  const planningWatchLayersRef = useRef<L.Layer[]>([]);
  const hotspotLayerRefs = useRef<Map<string, L.Circle>>(new Map());
  const [focusedHotspotId, setFocusedHotspotId] = useState<string | null>(null);
  const [failedPhotoIds, setFailedPhotoIds] = useState<Set<string>>(() => new Set());
  const [activeWorkspaceTab, setActiveWorkspaceTab] = useState<MapWorkspaceTab>('hotspots');
  const focusSelectedTabOnOpenRef = useRef(false);

  const liveHotspotById = useMemo(() => new Map(
    (trafficSnapshot?.data.zones ?? []).map(zone => [zone.zone_id, zone]),
  ), [trafficSnapshot]);
  const hotspots = useMemo(() => BATAM_HOTSPOT_REFERENCES
    .map((reference, referenceIndex) => {
      const liveZone = liveHotspotById.get(reference.id);
      return {
        ...reference,
        name: liveZone?.name ?? reference.name,
        category: liveZone?.category ?? reference.category,
        lat: liveZone?.lat ?? reference.lat,
        lng: liveZone?.lng ?? reference.lng,
        radiusM: liveZone?.radius_m ?? reference.radiusM,
        planningScore: liveZone?.selection_score === undefined
          ? reference.planningScore
          : Math.round(liveZone.selection_score * 10) / 10,
        priority: liveZone?.watch_priority ?? reference.priority,
        currentCongestionIndex: liveZone?.congestion_index,
        currentLevel: liveZone?.level,
        selectionRank: liveZone?.selection_rank,
        referenceIndex,
      };
    })
    .sort((left, right) => (
      (left.selectionRank ?? left.referenceIndex + 1)
      - (right.selectionRank ?? right.referenceIndex + 1)
      || left.id.localeCompare(right.id)
    )), [liveHotspotById]);

  /** Create the map exactly once. */
  useEffect(() => {
    if (!mapContainerRef.current || mapInstanceRef.current) return;

    const smallWidthQuery = window.matchMedia?.('(max-width: 1000px)');

    const map = L.map(mapContainerRef.current, {
      // Leaflet's delayed CSS-zoom callback can fire after a workspace unmount.
      zoomAnimation: false,
      zoomControl: false,
      minZoom: BATAM_MAP_MIN_ZOOM,
      scrollWheelZoom: !smallWidthQuery?.matches,
    }).setView([1.12, 104.02], 11);
    mapInstanceRef.current = map;
    L.control.zoom({ position: 'topright' }).addTo(map);

    const syncWheelZoom = () => {
      if (smallWidthQuery?.matches) map.scrollWheelZoom.disable();
      else map.scrollWheelZoom.enable();
    };
    smallWidthQuery?.addEventListener('change', syncWheelZoom);

    L.tileLayer(MAP_TILE_URL, {
      attribution: MAP_TILE_ATTRIBUTION,
      maxZoom: 18,
    }).addTo(map);

    Object.entries(MAP_NODES).forEach(([key, node]) => {
      const isSingapore = key.includes('sg');
      L.circleMarker([node.lat, node.lng], {
        radius: isSingapore ? 9 : 7,
        fillColor: isSingapore ? MAP_PALETTE.node.singapore : MAP_PALETTE.node.batam,
        color: MAP_PALETTE.route.casing,
        weight: 3,
        opacity: 1,
        fillOpacity: 1,
      })
        .addTo(map)
        .bindTooltip(linesTooltip([{ text: node.name, strong: true }]), { direction: 'top' });
    });

    return () => {
      smallWidthQuery?.removeEventListener('change', syncWheelZoom);
      map.remove();
      mapInstanceRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (activeWorkspaceTab !== 'corridor' || !focusSelectedTabOnOpenRef.current) return;

    focusSelectedTabOnOpenRef.current = false;
    document.getElementById('map-workspace-tab-1')?.focus();
  }, [activeSelectedId, activeWorkspaceTab]);

  /** Draw corridor road geometry throughout the congestion workspace. */
  useEffect(() => {
    const map = mapInstanceRef.current;
    const casings = corridorCasingRefs.current;
    const lines = corridorLineRefs.current;
    if (!map) return;

    const clearCorridorLayers = () => {
      casings.forEach(layer => layer.remove());
      lines.forEach(layer => layer.remove());
      casings.clear();
      lines.clear();
    };

    clearCorridorLayers();
    if (routesLoading) return clearCorridorLayers;

    const geometryById = new Map(routes.map(route => [route.id, route.geometry]));
    Object.entries(CORRIDOR_ENDPOINTS).forEach(([id, [from, to]]) => {
      const geometry = geometryById.get(id);
      // Endpoints are a placeholder only until /api/corridor-routes answers;
      // a two-point line is never presented as a road path.
      const latlngs: [number, number][] = geometry?.length
        ? geometry
        : [
            [MAP_NODES[from].lat, MAP_NODES[from].lng],
            [MAP_NODES[to].lat, MAP_NODES[to].lng],
          ];
      const isModelled = Boolean(geometry?.length);

      const casing = L.polyline(latlngs, {
        color: MAP_PALETTE.route.casing,
        weight: 9,
        opacity: 0.9,
      }).addTo(map);
      const line = L.polyline(latlngs, {
        color: MAP_PALETTE.route.connector,
        weight: 5,
        opacity: 1,
        dashArray: isModelled ? undefined : '8, 6',
      }).addTo(map);
      line.on('click', () => {
        focusSelectedTabOnOpenRef.current = true;
        setSelectedId(id);
        setActiveWorkspaceTab('corridor');
      });

      casings.set(id, casing);
      lines.set(id, line);
    });

    return clearCorridorLayers;
  }, [routes, routesLoading]);

  /**
   * Recolour on each poll by restyling the existing layers rather than
   * rebuilding them, so a 30-second refresh never resets the viewport.
   */
  useEffect(() => {
    corridorLineRefs.current.forEach((line, id) => {
      if (corridorViews.has(id)) return;
      line.setStyle({ color: MAP_PALETTE.route.connector, weight: 4, opacity: 0.75 });
      line.bindTooltip(linesTooltip([{ text: 'Traffic unavailable' }]), { sticky: true });
    });
    corridors.forEach(corridor => {
      const line = corridorLineRefs.current.get(corridor.id);
      const traffic = corridorViews.get(corridor.id);
      if (!line || !traffic) return;
      const isSelected = corridor.id === activeSelectedId;
      line.setStyle({
        color: traffic.color,
        weight: isSelected ? 7 : 4,
        opacity: isSelected ? 1 : 0.75,
      });
      if (isSelected) line.bringToFront();
      line.bindTooltip(linesTooltip([
        { text: prettyArrow(corridor.name), strong: true },
        {
          text: traffic.available ? `${traffic.pressureScore === null ? 'Pressure unavailable' : `Congestion pressure ${traffic.pressureScore}`} · ${traffic.status.replace(/_/g, ' ')}` : 'Traffic unavailable',
          color: traffic.color,
        },
        { text: traffic.coverage },
        ...(traffic.cameraWarning ? [{ text: 'Local congestion estimated', color: corridorColor('HEAVY') }] : []),
        { text: corridor.distance_km === null ? 'Distance unavailable' : `${corridor.distance_km} km` },
      ]), { sticky: true });
    });
  }, [activeSelectedId, corridors, corridorViews, routes]);

  /** Render backend-weighted watch areas alongside the corridor network. */
  useEffect(() => {
    const map = mapInstanceRef.current;
    if (!map) return;

    const clearPlanningWatchLayers = () => {
      planningWatchLayersRef.current.forEach(layer => layer.remove());
      planningWatchLayersRef.current = [];
      hotspotLayerRefs.current.clear();
    };

    clearPlanningWatchLayers();
    if (!showTrafficOverlays) return;

    hotspots.forEach(zone => {
      const isCritical = zone.priority === 'CRITICAL';
      const color = hotspotPriorityColor(zone.priority);
      const tooltipLines = [
        { text: `${hotspotPriorityLabel(zone.priority)} · modelled watch area`, strong: true, color },
        { text: zone.name, strong: true },
        { text: zone.category, italic: true },
        { text: `Watch score: ${zone.planningScore}` },
        ...(zone.currentCongestionIndex === undefined ? [] : [{
          text: `Modelled congestion: ${zone.currentCongestionIndex} · ${zone.currentLevel}`,
        }]),
        { text: 'Modelled planning area', italic: true },
      ];
      const circle = L.circle([zone.lat, zone.lng], {
        radius: zone.radiusM,
        color,
        weight: isCritical ? 3.5 : 2,
        fillColor: color,
        fillOpacity: isCritical ? 0.28 : 0.2,
        dashArray: isCritical ? '4, 4' : '8, 5',
      })
        .addTo(map)
        .bindTooltip(linesTooltip(tooltipLines), { sticky: true });
      circle.on('click', () => {
        setFocusedHotspotId(zone.id);
        setActiveWorkspaceTab('hotspots');
      });
      const centre = L.circleMarker([zone.lat, zone.lng], {
        radius: isCritical ? 7 : 5,
        color: MAP_PALETTE.route.casing,
        fillColor: color,
        fillOpacity: 1,
        weight: 2,
        interactive: false,
      }).addTo(map);
      hotspotLayerRefs.current.set(zone.id, circle);
      planningWatchLayersRef.current.push(circle, centre);
    });
    return clearPlanningWatchLayers;
  }, [hotspots, showTrafficOverlays]);

  const toggleTrafficOverlays = () => {
    setShowTrafficOverlays(value => !value);
  };
  const criticalHotspots = hotspots.filter(zone => zone.priority === 'CRITICAL').length;
  const heavyHotspots = hotspots.filter(zone => zone.priority === 'HEAVY').length;
  const focusHotspot = (zone: (typeof hotspots)[number]) => {
    const map = mapInstanceRef.current;
    if (!map) return;
    setFocusedHotspotId(zone.id);
    map.setView([zone.lat, zone.lng], Math.max(map.getZoom(), 14), { animate: true });
    hotspotLayerRefs.current.get(zone.id)?.openTooltip();
  };
  useEffect(() => {
    if (!focusedHotspotId || activeWorkspaceTab !== 'hotspots') return;
    document.getElementById(`hotspot-card-${focusedHotspotId}`)
      ?.scrollIntoView?.({ block: 'nearest' });
  }, [activeWorkspaceTab, focusedHotspotId]);
  /** Frame a corridor the user picked, so the selection is visible at once. */
  const focusCorridor = (corridorId: string) => {
    const map = mapInstanceRef.current;
    const line = corridorLineRefs.current.get(corridorId);
    if (!map || !line) return;
    map.fitBounds(line.getBounds().pad(0.2), { animate: true });
  };
  const markPhotoUnavailable = (photoId: string) => {
    setFailedPhotoIds(current => {
      if (current.has(photoId)) return current;
      const next = new Set(current);
      next.add(photoId);
      return next;
    });
  };

  return (
    <div className="app-screen-layout map-view-layout">
      <section className="glass-panel map-canvas-panel" aria-label="Batam traffic network map">
        <div ref={mapContainerRef} className="map-canvas" role="region" aria-label="Interactive Batam corridor map" />

        <div className="map-radar-card">
          <div className="map-radar-card__heading">
            <MapPin aria-hidden="true" size={ICON_SIZE.medium} color="var(--accent-cyan)" />
            Batam Mobility Watch • Hotspots
          </div>
          <div className="map-radar-card__legend" aria-label="Map radar legend">
            <span className="map-radar-card__legend-item"><span className="map-radar-card__legend-dot is-critical" /> High priority</span>
            <span className="map-radar-card__legend-item"><span className="map-radar-card__legend-dot is-heavy" /> Watch priority</span>
            <span className="map-radar-card__legend-item"><MapPin aria-hidden="true" size={ICON_SIZE.small} /> {hotspots.length} areas</span>
          </div>
        </div>

        {/* Toggle the weighted hotspot layer without adding route-line clutter. */}
        <button
          type="button"
          className={`map-traffic-toggle${showTrafficOverlays ? ' is-active' : ''}`}
          aria-pressed={showTrafficOverlays}
          aria-label={`${showTrafficOverlays ? 'Hide' : 'Show'} planning-watch overlays`}
          title={`${showTrafficOverlays ? 'Hide' : 'Show'} map overlays`}
          onClick={toggleTrafficOverlays}
        >
          <Radio aria-hidden="true" size={ICON_SIZE.large} />
        </button>
      </section>

      <WorkspaceSubtabs<MapWorkspaceTab>
        unwrapped
        idPrefix="map-workspace"
        ariaLabel="Corridor map sections"
        activeTab={activeWorkspaceTab}
        onActiveTabChange={setActiveWorkspaceTab}
        tabs={[
            {
              id: 'hotspots',
              label: 'Hotspots',
              content: (
        <section className="glass-panel hotspot-watch-panel" aria-labelledby="hotspot-watch-title">
            <div className="hotspot-watch__header">
              <div>
                <h3 id="hotspot-watch-title" className="hotspot-watch__title">
                  <MapPin aria-hidden="true" color="var(--accent-cyan)" size={ICON_SIZE.large} />
                  {hotspots.length}-Area Congestion Watch
                </h3>
                <p className="hotspot-watch__subtitle">
                  Ports, neighbourhoods and the roads between.
                </p>
              </div>
              <div className="hotspot-watch__counts" aria-label={`${criticalHotspots} high priority and ${heavyHotspots} watch priority modelled planning areas`}>
                <span className="badge badge-critical">{criticalHotspots} high priority</span>
                <span className="badge badge-heavy">{heavyHotspots} watch priority</span>
              </div>
            </div>

            <p className="hotspot-watch__disclaimer">
              Modelled planning priorities · choose a place to explore.
            </p>

            <div className="hotspot-watch__list" role="list" tabIndex={0} aria-label="Scrollable Batam congestion planning watch areas">
              {hotspots.map((zone, index) => (
                <article
                  key={zone.id}
                  id={`hotspot-card-${zone.id}`}
                  role="listitem"
                  data-priority={zone.priority.toLowerCase()}
                  className={`ui-sand-interactive hotspot-card${focusedHotspotId === zone.id ? ' is-focused' : ''}`}
                >
                  <button
                    type="button"
                    className="hotspot-card__focus"
                    aria-pressed={focusedHotspotId === zone.id}
                    aria-label={`Inspect ${zone.name}, ${hotspotPriorityLabel(zone.priority).toLowerCase()} modelled planning area, on the map`}
                    title={zone.name}
                    onClick={() => focusHotspot(zone)}
                  >
                    <span className="hotspot-card__media">
                      {failedPhotoIds.has(zone.photo.id) ? (
                        <span className="hotspot-card__photo-fallback" aria-label="Location photo unavailable">
                          <ImageOff aria-hidden="true" size={ICON_SIZE.big} />
                        </span>
                      ) : (
                        <img
                          className="hotspot-card__photo"
                          src={zone.photo.imageUrl}
                          alt={zone.photo.alt}
                          title={`${zone.photo.caption} · ${zone.photo.capturedYear}`}
                          loading="lazy"
                          decoding="async"
                          referrerPolicy="no-referrer"
                          onError={() => markPhotoUnavailable(zone.photo.id)}
                        />
                      )}
                      <span className="hotspot-card__rank" aria-hidden="true">{String(index + 1).padStart(2, '0')}</span>
                    </span>

                    <span className="hotspot-card__body">
                      <span className="hotspot-card__category">{zone.category}</span>
                      <strong className="hotspot-card__name">{zone.name}</strong>
                      <span className="hotspot-card__meta">
                        <span className={`badge ${zone.priority === 'CRITICAL' ? 'badge-critical' : 'badge-heavy'} hotspot-card__priority`}>
                          {hotspotPriorityLabel(zone.priority)}
                        </span>
                        <strong className="hotspot-card__score" aria-label={`Watch score ${zone.planningScore} out of 100`}>
                          {zone.planningScore}<small> / 100</small>
                        </strong>
                      </span>
                    </span>
                  </button>

                  <div className="hotspot-card__credit">
                    <span className="hotspot-card__photo-context" title={zone.photo.caption}>{zone.photoContext} · {zone.photo.capturedYear}</span>
                    <div className="hotspot-card__credit-meta">
                      <Camera aria-hidden="true" size={ICON_SIZE.small} />
                      <a className="hotspot-card__credit-author" href={zone.photo.sourceUrl} target="_blank" rel="noreferrer">
                        {zone.photo.author} <ExternalLink aria-hidden="true" size={ICON_SIZE.small} />
                      </a>
                      {' '}
                      <a className="hotspot-card__credit-license" href={zone.photo.licenseUrl} target="_blank" rel="noreferrer">
                        {zone.photo.license}
                      </a>
                    </div>
                  </div>
                </article>
              ))}
            </div>
          </section>
              ),
            },
            {
              id: 'corridor',
              label: 'Corridor',
              content: (
        <div className="map-corridor-stack">
          {selectedCorridor && selectedTraffic ? (
          <section className="glass-panel selected-corridor-panel" aria-labelledby="selected-corridor-title">
            <div className="selected-corridor-panel__header">
              <span className={`badge badge-${selectedTraffic.tone}`}>
                {selectedTraffic.available ? `${selectedTraffic.status.replace(/_/g, ' ')} · SAMPLED` : 'TRAFFIC UNAVAILABLE'}
              </span>
              {selectedTraffic.cameraWarning && <span className="badge badge-heavy">Local congestion estimated</span>}
            </div>

            <h3 id="selected-corridor-title" className="selected-corridor-panel__title">
              {prettyArrow(selectedCorridor.name)}
            </h3>
            <p className="muted">{selectedTraffic.coverage}</p>
            {selectedTraffic.observedAt && (
              <p className="selected-corridor-panel__route-meta">
                <Clock aria-hidden="true" size={ICON_SIZE.small} />
                Updated {corridorObservationTime(selectedTraffic.observedAt)}
              </p>
            )}

            <div className="selected-corridor-panel__metrics">
              <article className="selected-corridor-panel__metric">
                <div className="selected-corridor-panel__metric-label">
                  <Gauge aria-hidden="true" size={ICON_SIZE.medium} color="var(--accent-cyan)" /> Congestion pressure
                </div>
                <div className={`selected-corridor-panel__metric-value${selectedTraffic.pressureScore === null ? ' is-unavailable' : ''}`} style={{ color: selectedTraffic.pressureScore === null ? corridorColor('UNAVAILABLE') : selectedTraffic.color }}>
                  {selectedTraffic.pressureScore === null ? 'Unavailable' : <>{selectedTraffic.pressureScore} <span className="selected-corridor-panel__metric-suffix">/ 100</span></>}
                </div>
              </article>

              <article className="selected-corridor-panel__metric">
                <div className="selected-corridor-panel__metric-label">
                  <Clock aria-hidden="true" size={ICON_SIZE.medium} color="var(--accent-amber)" /> Est. Traffic Delay
                </div>
                <div className={`selected-corridor-panel__metric-value ${selectedTraffic.delay === null ? 'is-unavailable' : 'is-delay'}`}>
                  {selectedTraffic.delay === null ? 'Unavailable' : <>+{selectedTraffic.delay} <span className="selected-corridor-panel__metric-suffix">mins</span></>}
                </div>
              </article>
            </div>

            <div className="selected-corridor-panel__forecast">
              <div className="selected-corridor-panel__forecast-title">
                <Sparkles aria-hidden="true" size={ICON_SIZE.medium} /> 30 / 60 min outlook
              </div>
              <p className="selected-corridor-panel__forecast-copy">
                {selectedTraffic.pressureForecast30 === null || selectedTraffic.pressureForecast60 === null
                  ? 'Forecast unavailable for this corridor.'
                  : <>{selectedTraffic.forecastMethod === 'persistence' ? 'Latest-observation baseline' : 'Observed-data forecast'}: <strong>{selectedTraffic.pressureForecast30}</strong> in 30 min · <strong>{selectedTraffic.pressureForecast60}</strong> in 60 min <span>/ 100 pressure</span>.</>}
              </p>
            </div>

            <button
              type="button"
              className="ui-button-primary selected-corridor-panel__solve"
              aria-label={canPlanRoutes ? `Solve departure route for ${prettyArrow(selectedCorridor.name)}` : 'View assigned routes'}
              onClick={() => onSelectCorridor(selectedCorridor.id)}
            >
              {canPlanRoutes ? 'Solve Route Departure' : 'View Assigned Routes'} <ArrowUpRight aria-hidden="true" size={ICON_SIZE.large} />
            </button>
          </section>
        ) : (
          <div role="status" className="glass-panel selected-corridor-panel__empty">
            Corridor telemetry is unavailable. The network map remains available for orientation.
          </div>
        )}

        <section className="glass-panel corridor-feed-panel" aria-labelledby="corridor-feed-title">
          <h3 id="corridor-feed-title" className="corridor-feed-panel__title">
            Batam Corridor Telemetry Feeds ({corridors.length})
          </h3>
          <div className="corridor-feed-list" tabIndex={0} aria-label="Scrollable Batam corridor telemetry feeds">
            {corridors.map((c) => {
              const traffic = corridorViews.get(c.id)!;
              return (
              <button
                key={c.id}
                type="button"
                className="ui-sand-interactive corridor-feed-item"
                aria-pressed={activeSelectedId === c.id}
                aria-label={`Select ${prettyArrow(c.name)}, ${traffic.available ? `sampled ${traffic.status.replace(/_/g, ' ').toLowerCase()}, ${traffic.pressureScore === null ? 'pressure unavailable' : `congestion pressure ${traffic.pressureScore}`}${traffic.cameraWarning ? ', local congestion estimated' : ''}` : 'traffic unavailable'}`}
                onClick={() => {
                  focusSelectedTabOnOpenRef.current = true;
                  setSelectedId(c.id);
                  setActiveWorkspaceTab('corridor');
                  focusCorridor(c.id);
                }}
              >
                <div className="corridor-feed-item__heading">
                  <strong>{shortCorridorName(c.name)}</strong>
                  <span className={`badge badge-${traffic.tone}`}>{traffic.status.replace(/_/g, ' ')}</span>
                </div>
                {traffic.cameraWarning && <span className="badge badge-heavy">Local congestion estimated</span>}
                <div className="corridor-feed-item__metrics">
                  <span>{c.distance_km === null ? 'Distance unavailable' : `Dist: ${c.distance_km} km`}</span>
                  <span>{traffic.delay === null ? 'Delay: Unavailable' : `Delay: +${traffic.delay}m`}</span>
                  <span>{traffic.pressureScore === null ? 'Pressure: Unavailable' : `Pressure: ${traffic.pressureScore}`}</span>
                </div>
              </button>
              );
            })}
            {corridors.length === 0 ? (
              <div role="status" className="corridor-feed-panel__empty">
                No corridor feeds are available yet.
              </div>
            ) : null}
          </div>
        </section>
        </div>
              ),
            },
        ]}
      />
    </div>
  );
};
