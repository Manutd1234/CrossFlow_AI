import { describe, expect, it } from 'vitest';
import type { CongestionZone } from '../types';
import { modelledHotspotPressure } from './hotspotPressure';

function zone(overrides: Record<string, unknown> = {}): CongestionZone {
  const pressure = typeof overrides.pressure_index === 'number' ? overrides.pressure_index : 25.5;
  return {
    zone_id: 'zone-panbil', name: 'Simpang Panbil / Muka Kuning', lat: 1.072252, lng: 104.025136,
    radius_m: 700, congestion_index: 10.2, level: 'SMOOTH', color: '#10b981', avoid_recommended: false,
    source: 'modelled_spatial_hotspot', observed: false, selection_rank: 2, selection_score: 45.5,
    watch_priority: 'CRITICAL', pressure_index: 25.5, pressure_multiplier: 2.5,
    pressure_policy: 'congestion_pressure_v1', pressure_basis: 'modelled_congestion_index',
    pressure_condition: pressure <= 5 ? 'FREE_FLOW' : pressure < 70 ? 'SLOWING' : pressure < 80 ? 'HEAVY' : 'CRITICAL',
    pressure_condition_policy: 'congestion_pressure_bands_v1',
    modeled_emissions_pressure: {
      index: 20, queue_pressure_factor: 0.2, level: 'LOW', metric: 'relative_queue_emissions_pressure',
      unit: 'index_0_100', observed: false,
    },
    ...overrides,
  } as CongestionZone;
}

describe('modelled hotspot pressure provenance', () => {
  it('uses the backend pressure rather than the selection score and preserves source data', () => {
    const source = zone();
    const original = JSON.stringify(source);
    expect(modelledHotspotPressure(source)).toBe(25.5);
    expect(source.selection_score).toBe(45.5);
    expect(source.congestion_index).toBe(10.2);
    expect(JSON.stringify(source)).toBe(original);
  });

  it.each([[0, 0], [10.2, 25.5], [27.96, 69.9], [28, 70], [31.96, 79.9], [32, 80], [40, 100], [95, 100], [100, 100]])(
    'accepts backend modelled index %s and its capped pressure %s', (raw, pressure) => {
      expect(modelledHotspotPressure(zone({ congestion_index: raw, pressure_index: pressure }))).toBe(pressure);
    },
  );

  it.each([
    ['source missing', { source: undefined }],
    ['different source', { source: 'tomtom_live' }],
    ['observed declaration missing', { observed: undefined }],
    ['observed data mislabeled as modelled', { observed: true }],
    ['basis missing', { pressure_basis: undefined }],
    ['ranking basis', { pressure_basis: 'selection_score' }],
    ['policy missing', { pressure_policy: undefined }],
    ['unsupported policy', { pressure_policy: 'legacy_pressure' }],
    ['multiplier missing', { pressure_multiplier: undefined }],
    ['wrong multiplier', { pressure_multiplier: 3 }],
    ['condition missing', { pressure_condition: undefined }],
    ['condition policy missing', { pressure_condition_policy: undefined }],
    ['unsupported condition policy', { pressure_condition_policy: 'legacy_pressure_bands' }],
    ['condition conflicts with pressure', { pressure_condition: 'CRITICAL' }],
    ['raw index missing', { congestion_index: undefined }],
    ['raw index negative', { congestion_index: -1 }],
    ['raw index too large', { congestion_index: 101 }],
    ['raw index non-finite', { congestion_index: Number.NaN }],
    ['pressure missing', { pressure_index: undefined }],
    ['pressure null', { pressure_index: null }],
    ['pressure negative', { pressure_index: -1 }],
    ['pressure too large', { pressure_index: 101 }],
    ['pressure non-finite', { pressure_index: Number.POSITIVE_INFINITY }],
    ['pressure string', { pressure_index: '25.5' }],
    ['selection score used as pressure', { pressure_index: 45.5 }],
  ])('withholds unsupported pressure without deriving a fallback: %s', (_name, overrides) => {
    expect(modelledHotspotPressure(zone(overrides))).toBeNull();
  });

  it('does not derive pressure for a static planning catalogue entry without a backend zone', () => {
    expect(modelledHotspotPressure()).toBeNull();
  });
});
