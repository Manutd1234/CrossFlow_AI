import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchHistoricalResearch } from './api';
import { batamBenchmarkFixture, researchFixture, saturationFixture } from '../test/fixtures/historicalResearch';

afterEach(() => vi.unstubAllGlobals());
function reply(value: unknown) { vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(value), { status: 200 }))); }
describe('historical evidence contract', () => {
  it('accepts a separate site backtest and rejects training across its forecast origin', async () => {
    const value = { ...researchFixture(), batam_benchmark: batamBenchmarkFixture() };
    reply(value);
    expect((await fetchHistoricalResearch()).batam_benchmark?.unit).toBe('km/h');
    value.batam_benchmark.folds[0].latest_training_interval_end = value.batam_benchmark.folds[0].forecast_origin;
    reply(value);
    await expect(fetchHistoricalResearch()).rejects.toThrow('could not be validated');
  });
  it('accepts dated observations and a separate band-unit benchmark', async () => {
    reply(researchFixture());
    expect((await fetchHistoricalResearch()).survey.rows).toHaveLength(2);
  });
  it.each(['unsafe-link', 'invalid-speed', 'active-model', 'invalid-interval', 'invalid-metric'])('rejects %s instead of rendering sample evidence', async fault => {
    const value = researchFixture();
    if (fault === 'unsafe-link') value.survey.source_url = 'javascript:alert(1)';
    if (fault === 'invalid-speed') value.survey.rows[0].speed_kph = -1;
    if (fault === 'active-model') Object.assign(value.benchmark, { serving_model_replaced: true });
    if (fault === 'invalid-interval') value.survey.rows[0].interval_end = value.survey.rows[0].interval_start;
    if (fault === 'invalid-metric') value.benchmark.results[0].random_forest.mae_band_units = 99;
    reply(value);
    await expect(fetchHistoricalResearch()).rejects.toThrow('could not be validated');
  });
  it('rejects saturation records labelled as speed training targets', async () => {
    const value = researchFixture();
    Object.assign(value, { saturation_survey: { unit: 'km/h', eligible_speed_training_rows: 28 } });
    reply(value);
    await expect(fetchHistoricalResearch()).rejects.toThrow('could not be validated');
  });
  it('accepts published saturation ratios without converting them into speeds', async () => {
    reply({ ...researchFixture(), saturation_survey: saturationFixture() });
    const result = await fetchHistoricalResearch();
    expect(result.saturation_survey?.rows[1].degree_of_saturation).toBe(1.2);
    expect(result.saturation_survey?.eligible_speed_training_rows).toBe(0);
  });
});
