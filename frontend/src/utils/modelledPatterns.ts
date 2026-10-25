import type { ModelledDayProfile, ModelledPatternAnalysis } from '../types';

const count = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;
const score = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100;
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object';
const timestamp = (value: unknown): boolean => typeof value === 'string'
  && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value));

function validProfile(value: unknown): value is ModelledDayProfile {
  if (!record(value) || !count(value.sample_count) || !count(value.day_count)
    || value.day_count > value.sample_count || !Array.isArray(value.hourly_profile)
    || value.hourly_profile.length !== 24) return false;
  if (value.sample_count === 0 ? value.weighted_mean_score !== null : !score(value.weighted_mean_score)) return false;
  const dayCount = value.day_count;
  return value.hourly_profile.every((bucket, hour) => record(bucket) && bucket.hour === hour
    && count(bucket.sample_count) && count(bucket.day_count)
    && bucket.day_count <= bucket.sample_count && bucket.day_count <= dayCount
    && (bucket.sample_count === 0
      ? bucket.avg_score === null && bucket.p10_score === null && bucket.p90_score === null && bucket.day_count === 0
      : score(bucket.avg_score) && score(bucket.p10_score) && score(bucket.p90_score)
        && bucket.p10_score <= bucket.p90_score && bucket.day_count > 0))
    && value.hourly_profile.reduce((sum, bucket) => sum + bucket.sample_count, 0) === value.sample_count;
}

/** Descriptive synthetic-pattern statistics must never imply measured confidence. */
export function validatedModelledPatterns(value: unknown, days: number): ModelledPatternAnalysis | null {
  if (!record(value) || value.version !== 'modelled_pattern_analysis_v1'
    || value.basis !== 'synthetic_scenario_samples' || value.observed !== false
    || value.timezone !== 'Asia/Jakarta' || value.spread_method !== 'empirical_linear_interpolation'
    || value.spread_interpretation !== 'descriptive_scenario_spread_not_calibrated_confidence'
    || !record(value.day_profiles) || !record(value.coverage)) return null;
  const { all, weekday, weekend } = value.day_profiles;
  if (!validProfile(all) || !validProfile(weekday) || !validProfile(weekend)) return null;
  const coverage = value.coverage;
  if (coverage.requested_days !== days || !count(coverage.sample_count) || !count(coverage.day_count)
    || !count(coverage.hour_count) || coverage.hour_count > 24 || coverage.day_count > days + 1
    || coverage.sample_count !== all.sample_count || coverage.day_count !== all.day_count
    || all.sample_count !== weekday.sample_count + weekend.sample_count
    || all.day_count !== weekday.day_count + weekend.day_count
    || coverage.hour_count !== all.hourly_profile.filter(bucket => bucket.sample_count > 0).length
    || !all.hourly_profile.every((bucket, hour) => bucket.sample_count
      === weekday.hourly_profile[hour].sample_count + weekend.hourly_profile[hour].sample_count)) return null;
  if (all.sample_count === 0) {
    if (coverage.first_sample_at !== null || coverage.last_sample_at !== null) return null;
  } else if (!timestamp(coverage.first_sample_at) || !timestamp(coverage.last_sample_at)
    || Date.parse(coverage.first_sample_at as string) > Date.parse(coverage.last_sample_at as string)) return null;
  return value as unknown as ModelledPatternAnalysis;
}
