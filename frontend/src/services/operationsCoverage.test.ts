import { afterEach, expect, it, vi } from 'vitest';
import { fetchOperationsSummary } from './api';
import type { OperationsSummary } from '../types';

function payload(): OperationsSummary {
  return {
    generated_at: '2026-10-24T00:00:00Z', data_source: 'live', overall_network_status: 'OPTIMAL',
    average_congestion_index: 3.4, active_bottlenecks: 0, total_co2_reduced_today_kg: null,
    active_ferry_sailings: 7, scheduled_ferry_departures_next_12h: 7, alerts: [],
    operations_methodology: { observed: false, source: 'actual sources',
      scopes: { network: 'representative points', ferries: 'published timetable', emissions: 'unavailable' },
      assumptions: {} },
    road_network_coverage: { source: 'OpenStreetMap', scope: 'bundled_batam_routing_graph',
      mapped_nodes: 115320, mapped_source_ways: 27018, directed_edges: 249745,
      live_camera_edge_count: 12, live_camera_edge_coverage_percent: 0.0048, complete_live_coverage: false },
    camera_traffic_summary: { status: 'estimated', configured_cameras: 15, fresh_cameras: 12,
      validated_cameras: 0, estimated_cameras: 12, mean_congestion_index: 42.3, vehicle_detections: 89,
      latest_observed_at: '2026-10-24T00:00:00Z', oldest_observed_at: '2026-10-23T23:59:59Z', valid_until: '2026-10-24T00:00:10Z',
      source: 'live estimates', scope: 'current views', measured_road_speed: false, independently_validated: false },
  };
}
function respond(value: unknown) {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(value))));
}
afterEach(() => vi.unstubAllGlobals());

it('keeps measured-speed congestion separate from local estimates and mapped road coverage', async () => {
  const expected = payload(); respond(expected);
  const result = await fetchOperationsSummary();
  expect(result.source).toBe('live'); expect(result.data.average_congestion_index).toBe(3.4);
  expect(result.data.camera_traffic_summary?.mean_congestion_index).toBe(42.3);
  expect(result.data.road_network_coverage).toEqual(expected.road_network_coverage);
  expect(result.data.camera_traffic_summary?.valid_until).toBe('2026-10-24T00:00:10Z');
});

it('preserves unknown coverage and counts instead of substituting zeros', async () => {
  const value = payload();
  Object.assign(value.road_network_coverage!, { live_camera_edge_count: null, live_camera_edge_coverage_percent: null });
  Object.assign(value.camera_traffic_summary!, { fresh_cameras: 0, estimated_cameras: 0,
    mean_congestion_index: null, vehicle_detections: null, latest_observed_at: null, oldest_observed_at: null, valid_until: null });
  respond(value); const result = await fetchOperationsSummary();
  expect(result.source).toBe('live'); expect(result.data.camera_traffic_summary?.vehicle_detections).toBeNull();
  expect(result.data.road_network_coverage?.live_camera_edge_count).toBeNull();
});

it('retains measured congestion and mapped roads when the camera store is temporarily unavailable', async () => {
  const value = payload();
  Object.assign(value.camera_traffic_summary!, { status: 'unavailable', configured_cameras: null,
    fresh_cameras: 0, validated_cameras: 0, estimated_cameras: 0, mean_congestion_index: null,
    vehicle_detections: null, latest_observed_at: null, oldest_observed_at: null, valid_until: null });
  Object.assign(value.road_network_coverage!, { live_camera_edge_count: null, live_camera_edge_coverage_percent: null });
  respond(value);
  const result = await fetchOperationsSummary();
  expect(result.source).toBe('live');
  expect(result.data.average_congestion_index).toBe(3.4);
  expect(result.data.scheduled_ferry_departures_next_12h).toBe(7);
  expect(result.data.road_network_coverage?.mapped_nodes).toBe(115320);
  expect(result.data.camera_traffic_summary?.configured_cameras).toBeNull();
  expect(result.data.camera_traffic_summary?.status).toBe('unavailable');
});

it('preserves the rest of the snapshot when graph metadata has no source-way count', async () => {
  const value = payload(); value.road_network_coverage!.mapped_source_ways = null;
  respond(value); const result = await fetchOperationsSummary();
  expect(result.source).toBe('live'); expect(result.data.average_congestion_index).toBe(3.4);
  expect(result.data.road_network_coverage?.mapped_source_ways).toBeNull();
  expect(result.data.road_network_coverage?.directed_edges).toBe(249745);
});

it.each(['negative-road-count', 'overstated-links', 'invalid-percentage', 'invalid-estimate', 'inconsistent-views', 'invalid-deadline'])('rejects malformed additive operations evidence: %s', async change => {
  const value = payload();
  if (change === 'negative-road-count') value.road_network_coverage!.mapped_nodes = -1;
  if (change === 'overstated-links') value.road_network_coverage!.live_camera_edge_count = 300000;
  if (change === 'invalid-percentage') value.road_network_coverage!.live_camera_edge_coverage_percent = 101;
  if (change === 'invalid-estimate') value.camera_traffic_summary!.mean_congestion_index = 101;
  if (change === 'inconsistent-views') value.camera_traffic_summary!.estimated_cameras = 15;
  if (change === 'invalid-deadline') value.camera_traffic_summary!.valid_until = 'not a time';
  respond(value); const result = await fetchOperationsSummary();
  expect(result.source).toBe('offline'); expect(result.data.average_congestion_index).toBeNull();
  expect(result.data.camera_traffic_summary).toBeUndefined();
});
