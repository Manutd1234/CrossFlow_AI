import {
  ApiHistoryMetadata, Corridor, CorridorRoute, DataSource, Envelope, Fetched,
  FerryRefreshReport, FerryRefreshSourceResult, FerrySchedule, FerryTimetableMetadata,
  FetchedFerryRefresh, FetchedFerrySchedule, FreeLocation, GeocodedLocation, HistoricalProfile,
  LiveTrafficData, ModelMetrics, NavigationData, OperationsSummary, PortStatus, RouteLocation,
  ObservedBacktestMetrics, ObservedBacktestPartition, ObservedBacktestReport, ObservedBacktestResult,
  RouteBenchmarkResult, RouteEndpointSnapshot, RouteOptimizationResult, RoutePreference,
  RouteRunHistory,
  RouteScheduleOptions,
  RoutingCostBreakdown, VehicleType,
} from '../types';

import {
  INITIAL_CORRIDORS, CORRIDOR_INDEX, ROUTE_LOCATIONS,
  EMISSIONS_PRESSURE_MODEL, PUBLISHED_FERRY_TIMETABLE_METADATA,
  offlineCongestionZones, offlineFerries,
} from '../data/mockData';
import { UNAVAILABLE_OPERATIONS } from '../data/unavailableOperations';
import { CORRIDOR_STREET_GEOMETRIES } from '../data/corridorGeometries';
import { predictLocal } from './localForecast';
import { planWithBundledRoadGraph } from './offlineRoadRouter';
import {
  isPassengerFerryIncompatibleVehicle,
  locationRegion,
  offlineCrossBorderOptimize,
  PASSENGER_FERRY_TRUCK_MESSAGE,
} from './crossBorderRouting';
import type { OfflineRoadPlan, OfflineRoutingContext } from '../workers/offlineRouterCore';
import { vehicleProfile, vehicleProfileSnapshot } from '../data/vehicleCatalog';
import { batamParts, nextBatamHour, toBatamIso, toCorridorIso } from '../utils/batamTime';


/** Same-origin by default (Vite proxies /api in development); configurable for split deployments. */
const configuredApiBase = import.meta.env.VITE_API_BASE_URL?.trim().replace(/\/$/, '');
const API_BASE = configuredApiBase ?? '';
const API_ENABLED = import.meta.env.VITE_API_ENABLED !== 'false';


// A cold worker builds its per-vehicle routing caches inside the first request
// that needs them. Measured cold on this graph: ~15s for corridors and ~17s for
// a route, so the previous 6s corridor budget abandoned a response that was
// about to arrive and reported the backend as unavailable.
const CORRIDOR_TIMEOUT_MS = 20_000;
const ROUTE_TIMEOUT_MS = 120_000;

// Every extra stop adds a full A* over the 115k-node graph, and whole-journey
// alternatives re-solve the longest legs on top of that. Measured on a
// five-stop Batam journey: 90s warm, and more on a cold worker. The previous
// 60s budget aborted mid-solve, and the client reported the abandoned request
// as "the backend returned an invalid multi-stop route" — a correct response
// that simply arrived after nobody was listening.
const MULTI_STOP_TIMEOUT_MS = 240_000;
const FERRY_REFRESH_TIMEOUT_MS = 20_000;
const FERRY_TERMINAL_MATCH_KM = 0.5;

/** Serialize only the new scheduling fields that are actually selected. */
function scheduleRequestFields(schedule?: RouteScheduleOptions): RouteScheduleOptions {
  if (!schedule) return {};
  const departureAt = schedule.departure_at?.trim();
  const arriveBy = schedule.arrive_by?.trim();
  // The backend enforces mutual exclusivity too; keeping this guard here
  // avoids sending an ambiguous request if a caller changes modes mid-flight.
  if (departureAt) return { departure_at: departureAt };
  if (arriveBy) return { arrive_by: arriveBy };
  return {};
}

function hasExactSchedule(schedule?: RouteScheduleOptions): boolean {
  return Boolean(schedule?.departure_at?.trim() || schedule?.arrive_by?.trim());
}

function hourFromSchedule(schedule?: RouteScheduleOptions): number | undefined {
  const value = schedule?.departure_at;
  if (!value) return undefined;
  const instant = new Date(value);
  return Number.isNaN(instant.getTime()) ? undefined : batamParts(instant).hour;
}

function simulatedSchedule(
  route: RouteOptimizationResult,
  schedule?: RouteScheduleOptions,
): RouteOptimizationResult {
  const requestedDeparture = schedule?.departure_at?.trim();
  const requestedArrival = schedule?.arrive_by?.trim();
  if (!requestedDeparture && !requestedArrival) return route;

  const totalEtaMins = route.total_eta_mins ?? route.estimated_travel_time_mins ?? 0;
  const arrivalDeadline = requestedArrival ? new Date(requestedArrival) : null;
  const calculatedDeparture = arrivalDeadline && !Number.isNaN(arrivalDeadline.getTime())
    ? route.scheduling?.mode === 'ARRIVE_BY'
      ? route.planned_departure
      : new Date(arrivalDeadline.getTime() - totalEtaMins * 60_000).toISOString()
    : undefined;
  const origin = route.requested_origin;
  const destination = route.requested_destination;
  const departureTimezone = origin && locationRegion(origin.lat, origin.lng) === 'SINGAPORE'
    ? 'Asia/Singapore' : 'Asia/Jakarta';
  const arrivalTimezone = destination && locationRegion(destination.lat, destination.lng) === 'SINGAPORE'
    ? 'Asia/Singapore' : 'Asia/Jakarta';
  const plannedDeparture = toCorridorIso(new Date(requestedDeparture ?? calculatedDeparture
    ?? route.planned_departure ?? toBatamIso(new Date())), departureTimezone);
  const estimatedArrival = new Date(new Date(plannedDeparture).getTime() + totalEtaMins * 60_000);
  const deadlineSlack = arrivalDeadline
    ? Math.round((arrivalDeadline.getTime() - estimatedArrival.getTime()) / 60_000)
    : null;
  const destinationPort = route.requested_destination
    ? destinationFerryPort(route.requested_destination)
    : null;
  const hasFerryLeg = route.route_legs?.some(leg => leg.mode === 'FERRY');
  const retimedFerries = (etaMins: number) => destinationPort && !hasFerryLeg
    ? offlineFerries(new Date(new Date(plannedDeparture).getTime()
      + (etaMins + FERRY_BOARDING_CUTOFF_MINS) * 60_000), new Date(), 48)
      .filter(ferry => ferry.departure_port === destinationPort).slice(0, 3)
    : null;

  return {
    ...route,
    planned_departure: plannedDeparture,
    estimated_arrival: toCorridorIso(estimatedArrival, arrivalTimezone),
    next_matching_ferries: retimedFerries(totalEtaMins) ?? route.next_matching_ferries,
    alternative_routes: route.alternative_routes?.map(alternative => ({
      ...alternative,
      next_matching_ferries: retimedFerries(alternative.total_eta_mins
        ?? alternative.estimated_travel_time_mins + route.customs_buffer_mins)
        ?? alternative.next_matching_ferries,
    })),
    scheduling: {
      mode: requestedArrival ? 'ARRIVE_BY' : 'DEPART_AT',
      requested_departure_at: requestedDeparture ?? null,
      requested_arrive_by: requestedArrival ?? null,
      deadline_slack_mins: deadlineSlack,
    },
    schedule_mode: requestedArrival ? 'arrive_by' : 'departure',
    requested_departure_at: requestedDeparture,
    requested_arrive_by: requestedArrival,
    deadline_slack_mins: deadlineSlack,
    schedule_provenance: {
      source: 'committed_timetable_simulation',
      snapshot_id: PUBLISHED_FERRY_TIMETABLE_METADATA.snapshot_id,
      snapshot_verified_at: PUBLISHED_FERRY_TIMETABLE_METADATA.last_verified_at,
      last_verified_at: PUBLISHED_FERRY_TIMETABLE_METADATA.last_verified_at,
      latest_checked_at: null,
      freshness_durability: 'committed_browser_snapshot',
      shared_freshness: false,
      live: false,
      limitations: PUBLISHED_FERRY_TIMETABLE_METADATA.limitations,
    },
  };
}

export class ApiRequestError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiRequestError';
    this.status = status;
  }
}

/**
 * One request path for every endpoint.
 *
 * Each fetcher previously wrote `if (res.ok) { ... }` with no `else`, so an
 * HTTP 500 or 404 fell through to the mock return without even logging — a
 * broken-but-reachable backend was completely invisible.
 */
async function getJSON<T>(
  path: string,
  timeoutMs: number,
  init?: RequestInit,
  throwClientErrors = false,
): Promise<T | null> {
  if (!API_ENABLED) return null;
  try {
    const timeoutSignal = AbortSignal.timeout(timeoutMs);
    const signal = init?.signal
      ? AbortSignal.any([init.signal, timeoutSignal])
      : timeoutSignal;
    const res = await fetch(`${API_BASE}${path}`, {
      ...init,
      signal,
    });
    if (!res.ok) {
      console.warn(`[crossflow] ${path} -> HTTP ${res.status} ${res.statusText}`);
      // Validation and authentication failures are authoritative. A 404/405 from
      // an older deployment, plus rate limits and gateway timeouts, should
      // still permit the bundled road engine to plan the trip.
      if (throwClientErrors && [400, 401, 403, 422].includes(res.status)) {
        let message = res.status === 401
          ? 'Sign in again before planning a route.'
          : res.status === 403
            ? 'An authorized operator account is required to plan routes.'
            : `The route request was rejected (${res.status}).`;
        try {
          const body = await res.json() as { detail?: unknown };
          if (typeof body.detail === 'string' && body.detail.trim()) message = body.detail;
        } catch {
          // The status-specific fallback above is still useful for non-JSON errors.
        }
        throw new ApiRequestError(message, res.status);
      }
      return null;
    }
    return (await res.json()) as T;
  } catch (err) {
    if (err instanceof ApiRequestError) throw err;
    console.warn(`[crossflow] ${path} unreachable:`, err);
    return null;
  }
}

function sourceOf(payload: Envelope | null): DataSource {
  if (!payload) return 'offline';
  // Absent provenance means an older backend; 'simulated' is the honest default.
  return payload.data_source === 'live' ? 'live' : 'simulated';
}

function stampOf(payload: Envelope | null): string {
  const raw = payload?.generated_at;
  if (raw && !Number.isNaN(new Date(raw).getTime())) return raw;
  return toBatamIso(new Date());
}

function wrap<T>(payload: Envelope | null, data: T): Fetched<T> {
  return {
    data,
    source: sourceOf(payload),
    fetchedAt: stampOf(payload),
    provenance: payload?.provenance,
  };
}

function hasFiniteRoadGeometry(geometry: unknown): geometry is [number, number][] {
  return Array.isArray(geometry)
    && geometry.length >= 2
    && geometry.every((point) => Array.isArray(point)
      && point.length === 2
      && point.every((coordinate) => typeof coordinate === 'number' && Number.isFinite(coordinate)));
}

/** Reject stale/malformed 200 responses before they can suppress local A*. */
function validatedRoadPayload<T extends RouteOptimizationResult>(payload: T | null): T | null {
  const validPrimary = payload
    && payload.route_data_source !== 'offline_straight_line'
    && typeof payload.route_data_source === 'string'
    && Number.isFinite(payload.corridor?.distance_km)
    && payload.corridor.distance_km > 0
    && hasFiniteRoadGeometry(payload.route_geometry)
    && Array.isArray(payload.navigation?.maneuvers)
    && payload.navigation.maneuvers.length >= 2;
  if (!validPrimary || !payload) {
    if (payload) console.warn('[crossflow] ignored a non-drivable or incomplete route response');
    return null;
  }
  const alternativeRoutes = (payload.alternative_routes ?? []).filter((route) => (
    Number.isFinite(route.distance_km)
    && route.distance_km > 0
    && hasFiniteRoadGeometry(route.route_geometry)
    && Array.isArray(route.navigation?.maneuvers)
    && route.navigation.maneuvers.length >= 2
  ));
  return { ...payload, alternative_routes: alternativeRoutes };
}

export async function fetchCorridors(): Promise<Fetched<Corridor[]>> {
  const payload = await getJSON<Envelope & { corridors: Corridor[] }>(
    '/api/corridors', CORRIDOR_TIMEOUT_MS);
  if (!payload?.corridors?.length) return wrap(null, INITIAL_CORRIDORS);
  return wrap(payload, payload.corridors);
}

function isPublishedFerrySchedule(value: unknown): value is FerrySchedule {
  if (!value || typeof value !== 'object') return false;
  const ferry = value as Partial<FerrySchedule>;
  return ferry.status === 'SCHEDULED'
    && ferry.data_source === 'official_timetable_snapshot'
    && ferry.live_status_available === false
    && ferry.available_seats === null
    && (ferry.capacity === null || ferry.capacity === undefined)
    && typeof ferry.operator === 'string'
    && ferry.ferry_name === ferry.operator
    && typeof ferry.departure_time === 'string'
    && typeof ferry.departure_port === 'string'
    && typeof ferry.arrival_port === 'string'
    && typeof ferry.schedule_source_url === 'string'
    && typeof ferry.schedule_last_verified_at === 'string';
}

function isFerryTimetableMetadata(value: unknown): value is FerryTimetableMetadata {
  if (!value || typeof value !== 'object') return false;
  const timetable = value as Partial<FerryTimetableMetadata>;
  return timetable.schema_version === 1
    && timetable.status === 'published_schedule_snapshot'
    && timetable.timezone === 'Asia/Jakarta'
    && typeof timetable.snapshot_id === 'string'
    && typeof timetable.last_verified_at === 'string'
    && typeof timetable.live_board_url === 'string'
    && typeof timetable.limitations === 'string'
    && Array.isArray(timetable.sources);
}

export async function fetchFerries(): Promise<FetchedFerrySchedule> {
  const payload = await getJSON<Envelope & {
    ferries: FerrySchedule[];
    timetable: FerryTimetableMetadata;
  }>(
    '/api/ferries', CORRIDOR_TIMEOUT_MS);
  if (payload?.data_source !== 'published_schedule'
    || !payload.ferries?.length
    || !payload.ferries.every(isPublishedFerrySchedule)
    || !isFerryTimetableMetadata(payload.timetable)) {
    return {
      ...wrap(null, offlineFerries()),
      timetable: PUBLISHED_FERRY_TIMETABLE_METADATA,
    };
  }
  return {
    ...wrap(payload, payload.ferries),
    timetable: payload.timetable,
  };
}

