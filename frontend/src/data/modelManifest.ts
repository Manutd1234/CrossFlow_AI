import type { ModelMetrics, ObservedBacktestReport } from '../types';

export type ModelTrainingDataSource =
  | 'synthetic_profile_generator'
  | 'history_store_observed'
  | 'history_store_mixed'
  | 'history_store_non_observed'
  | 'validated_spatial_observed'
  | 'validated_spatial_mixed'
  | 'validated_spatial_modelled';

export type ModelValidationScope =
  | 'synthetic_holdout'
  | 'history_holdout_observed'
  | 'history_holdout_mixed'
  | 'history_holdout_non_observed'
  | 'chronological_spatial_holdout_observed'
  | 'purged_chronological_observed_holdout'
  | 'chronological_spatial_holdout_mixed'
  | 'chronological_spatial_holdout_modelled';

/** Additive metadata understood by new clients without changing ModelMetrics. */
export type ModelMetricsWithProvenance = ModelMetrics & {
  training_data_source?: ModelTrainingDataSource;
  validation_scope?: ModelValidationScope;
  training_source_counts?: Record<string, number>;
  observed_training_rows?: number;
  observed_fit_rows?: number;
  observed_calibration_rows?: number;
  observed_evaluation_rows?: number;
  observed_evaluation_horizon_minutes?: number;
  observed_training?: ObservedBacktestReport;
  calibration?: {
    interval?: string;
    interval_coverage_target: number;
    interval_coverage_observed: number;
    mean_interval_width_points?: number;
    empirical_crps?: number;
    empirical_crps_method?: string;
    holdout_order?: string;
    holdout_samples?: number;
    mean_prediction?: number;
    calibration_rows?: number;
    calibration_forecast_pairs?: number;
    padding_points?: number;
    coverage_guaranteed?: boolean;
  };
};

/**
 * Reproducible output of backend/models/congestion_model.py (seed 42).
 * Engineering and test reference only. Operator telemetry requires a model API
 * response and never substitutes this bundled manifest for a missing backend.
 */
export const BUNDLED_RF_VALIDATION: ModelMetricsWithProvenance = {
  is_trained: true,
  retraining_enabled: false,
  total_samples: 4000,
  r2_score: 0.949,
  mae: 2.87,
  rmse: 3.61,
  last_trained_at: null,
  training_data_source: 'synthetic_profile_generator',
  validation_scope: 'synthetic_holdout',
  evaluation_scope: 'raw_forest_holdout_excludes_post_model_adjustment',
  feature_importance_scope: 'raw_forest_impurity_importance',
  training_source_counts: { synthetic_profile_generator: 4000 },
  observed_training_rows: 0,
  calibration: {
    interval: 'P10-P90 empirical tree quantiles',
    interval_coverage_target: 0.8,
    interval_coverage_observed: 0.6613,
    mean_interval_width_points: 7.873,
    empirical_crps: 2.223,
    empirical_crps_method: 'mean|tree_prediction-y| - 0.5*mean|tree_prediction_i-tree_prediction_j|',
    holdout_order: 'deterministic_generator_order',
    holdout_samples: 800,
    mean_prediction: 35.614,
  },
  feature_importances: {
    'Time of Day (Cyclical)': 0.551,
    'Day of Week (Cyclical)': 0.032,
    'Weather Condition': 0.232,
    'Ferry Surge Feature (Training Forest)': 0.152,
    'Corridor Identity (One-hot)': 0.005,
    'Road Class (One-hot)': 0.003,
    Capacity: 0.007,
    'Terminal Distance': 0.01,
    'Free-flow Speed': 0.008,
  },
};
