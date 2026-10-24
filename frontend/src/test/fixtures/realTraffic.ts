import type { RealTrafficForecast } from '../../services/api';

/** Deliberately varied test values: never a production fallback. */
export function realTrafficFixture(): RealTrafficForecast {
  const test = { samples: 20, mae_points: .4, rmse_points: .7, r2: -.2 };
  return {
    generated_at: '2026-10-24T00:00:00Z', source: 'observed_traffic_model', observed: false,
    status: 'provisional_observed_model',
    model: { available: true, engine: { algorithm: 'RandomForestRegressor', trees_per_horizon: { '15': 17, '30': 23, '60': 31 }, feature_count: 3, synthetic_rows: 0 },
      report: { trained_at: '2026-10-23T23:00:00Z', raw_observed_rows: 1234, selected_rows: 200, training_pairs: 43,
        span_hours: 9.75, distinct_local_days: 2, weather_training_rows: 7, synthetic_rows: 0,
        feature_names: ['current_index', 'weather_missing', 'mapped_segment_length_m'],
        source_counts: { loop_sensor: 200 }, quality_gate: { eligible: false, reasons: ['Needs seven days of measured coverage.'] },
        horizons: [15, 30, 60].map(horizon => ({ horizon_minutes: horizon, selected_strategy: 'persistence',
          test, forest_test: { ...test, mae_points: 1.7 }, persistence_test: test, speed_mae_kph: .2,
          pairs: { fit: 30, validation: 13, calibration: 12, holdout: 20 },
          interval: { target: .8, calibration_pairs: 12, test_coverage: .7, coverage_guaranteed: false, network_calibration_groups: 2 },
          feature_importances: { current_index: .8, weather_missing: 0, mapped_segment_length_m: .2 } })),
      } },
    weather: { status: 'fresh', source: 'Open-Meteo', source_url: 'https://open-meteo.com/en/docs', captured_at: '2026-10-24T00:00:00Z',
      current: { valid_at: '2026-10-24T00:00:00Z', captured_at: '2026-10-24T00:00:00Z', weather_code: 61,
        precipitation_mm: 1.2, temperature_c: 27.3, wind_kph: 7 }, hourly: [] },
    points: [{ corridor_id: 'corridor-1', observed_at: '2026-10-23T23:59:00Z', current_speed_kph: 40, free_flow_speed_kph: 50, current_index: 20,
      road: { matched: true, match_distance_m: .5, road_name: 'Fixture road', road_class: 'secondary', lanes: null,
        posted_maxspeed_kph: null, capacity_vph: null, segment_length_m: 80, nearest_mapped_terminal_km: 3,
        graph_revision: 'fixture-graph', source: 'OpenStreetMap', source_url: 'https://www.openstreetmap.org/copyright' },
      forecasts: [15, 30, 60].map(horizon => ({ horizon_minutes: horizon, mean_score: 20, lower: null, upper: null,
        strategy: 'persistence', forest_candidate_score: 23 })),
    }],
    network: [15, 30, 60].map(horizon => ({ horizon_minutes: horizon, mean_score: 20, lower: null, upper: null })),
    coverage: { monitored_points: 1, expected_points: 5, max_horizon_minutes: 60 },
    limitations: ['Fixture evidence is limited to monitored points.'],
  };
}
