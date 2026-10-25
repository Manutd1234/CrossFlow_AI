import type { CongestionZone } from '../types';
import { pressureBandView } from './pressureBands';

interface RankedHotspot {
  id: string;
  pressureScore: number | null;
}

/** Order the validated value shown on each card, independently of planning priority. */
export function compareModelledHotspotPressure(left: RankedHotspot, right: RankedHotspot): number {
  if (left.pressureScore === null && right.pressureScore !== null) return 1;
  if (right.pressureScore === null && left.pressureScore !== null) return -1;
  if (left.pressureScore !== null && right.pressureScore !== null && left.pressureScore !== right.pressureScore) {
    return right.pressureScore - left.pressureScore;
  }
  // Match the backend's zone-id tie order without browser-locale collation.
  return left.id < right.id ? -1 : left.id > right.id ? 1 : 0;
}

/** A modelled area's ranking score cannot stand in for congestion pressure. */
export function modelledHotspotPressure(zone?: CongestionZone): number | null {
  if (!zone || zone.source !== 'modelled_spatial_hotspot' || zone.observed !== false
    || zone.pressure_basis !== 'modelled_congestion_index'
    || zone.pressure_policy !== 'congestion_pressure_v1' || zone.pressure_multiplier !== 2.5
    || typeof zone.congestion_index !== 'number' || !Number.isFinite(zone.congestion_index)
    || zone.congestion_index < 0 || zone.congestion_index > 100
    || typeof zone.pressure_index !== 'number' || !Number.isFinite(zone.pressure_index)
    || zone.pressure_index < 0 || zone.pressure_index > 100
    || Math.abs(zone.pressure_index - Math.min(100, zone.congestion_index * 2.5)) > 0.051
    || !pressureBandView(zone)) return null;
  return zone.pressure_index;
}
