import { describe, expect, it } from 'vitest';
import { pressureBandView } from './pressureBands';
import { corridorColor } from './format';

function record(index: number, condition: string, overrides: Record<string, unknown> = {}) {
  return { pressure_index: index, pressure_condition: condition,
    pressure_condition_policy: 'congestion_pressure_bands_v1', ...overrides } as Parameters<typeof pressureBandView>[0];
}

describe('backend pressure condition presentation', () => {
  it.each([
    [0, 'FREE_FLOW', 'smooth'], [5, 'FREE_FLOW', 'smooth'], [5.1, 'SLOWING', 'smooth'],
    [25.5, 'SLOWING', 'smooth'], [69.9, 'SLOWING', 'smooth'], [70, 'HEAVY', 'heavy'],
    [79.9, 'HEAVY', 'heavy'], [80, 'CRITICAL', 'critical'], [100, 'CRITICAL', 'critical'],
  ] as const)('shows backend pressure %s as %s with %s tone', (index, condition, tone) => {
    const source = record(index, condition);
    const original = JSON.stringify(source);
    expect(pressureBandView(source)).toMatchObject({ condition, tone,
      color: corridorColor(tone === 'smooth' ? 'SMOOTH' : tone === 'heavy' ? 'HEAVY' : 'CRITICAL') });
    expect(JSON.stringify(source)).toBe(original);
  });

  it.each([
    { pressure_index: undefined }, { pressure_index: null }, { pressure_index: Number.NaN },
    { pressure_index: -1 }, { pressure_index: 101 }, { pressure_index: '69.9' },
    { pressure_condition: undefined }, { pressure_condition: 'HEAVY' },
    { pressure_condition: 'SMOOTH' }, { pressure_condition: 'CAMERA_ESTIMATE' },
    { pressure_condition_policy: undefined }, { pressure_condition_policy: 'legacy_pressure_bands' },
  ])('withholds missing or incoherent backend pressure classification: %j', overrides => {
    expect(pressureBandView(record(69.9, 'SLOWING', overrides))).toBeNull();
  });
});