const FERRY_REFRESH_STATUSES = new Set<FerryRefreshReport['status']>([
  'checked', 'partial', 'failed_using_last_known_good', 'cached',
]);

const FERRY_REFRESH_SOURCE_STATUSES = new Set<FerryRefreshSourceResult['status']>([
  'verified_structure', 'unavailable_or_invalid', 'skipped_permission_required',
]);

function isFerryRefreshSourceResult(value: unknown): value is FerryRefreshSourceResult {
  if (!isRecord(value)) return false;
  return typeof value.source_id === 'string'
    && typeof value.authority === 'string'
    && typeof value.kind === 'string'
    && typeof value.url === 'string'
    && typeof value.permission_status === 'string'
    && FERRY_REFRESH_SOURCE_STATUSES.has(value.status as FerryRefreshSourceResult['status'])
    && typeof value.checked_at === 'string'
    && (value.http_status === null || isNonNegativeInteger(value.http_status))
    && typeof value.note === 'string';
}

function isFerryRefreshReport(value: unknown): value is FerryRefreshReport {
  if (!isRecord(value) || !isRecord(value.summary)) return false;
  return typeof value.refresh_id === 'string'
    && FERRY_REFRESH_STATUSES.has(value.status as FerryRefreshReport['status'])
    && typeof value.started_at === 'string'
    && typeof value.finished_at === 'string'
    && value.refresh_scope === 'fixed_official_allowlist'
    && Array.isArray(value.source_results)
    && value.source_results.length > 0
    && value.source_results.every(isFerryRefreshSourceResult)
    && isNonNegativeInteger(value.summary.verified)
    && isNonNegativeInteger(value.summary.failed)
    && isNonNegativeInteger(value.summary.permission_gated)
    && typeof value.schedule_applied === 'boolean'
    && typeof value.last_known_good_active === 'boolean'
    && typeof value.promotion_requirement === 'string'
    && typeof value.data_changed === 'boolean'
    && typeof value.limitations === 'string';
}

/**
 * Recheck the reviewed official-source allowlist and accept the backend's
 * ferry, timetable and terminal planning outputs as one atomic response.
 */
export async function refreshOfficialFerrySources(): Promise<FetchedFerryRefresh> {
  const payload = await getJSON<Envelope & {
    refresh: FerryRefreshReport;
    ferries: FerrySchedule[];
    ports: PortStatus[];
    timetable: FerryTimetableMetadata;
  }>('/api/ferry-refresh', FERRY_REFRESH_TIMEOUT_MS, {
    method: 'POST',
    headers: { Accept: 'application/json' },
  });

  if (payload?.data_source !== 'published_schedule'
    || !Array.isArray(payload.ferries)
    || payload.ferries.length === 0
    || !payload.ferries.every(isPublishedFerrySchedule)
    || !Array.isArray(payload.ports)
    || !isFerryTimetableMetadata(payload.timetable)
    || !isFerryRefreshReport(payload.refresh)) {
    throw new Error('Official-source refresh returned no valid coordinated planning update.');
  }

  const seed = scheduleInformedPortSeed();
  const mergedPorts = mergePortIntelligence(payload.ports, seed);
  const hasKnownTerminal = payload.ports.some(port => (
    isRecord(port)
    && typeof port.port_name === 'string'
    && BROWSER_PORT_CONFIG.some(known => known.port_name === port.port_name)
  ));
  if (!hasKnownTerminal) {
    throw new Error('Official-source refresh did not return a recognised terminal update.');
  }

  return {
    ...wrap(payload, payload.ferries),
    timetable: payload.timetable,
    ports: mergedPorts,
    refresh: payload.refresh,
  };
}

const BROWSER_PORT_CONFIG = [
  { port_name: 'Batam Centre', terminal_code: 'BCT', officialUrl: 'https://batamport.bpbatam.go.id/batam-centre/' },
  { port_name: 'HarbourBay', terminal_code: 'HBT', officialUrl: 'https://batamport.bpbatam.go.id/harbour-bay/' },
  { port_name: 'Sekupang', terminal_code: 'SKP', officialUrl: 'https://batamport.bpbatam.go.id/sekupang/' },
  { port_name: 'Nongsa Pura', terminal_code: 'NPT', officialUrl: 'https://batamport.bpbatam.go.id/nongsapura/' },
] as const;

/** Published terminal references and current timetable selection; no invented queues. */
export function scheduleInformedPortSeed(at: Date = new Date()): PortStatus[] {
  const publishedDepartures = offlineFerries(at, at, 24);
  return BROWSER_PORT_CONFIG.map(terminal => {
    const nextDeparture = publishedDepartures.find(ferry => ferry.departure_port === terminal.port_name);
    return {
      port_name: terminal.port_name, terminal_code: terminal.terminal_code,
      passenger_queue_mins: null, customs_processing_mins: null, freight_clearance_mins: null,
      active_berths: null, total_berths: null, status: null, next_vessel: null,
      next_sailing_in_mins: nextDeparture?.minutes_until_departure ?? null,
      next_operator: nextDeparture?.operator ?? null,
      data_source: 'published_terminal_reference', observed: false,
      estimate_basis: 'Next published timetable departure only; operating conditions are not observed.',
      official_reference_url: terminal.officialUrl,
      limitations: 'No observed queue, clearance, berth occupancy or live vessel feed is connected.',
    };
  });
}

function mergePortIntelligence(payloadPorts: unknown[], seed: PortStatus[]): PortStatus[] {
  return seed.map(fallback => {
    const candidate = payloadPorts.find(item => isRecord(item) && item.port_name === fallback.port_name);
    if (!isRecord(candidate)) return fallback;
    // Existing adapters supply schedule estimates, not measured operations.
    // Preserve published references/sailings but do not promote those estimates.
    return {
      ...fallback,
      next_sailing_in_mins: isNonNegativeInteger(candidate.next_sailing_in_mins)
        ? candidate.next_sailing_in_mins : fallback.next_sailing_in_mins,
      next_operator: typeof candidate.next_operator === 'string'
        ? candidate.next_operator : fallback.next_operator,
    };
  });
}

export async function fetchPortIntelligence(): Promise<Fetched<PortStatus[]>> {
  const seed = scheduleInformedPortSeed();
  const payload = await getJSON<Envelope & { ports: PortStatus[] }>(
    '/api/port-intelligence', CORRIDOR_TIMEOUT_MS);
  if (!Array.isArray(payload?.ports)) {
    return wrap(null, seed);
  }
  const merged = mergePortIntelligence(payload.ports, seed);
  const hasKnownTerminal = payload.ports.some(port => (
    isRecord(port)
    && typeof port.port_name === 'string'
    && BROWSER_PORT_CONFIG.some(known => known.port_name === port.port_name)
  ));
  return wrap(hasKnownTerminal ? payload : null, merged);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isInteger(value) && Number(value) >= 0;
}

function isFiniteNonNegative(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0;
}

function isOperationsSummaryPayload(value: unknown): value is OperationsSummary {
  if (!isRecord(value)
    || typeof value.overall_network_status !== 'string'
    || !(value.average_congestion_index === null || isFiniteNonNegative(value.average_congestion_index))
    || (typeof value.average_congestion_index === 'number' && value.average_congestion_index > 100)
    || !(value.active_bottlenecks === null || isNonNegativeInteger(value.active_bottlenecks))
    || !(value.total_co2_reduced_today_kg === null || isFiniteNonNegative(value.total_co2_reduced_today_kg))
    || !(value.active_ferry_sailings === null || isNonNegativeInteger(value.active_ferry_sailings))
    || !Array.isArray(value.alerts)
    || !value.alerts.every(alert => (
      isRecord(alert)
      && typeof alert.id === 'string'
      && ['WARNING', 'INFO', 'CRITICAL'].includes(String(alert.severity))
      && typeof alert.title === 'string'
      && typeof alert.message === 'string'
      && typeof alert.timestamp === 'string'
    ))) return false;

  const coverage = value.road_network_coverage;
  if (coverage !== undefined && (!isRecord(coverage)
    || coverage.source !== 'OpenStreetMap' || coverage.scope !== 'bundled_batam_routing_graph'
    || ![coverage.mapped_nodes, coverage.directed_edges].every(isNonNegativeInteger)
    || !(coverage.mapped_source_ways === null || isNonNegativeInteger(coverage.mapped_source_ways))
    || !(coverage.live_camera_edge_count === null || (isNonNegativeInteger(coverage.live_camera_edge_count)
      && coverage.live_camera_edge_count <= (coverage.directed_edges as number)))
    || !(coverage.live_camera_edge_coverage_percent === null || (isFiniteNonNegative(coverage.live_camera_edge_coverage_percent)
      && coverage.live_camera_edge_coverage_percent <= 100))
    || coverage.complete_live_coverage !== false)) return false;
  const estimates = value.camera_traffic_summary;
  if (estimates !== undefined && (!isRecord(estimates)
    || typeof estimates.status !== 'string' || typeof estimates.source !== 'string' || typeof estimates.scope !== 'string'
    || !(estimates.configured_cameras === null || isNonNegativeInteger(estimates.configured_cameras))
    || ![estimates.fresh_cameras, estimates.validated_cameras, estimates.estimated_cameras].every(isNonNegativeInteger)
    || (estimates.configured_cameras !== null && (estimates.fresh_cameras as number) > (estimates.configured_cameras as number))
    || (estimates.validated_cameras as number) + (estimates.estimated_cameras as number) !== estimates.fresh_cameras
    || !(estimates.mean_congestion_index === null || (isFiniteNonNegative(estimates.mean_congestion_index) && estimates.mean_congestion_index <= 100))
    || !(estimates.vehicle_detections === null || isNonNegativeInteger(estimates.vehicle_detections))
    || !['latest_observed_at', 'oldest_observed_at', 'valid_until'].every(key => estimates[key] === undefined
      || estimates[key] === null || (typeof estimates[key] === 'string' && Number.isFinite(Date.parse(estimates[key] as string))))
    || estimates.measured_road_speed !== false || typeof estimates.independently_validated !== 'boolean')) return false;

  const methodology = value.operations_methodology;
  if (!isRecord(methodology)
    || methodology.observed !== false
    || typeof methodology.source !== 'string'
    || !isRecord(methodology.scopes)
    || typeof methodology.scopes.network !== 'string'
    || typeof methodology.scopes.emissions !== 'string'
    || typeof methodology.scopes.ferries !== 'string') return false;

  const optionalNumbers = [
    value.modeled_avoidable_emissions_opportunity_kg_today,
    value.projected_full_day_co2_kg,
    value.modeled_projected_full_day_avoidable_emissions_kg,
    value.live_co2_rate_kg_hr,
  ];
  if (optionalNumbers.some(item => item !== undefined && item !== null && !isFiniteNonNegative(item))) return false;

  for (const candidate of [value.co2_by_corridor_kg, value.co2_by_vehicle_type]) {
    if (candidate !== undefined && (
      !isRecord(candidate)
      || Object.values(candidate).some(item => !isFiniteNonNegative(item))
    )) return false;
  }
  return value.hourly_co2_distribution === undefined
    || (Array.isArray(value.hourly_co2_distribution)
      && value.hourly_co2_distribution.every(item => (
        isRecord(item)
        && typeof item.hour === 'string'
        && isFiniteNonNegative(item.baseline_co2)
        && isFiniteNonNegative(item.optimized_co2)
      )));
}


export async function fetchOperationsSummary(): Promise<Fetched<OperationsSummary>> {
  const payload = await getJSON<OperationsSummary>('/api/operations', CORRIDOR_TIMEOUT_MS);
  if (!isOperationsSummaryPayload(payload)) return wrap(null, UNAVAILABLE_OPERATIONS);
  return wrap(payload, payload);
}

export async function fetchCorridorRoutes(): Promise<Fetched<CorridorRoute[]>> {
  const payload = await getJSON<Envelope & { routes: CorridorRoute[] }>(
    '/api/corridor-routes', ROUTE_TIMEOUT_MS);
  if (!payload?.routes?.length) return wrap(null, []);
  return wrap(payload, payload.routes);
}

export async function fetchRouteLocations(): Promise<Fetched<RouteLocation[]>> {
  const payload = await getJSON<Envelope & { locations: RouteLocation[] }>(
    '/api/route-locations', CORRIDOR_TIMEOUT_MS);
  if (!payload?.locations?.length) return wrap(null, ROUTE_LOCATIONS);
  return wrap(payload, payload.locations);
}

export function googleRouteBenchmarkEnabled(): boolean {
  return import.meta.env.VITE_ENABLE_GOOGLE_BENCHMARK === 'true';
}

function isRouteBenchmarkResult(value: unknown): value is RouteBenchmarkResult {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<RouteBenchmarkResult>;
  return candidate.data_source === 'google_routes_v2_text_benchmark'
    && candidate.attribution === 'Google Maps'
    && typeof candidate.policy_url === 'string'
    && candidate.policy_url.startsWith('https://')
    && candidate.cacheable === false
    && candidate.persisted === false
    && candidate.training_eligible === false
    && candidate.map_overlay_allowed === false
    && Array.isArray(candidate.routes)
    && candidate.routes.length > 0
    && candidate.routes.every((route) => (
      typeof route?.id === 'string'
      && Number.isFinite(route.duration_mins)
      && Number.isFinite(route.distance_km)
      && typeof route.summary === 'string'
      && Array.isArray(route.route_labels)
    ));
}

