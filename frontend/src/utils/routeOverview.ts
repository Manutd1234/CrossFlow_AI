import type { NavigationData, RouteLeg } from '../types';

const label = (value: string, limit = 64) => {
  const cleaned = value.replace(/\s+/g, ' ').trim();
  return cleaned.length > limit ? `${cleaned.slice(0, limit - 1).trimEnd()}…` : cleaned;
};

/** Describe the journey from structured route data, without repeating directions. */
export function routeOverviewSummary(origin: string, destination: string, distanceKm: number,
  navigation?: NavigationData, legs: RouteLeg[] = []): string {
  const journey = `${distanceKm} km from ${label(origin)} to ${label(destination)}`;
  const ferries = legs.filter(leg => leg.mode === 'FERRY');
  if (ferries.length === 1) {
    return `${journey}, with a ferry crossing from ${label(ferries[0].from_name)} to ${label(ferries[0].to_name)}.`;
  }
  if (ferries.length > 1) return `${journey}, including ${ferries.length} ferry crossings.`;

  const roads = new Map<string, { name: string; distance: number; order: number }>();
  for (const [order, step] of (navigation?.maneuvers ?? []).entries()) {
    const name = typeof step.street === 'string' ? step.street.trim() : '';
    if (!name || /^(unnamed\b|unknown\b|road access\b|service road$|road$)/i.test(name)
      || !Number.isFinite(step.distance_m) || step.distance_m <= 0) continue;
    const key = name.toLocaleLowerCase('en');
    const existing = roads.get(key);
    if (existing) existing.distance += step.distance_m;
    else roads.set(key, { name, distance: step.distance_m, order });
  }
  const mainRoads = [...roads.values()].sort((a, b) => b.distance - a.distance || a.order - b.order)
    .slice(0, 2).sort((a, b) => a.order - b.order).map(road => label(road.name, 48));
  return mainRoads.length ? `${journey}, mainly via ${mainRoads.join(' and ')}.` : `${journey}.`;
}
