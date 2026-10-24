import type { HistoricalResearch } from '../../services/api';

// Deliberately small unit fixture; these are not reported as real observations.
export const batamBenchmarkFixture = (): NonNullable<HistoricalResearch['batam_benchmark']> => ({
  scope: 'exploratory_single_batam_site_next_survey_day', evaluated_at: '2026-10-23T12:00:00Z', unit: 'km/h',
  source_rows: 48, source_calendar_days: 3, synthetic_training_rows: 0, serving_model_replaced: false,
  application_history_modified: false, promotion_eligible: false,
  random_forest: { samples: 32, mae_kph: 2, rmse_kph: 3 }, previous_survey_day: { samples: 32, mae_kph: 3, rmse_kph: 4 },
  historical_slot_mean: { samples: 32, mae_kph: 2.5, rmse_kph: 3.5 }, limitations: ['Unit site only'],
  folds: [17, 18].map((day, i) => ({ test_date: `2023-02-${day}`, forecast_origin: `2023-02-${day}T00:00:00+07:00`,
    latest_training_interval_end: `2023-02-${day - 1}T18:00:00+07:00`, training_rows: [16, 32][i], test_rows: 16 })),
});

export const researchFixture = (): HistoricalResearch => ({
  survey: { name: 'Unit survey', location: 'Unit site', source_url: 'https://example.org/article', source_pdf_url: 'https://example.org/survey.pdf',
    attribution: 'Unit source', license: 'Unit licence', license_url: 'https://example.org/licence', source_table: 'Unit table',
    csv_sha256: 'a'.repeat(64), dates: ['2023-02-16', '2023-02-17'], row_count: 2, min_speed_kph: 21, max_speed_kph: 31,
    scope_limitations: ['One site only'], canonical_corridor_mapping: null, serving_model_replaced: false,
    rows: [
      { interval_start: '2023-02-16T07:00:00+07:00', interval_end: '2023-02-16T07:30:00+07:00', direction: 'from_east', speed_kph: 21, travel_seconds: 17, segment_length_m: 100, quality_flag: 'published_aggregate' },
      { interval_start: '2023-02-17T07:00:00+07:00', interval_end: '2023-02-17T07:30:00+07:00', direction: 'from_east', speed_kph: 31, travel_seconds: 12, segment_length_m: 100, quality_flag: 'speed_time_difference_over_0.5kph' },
    ] },
  benchmark: { scope: 'Singapore unit benchmark', source_url: 'https://example.org/archive', source_signal: 'Ordinal bands, not exact km/h', source_license: 'Unit licence',
    license_url: 'https://example.org/licence', source_provenance: 'Unit provenance', timestamp_basis: 'Timezone unknown',
    source_first_timestamp: '2021-03-01T00:00:00', source_last_timestamp: '2021-03-27T23:00:00', source_calendar_days: 27,
    roads: 53, snapshots: 100, speed_band_records: 5300, evaluated_at: '2026-10-23T11:00:00Z', limitations: ['No Batam observations'], synthetic_training_rows: 0, serving_model_replaced: false,
    results: [15, 30, 60].map(horizon_minutes => ({ horizon_minutes, random_forest: { samples: 100, mae_band_units: .4, rmse_band_units: .6 },
      persistence: { samples: 100, mae_band_units: .6, rmse_band_units: .8 }, interval_empirical_coverage: .8 })) },
});

export const saturationFixture = (): NonNullable<HistoricalResearch['saturation_survey']> => ({
  dataset_name: 'Unit saturation survey', location: 'Unit intersection', source_url: 'https://example.org/article',
  source_pdf_url: 'https://example.org/survey.pdf', license: 'Unit licence', license_url: 'https://example.org/licence',
  attribution: 'Unit source', source_table: 'Unit ratio table', scope_limitations: ['No speed observations'],
  unit: 'volume_capacity_ratio', eligible_speed_training_rows: 0, dates: ['2023-06-12', '2023-06-13'],
  rows: [
    { interval_start: '2023-06-12T06:00:00+07:00', interval_end: '2023-06-12T07:00:00+07:00', degree_of_saturation: .9 },
    { interval_start: '2023-06-13T06:00:00+07:00', interval_end: '2023-06-13T07:00:00+07:00', degree_of_saturation: 1.2 },
  ],
});