export async function requestRouteBenchmark(
  origin: Pick<RouteEndpointSnapshot, 'lat' | 'lng'>,
  destination: Pick<RouteEndpointSnapshot, 'lat' | 'lng'>,
  routePreference: RoutePreference,
  accessToken?: string | null,
): Promise<RouteBenchmarkResult> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/route-benchmark`, {
      method: 'POST',
      cache: 'no-store',
      signal: AbortSignal.timeout(ROUTE_TIMEOUT_MS),
      headers: routeRequestHeaders(accessToken),
      body: JSON.stringify({
        origin_lat: origin.lat,
        origin_lng: origin.lng,
        destination_lat: destination.lat,
        destination_lng: destination.lng,
        route_preference: routePreference,
      }),
    });
  } catch {
    throw new ApiRequestError('Route benchmark is temporarily unavailable.', 503);
  }

  if (!response.ok) {
    let detail = response.status === 404
      ? 'Route benchmark is disabled.'
      : response.status === 503
        ? 'Route benchmark is temporarily unavailable.'
        : `Route benchmark request failed (${response.status}).`;
    try {
      const body = await response.json() as { detail?: unknown };
      if (typeof body.detail === 'string' && body.detail.trim()) detail = body.detail;
    } catch {
      // Keep the status-specific, provider-neutral message for non-JSON errors.
    }
    throw new ApiRequestError(detail, response.status);
  }

  const payload = await response.json() as unknown;
  if (!isRouteBenchmarkResult(payload)) {
    throw new ApiRequestError('Route benchmark returned an invalid metric-only response.', 503);
  }
  return payload;
}

/** Forward the operator session; the server decides whether authentication is required. */
function routeRequestHeaders(accessToken?: string | null): Record<string, string> {
  return {
    'Content-Type': 'application/json',
    ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
  };
}

export async function requestRouteOptimization(
  originId: string,
  destinationId: string,
  vehicleType: VehicleType,
  hour: number = 14,
  weather: number = 0,
  routePreference: RoutePreference = 'BALANCED',
  schedule?: RouteScheduleOptions,
  accessToken?: string | null,
): Promise<Fetched<RouteOptimizationResult>> {
  const origin = ROUTE_LOCATIONS.find(location => location.id === originId) ?? ROUTE_LOCATIONS[0];
  const destination = ROUTE_LOCATIONS.find(location => location.id === destinationId)
    ?? ROUTE_LOCATIONS[1];
  const seededCorridor = INITIAL_CORRIDORS.find(corridor => (
    (corridor.origin === originId && corridor.destination === destinationId)
      || (corridor.origin === destinationId && corridor.destination === originId)
  ));
  const corridorIdx = seededCorridor
    ? (CORRIDOR_INDEX[seededCorridor.id] ?? 0)
    : Math.abs(Math.round((origin.lat * 1000) + (origin.lng * 100))) % 5;
  const effectiveHour = hourFromSchedule(schedule) ?? hour;
  const requestBody = JSON.stringify({
    origin_id: originId, destination_id: destinationId,
    vehicle_type: vehicleType, hour: effectiveHour, weather,
    route_preference: routePreference, ...scheduleRequestFields(schedule),
  });
  const exactSchedule = hasExactSchedule(schedule);
  if (exactSchedule) {
    const scheduledPayload = await getJSON<Envelope & RouteOptimizationResult>(
      '/api/optimize-route',
      ROUTE_TIMEOUT_MS,
      {
        method: 'POST',
        headers: routeRequestHeaders(accessToken),
        body: requestBody,
      },
      true,
    ).then(validatedRoadPayload);
    if (scheduledPayload) return wrap(scheduledPayload, scheduledPayload);
  }
  const roadHedge = bundledRoadHedge(
    [origin.lat, origin.lng],
    [destination.lat, destination.lng],
    destination.name,
    browserRoutingContext(vehicleType, effectiveHour, weather, corridorIdx, routePreference),
  );
  const backendAbort = new AbortController();
  const sources = await raceRoadRouteSources(
    exactSchedule ? Promise.resolve(null) : getJSON<Envelope & RouteOptimizationResult>(
      '/api/optimize-route',
      ROUTE_TIMEOUT_MS,
      {
        method: 'POST',
        signal: backendAbort.signal,
        headers: routeRequestHeaders(accessToken),
        body: requestBody,
      },
      true,
    ).then(validatedRoadPayload),
    roadHedge,
    () => backendAbort.abort(),
  );

  if (sources.payload) {
    return wrap(sources.payload, sources.payload);
  }
  const fallback = offlineOptimize(originId, destinationId, vehicleType, effectiveHour, weather);
  if (!sources.roadPlan) throw roadRoutingUnavailable();
  return wrap(null, simulatedSchedule(applyBundledRoadPlan(
    fallback,
    sources.roadPlan,
    vehicleType,
    effectiveHour,
    weather,
    corridorIdx,
    routePreference,
    !hasExactSchedule(schedule),
  ), schedule));
}

/** Retrieve the immutable route assigned to a driver by its seven-character code. */
export async function requestPersistedRoute(
  routeCode: string,
  accessToken: string | null,
): Promise<Fetched<RouteOptimizationResult>> {
  const normalizedCode = routeCode.trim().toUpperCase();
  if (!/^[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{7}$/.test(normalizedCode)) {
    throw new ApiRequestError('Enter the seven-character route code supplied by dispatch.', 400);
  }

  // Drivers are guests by design: the dispatch code is the capability, so no
  // credential is sent or required. The session token is still forwarded when
  // one happens to exist, purely so server logs can attribute the read.
  const headers: Record<string, string> = {};
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;

  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/routes/${normalizedCode}`, {
      signal: AbortSignal.timeout(ROUTE_TIMEOUT_MS),
      headers,
    });
  } catch {
    throw new ApiRequestError('The assigned route could not be reached. Try again.', 503);
  }
  if (!response.ok) {
    let detail = response.status === 404
      ? 'No assigned route matches that code.'
      : response.status === 401 || response.status === 403
        ? 'Your account is not authorized to retrieve this assigned route.'
        : 'The assigned route service is temporarily unavailable. Try again.';
    try {
      const body = await response.json() as { detail?: unknown };
      if (typeof body.detail === 'string' && body.detail.trim()) detail = body.detail;
    } catch {
      // Keep the status-specific message for non-JSON responses.
    }
    throw new ApiRequestError(detail, response.status);
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new ApiRequestError('The assigned route response is incomplete. Try again.', 503);
  }
  const payload = validatedRoadPayload(body as RouteOptimizationResult);
  if (!payload) throw new ApiRequestError('The assigned route response is incomplete.', 503);
  return wrap(payload as RouteOptimizationResult & Envelope, payload);
}
/**
 * List recent route-solver runs for the signed-in account.
 *
 * Requires a session: the server returns every account's runs to an
 * administrator and only the caller's own to anyone else, so this must never
 * be called without a token in the hope of a public listing.
 */
