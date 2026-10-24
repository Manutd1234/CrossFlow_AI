import type { PressureCondition } from '../types';
import { corridorColor } from './format';

export interface PressureBandView {
  condition: PressureCondition;
  tone: 'smooth' | 'heavy' | 'critical' | 'neutral';
  color: string;
}

/** Validate the server's warning on its displayed pressure scale, not raw loss. */
export function pressureBandView(input?: {
  pressure_index?: unknown;
  pressure_condition?: unknown;
  pressure_condition_policy?: unknown;
}): PressureBandView | null {
  const value = input?.pressure_index;
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 100
    || input?.pressure_condition_policy !== 'congestion_pressure_bands_v1') return null;
  const expected = value <= 5 ? 'FREE_FLOW' : value < 70 ? 'SLOWING' : value < 80 ? 'HEAVY' : 'CRITICAL';
  if (input.pressure_condition !== expected) return null;
  const tone = expected === 'CRITICAL' ? 'critical' : expected === 'HEAVY' ? 'heavy' : 'smooth';
  return {
    condition: expected, tone,
    color: corridorColor(tone === 'critical' ? 'CRITICAL' : tone === 'heavy' ? 'HEAVY' : 'SMOOTH'),
  };
}
