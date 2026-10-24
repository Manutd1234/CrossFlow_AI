import type { CongestionZone } from '../types';
import { pressureBandView } from './pressureBands';

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