export async function fetchRouteRunHistory(
  accessToken: string,
  options: { limit?: number; mine?: boolean } = {},
): Promise<RouteRunHistory> {
  const params = new URLSearchParams();
  if (options.limit) params.set('limit', String(options.limit));
  if (options.mine) params.set('mine', 'true');
  const query = params.toString();

  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/routes${query ? `?${query}` : ''}`, {
      signal: AbortSignal.timeout(CORRIDOR_TIMEOUT_MS),
      headers: { Authorization: `Bearer ${accessToken}` },
    });
  } catch {
    throw new ApiRequestError('Route history could not be reached. Try again.', 503);
  }
  if (!response.ok) {
    let detail = response.status === 401 || response.status === 403
      ? 'Sign in again to view route history.'
      : 'Route history is unavailable right now.';
    try {
      const body = await response.json() as { detail?: unknown };
      if (typeof body.detail === 'string' && body.detail.trim()) detail = body.detail;
    } catch {
      // Keep the status-specific message for non-JSON responses.
    }
    throw new ApiRequestError(detail, response.status);
  }
  const body = await response.json() as { data?: RouteRunHistory } & Partial<RouteRunHistory>;
  const payload = body.data ?? body;
  return {
    runs: Array.isArray(payload.runs) ? payload.runs : [],
    scope: payload.scope === 'all_accounts' ? 'all_accounts' : 'own_account',
    ...(payload.history_storage ? { history_storage: payload.history_storage } : {}),
  };
}

const FERRY_BOARDING_CUTOFF_MINS = 15;

function browserRoutingContext(
  vehicleType: VehicleType,
  hour: number,
  weather: number,
  corridorIdx: number,
  routePreference: RoutePreference,
): OfflineRoutingContext {
  const departure = nextBatamHour(hour);
  const weekday = batamParts(departure).weekday;
  const weekend = weekday === 0 || weekday === 6 ? 1 : 0;
  const current = predictLocal(hour, weekend, weather, 0, corridorIdx);
  const later = predictLocal(hour + 0.5, weekend, weather, 0, corridorIdx);
  return {
    vehicleType,
    hour,
    weather,
    routePreference,
    networkCongestionScore: current.current_score,
    networkCongestionScoreAfter30: later.current_score,
  };
}

type ComputedRoadMetrics = Omit<Pick<
  RouteOptimizationResult,
  | 'planned_departure'
  | 'congestion_prediction'
  | 'estimated_travel_time_mins'
  | 'customs_buffer_mins'
  | 'total_eta_mins'
  | 'co2_emissions_kg'
  | 'co2_saved_kg'
  | 'optimal_departure'
>, 'planned_departure'> & { baseTimeMins: number; planned_departure: string };

async function bundledRoadPlan(
  origin: [number, number],
  destination: [number, number],
  destinationName: string,
  context: OfflineRoutingContext,
): Promise<OfflineRoadPlan | null> {
  try {
    const plan = await planWithBundledRoadGraph(origin, destination, destinationName, context);
    return plan.routes.length ? plan : null;
  } catch (error) {
    console.warn('[crossflow] bundled browser road routing unavailable:', error);
    return null;
  }
}

interface BundledRoadHedge {
  cancel: () => void;
  delayed: Promise<OfflineRoadPlan | null>;
  result: () => Promise<OfflineRoadPlan | null>;
}

/**
 * Start the heavier browser graph only when the backend has not answered
 * quickly. A successful warm API request therefore avoids the graph download,
 * while a slow transport failure overlaps its timeout with local A* work.
 */
function bundledRoadHedge(
  origin: [number, number],
  destination: [number, number],
  destinationName: string,
  context: OfflineRoutingContext,
): BundledRoadHedge {
  let timer: ReturnType<typeof globalThis.setTimeout> | null = null;
  let planPromise: Promise<OfflineRoadPlan | null> | null = null;
  let delayedSettled = false;
  let settleDelayed: (plan: OfflineRoadPlan | null) => void = () => undefined;
  const start = () => {
    if (!planPromise) {
      planPromise = bundledRoadPlan(origin, destination, destinationName, context);
    }
    return planPromise;
  };
  const settleOnce = (plan: OfflineRoadPlan | null) => {
    if (delayedSettled) return;
    delayedSettled = true;
    settleDelayed(plan);
  };
  const delayed = new Promise<OfflineRoadPlan | null>((resolve) => {
    settleDelayed = resolve;
    timer = globalThis.setTimeout(() => {
      timer = null;
      // bundledRoadPlan handles worker/asset errors and always settles to null.
      void start().then(settleOnce);
    }, 1_200);
  });

  return {
    cancel: () => {
      if (timer !== null) globalThis.clearTimeout(timer);
      timer = null;
      settleOnce(null);
    },
    delayed,
    result: () => {
      if (timer !== null) globalThis.clearTimeout(timer);
      timer = null;
      const result = start();
      void result.then(settleOnce);
      return result;
    },
  };
}

type BackendRouteOutcome<T> =
  | { kind: 'backend'; payload: T | null }
  | { kind: 'backend-error'; error: unknown };

type LocalRouteOutcome = { kind: 'local'; roadPlan: OfflineRoadPlan | null };

/**
 * Return whichever valid road engine finishes first after the short warm-API
 * grace period. Rejections become values before the race, so a late backend
 * validation error or timeout cannot become an unhandled rejection.
 */
async function raceRoadRouteSources<T>(
  backendRequest: Promise<T | null>,
  roadHedge: BundledRoadHedge,
  cancelBackend: () => void,
): Promise<{ payload: T | null; roadPlan: OfflineRoadPlan | null }> {
  const backendOutcome: Promise<BackendRouteOutcome<T>> = backendRequest.then(
    (payload) => ({ kind: 'backend', payload }),
    (error: unknown) => ({ kind: 'backend-error', error }),
  );
  const localOutcome: Promise<LocalRouteOutcome> = roadHedge.delayed.then(
    (roadPlan) => ({ kind: 'local', roadPlan }),
  );
  const first = await Promise.race([backendOutcome, localOutcome]);

  if (first.kind === 'backend-error') {
    roadHedge.cancel();
    throw first.error;
  }
  if (first.kind === 'backend') {
    if (first.payload) {
      roadHedge.cancel();
      return { payload: first.payload, roadPlan: null };
    }
    return { payload: null, roadPlan: await roadHedge.result() };
  }
  if (first.roadPlan) {
    cancelBackend();
    return { payload: null, roadPlan: first.roadPlan };
  }

  const backend = await backendOutcome;
  if (backend.kind === 'backend-error') throw backend.error;
  return { payload: backend.payload, roadPlan: null };
}

function roadRoutingUnavailable(): ApiRequestError {
  return new ApiRequestError(
    'A drivable road route could not be calculated. Check the selected Batam road points and try again.',
    503,
  );
}

function routeMetrics(
  distanceKm: number,
  vehicleType: VehicleType,
  hour: number,
  weather: number,
  corridorIdx: number,
  navigation?: NavigationData,
  accessTimeMins = 0,
  terminalBufferMins = 0,
  routedTiming?: {
    free_flow_time_mins?: number;
    estimated_travel_time_mins?: number;
    estimated_travel_time_after_30_mins?: number;
    congestion_delay_after_30_mins?: number;
    routing_cost_breakdown?: { congestion_delay_mins?: number };
  },
  allowDeferral = true,
): ComputedRoadMetrics {
  const maneuverDelayMins = (navigation?.maneuvers ?? []).reduce((delay, maneuver) => {
    if (maneuver.type === 'turn') return delay + 0.2;
    if (maneuver.type === 'roundabout') return delay + 0.35;
    if (maneuver.type !== 'depart' && maneuver.type !== 'arrive') return delay + 0.05;
    return delay;
  }, 0);
  const fallbackBaseTimeMins = Math.max(
    1,
    Math.round(((distanceKm / 35 * 60) + maneuverDelayMins) * 10) / 10,
  );
  const baseTimeMins = routedTiming?.free_flow_time_mins ?? fallbackBaseTimeMins;
  const profile = vehicleProfile(vehicleType);
  const departure = nextBatamHour(hour);
  const departureWeekday = batamParts(departure).weekday;
  const isWeekend = departureWeekday === 0 || departureWeekday === 6 ? 1 : 0;
  const now = predictLocal(hour, isWeekend, weather, 0, corridorIdx);
  const later = predictLocal(hour + 0.5, isWeekend, weather, 0, corridorIdx);
  const fallbackTimeFactor = 1 / profile.speedFactor;
  const travelNow = routedTiming?.estimated_travel_time_mins ?? Math.round(
    (baseTimeMins + now.estimated_delay_mins) * fallbackTimeFactor * 10,
  ) / 10;
  const travelLater = routedTiming?.estimated_travel_time_after_30_mins ?? Math.round(
    (baseTimeMins + later.estimated_delay_mins) * fallbackTimeFactor * 10,
  ) / 10;
  const defer = allowDeferral && travelLater <= travelNow - 0.5 && later.current_score < now.current_score;
  const savedMins = Math.max(0, Math.round((travelNow - travelLater) * 10) / 10);
  const recommendedDeparture = defer
    ? new Date(departure.getTime() + 30 * 60_000)
    : departure;
  const forecastPrediction = defer ? later : now;
  const estimatedTravel = defer ? travelLater : travelNow;
  const currentCongestionDelay = routedTiming?.routing_cost_breakdown?.congestion_delay_mins
    ?? now.estimated_delay_mins;
  const laterCongestionDelay = routedTiming?.congestion_delay_after_30_mins
    ?? later.estimated_delay_mins;
  const modeledDelay = Math.max(
    0,
    Math.round((defer ? laterCongestionDelay : currentCongestionDelay) * 10) / 10,
  );
  const avoidedQueueMins = defer
    ? Math.max(0, currentCongestionDelay - laterCongestionDelay)
    : 0;

  return {
    baseTimeMins,
    planned_departure: toBatamIso(recommendedDeparture),
    congestion_prediction: {
      current_score: forecastPrediction.current_score,
      predicted_30min: forecastPrediction.predicted_30min,
      predicted_60min: forecastPrediction.predicted_60min,
      estimated_delay_mins: modeledDelay,
      status: forecastPrediction.status,
      risk_level: forecastPrediction.risk_level,
      trend: forecastPrediction.trend,
    },
    estimated_travel_time_mins: estimatedTravel,
    customs_buffer_mins: terminalBufferMins,
    total_eta_mins: Math.round(
      (estimatedTravel + terminalBufferMins + accessTimeMins) * 10,
    ) / 10,
    co2_emissions_kg: Number(
      (distanceKm * profile.co2KgPerKm + (modeledDelay / 60) * profile.idleCo2KgPerHour)
        .toFixed(2),
    ),
    co2_saved_kg: Number(
      ((avoidedQueueMins / 60) * profile.idleCo2KgPerHour).toFixed(2),
    ),
    optimal_departure: {
      recommended: defer ? 'DEFER_30_MINS' : 'DEPART_NOW',
      time_saved_mins: defer ? savedMins : 0,
      reason: defer
        ? `Congestion eases ${Math.round(now.current_score - later.current_score)} points over the next 30 minutes.`
        : 'Current road traffic is within optimal flow parameters.',
    },
  };
}

function destinationFerryPort(
  destination: { lat: number; lng: number },
): string | null {
  const nearestTerminal = ROUTE_LOCATIONS
    .filter(location => location.ferry_port)
    .map(location => ({
      location,
      distanceKm: haversineKm(destination.lat, destination.lng, location.lat, location.lng),
    }))
    .sort((a, b) => a.distanceKm - b.distanceKm)[0];
  return nearestTerminal && nearestTerminal.distanceKm < FERRY_TERMINAL_MATCH_KM
    ? nearestTerminal.location.ferry_port ?? null
    : null;
}

function offlineFerryConnection(
  destination: { lat: number; lng: number },
  plannedDeparture: string,
  totalEtaMins: number,
): { ferries: FerrySchedule[]; note?: string } {
  const destinationPort = destinationFerryPort(destination);
  const departure = new Date(plannedDeparture);
  const arrival = new Date(departure.getTime() + totalEtaMins * 60_000);
  const earliestBoardable = new Date(arrival.getTime() + FERRY_BOARDING_CUTOFF_MINS * 60_000);
  const ferries = offlineFerries(earliestBoardable, new Date())
    .filter(ferry => ferry.departure_port === (destinationPort ?? 'Batam Centre'))
    .slice(0, 3);
  return {
    ferries,
    note: destinationPort
      ? undefined
      : 'Destination is not a ferry port; showing Batam Centre departures for reference.',
  };
}

function breakdownForRecommendedDeparture(
  route: OfflineRoadPlan['routes'][number],
  metrics: ComputedRoadMetrics,
): RoutingCostBreakdown {
  if (metrics.optimal_departure.recommended !== 'DEFER_30_MINS') {
    return route.routing_cost_breakdown;
  }
  const congestionDelay = route.congestion_delay_after_30_mins;
  const modeledTravel = route.estimated_travel_time_after_30_mins;
  const suitability = route.routing_cost_breakdown.road_suitability_penalty_mins;
  return {
    ...route.routing_cost_breakdown,
    congestion_delay_mins: congestionDelay,
    modeled_travel_time_mins: modeledTravel,
    generalized_cost_mins: Math.round((modeledTravel + suitability) * 100) / 100,
  };
}

function applyBundledRoadPlan(
  fallback: RouteOptimizationResult,
  plan: OfflineRoadPlan,
  vehicleType: VehicleType,
  hour: number,
  weather: number,
  corridorIdx: number,
  routePreference: RoutePreference,
  allowDeferral = true,
): RouteOptimizationResult {
  const primary = plan.routes[0];
  const totalAccessDistanceKm = (plan.origin_snap_m + plan.destination_snap_m) / 1000;
  const totalAccessTimeMins = totalAccessDistanceKm / 5 * 60;
  const roundedAccessDistanceKm = Math.round(totalAccessDistanceKm * 1000) / 1000;
  const roundedAccessTimeMins = Math.round(totalAccessTimeMins * 10) / 10;
  const snapInfo = {
    origin_snap_m: Math.round(plan.origin_snap_m * 10) / 10,
    destination_snap_m: Math.round(plan.destination_snap_m * 10) / 10,
    origin_access_time_mins: Math.round((plan.origin_snap_m / 1000 / 5 * 60) * 10) / 10,
    destination_access_time_mins: Math.round(
      (plan.destination_snap_m / 1000 / 5 * 60) * 10,
    ) / 10,
    total_access_distance_km: roundedAccessDistanceKm,
    total_access_time_mins: roundedAccessTimeMins,
    assumed_access_speed_kph: 5,
    included_in_road_distance: false,
  };
  const destination = fallback.requested_destination ?? {
    lat: primary.geometry[primary.geometry.length - 1][0],
    lng: primary.geometry[primary.geometry.length - 1][1],
  };
  const terminalBufferMins = destinationFerryPort(destination)
    ? vehicleProfile(vehicleType).terminalBufferMins
    : 0;
  const metrics = routeMetrics(
    primary.distance_km,
    vehicleType,
    hour,
    weather,
    corridorIdx,
    primary.navigation,
    totalAccessTimeMins,
    terminalBufferMins,
    primary,
    allowDeferral,
  );
  const primaryFerries = offlineFerryConnection(
    destination,
    metrics.planned_departure,
    metrics.total_eta_mins,
  );
  const routingModelFor = (
    routeMetricsResult: ComputedRoadMetrics,
    route: OfflineRoadPlan['routes'][number],
  ) => ({
    ...route.routing_model,
    network_congestion_score: routeMetricsResult.congestion_prediction.current_score,
    weather,
  });
  const routingModel = routingModelFor(metrics, primary);
  const primaryBreakdown = breakdownForRecommendedDeparture(primary, metrics);
  const alternatives = plan.routes.slice(1).map((route) => {
    const optionMetrics = routeMetrics(
      route.distance_km,
      vehicleType,
      hour,
      weather,
      corridorIdx,
      route.navigation,
      totalAccessTimeMins,
      terminalBufferMins,
      route,
      allowDeferral,
    );
    const optionFerries = offlineFerryConnection(
      destination,
      optionMetrics.planned_departure,
      optionMetrics.total_eta_mins,
    );
    const optionBreakdown = breakdownForRecommendedDeparture(route, optionMetrics);
    return {
      id: route.id,
      route_type: 'ROAD_ALTERNATIVE' as const,
      name: route.name,
      description: route.description,
      distance_km: route.distance_km,
      estimated_travel_time_mins: optionMetrics.estimated_travel_time_mins,
      total_eta_mins: optionMetrics.total_eta_mins,
      co2_emissions_kg: optionMetrics.co2_emissions_kg,
      co2_saved_kg: optionMetrics.co2_saved_kg,
      route_geometry: route.geometry,
      route_data_source: 'bundled_client_openstreetmap',
      overlap_ratio: route.overlap_ratio,
      congestion_cost: optionBreakdown.generalized_cost_mins,
      objective_cost_s: route.objective_cost_s,
      route_preference: route.route_preference,
      route_preference_profile: route.route_preference_profile,
      free_flow_time_mins: route.free_flow_time_mins,
      estimated_travel_time_after_30_mins: route.estimated_travel_time_after_30_mins,
      congestion_delay_after_30_mins: route.congestion_delay_after_30_mins,
      routing_cost_breakdown: optionBreakdown,
      routing_model: routingModelFor(optionMetrics, route),
      local_road_distance_km: route.local_road_distance_km,
      local_road_segments: route.local_road_segments,
      local_road_audit: route.local_road_audit,
      navigation: route.navigation,
      next_matching_ferries: optionFerries.ferries,
      access_distance_km: roundedAccessDistanceKm,
      access_time_mins: roundedAccessTimeMins,
      snap_info: snapInfo,
    };
  });

  return {
    ...fallback,
    vehicle_profile: vehicleProfileSnapshot(vehicleType),
    route_preference: primary.route_preference ?? routePreference,
    route_preference_profile: primary.route_preference_profile,
    objective_cost_s: primary.objective_cost_s,
    generalized_cost_mins: primaryBreakdown.generalized_cost_mins,
    routing_cost_breakdown: primaryBreakdown,
    routing_model: routingModel,
    local_road_distance_km: primary.local_road_distance_km,
    local_road_segments: primary.local_road_segments,
    local_road_audit: primary.local_road_audit,
    corridor: {
      ...fallback.corridor,
      distance_km: primary.distance_km,
      base_time_mins: metrics.baseTimeMins,
      detour_ratio: fallback.corridor.straight_line_km
        ? Math.round((primary.distance_km / fallback.corridor.straight_line_km) * 100) / 100
        : null,
    },
    planned_departure: metrics.planned_departure,
    congestion_prediction: metrics.congestion_prediction,
    estimated_travel_time_mins: metrics.estimated_travel_time_mins,
    customs_buffer_mins: metrics.customs_buffer_mins,
    total_eta_mins: metrics.total_eta_mins,
    access_distance_km: roundedAccessDistanceKm,
    access_time_mins: roundedAccessTimeMins,
    co2_emissions_kg: metrics.co2_emissions_kg,
    co2_saved_kg: metrics.co2_saved_kg,
    optimal_departure: metrics.optimal_departure,
    next_matching_ferries: primaryFerries.ferries,
    ferry_connection_note: primaryFerries.note,
    route_geometry: primary.geometry,
    route_data_source: 'bundled_client_openstreetmap',
    snap_info: snapInfo,
    navigation: primary.navigation,
    alternative_routes: alternatives,
  };
}

/**
 * Offline route computation.
 *
 * Mirrors backend/services/route_solver.py::optimize_route, driven by the same
 * hour and weather the user selected.
 */
function offlineOptimize(
  originId: string,
  destinationId: string,
  vehicleType: VehicleType,
  hour: number,
  weather: number
): RouteOptimizationResult {
  const origin = ROUTE_LOCATIONS.find(location => location.id === originId) ?? ROUTE_LOCATIONS[0];
  const destination = ROUTE_LOCATIONS.find(location => location.id === destinationId)
    ?? ROUTE_LOCATIONS[1];
  const directSeeded = INITIAL_CORRIDORS.find(
    corridor => corridor.origin === origin.id && corridor.destination === destination.id,
  );
  const reverseSeeded = INITIAL_CORRIDORS.find(
    corridor => corridor.origin === destination.id && corridor.destination === origin.id,
  );
  const seeded = directSeeded ?? reverseSeeded;
  const key1 = `${origin.id}:${destination.id}`;
  const key2 = `${destination.id}:${origin.id}`;
  const bundledRoadGeom = CORRIDOR_STREET_GEOMETRIES[key1]
    ?? (CORRIDOR_STREET_GEOMETRIES[key2] ? [...CORRIDOR_STREET_GEOMETRIES[key2]].reverse() : null);
  const straightLineKm = haversineKm(origin.lat, origin.lng, destination.lat, destination.lng);
  const distanceKm = seeded?.distance_km ?? Math.round(straightLineKm * 100) / 100;
  const baseTimeMins = seeded?.base_time_mins ?? Math.max(
    5, Math.round(distanceKm / 35 * 60 * 10) / 10,
  );
  const corridorId = seeded?.id ?? `route:${origin.id}:${destination.id}`;
  const corridorName = directSeeded?.name ?? `${origin.name} -> ${destination.name}`;
  const idx = seeded
    ? (CORRIDOR_INDEX[seeded.id] ?? 0)
    : ROUTE_LOCATIONS.findIndex(location => location.id === origin.id) % 5;
  const profile = vehicleProfile(vehicleType);

  const departure = nextBatamHour(hour);
  const departureWeekday = batamParts(departure).weekday;
  const isWeekend = departureWeekday === 0 || departureWeekday === 6 ? 1 : 0;

  const now = predictLocal(hour, isWeekend, weather, 0, idx);
  const later = predictLocal(hour + 0.5, isWeekend, weather, 0, idx);

  const travelNow = Math.round(
    (baseTimeMins + now.estimated_delay_mins) / profile.speedFactor * 10) / 10;
  const travelLater = Math.round(
    (baseTimeMins + later.estimated_delay_mins) / profile.speedFactor * 10) / 10;

  const defer = later.current_score < now.current_score - 10;
  const savedMins = Math.max(0, Math.round((travelNow - travelLater) * 10) / 10);
  const recommendedDeparture = defer
    ? new Date(departure.getTime() + 30 * 60_000)
    : departure;
  const recommendedPrediction = defer ? later : now;
  const recommendedTravel = defer ? travelLater : travelNow;
  const terminalBufferMins = destination.ferry_port ? profile.terminalBufferMins : 0;
  const totalEta = Math.round(
    (recommendedTravel + terminalBufferMins) * 10,
  ) / 10;

  const arrival = new Date(recommendedDeparture.getTime() + totalEta * 60_000);
  const earliestBoardable = new Date(
    arrival.getTime() + FERRY_BOARDING_CUTOFF_MINS * 60_000,
  );
  const ferries = offlineFerries(earliestBoardable, new Date());
  const matchingFerries = destination.ferry_port
    ? ferries.filter(ferry => ferry.departure_port === destination.ferry_port).slice(0, 3)
    : ferries.filter(ferry => ferry.departure_port === 'Batam Centre').slice(0, 3);

  const roadGeom: [number, number][] = bundledRoadGeom
    ?? [[origin.lat, origin.lng], [destination.lat, destination.lng]];

  return {
    route_type: 'ROAD_ROUTE',
    corridor: {
      id: corridorId,
      name: corridorName,
      origin: origin.id,
      destination: destination.id,
      distance_km: distanceKm,
      base_time_mins: baseTimeMins,
      straight_line_km: Math.round(straightLineKm * 100) / 100,
      detour_ratio: straightLineKm > 0
        ? Math.round((distanceKm / straightLineKm) * 100) / 100
        : null,
    },
    vehicle_type: vehicleType,
    vehicle_profile: vehicleProfileSnapshot(vehicleType),
    planned_departure: toBatamIso(recommendedDeparture),
    congestion_prediction: {
      current_score: recommendedPrediction.current_score,
      predicted_30min: recommendedPrediction.predicted_30min,
      predicted_60min: recommendedPrediction.predicted_60min,
      estimated_delay_mins: recommendedPrediction.estimated_delay_mins,
      status: recommendedPrediction.status,
      risk_level: recommendedPrediction.risk_level,
      trend: recommendedPrediction.trend,
    },
    estimated_travel_time_mins: recommendedTravel,
    customs_buffer_mins: terminalBufferMins,
    total_eta_mins: totalEta,
    co2_emissions_kg: Number(
      (distanceKm * profile.co2KgPerKm
        + (recommendedPrediction.estimated_delay_mins / 60) * profile.idleCo2KgPerHour)
        .toFixed(2)),
    co2_saved_kg: Number(
      (((defer ? savedMins : 0) / 60) * profile.idleCo2KgPerHour).toFixed(2),
    ),
    optimal_departure: {
      recommended: defer ? 'DEFER_30_MINS' : 'DEPART_NOW',
      time_saved_mins: defer ? savedMins : 0,
      reason: defer
        ? `Congestion eases ${Math.round(now.current_score - later.current_score)} points over the next 30 minutes.`
        : 'Current corridor traffic is within optimal flow parameters.',
    },
    next_matching_ferries: matchingFerries,
    ferry_connection_note: destination.ferry_port
      ? undefined
      : 'This route does not terminate at a ferry port; showing Batam Centre departures for reference.',
    route_geometry: roadGeom,
    route_data_source: bundledRoadGeom ? 'bundled_openstreetmap' : 'offline_straight_line',
    requested_origin: {
      lat: origin.lat,
      lng: origin.lng,
      display_name: origin.name,
    },
    requested_destination: {
      lat: destination.lat,
      lng: destination.lng,
      display_name: destination.name,
    },
  };
}



function haversineKm(lat1: number, lng1: number, lat2: number, lng2: number): number {
  const earthRadiusKm = 6371.0088;
  const toRadians = (degrees: number) => degrees * Math.PI / 180;
  const phi1 = toRadians(lat1);
  const phi2 = toRadians(lat2);
  const deltaPhi = phi2 - phi1;
  const deltaLambda = toRadians(lng2 - lng1);
  const a = Math.sin(deltaPhi / 2) ** 2
    + Math.cos(phi1) * Math.cos(phi2) * Math.sin(deltaLambda / 2) ** 2;
  return 2 * earthRadiusKm * Math.asin(Math.sqrt(a));
}

// ---------------------------------------------------------------------------
// Free-form geocode + Batam road-network routing
// ---------------------------------------------------------------------------

export async function geocodeQuery(
  query: string,
  limit = 5,
): Promise<GeocodedLocation[]> {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  const payload = await getJSON<{ results: GeocodedLocation[] }>(
    `/api/geocode?${params}`, ROUTE_TIMEOUT_MS,
  );
  return payload?.results ?? [];
}

export async function reverseGeocode(lat: number, lng: number): Promise<FreeLocation> {
  const params = new URLSearchParams({ lat: String(lat), lng: String(lng) });
  const payload = await getJSON<{ result: FreeLocation }>(
    `/api/reverse-geocode?${params}`, CORRIDOR_TIMEOUT_MS,
  );
  return payload?.result ?? {
    lat, lng,
    display_name: `${lat.toFixed(5)}, ${lng.toFixed(5)}`,
    supported_region: locationRegion(lat, lng),
  };
}

export async function requestFreeRouteOptimization(
  origin: FreeLocation,
  destination: FreeLocation,
  vehicleType: VehicleType,
  hour: number = 14,
  weather: number = 0,
  routePreference: RoutePreference = 'BALANCED',
  schedule?: RouteScheduleOptions,
  accessToken?: string | null,
): Promise<Fetched<RouteOptimizationResult>> {
  const originRegion = locationRegion(origin.lat, origin.lng);
  const destinationRegion = locationRegion(destination.lat, destination.lng);
  if (!originRegion || !destinationRegion) {
    throw new ApiRequestError('Both route points must be within Singapore or Batam.', 400);
  }
  if (
    originRegion !== destinationRegion
    && isPassengerFerryIncompatibleVehicle(vehicleType)
  ) {
    throw new ApiRequestError(PASSENGER_FERRY_TRUCK_MESSAGE, 400);
  }
  const corridorIdx = Math.abs(Math.round((origin.lat * 1000 + origin.lng * 100))) % 5;
  const effectiveHour = hourFromSchedule(schedule) ?? hour;
  const requestBody = JSON.stringify({
    origin_lat: origin.lat,
    origin_lng: origin.lng,
    destination_lat: destination.lat,
    destination_lng: destination.lng,
    origin_name: origin.display_name,
    destination_name: destination.display_name,
    vehicle_type: vehicleType,
    hour: effectiveHour,
    weather,
    route_preference: routePreference,
    ...scheduleRequestFields(schedule),
  });

  // The bundled graph covers Batam only. Singapore local and cross-border
  // requests go to the multimodal backend directly, then fall back to a
  // deterministic, per-leg-labelled continuity plan instead of failing.
  if (originRegion === 'SINGAPORE' || destinationRegion === 'SINGAPORE') {
    const payload = await getJSON<Envelope & RouteOptimizationResult>(
      '/api/optimize-free-route',
      ROUTE_TIMEOUT_MS,
      {
        method: 'POST',
        headers: routeRequestHeaders(accessToken),
        body: requestBody,
      },
      true,
    ).then(validatedRoadPayload);
    if (payload) return wrap(payload, payload);
    try {
      return wrap(null, simulatedSchedule(offlineCrossBorderOptimize(
        origin, destination, vehicleType, effectiveHour, routePreference, new Date(), schedule,
      ), schedule));
    } catch (error) {
      throw new ApiRequestError(error instanceof Error ? error.message : 'The scheduled journey could not be planned.', 400);
    }
  }

  const exactSchedule = hasExactSchedule(schedule);
  if (exactSchedule) {
    const scheduledPayload = await getJSON<Envelope & RouteOptimizationResult>(
      '/api/optimize-free-route',
      ROUTE_TIMEOUT_MS,
      {
        method: 'POST',
        headers: routeRequestHeaders(accessToken),
        body: requestBody,
      },
      true,
    ).then(validatedRoadPayload);
    if (scheduledPayload) return wrap(scheduledPayload, scheduledPayload);
  }

  const roadHedge = bundledRoadHedge(
    [origin.lat, origin.lng],
    [destination.lat, destination.lng],
    destination.display_name,
    browserRoutingContext(vehicleType, effectiveHour, weather, corridorIdx, routePreference),
  );
  const backendAbort = new AbortController();
  const sources = await raceRoadRouteSources(
    exactSchedule ? Promise.resolve(null) : getJSON<Envelope & RouteOptimizationResult>(
      '/api/optimize-free-route',
      ROUTE_TIMEOUT_MS,
      {
        method: 'POST',
        signal: backendAbort.signal,
        headers: routeRequestHeaders(accessToken),
        body: requestBody,
      },
      true,
    ).then(validatedRoadPayload),
    roadHedge,
    () => backendAbort.abort(),
  );
  if (sources.payload) {
    return wrap(sources.payload, sources.payload);
  }
  const fallback = offlineOptimizeFree(origin, destination, vehicleType, effectiveHour, weather);
  if (!sources.roadPlan) {
    return wrap(null, simulatedSchedule(offlineCrossBorderOptimize(
      origin, destination, vehicleType, effectiveHour, routePreference, new Date(), schedule,
    ), schedule));
  }
  return wrap(null, simulatedSchedule(applyBundledRoadPlan(
    fallback,
    sources.roadPlan,
    vehicleType,
    effectiveHour,
    weather,
    corridorIdx,
    routePreference,
    !hasExactSchedule(schedule),
  ), schedule));
}

export async function requestMultiStopRouteOptimization(
  stops: Array<FreeLocation & { dwell_mins?: number }>,
  vehicleType: VehicleType,
  schedule: { departureAt?: string; arriveBy?: string },
  weather: number = 0,
  routePreference: RoutePreference = 'BALANCED',
  accessToken?: string | null,
): Promise<Fetched<RouteOptimizationResult>> {
  const payload = await getJSON<Envelope & RouteOptimizationResult>(
    '/api/optimize-multi-stop-route', MULTI_STOP_TIMEOUT_MS, {
      method: 'POST',
      headers: routeRequestHeaders(accessToken),
      body: JSON.stringify({
        stops: stops.map((stop) => ({
          lat: stop.lat, lng: stop.lng, name: stop.display_name,
          dwell_mins: stop.dwell_mins ?? 0,
        })),
        vehicle_type: vehicleType,
        ...(schedule.departureAt ? { departure_at: schedule.departureAt } : {}),
        ...(schedule.arriveBy ? { arrive_by: schedule.arriveBy } : {}),
        weather,
        route_preference: routePreference, optimize_order: false,
      }),
    }, true,
  ).then(validatedRoadPayload);
  // An aborted request and a malformed body both arrive here as a null payload,
  // and blaming the backend for a response the client stopped waiting for sent
  // this in exactly the wrong direction once already.
  if (!payload) {
    throw new ApiRequestError(
      'The multi-stop route did not arrive in time. Each stop adds a full '
      + 'road search, so a journey with several stops can take a few minutes.',
      504,
    );
  }
  if (payload.dispatch_available !== false
    && (typeof payload.route_code !== 'string' || !/^[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{7}$/.test(payload.route_code))) {
    throw new ApiRequestError('The backend returned no valid seven-character route code.', 503);
  }
  return wrap(payload, payload);
}

/**
 * Metric scaffold for the bundled free-form road planner. This straight-line
 * geometry is never returned to the UI: if both road engines fail, the caller
 * switches to the explicitly limited access-leg continuity route.
 */
function offlineOptimizeFree(
  origin: FreeLocation,
  destination: FreeLocation,
  vehicleType: VehicleType,
  hour: number,
  weather: number,
): RouteOptimizationResult {
  const distanceKm = Math.round(
    haversineKm(origin.lat, origin.lng, destination.lat, destination.lng) * 100,
  ) / 100;
  const baseTimeMins = Math.max(1, Math.round(distanceKm / 35 * 60 * 10) / 10);
  const profile = vehicleProfile(vehicleType);

  const departure = nextBatamHour(hour);
  const departureWeekday = batamParts(departure).weekday;
  const isWeekend = departureWeekday === 0 || departureWeekday === 6 ? 1 : 0;
  const corridorIdx = Math.abs(Math.round((origin.lat * 1000 + origin.lng * 100))) % 5;
  const now = predictLocal(hour, isWeekend, weather, 0, corridorIdx);
  const later = predictLocal(hour + 0.5, isWeekend, weather, 0, corridorIdx);
  const travelNow = Math.round(
    (baseTimeMins + now.estimated_delay_mins) / profile.speedFactor * 10,
  ) / 10;
  const travelLater = Math.round(
    (baseTimeMins + later.estimated_delay_mins) / profile.speedFactor * 10,
  ) / 10;
  const defer = later.current_score < now.current_score - 10;
  const savedMins = Math.max(0, Math.round((travelNow - travelLater) * 10) / 10);
  const recommendedDeparture = defer
    ? new Date(departure.getTime() + 30 * 60_000)
    : departure;
  const recommendedPrediction = defer ? later : now;
  const recommendedTravel = defer ? travelLater : travelNow;
  const destinationPort = destinationFerryPort(destination);
  const terminalBufferMins = destinationPort ? profile.terminalBufferMins : 0;
  const totalEta = Math.round(
    (recommendedTravel + terminalBufferMins) * 10,
  ) / 10;
  const arrival = new Date(recommendedDeparture.getTime() + totalEta * 60_000);
  const earliestBoardable = new Date(
    arrival.getTime() + FERRY_BOARDING_CUTOFF_MINS * 60_000,
  );
  const ferries = offlineFerries(earliestBoardable, new Date());
  const matchingFerries = ferries
    .filter(ferry => ferry.departure_port === (destinationPort ?? 'Batam Centre'))
    .slice(0, 3);

  return {
    route_type: 'ROAD_ROUTE',
    corridor: {
      id: 'free:offline',
      name: `${origin.display_name} -> ${destination.display_name}`,
      origin: 'free-origin',
      destination: 'free-destination',
      distance_km: distanceKm,
      base_time_mins: baseTimeMins,
      straight_line_km: distanceKm,
      detour_ratio: 1,
    },
    vehicle_type: vehicleType,
    vehicle_profile: vehicleProfileSnapshot(vehicleType),
    planned_departure: toBatamIso(recommendedDeparture),
    congestion_prediction: {
      current_score: recommendedPrediction.current_score,
      predicted_30min: recommendedPrediction.predicted_30min,
      predicted_60min: recommendedPrediction.predicted_60min,
      estimated_delay_mins: recommendedPrediction.estimated_delay_mins,
      status: recommendedPrediction.status,
      risk_level: recommendedPrediction.risk_level,
      trend: recommendedPrediction.trend,
    },
    estimated_travel_time_mins: recommendedTravel,
    customs_buffer_mins: terminalBufferMins,
    total_eta_mins: totalEta,
    co2_emissions_kg: Number(
      (distanceKm * profile.co2KgPerKm
        + (recommendedPrediction.estimated_delay_mins / 60) * profile.idleCo2KgPerHour)
        .toFixed(2),
    ),
    co2_saved_kg: Number(
      (((defer ? savedMins : 0) / 60) * profile.idleCo2KgPerHour).toFixed(2),
    ),
    optimal_departure: {
      recommended: defer ? 'DEFER_30_MINS' : 'DEPART_NOW',
      time_saved_mins: defer ? savedMins : 0,
      reason: defer
        ? `Congestion eases ${Math.round(now.current_score - later.current_score)} points over the next 30 minutes.`
        : 'Live road routing is unavailable; traffic timing is an offline estimate.',
    },
    next_matching_ferries: matchingFerries,
    ferry_connection_note: destinationPort
      ? undefined
      : 'Destination is not a ferry port; showing Batam Centre departures for reference.',
    route_geometry: [[origin.lat, origin.lng], [destination.lat, destination.lng]],
    route_data_source: 'offline_straight_line',
    requested_origin: origin,
    requested_destination: destination,
  };
}

// ---------------------------------------------------------------------------
// Live traffic
// ---------------------------------------------------------------------------

export async function fetchLiveTraffic(): Promise<Fetched<LiveTrafficData>> {
  const payload = await getJSON<Envelope & LiveTrafficData>(
    '/api/live-traffic', CORRIDOR_TIMEOUT_MS,
  );
  if (payload) return wrap(payload, payload);
  // Local continuity fallback: keep the overlay useful and honest when the
  // API cannot be reached. The previous fallback claimed to derive corridor
  // traffic but returned an empty array, producing "Traffic ON · 0 segments".
  const fallbackPoints: Record<string, [number, number]> = {
    'corridor-1': [1.103860, 104.039342],
    'corridor-2': [1.149066, 104.023257],
    'corridor-3': [1.119412, 104.020678],
    'corridor-4': [1.105226, 103.955302],
    'corridor-5': [1.116191, 104.099020],
  };
  const zones = offlineCongestionZones();
  const levelCounts = {
    SMOOTH: zones.filter(zone => zone.level === 'SMOOTH').length,
    HEAVY: zones.filter(zone => zone.level === 'HEAVY').length,
    SUPER_CONGESTED: zones.filter(zone => zone.level === 'SUPER_CONGESTED').length,
  };
  const emissionsPressureLevelCounts = {
    LOW: zones.filter(zone => zone.modeled_emissions_pressure.level === 'LOW').length,
    ELEVATED: zones.filter(zone => zone.modeled_emissions_pressure.level === 'ELEVATED').length,
    HIGH: zones.filter(zone => zone.modeled_emissions_pressure.level === 'HIGH').length,
  };
  return wrap(null, {
    segments: INITIAL_CORRIDORS.flatMap((corridor) => {
      const point = fallbackPoints[corridor.id];
      return point ? [{
        corridor_id: corridor.id,
        lat: point[0],
        lng: point[1],
        congestion_index: corridor.live_congestion_score,
        current_speed_kmh: null,
        free_flow_speed_kmh: null,
        source: 'simulated' as const,
      }] : [];
    }),
    zones,
    coverage: {
      hotspot_count: zones.length,
      level_counts: levelCounts,
      emissions_pressure_level_counts: emissionsPressureLevelCounts,
      emissions_pressure_model: EMISSIONS_PRESSURE_MODEL,
      method: 'local model areas blended with nearby corridor telemetry',
    },
    overall_source: 'local_model',
    tomtom_key_configured: false,
  });
}

// ---------------------------------------------------------------------------
// Historical congestion
// ---------------------------------------------------------------------------

function hasApiHistoryMetadata(value: unknown): value is ApiHistoryMetadata {
  if (!isRecord(value)
    || !Number.isInteger(value.window_days)
    || Number(value.window_days) < 1
    || Number(value.window_days) > 30
    || typeof value.observed !== 'boolean'
    || typeof value.contains_observed_samples !== 'boolean'
    || !isRecord(value.source_counts)
    || !isRecord(value.sources)
    || !(typeof value.latest_sample_at === 'string' || value.latest_sample_at === null)
    || !(isNonNegativeInteger(value.latest_sample_age_seconds)
      || value.latest_sample_age_seconds === null)
    || !['current', 'recent', 'stale', 'empty'].includes(String(value.freshness))
    || typeof value.freshness_basis !== 'string') return false;

  const sourceCounts = Object.entries(value.source_counts);
  const sources = Object.entries(value.sources);
  const sourceCountMap = value.source_counts;
  const observedFlags = sources
    .map(([, details]) => isRecord(details) && details.observed === true);
  if (sourceCounts.some(([, count]) => !isNonNegativeInteger(count))
    || sources.length !== sourceCounts.length
    || sources.some(([source, details]) => (
      !isRecord(details)
      || !isNonNegativeInteger(details.sample_count)
      || typeof details.observed !== 'boolean'
      || sourceCountMap[source] !== details.sample_count
    ))
    || value.contains_observed_samples !== observedFlags.some(Boolean)
    || value.observed !== (observedFlags.length > 0 && observedFlags.every(Boolean))) return false;

  const storage = value.storage;
  if (!isRecord(storage)
    || typeof storage.engine !== 'string'
    || typeof storage.durability !== 'string'
    || typeof storage.durable !== 'boolean'
    || typeof storage.shared_across_instances !== 'boolean'
    || typeof storage.fallback_to_memory !== 'boolean') return false;

  const syntheticSeed = value.synthetic_seed;
  return isRecord(syntheticSeed)
    && typeof syntheticSeed.source === 'string'
    && isNonNegativeInteger(syntheticSeed.version)
    && isNonNegativeInteger(syntheticSeed.days)
    && syntheticSeed.days > 0
    && typeof syntheticSeed.timezone === 'string'
    && (typeof syntheticSeed.generated_for_date === 'string'
      || syntheticSeed.generated_for_date === null)
    && syntheticSeed.observed === false;
}

function isHistoricalProfilePayload(
  payload: (Envelope & HistoricalProfile) | null,
  corridorId: string,
  days: number,
): payload is Envelope & HistoricalProfile {
  if (!payload
    || payload.corridor_id !== corridorId
    || payload.days_requested !== days
    || !Array.isArray(payload.hourly_profile)
    || payload.hourly_profile.length !== 24
    || !Array.isArray(payload.weekly_trend)
    || !hasApiHistoryMetadata(payload.history_metadata)
    || payload.history_metadata.window_days !== days) return false;

  const hours = new Set(payload.hourly_profile.map(bucket => bucket?.hour));
  return hours.size === 24 && payload.hourly_profile.every(bucket => (
    Number.isInteger(bucket?.hour)
    && bucket.hour >= 0
    && bucket.hour <= 23
    && (bucket.avg_score === null
      || (Number.isFinite(bucket.avg_score) && bucket.avg_score >= 0 && bucket.avg_score <= 100))
    && Number.isInteger(bucket.sample_count)
    && bucket.sample_count >= 0
  )) && payload.weekly_trend.every(day => (
    typeof day?.date === 'string'
    && Number.isFinite(day.avg_score)
    && day.avg_score >= 0
    && day.avg_score <= 100
    && Number.isInteger(day.sample_count)
    && day.sample_count >= 0
  ));
}

export async function fetchHistoricalCongestion(
  corridorId: string,
  days = 7,
  includeModelled = false,
): Promise<Fetched<HistoricalProfile>> {
  const params = new URLSearchParams({ corridor_id: corridorId, days: String(days) });
  if (includeModelled) params.set('include_modelled', 'true');
  const payload = await getJSON<Envelope & HistoricalProfile>(
    `/api/historical-congestion?${params}`, ROUTE_TIMEOUT_MS,
  );
  if (isHistoricalProfilePayload(payload, corridorId, days)) return wrap(payload, payload);
  const referenceTime = new Date().toISOString();
  return wrap(null, {
    corridor_id: corridorId,
    days_requested: days,
    hourly_profile: Array.from({ length: 24 }, (_, hour) => ({ hour, avg_score: null, sample_count: 0 })),
    weekly_trend: [],
    history_metadata: {
      observed: false,
      source: 'history_unavailable',
      model: 'crossflow_local_forecast_equation_v1',
      methodology: 'Observed traffic history could not be retrieved; no synthetic samples were substituted.',
      limitations: ['Connect an observed traffic source and a reachable history store.'],
      reference_time: referenceTime,
    },
  });
}

// ---------------------------------------------------------------------------
// AI Model Status & Retraining
// ---------------------------------------------------------------------------

function isModelMetricsPayload(value: unknown): value is ModelMetrics {
  if (!isRecord(value)
    || typeof value.is_trained !== 'boolean'
    || (value.retraining_enabled !== undefined
      && typeof value.retraining_enabled !== 'boolean')
    || !isNonNegativeInteger(value.total_samples)
    || typeof value.r2_score !== 'number'
    || !Number.isFinite(value.r2_score)
    || !isFiniteNonNegative(value.mae)
    || !isFiniteNonNegative(value.rmse)
    || !(typeof value.last_trained_at === 'string' || value.last_trained_at === null)
    || !isRecord(value.feature_importances)) return false;

  for (const field of ['observed_training_rows', 'observed_fit_rows', 'observed_calibration_rows', 'observed_evaluation_rows']) {
    if (value[field] !== undefined && !isNonNegativeInteger(value[field])) return false;
  }
  if (value.observed_evaluation_horizon_minutes !== undefined && (!isNonNegativeInteger(value.observed_evaluation_horizon_minutes)
    || value.observed_evaluation_horizon_minutes < 1 || value.observed_evaluation_horizon_minutes > 180)) return false;
  if (value.training_source_counts !== undefined && (!isRecord(value.training_source_counts)
    || !Object.values(value.training_source_counts).every(isNonNegativeInteger))) return false;
  if (value.observed_training !== undefined && !isObservedBacktestReport(value.observed_training)) return false;
  if (value.observed_artifact_status !== undefined) {
    const artifact = value.observed_artifact_status;
    if (!isRecord(artifact) || typeof artifact.configured !== 'boolean' || typeof artifact.restored !== 'boolean'
      || !['disabled', 'waiting_for_eligible_model', 'restored', 'restore_failed_previous_model_retained'].includes(String(artifact.status))) return false;
  }

  return Object.values(value.feature_importances).every(weight => (
    isFiniteNonNegative(weight) && weight <= 1
  ));
}

export async function fetchModelStatus(): Promise<Fetched<ModelMetrics>> {
  const payload = await getJSON<Envelope & { metrics: ModelMetrics }>(
    '/api/model-status', ROUTE_TIMEOUT_MS,
  );
  if (payload && isModelMetricsPayload(payload.metrics)) return wrap(payload, payload.metrics);
  return wrap(null, {
    is_trained: false, total_samples: 0, r2_score: 0, mae: 0, rmse: 0,
    last_trained_at: null, feature_importances: {},
  });
}

export async function requestModelRetrain(): Promise<Fetched<ModelMetrics>> {
  const payload = await getJSON<Envelope & { metrics: ModelMetrics }>(
    '/api/retrain-model',
    ROUTE_TIMEOUT_MS,
    { method: 'POST' },
    true,
  );
  if (payload && isModelMetricsPayload(payload.metrics)) return wrap(payload, payload.metrics);
  throw new ApiRequestError('The backend is unavailable; the model was not retrained.', 503);
}

function isObservedBacktestReport(value: unknown): value is ObservedBacktestReport {
  const dateOrNull = (date: unknown) => date === null || (typeof date === 'string' && Number.isFinite(Date.parse(date)));
  const nullableMetric = (metric: unknown, allowNegative = false) => metric === null
    || (typeof metric === 'number' && Number.isFinite(metric) && (allowNegative || metric >= 0));
  const validMetrics = (metrics: unknown): metrics is ObservedBacktestMetrics => isRecord(metrics)
    && isNonNegativeInteger(metrics.samples) && nullableMetric(metrics.mae_points)
    && nullableMetric(metrics.rmse_points) && nullableMetric(metrics.bias_points, true);
  const validPartition = (partition: unknown): partition is ObservedBacktestPartition => isRecord(partition)
    && isNonNegativeInteger(partition.rows) && dateOrNull(partition.start_at) && dateOrNull(partition.end_at);
  if (!isRecord(value) || value.schema_version !== 1
    || value.fixture_only === true || value.not_real_accuracy_evidence === true
    || typeof value.generated_at !== 'string' || !Number.isFinite(Date.parse(value.generated_at))
    || !['insufficient_observations', 'evaluated', 'eligible', 'activated'].includes(String(value.status))
    || value.validation_scope !== 'observed_chronological_backtest'
    || value.evaluation_subject !== 'private_observed_candidate_serving_pipeline'
    || value.running_model_evaluated !== false || typeof value.model_replaced !== 'boolean'
    || !isRecord(value.selection) || !isRecord(value.chronology) || !isRecord(value.activation)
    || !Array.isArray(value.assumptions) || !value.assumptions.every(item => typeof item === 'string')) return false;
  const selection = value.selection;
  if (!['input_rows', 'unique_rows', 'eligible_rows', 'observed_rows', 'selected_rows', 'excluded_non_observed_rows',
    'excluded_unsupported_corridor_rows', 'duplicate_rows', 'distinct_timestamps'].every(key => isNonNegativeInteger(selection[key]))
    || !isRecord(selection.source_counts) || !Object.values(selection.source_counts).every(isNonNegativeInteger)
    || !dateOrNull(selection.first_at) || !dateOrNull(selection.last_at) || !isFiniteNonNegative(selection.span_hours)
    || typeof selection.sampling_policy !== 'string') return false;
  if ((selection.distinct_local_dates !== undefined && !isNonNegativeInteger(selection.distinct_local_dates))
    || (selection.observed_hour_bins !== undefined && (!Array.isArray(selection.observed_hour_bins)
      || !selection.observed_hour_bins.every(hour => Number.isInteger(hour) && hour >= 0 && hour <= 23)))) return false;
  if (!validPartition(value.chronology.fit) || !validPartition(value.chronology.calibration)
    || !validPartition(value.chronology.holdout) || value.chronology.strict_timestamp_groups !== true
    || !isFiniteNonNegative(value.chronology.purge_minutes)) return false;
  if (value.interval_calibration !== undefined) {
    const calibration = value.interval_calibration;
    if (!isRecord(calibration) || typeof calibration.method !== 'string'
      || !isNonNegativeInteger(calibration.calibration_rows) || !isNonNegativeInteger(calibration.calibration_forecast_pairs)
      || !isFiniteNonNegative(calibration.padding_points) || calibration.coverage_guaranteed !== false
      || typeof calibration.target_coverage !== 'number' || calibration.target_coverage < 0 || calibration.target_coverage > 1) return false;
  }
  if (!Array.isArray(value.horizons) || !value.horizons.every(row => isRecord(row)
    && [15, 30, 60].includes(Number(row.horizon_minutes)) && typeof row.horizon_minutes === 'number'
    && typeof row.status === 'string' && validMetrics(row.serving_model)
    && validMetrics(row.persistence) && validMetrics(row.current_history_blend)
    && (row.per_corridor === undefined || (isRecord(row.per_corridor) && Object.values(row.per_corridor).every(corridor => (
      isRecord(corridor) && validMetrics(corridor.serving_model) && validMetrics(corridor.persistence)
    ))))
    && (row.p10_p90_coverage === null || (typeof row.p10_p90_coverage === 'number'
      && Number.isFinite(row.p10_p90_coverage) && row.p10_p90_coverage >= 0 && row.p10_p90_coverage <= 1)))) return false;
  if (value.model_replaced !== (value.status === 'activated')) return false;
  return typeof value.activation.eligible === 'boolean' && Array.isArray(value.activation.reasons)
    && value.activation.reasons.every(reason => typeof reason === 'string') && isRecord(value.activation.policy)
    && Object.values(value.activation.policy).every(policy => typeof policy === 'string'
      || (typeof policy === 'number' && Number.isFinite(policy))
      || (Array.isArray(policy) && policy.every(item => typeof item === 'number' && Number.isFinite(item))));
}

/** Explicit read-only evaluation: no machine credential, synthetic fallback or model promotion. */
export async function requestObservedBacktest(accessToken: string | null): Promise<Fetched<ObservedBacktestResult>> {
  if (!API_ENABLED) throw new ApiRequestError('Connect the backend to evaluate observed history.', 503);
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/analytics/backtest`, {
      method: 'POST', headers: routeRequestHeaders(accessToken), body: '{}',
      signal: AbortSignal.timeout(ROUTE_TIMEOUT_MS), cache: 'no-store',
    });
  } catch {
    throw new ApiRequestError('The observed-data backtest could not be reached. Try again when the service is available.', 503);
  }
  if (!response.ok) {
    let message = response.status === 401 ? 'Sign in again to evaluate observed history.'
      : response.status === 403 ? 'An authorized operator account is required to evaluate observed history.'
        : response.status === 429 ? 'Another observed-data evaluation is running.'
          : 'The observed-data backtest is temporarily unavailable.';
    try {
      const body = await response.json() as { detail?: unknown };
      if (typeof body.detail === 'string' && body.detail.trim()) message = body.detail;
    } catch { /* Preserve a useful message for non-JSON gateway errors. */ }
    if (response.status === 429) {
      const retryAfter = Number(response.headers.get('Retry-After'));
      message += Number.isFinite(retryAfter) && retryAfter > 0 && retryAfter <= 120
        ? ` Try again in ${retryAfter} seconds.` : ' Wait briefly and try again.';
    }
    throw new ApiRequestError(message, response.status);
  }
  let payload: unknown;
  try { payload = await response.json(); } catch { payload = null; }
  if (!isRecord(payload) || !isObservedBacktestReport(payload.backtest) || !isRecord(payload.evaluation_snapshot)) {
    throw new ApiRequestError('The observed-data backtest is unavailable or returned an incomplete report. No synthetic evaluation was substituted.', 503);
  }
  return wrap(payload as Envelope, { backtest: payload.backtest, evaluation_snapshot: payload.evaluation_snapshot });
}

