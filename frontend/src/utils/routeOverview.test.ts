import { describe, expect, it } from 'vitest';
import type { ManeuverItem, NavigationData, RouteLeg } from '../types';
import { routeOverviewSummary } from './routeOverview';

const step = (street: string, distance_m: number): ManeuverItem => ({
  street, distance_m, step: 1, type: 'turn', icon: 'turn', coords: [1.1, 104], instruction: `Turn onto ${street}`,
});
const navigation = (maneuvers: ManeuverItem[]): NavigationData => ({
  maneuvers, landmarks_along_route: [], traffic_lights_count: 0,
  route_narrative_words: 'Turn right. Turn left. Repeat many directions.',
});

describe('compact route overview', () => {
  it('summarises the longest named roads in journey order without repeating turns', () => {
    const summary = routeOverviewSummary('Harbour Bay', 'Batam Centre', 11.7, navigation([
      step('Unnamed service road', 6000), step('Jalan Duyung', 1000),
      step('Jalan Yos Sudarso', 3000), step('Jalan Duyung', 2500), step('Minor road', 40),
    ]));
    expect(summary).toBe('11.7 km from Harbour Bay to Batam Centre, mainly via Jalan Duyung and Jalan Yos Sudarso.');
    expect(summary).not.toMatch(/Turn|Unnamed|Minor/);
  });
  it('describes the ferry connection from structured legs', () => {
    const legs = [{ mode: 'FERRY', from_name: 'Batam Centre', to_name: 'HarbourFront' }] as RouteLeg[];
    expect(routeOverviewSummary('Batamindo', 'Singapore', 39.8, undefined, legs))
      .toBe('39.8 km from Batamindo to Singapore, with a ferry crossing from Batam Centre to HarbourFront.');
  });
  it('uses a short journey summary when named roads are unavailable', () => {
    expect(routeOverviewSummary('A', 'B', 2, navigation([step('Unnamed road', 20), step('Destination', 0)])))
      .toBe('2 km from A to B.');
  });
  it('keeps long provider labels bounded without altering the original directions', () => {
    const source = navigation([step('Jalan '+ 'X'.repeat(300), 1000)]);
    const original = JSON.stringify(source);
    const summary = routeOverviewSummary('A'.repeat(500), 'B'.repeat(500), 10, source);
    expect(summary.length).toBeLessThan(220);
    expect(JSON.stringify(source)).toBe(original);
  });
});
