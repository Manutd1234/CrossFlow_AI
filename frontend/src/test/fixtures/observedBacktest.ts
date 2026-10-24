import type { Fetched, ObservedBacktestReport, ObservedBacktestResult } from '../../types';

/** Test evidence only; production never imports these sample observations or scores. */
export function observedReport(evaluated = false): ObservedBacktestReport {
  const report: ObservedBacktestReport = {
    schema_version: 1, generated_at: '2026-10-23T08:00:00Z', status: 'insufficient_observations',
    validation_scope: 'observed_chronological_backtest', model_replaced: false,
    evaluation_subject: 'private_observed_candidate_serving_pipeline', running_model_evaluated: false,
    assumptions: ['Weather uses clear encoding because measurements do not record weather; weather response is not validated.',
      'Ferry proximity is unknown; the published-timetable adjustment is not validated by this backtest.'],
    selection: { input_rows: 60, unique_rows: 60, eligible_rows: 60, observed_rows: 60, selected_rows: 10,
      excluded_non_observed_rows: 0, excluded_unsupported_corridor_rows: 0, duplicate_rows: 0,
      source_counts: { tomtom_live: 60 }, first_at: '2026-10-23T07:38:00Z', last_at: '2026-10-23T08:00:00Z',
      span_hours: 22 / 60, distinct_timestamps: 12, sampling_policy: 'first_original_per_location_15min_utc_bucket' },
    chronology: { fit: { rows: 0, start_at: null, end_at: null }, calibration: { rows: 0, start_at: null, end_at: null },
      holdout: { rows: 0, start_at: null, end_at: null }, purge_minutes: 60, strict_timestamp_groups: true },
    horizons: [], activation: { eligible: false, reasons: ['observed_span_below_required_minimum'],
      policy: { min_span_hours: 168, min_fit_rows: 100, min_calibration_rows: 50, min_holdout_rows: 100, horizons_minutes: [15, 30, 60] } },
  };
  if (evaluated) {
    report.status = 'evaluated';
    report.selection = { ...report.selection, input_rows: 5000, unique_rows: 5000, eligible_rows: 5000, observed_rows: 5000,
      selected_rows: 300, source_counts: { tomtom_live: 5000 }, first_at: '2026-10-15T08:00:00Z', span_hours: 192, distinct_timestamps: 1000 };
    report.chronology = { fit: { rows: 120, start_at: '2026-10-15T08:00:00Z', end_at: '2026-10-18T07:58:00Z' },
      calibration: { rows: 60, start_at: '2026-10-18T08:00:00Z', end_at: '2026-10-19T08:00:00Z' },
      holdout: { rows: 120, start_at: '2026-10-20T08:00:00Z', end_at: '2026-10-23T08:00:00Z' }, purge_minutes: 60, strict_timestamp_groups: true };
    report.horizons = [15, 30, 60].map(horizon => ({ horizon_minutes: horizon, status: 'evaluated',
      serving_model: { samples: 40, mae_points: 4.25, rmse_points: 5.125, bias_points: -1.25 },
      persistence: { samples: 40, mae_points: 3.125, rmse_points: 4.25, bias_points: -0.25 },
      current_history_blend: { samples: 40, mae_points: 3.5, rmse_points: 4.5, bias_points: -0.5 }, p10_p90_coverage: 0.625 }));
    report.activation.reasons = ['candidate_does_not_beat_persistence'];
    report.interval_calibration = { method: 'chronological_split_conformal_tree_interval', calibration_rows: 60,
      calibration_forecast_pairs: 180, padding_points: 1.125, target_coverage: 0.8, coverage_guaranteed: false };
  }
  return report;
}

export function observedResult(evaluated = false): Fetched<ObservedBacktestResult> {
  return { source: 'simulated', fetchedAt: '2026-10-23T08:00:00Z', data: {
    backtest: observedReport(evaluated), evaluation_snapshot: { durability: 'local_sqlite', loaded_rows: evaluated ? 5000 : 60,
      bounded_max_rows: 50000, excluded_modelled_rows: 0, model_replaced: false, cache_ttl_seconds: 60 },
  } };
}