export interface SimulationPoint {
  timestamp: string;
  mean_score: number;
  p10_score: number;
  p90_score: number;
}

export interface MeasuredRoadFeatures {
  matched: boolean; match_distance_m: number; road_name: string | null; road_class: string | null;
  lanes: number | null; posted_maxspeed_kph: number | null; capacity_vph: null;
  segment_length_m: number | null; nearest_mapped_terminal_km: number; graph_revision: string;
  source: string; source_url: string;
}
export interface RealForecastPoint {
  horizon_minutes: number; mean_score: number; lower: number | null; upper: number | null;
  strategy: 'random_forest' | 'persistence'; forest_candidate_score: number;
}
export interface RealHorizonEvidence {
  horizon_minutes: number; selected_strategy: 'random_forest' | 'persistence';
  test: { samples: number; mae_points: number; rmse_points: number; r2: number | null };
  forest_test: { samples: number; mae_points: number; rmse_points: number; r2: number | null };
  persistence_test: { samples: number; mae_points: number; rmse_points: number; r2: number | null };
  speed_mae_kph: number;
  pairs: Record<string, number>;
  interval: { target: number; calibration_pairs: number; test_coverage: number; coverage_guaranteed: false; network_calibration_groups: number };
  feature_importances: Record<string, number>;
}
export interface RealTrafficForecast {
  generated_at: string; source: 'observed_traffic_model'; observed: false;
  status: 'provisional_observed_model' | 'validated_observed_candidate';
  model: { available: boolean; automatic_training?: boolean; engine: { algorithm: string; trees_per_horizon: Record<string, number>; feature_count: number; synthetic_rows: 0 };
    report: { trained_at: string; raw_observed_rows: number; selected_rows: number; training_pairs: number;
      span_hours: number; distinct_local_days: number; weather_training_rows: number; synthetic_rows: 0;
      feature_names: string[]; horizons: RealHorizonEvidence[]; source_counts: Record<string, number>;
      quality_gate: { eligible: boolean; reasons: string[] } } };
  weather: { status: 'fresh' | 'stale' | 'unavailable'; source: string; source_url: string;
    captured_at?: string; kind?: string; coverage?: string;
    current: null | { valid_at: string; captured_at: string; weather_code: number; precipitation_mm: number; temperature_c: number; wind_kph: number };
    hourly: Array<{ valid_at: string; weather_code: number; precipitation_mm: number; temperature_c: number; wind_kph: number }> };
  points: Array<{ corridor_id: string; observed_at: string; current_speed_kph: number; free_flow_speed_kph: number;
    current_index: number; road: MeasuredRoadFeatures; forecasts: RealForecastPoint[] }>;
  network: Array<{ horizon_minutes: number; mean_score: number; lower: number | null; upper: number | null; strategy?: 'random_forest' | 'persistence' | 'mixed' }>;
  coverage: { monitored_points: number; expected_points: number; max_horizon_minutes: number;
    configured_point_ids?: string[]; sampling_scope?: 'representative_provider_points' };
  limitations: string[];
  prospective_evaluation?: { status: string; issued: number; matched: number; matched_days: number;
    horizons?: Array<{ horizon_minutes: number; samples: number; candidate_mae_points: number; selected_mae_points: number; persistence_mae_points: number; within_5_points_fraction: number }> };
  historical_benchmarks?: Array<{ id: string; name: string; geography: string; source_rows: number; source_days: number; source_url: string; scope: string;
    horizons: Array<{ horizon_minutes: number; candidate: { samples: number; mae_kph: number; rmse_kph: number; within_5_kph: number };
      persistence: { samples: number; mae_kph: number; rmse_kph: number; within_5_kph: number };
      mae_improvement_percent: number; test_days: number; improvement_ci95_kph: [number, number] | null }> }>;
}

