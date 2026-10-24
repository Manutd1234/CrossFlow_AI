/* @vitest-environment jsdom */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { OperationsRoadCoverage, OperationsTrafficEstimates } from '../../types';
import { RoadCoverageSummary } from './RoadCoverageSummary';

const coverage: OperationsRoadCoverage = { source: 'OpenStreetMap', scope: 'bundled_batam_routing_graph',
  mapped_nodes: 115320, mapped_source_ways: 27018, directed_edges: 249745,
  live_camera_edge_count: 12, live_camera_edge_coverage_percent: 0.0048, complete_live_coverage: false };
const estimates: OperationsTrafficEstimates = { status: 'estimated', configured_cameras: 15,
  fresh_cameras: 12, validated_cameras: 0, estimated_cameras: 12, mean_congestion_index: 42.3,
  vehicle_detections: 89, latest_observed_at: '2026-10-24T00:00:00Z', oldest_observed_at: '2026-10-23T23:59:59Z',
  valid_until: '2026-10-24T00:00:10Z', source: 'fixture', scope: 'current views only', measured_road_speed: false,
  independently_validated: false };
let root: Root; let view: HTMLDivElement;
beforeEach(() => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-24T00:00:00Z'));
  view = document.createElement('div'); document.body.append(view); root = createRoot(view);
});
afterEach(() => { act(() => root.unmount()); document.body.replaceChildren(); vi.useRealTimers(); });

it('separates graph counts from current estimates without exposing streams or calculations', async () => {
  await act(async () => root.render(<RoadCoverageSummary coverage={coverage} estimates={estimates} />));
  expect(view.textContent).toContain('27,018'); expect(view.textContent).toContain('115,320');
  expect(view.textContent).toContain('249,745'); expect(view.textContent).toContain('42.3 / 100');
  expect(view.textContent).toContain('<0.01% of mapped links');
  expect(view.textContent).toContain('Provisional'); expect(view.textContent).toContain('89 vehicle detections');
  expect(view.textContent).toContain('overlapping views may include the same vehicle');
  expect(view.textContent).not.toMatch(/CCTV|surveillance|YOLO|camera feed/i);
  expect(view.querySelector('video, img, input')).toBeNull();
});

it('expires live estimates and link coverage before the next source refresh, retaining map counts', async () => {
  await act(async () => root.render(<RoadCoverageSummary coverage={coverage} estimates={estimates} />));
  await act(async () => vi.advanceTimersByTimeAsync(10_001));
  expect(view.textContent).not.toContain('42.3 / 100'); expect(view.textContent).not.toContain('89 vehicle detections');
  expect(view.textContent).not.toContain('<0.01%'); expect(view.textContent).toContain('Awaiting fresh observations');
  expect(view.textContent).toContain('A current local estimate is unavailable'); expect(view.textContent).toContain('115,320');
});

it('does not assume old responses without a validity deadline contain current traffic', async () => {
  await act(async () => root.render(<RoadCoverageSummary coverage={coverage} estimates={{ ...estimates, valid_until: undefined }} />));
  expect(view.textContent).not.toContain('42.3 / 100'); expect(view.textContent).toContain('Unavailable');
});

it('preserves measured zero and a separate validated status', async () => {
  await act(async () => root.render(<RoadCoverageSummary coverage={coverage} estimates={{ ...estimates,
    independently_validated: true, mean_congestion_index: 0, vehicle_detections: 0 }} />));
  expect(view.textContent).toContain('Validated views'); expect(view.textContent).toContain('0.0 / 100');
  expect(view.textContent).toContain('0 vehicle detections');
});

it('shows an unknown source-way count without losing the other graph measurements', async () => {
  await act(async () => root.render(<RoadCoverageSummary coverage={{ ...coverage, mapped_source_ways: null }} />));
  expect(view.textContent).toContain('Mapped roads & pathsUnavailable');
  expect(view.textContent).toContain('Map nodes115,320');
  expect(view.textContent).toContain('Directed road links249,745');
});
