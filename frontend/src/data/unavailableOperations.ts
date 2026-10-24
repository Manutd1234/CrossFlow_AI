import type { OperationsSummary } from '../types';

/** Missing telemetry is unknown, never a zero-valued or simulated operating day. */
export const UNAVAILABLE_OPERATIONS: OperationsSummary = {
  overall_network_status: 'UNAVAILABLE',
  average_congestion_index: null,
  active_bottlenecks: null,
  total_co2_reduced_today_kg: null,
  active_ferry_sailings: null,
  alerts: [],
};