export async function fetchRealTrafficForecast(accessToken: string | null): Promise<RealTrafficForecast> {
  const value = await getJSON<Envelope & { forecast: RealTrafficForecast }>('/api/analytics/real-forecast', 15_000,
    { method: 'POST', headers: routeRequestHeaders(accessToken), body: '{}' }, true);
  const forecast = value?.forecast;
  const finite = (x: unknown) => typeof x === 'number' && Number.isFinite(x);
  const count = (x: unknown) => Number.isSafeInteger(x) && (x as number) >= 0;
  const score = (x: unknown) => finite(x) && (x as number) >= 0 && (x as number) <= 100;
  const date = (x: unknown) => typeof x === 'string' && Number.isFinite(Date.parse(x));
  const band = (p: { mean_score: number; lower: number | null; upper: number | null }) => p && score(p.mean_score)
    && ((p.lower === null && p.upper === null) || (score(p.lower) && score(p.upper) && p.lower! <= p.mean_score && p.mean_score <= p.upper!));
  const metrics = (m: RealHorizonEvidence['test']) => m && Number.isSafeInteger(m.samples) && m.samples > 0
    && finite(m.mae_points) && m.mae_points >= 0 && finite(m.rmse_points) && m.rmse_points >= 0 && (m.r2 === null || finite(m.r2));
  if (!forecast || forecast.source !== 'observed_traffic_model' || forecast.observed !== false
    || !date(forecast.generated_at) || !['provisional_observed_model', 'validated_observed_candidate'].includes(forecast.status)
    || forecast.model?.report?.synthetic_rows !== 0 || forecast.model?.engine?.synthetic_rows !== 0
    || forecast.model.available !== true
    || !Number.isSafeInteger(forecast.model.engine.feature_count) || forecast.model.engine.feature_count < 1
    || !isRecord(forecast.model.engine.trees_per_horizon) || !Object.values(forecast.model.engine.trees_per_horizon).every(n => Number.isSafeInteger(n) && n > 0)
    || !date(forecast.model.report.trained_at) || !finite(forecast.model.report.span_hours)
    || !['raw_observed_rows', 'selected_rows', 'training_pairs', 'distinct_local_days', 'weather_training_rows'].every(key => count((forecast.model.report as unknown as Record<string, unknown>)[key]))
    || !Array.isArray(forecast.model.report.feature_names) || forecast.model.report.feature_names.length !== forecast.model.engine.feature_count
    || !forecast.model.report.feature_names.every(x => typeof x === 'string')
    || !isRecord(forecast.model.report.source_counts) || !Object.values(forecast.model.report.source_counts).every(count)
    || typeof forecast.model.report.quality_gate?.eligible !== 'boolean'
    || !Array.isArray(forecast.model.report.quality_gate.reasons) || !forecast.model.report.quality_gate.reasons.every(x => typeof x === 'string')
    || (forecast.status === 'validated_observed_candidate') !== forecast.model.report.quality_gate.eligible
    || !Array.isArray(forecast.model.report.horizons) || forecast.model.report.horizons.length !== 3
    || !forecast.model.report.horizons.every((h, i) => h && h.horizon_minutes === [15, 30, 60][i] && metrics(h.test) && metrics(h.forest_test) && metrics(h.persistence_test)
      && ['random_forest', 'persistence'].includes(h.selected_strategy) && isRecord(h.feature_importances)
      && Object.values(h.feature_importances).every(value => finite(value) && value >= 0 && value <= 1)
      && h.interval?.coverage_guaranteed === false && count(h.interval.calibration_pairs)
      && finite(h.interval.test_coverage) && h.interval.test_coverage >= 0 && h.interval.test_coverage <= 1)
    || !count(forecast.coverage?.expected_points) || forecast.coverage.expected_points < 1 || forecast.coverage.expected_points > 512
    || !Array.isArray(forecast.points) || forecast.points.length < 1 || forecast.points.length > forecast.coverage.expected_points
    || new Set(forecast.points.map(p => p?.corridor_id)).size !== forecast.points.length
    || !forecast.points.every(p => p && typeof p.corridor_id === 'string' && /^[A-Za-z0-9._:-]{1,128}$/.test(p.corridor_id) && date(p.observed_at) && score(p.current_index)
      && finite(p.current_speed_kph) && p.current_speed_kph >= 0 && p.current_speed_kph <= 250 && finite(p.free_flow_speed_kph) && p.free_flow_speed_kph > 0
      && p.road?.source === 'OpenStreetMap' && p.road.capacity_vph === null
      && finite(p.road.match_distance_m) && Array.isArray(p.forecasts) && p.forecasts.length === 3
      && p.forecasts.every((h, i) => h && h.horizon_minutes === [15, 30, 60][i] && band(h) && score(h.forest_candidate_score) && ['random_forest', 'persistence'].includes(h.strategy)))
    || !Array.isArray(forecast.network) || forecast.network.length !== 3 || !forecast.network.every((p, i) => {
      const strategies = new Set(forecast.points.map(point => point.forecasts[i].strategy));
      const pointMean = forecast.points.reduce((sum, point) => sum + point.forecasts[i].mean_score, 0) / forecast.points.length;
      return p && p.horizon_minutes === [15, 30, 60][i] && band(p) && Math.abs(p.mean_score - pointMean) <= 0.001
        && (p.strategy === undefined || p.strategy === (strategies.size === 1 ? [...strategies][0] : 'mixed'));
    })
    || forecast.coverage.monitored_points !== forecast.points.length || forecast.coverage.max_horizon_minutes !== 60
    || (forecast.coverage.sampling_scope !== undefined && forecast.coverage.sampling_scope !== 'representative_provider_points')
    || (forecast.coverage.configured_point_ids !== undefined && (!Array.isArray(forecast.coverage.configured_point_ids)
      || forecast.coverage.configured_point_ids.length !== forecast.coverage.expected_points
      || new Set(forecast.coverage.configured_point_ids).size !== forecast.coverage.expected_points
      || !forecast.coverage.configured_point_ids.every(id => typeof id === 'string' && /^[A-Za-z0-9._:-]{1,128}$/.test(id))
      || !forecast.points.every(point => forecast.coverage.configured_point_ids!.includes(point.corridor_id))))
    || !['fresh', 'stale', 'unavailable'].includes(forecast.weather?.status)
    || forecast.weather.source_url !== 'https://open-meteo.com/en/docs'
    || (forecast.weather.status === 'fresh' && (!forecast.weather.current || !date(forecast.weather.current.valid_at)
      || !finite(forecast.weather.current.weather_code) || !finite(forecast.weather.current.wind_kph)
      || !finite(forecast.weather.current.temperature_c) || !finite(forecast.weather.current.precipitation_mm)))
    || !Array.isArray(forecast.limitations) || !forecast.limitations.every(x => typeof x === 'string')) {
    throw new ApiRequestError('Real traffic forecast failed validation. No synthetic values were substituted.', 503);
  }
  const prospective = forecast.prospective_evaluation;
  const benchmarks = forecast.historical_benchmarks;
  const benchmarkMetric = (m: NonNullable<RealTrafficForecast['historical_benchmarks']>[number]['horizons'][number]['candidate']) => m
    && count(m.samples) && m.samples > 0 && finite(m.mae_kph) && m.mae_kph >= 0 && finite(m.rmse_kph) && m.rmse_kph >= 0
    && finite(m.within_5_kph) && m.within_5_kph >= 0 && m.within_5_kph <= 1;
  if ((prospective && (typeof prospective.status !== 'string' || ![prospective.issued, prospective.matched, prospective.matched_days].every(count) || prospective.matched > prospective.issued
      || (prospective.horizons && (!Array.isArray(prospective.horizons) || !prospective.horizons.every(h => h && [15, 30, 60].includes(h.horizon_minutes) && count(h.samples)
        && [h.candidate_mae_points, h.selected_mae_points, h.persistence_mae_points].every(score) && finite(h.within_5_points_fraction) && h.within_5_points_fraction >= 0 && h.within_5_points_fraction <= 1)))))
    || (benchmarks && (!Array.isArray(benchmarks) || !benchmarks.every(b => b && ['grand_batam_survey', 'guangzhou_speed'].includes(b.id)
      && typeof b.name === 'string' && typeof b.geography === 'string' && typeof b.scope === 'string' && count(b.source_rows) && count(b.source_days)
      && typeof b.source_url === 'string' && (b.source_url === 'https://zenodo.org/records/1205229' || b.source_url.startsWith('https://repository.unrika.ac.id/xmlui/'))
      && Array.isArray(b.horizons) && b.horizons.every(h => h && [10, 30, 60].includes(h.horizon_minutes)
        && benchmarkMetric(h.candidate) && benchmarkMetric(h.persistence) && h.candidate.samples === h.persistence.samples
        && finite(h.mae_improvement_percent) && count(h.test_days) && (h.improvement_ci95_kph === null
          || (Array.isArray(h.improvement_ci95_kph) && h.improvement_ci95_kph.length === 2 && h.improvement_ci95_kph.every(finite) && h.improvement_ci95_kph[0] <= h.improvement_ci95_kph[1]))))))) {
    throw new ApiRequestError('Traffic evaluation evidence failed validation.', 503);
  }
  return forecast;
}

export interface AnalyticsSimulation {
  prediction_count?: number;
  engine?: { algorithm: string; estimator_count: number; feature_count: number; is_trained: boolean; last_trained_at: string | null };
  horizon_hours: number;
  step_minutes: number;
  weather: number;
  timezone: string;
  hourly_forecast: SimulationPoint[];
  corridors: Array<{ corridor_id: string; corridor_name: string; hourly_forecast: SimulationPoint[] }>;
  weather_comparison: Array<{ weather: number; label: string; mean_score: number; p10_score: number; p90_score: number }>;
  model: { training_data_source?: string; total_samples?: number; observed_training_rows?: number; evaluation_scope?: string };
  limitations: string[];
}
export async function requestAnalyticsSimulation(
  weather: number, corridorIds: string[] | undefined, accessToken: string | null,
): Promise<Fetched<AnalyticsSimulation>> {
  const payload = await getJSON<Envelope & { observed: false; simulation: AnalyticsSimulation }>(
    '/api/analytics/simulation', CORRIDOR_TIMEOUT_MS, {
      method: 'POST', headers: routeRequestHeaders(accessToken),
      body: JSON.stringify({ weather, ...(corridorIds ? { corridor_ids: corridorIds } : {}) }),
    }, true,
  );
  const points = payload?.simulation?.hourly_forecast;
  const validScore = (value: unknown) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100;
  const validPoint = (point: SimulationPoint) => point && typeof point.timestamp === 'string' && Number.isFinite(Date.parse(point.timestamp))
    && [point.mean_score, point.p10_score, point.p90_score].every(validScore) && point.p10_score <= point.p90_score;
  if (!payload || payload.observed !== false || !Array.isArray(points) || !points.length
    || (payload.simulation.prediction_count !== undefined && (!Number.isSafeInteger(payload.simulation.prediction_count) || payload.simulation.prediction_count < 1))
    || (payload.simulation.engine !== undefined && (!isRecord(payload.simulation.engine)
      || typeof payload.simulation.engine.algorithm !== 'string'
      || typeof payload.simulation.engine.is_trained !== 'boolean'
      || !Number.isSafeInteger(payload.simulation.engine.estimator_count) || payload.simulation.engine.estimator_count < 0
      || !Number.isSafeInteger(payload.simulation.engine.feature_count) || payload.simulation.engine.feature_count < 0
      || (payload.simulation.engine.last_trained_at !== null && (typeof payload.simulation.engine.last_trained_at !== 'string' || !Number.isFinite(Date.parse(payload.simulation.engine.last_trained_at))))))
    || payload.simulation.horizon_hours !== 24 || payload.simulation.step_minutes !== 60
    || ![0, 1, 2].includes(payload.simulation.weather) || payload.simulation.timezone !== 'Asia/Jakarta'
    || typeof payload.generated_at !== 'string' || !Number.isFinite(Date.parse(payload.generated_at))
    || !points.every(validPoint) || !isRecord(payload.simulation.model)
    || !Array.isArray(payload.simulation.corridors) || !payload.simulation.corridors.every(corridor => (
      corridor && typeof corridor.corridor_id === 'string' && typeof corridor.corridor_name === 'string'
      && Array.isArray(corridor.hourly_forecast) && corridor.hourly_forecast.every(validPoint)
    ))
    || !Array.isArray(payload.simulation.weather_comparison) || !payload.simulation.weather_comparison.every(row => (
      row && typeof row.label === 'string' && [0, 1, 2].includes(row.weather)
      && [row.mean_score, row.p10_score, row.p90_score].every(validScore)
    ))
    || !Array.isArray(payload.simulation.limitations) || !payload.simulation.limitations.every(limit => typeof limit === 'string')) {
    throw new ApiRequestError('The backend simulation is unavailable. No illustrative curve was substituted.', 503);
  }
  return wrap(payload, payload.simulation);
}
export interface AuthorityReport {
  publisher: string; source_url: string; period_label: string; period_start: string; period_end: string;
  metrics: Array<{ id: string; label: string; value: number; unit: string }>;
  content_sha256: string;
}
export interface AuthorityReports {
  status: 'fresh' | 'stale' | 'unavailable'; checked_at: string | null; fetched_at: string | null;
  report: AuthorityReport | null; stored_reports: AuthorityReport[];
  storage: { durable: boolean; scope: string }; error: string | null;
}
export async function fetchAuthorityReports(): Promise<AuthorityReports> {
  const payload = await getJSON<Envelope & AuthorityReports>('/api/authority-reports', CORRIDOR_TIMEOUT_MS);
  const validReport = (report: AuthorityReport) => report && typeof report.period_label === 'string'
    && typeof report.publisher === 'string' && typeof report.content_sha256 === 'string'
    && /^\d{4}-\d{2}$/.test(report.period_start) && /^\d{4}-\d{2}$/.test(report.period_end)
    && typeof report.source_url === 'string' && report.source_url.startsWith('https://') && Array.isArray(report.metrics)
    && report.metrics.every(metric => metric && typeof metric.id === 'string' && typeof metric.unit === 'string'
      && typeof metric.label === 'string' && isFiniteNonNegative(metric.value));
  if (!payload || !['fresh', 'stale', 'unavailable'].includes(payload.status)
    || (payload.report !== null && !validReport(payload.report)) || !Array.isArray(payload.stored_reports)
    || !payload.stored_reports.every(validReport) || !isRecord(payload.storage)
    || typeof payload.storage.durable !== 'boolean' || typeof payload.storage.scope !== 'string') {
    throw new ApiRequestError('Published operating reports could not be checked. No fixed totals were substituted.', 503);
  }
  return payload;
}

export interface HistoricalResearch {
  batam_benchmark?: {
    scope: 'exploratory_single_batam_site_next_survey_day'; evaluated_at: string;
    unit: 'km/h'; source_rows: 48; source_calendar_days: 3; synthetic_training_rows: 0;
    serving_model_replaced: false; application_history_modified: false; promotion_eligible: false;
    random_forest: { samples: number; mae_kph: number; rmse_kph: number };
    previous_survey_day: { samples: number; mae_kph: number; rmse_kph: number };
    historical_slot_mean: { samples: number; mae_kph: number; rmse_kph: number };
    limitations: string[];
    folds: Array<{ test_date: string; forecast_origin: string; latest_training_interval_end: string;
      training_rows: number; test_rows: number }>;
  };
  saturation_survey?: {
    dataset_name: string; source_url: string; source_pdf_url: string; license: string; license_url: string;
    attribution: string; source_table: string; location: string; scope_limitations: string[];
    unit: 'volume_capacity_ratio'; eligible_speed_training_rows: 0; dates: string[];
    rows: Array<{ interval_start: string; interval_end: string; degree_of_saturation: number }>;
  };
  survey: {
    name: string; location: string; source_url: string; source_pdf_url: string;
    attribution: string; license: string; license_url: string; source_table: string;
    csv_sha256: string; dates: string[]; row_count: number; min_speed_kph: number; max_speed_kph: number;
    scope_limitations: string[]; canonical_corridor_mapping: null; serving_model_replaced: false;
    rows: Array<{ interval_start: string; interval_end: string; direction: 'from_east' | 'from_west';
      speed_kph: number; travel_seconds: number; segment_length_m: number; quality_flag: string }>;
  };
  benchmark: {
    scope: string; source_url: string; source_signal: string; source_license: string; license_url: string;
    source_provenance: string; timestamp_basis: string; source_first_timestamp: string; source_last_timestamp: string;
    source_calendar_days: number; roads: number; snapshots: number; speed_band_records: number;
    evaluated_at: string; limitations: string[]; synthetic_training_rows: 0; serving_model_replaced: false;
    results: Array<{ horizon_minutes: number; random_forest: { samples: number; mae_band_units: number; rmse_band_units: number };
      persistence: { samples: number; mae_band_units: number; rmse_band_units: number };
      interval_empirical_coverage: number }>;
  };
}

export async function fetchHistoricalResearch(): Promise<HistoricalResearch> {
  const payload = await getJSON<HistoricalResearch>('/api/analytics/research-history', CORRIDOR_TIMEOUT_MS);
  const survey = payload?.survey;
  const benchmark = payload?.benchmark;
  const text = (v: unknown): v is string => typeof v === 'string' && v.length > 0;
  const texts = (v: unknown) => Array.isArray(v) && v.every(text);
  const positive = (v: unknown) => isFiniteNonNegative(v) && Number(v) > 0;
  const https = (v: unknown) => text(v) && /^https:\/\//.test(v);
  const instant = (v: unknown) => text(v) && Number.isFinite(Date.parse(v));
  const metrics = (v: HistoricalResearch['benchmark']['results'][number]['random_forest']) => v
    && Number.isInteger(v.samples) && v.samples > 0
    && isFiniteNonNegative(v.mae_band_units) && v.mae_band_units <= 7
    && isFiniteNonNegative(v.rmse_band_units) && v.rmse_band_units <= 7;
  const saturation = payload?.saturation_survey;
  const batam = payload?.batam_benchmark;
  const invalidBatam = batam !== undefined && (!batam || batam.scope !== 'exploratory_single_batam_site_next_survey_day'
    || batam.unit !== 'km/h' || batam.source_rows !== 48 || batam.source_calendar_days !== 3
    || batam.synthetic_training_rows !== 0 || batam.serving_model_replaced !== false
    || batam.application_history_modified !== false || batam.promotion_eligible !== false
    || !instant(batam.evaluated_at) || !texts(batam.limitations)
    || ![batam.random_forest, batam.previous_survey_day, batam.historical_slot_mean].every(m => m
      && m.samples === 32 && isFiniteNonNegative(m.mae_kph) && m.mae_kph <= 200
      && isFiniteNonNegative(m.rmse_kph) && m.rmse_kph <= 200)
    || !Array.isArray(batam.folds) || batam.folds.length !== 2
    || !batam.folds.every((f, i) => f && f.test_date === ['2023-02-17', '2023-02-18'][i]
      && f.training_rows === [16, 32][i] && f.test_rows === 16
      && instant(f.forecast_origin) && instant(f.latest_training_interval_end)
      && f.forecast_origin.slice(0, 10) === f.test_date && f.forecast_origin.endsWith('+07:00')
      && Date.parse(f.latest_training_interval_end) < Date.parse(f.forecast_origin)));
  const invalidSaturation = saturation !== undefined && (!saturation || saturation.unit !== 'volume_capacity_ratio'
    || saturation.eligible_speed_training_rows !== 0 || ![saturation.source_url, saturation.source_pdf_url, saturation.license_url].every(https)
    || ![saturation.dataset_name, saturation.attribution, saturation.source_table, saturation.location, saturation.license].every(text)
    || !texts(saturation.scope_limitations) || !Array.isArray(saturation.dates) || !saturation.dates.length
    || !saturation.dates.every(d => /^\d{4}-\d{2}-\d{2}$/.test(d))
    || !Array.isArray(saturation.rows) || !saturation.rows.length || saturation.rows.length > 1000
    || !saturation.rows.every(r => r && instant(r.interval_start) && instant(r.interval_end)
      && r.interval_start.endsWith('+07:00') && r.interval_end.endsWith('+07:00')
      && Date.parse(r.interval_end) - Date.parse(r.interval_start) === 60 * 60_000
      && saturation.dates.includes(r.interval_start.slice(0, 10))
      && isFiniteNonNegative(r.degree_of_saturation) && r.degree_of_saturation <= 5));
  if (!survey || !benchmark || !text(survey.name) || !text(survey.location) || !text(survey.source_table)
    || invalidSaturation || invalidBatam
    || !text(survey.attribution) || !text(survey.license) || !/^[a-f0-9]{64}$/.test(survey.csv_sha256)
    || ![survey.source_url, survey.source_pdf_url, survey.license_url, benchmark.source_url, benchmark.license_url].every(https)
    || survey.canonical_corridor_mapping !== null || survey.serving_model_replaced !== false
    || benchmark.synthetic_training_rows !== 0 || benchmark.serving_model_replaced !== false
    || !texts(survey.scope_limitations) || !texts(benchmark.limitations)
    || !Array.isArray(survey.dates) || !survey.dates.length || !survey.dates.every(d => /^\d{4}-\d{2}-\d{2}$/.test(d))
    || !Array.isArray(survey.rows) || !survey.rows.length || survey.rows.length > 1000 || survey.rows.length !== survey.row_count
    || !survey.rows.every(row => row && instant(row.interval_start) && instant(row.interval_end)
      && row.interval_start.endsWith('+07:00') && row.interval_end.endsWith('+07:00')
      && Date.parse(row.interval_end) - Date.parse(row.interval_start) === 30 * 60_000
      && survey.dates.includes(row.interval_start.slice(0, 10))
      && ['from_east', 'from_west'].includes(row.direction) && positive(row.speed_kph) && row.speed_kph <= 200
      && positive(row.travel_seconds) && positive(row.segment_length_m) && text(row.quality_flag))
    || ![survey.min_speed_kph, survey.max_speed_kph].every(positive)
    || ![benchmark.scope, benchmark.source_signal, benchmark.source_license, benchmark.source_provenance, benchmark.timestamp_basis].every(text)
    || ![benchmark.source_first_timestamp, benchmark.source_last_timestamp, benchmark.evaluated_at].every(instant)
    || ![benchmark.source_calendar_days, benchmark.roads, benchmark.snapshots, benchmark.speed_band_records].every(v => Number.isInteger(v) && v > 0)
    || !Array.isArray(benchmark.results) || benchmark.results.length !== 3
    || !benchmark.results.every((r, i) => r && r.horizon_minutes === [15, 30, 60][i] && metrics(r.random_forest) && metrics(r.persistence)
      && r.random_forest.samples === r.persistence.samples && isFiniteNonNegative(r.interval_empirical_coverage) && r.interval_empirical_coverage <= 1)) {
    throw new ApiRequestError('Published historical evidence could not be validated. No sample observations were substituted.', 503);
  }
  return payload;
}
