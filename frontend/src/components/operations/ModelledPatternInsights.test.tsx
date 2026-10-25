/* @vitest-environment jsdom */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, it } from 'vitest';
import type { ModelledDayProfile, ModelledPatternAnalysis } from '../../types';
import { validatedModelledPatterns } from '../../utils/modelledPatterns';
import { ModelledPatternInsights } from './ModelledPatternInsights';

function fixture(): ModelledPatternAnalysis {
  const profile = (days: number, peak: number, high: number, low: number): ModelledDayProfile => {
    const hourly_profile = Array.from({ length: 24 }, (_, hour) => ({ hour,
      avg_score: hour === peak ? high : low, p10_score: hour === peak ? high - 5 : low - 5,
      p90_score: hour === peak ? high + 5 : low + 5, sample_count: days, day_count: days,
    }));
    return { hourly_profile, day_count: days, sample_count: 24 * days,
      weighted_mean_score: hourly_profile.reduce((sum, row) => sum + row.avg_score, 0) / 24 };
  };
  const weekday = profile(2, 8, 60, 20), weekend = profile(1, 14, 70, 10);
  const all: ModelledDayProfile = { sample_count: 72, day_count: 3,
    weighted_mean_score: (weekday.weighted_mean_score! * 2 + weekend.weighted_mean_score!) / 3,
    hourly_profile: weekday.hourly_profile.map((bucket, hour) => ({ ...bucket,
      avg_score: (bucket.avg_score! * 2 + weekend.hourly_profile[hour].avg_score!) / 3,
      p10_score: Math.min(bucket.p10_score!, weekend.hourly_profile[hour].p10_score!),
      p90_score: Math.max(bucket.p90_score!, weekend.hourly_profile[hour].p90_score!),
      sample_count: 3, day_count: 3,
    })),
  };
  return { version: 'modelled_pattern_analysis_v1', basis: 'synthetic_scenario_samples', observed: false,
    timezone: 'Asia/Jakarta', spread_method: 'empirical_linear_interpolation',
    spread_interpretation: 'descriptive_scenario_spread_not_calibrated_confidence',
    day_profiles: { all, weekday, weekend },
    coverage: { sample_count: 72, day_count: 3, hour_count: 24, first_sample_at: '2026-10-22T00:00:00+07:00', last_sample_at: '2026-10-24T23:00:00+07:00', requested_days: 7 },
  };
}

let root: Root | undefined;
afterEach(() => { act(() => root?.unmount()); root = undefined; document.body.replaceChildren(); });
async function render(analysis = fixture()) {
  const view = document.createElement('div'); document.body.append(view); root = createRoot(view);
  await act(async () => root?.render(<ModelledPatternInsights analysis={analysis} corridorName="Test corridor" daily={[
    { date: '2026-10-22', avg_score: 20, sample_count: 24 },
    { date: '2026-10-23', avg_score: 25, sample_count: 24 },
    { date: '2026-10-24', avg_score: 10, sample_count: 24 },
  ]} />));
  return view;
}

describe('qualified modelled pattern statistics', () => {
  it('accepts explicit scenario-only coverage without turning spread into prediction confidence', () => {
    const data = fixture();
    expect(validatedModelledPatterns(data, 7)).toBe(data);
    data.day_profiles.all.hourly_profile[0].avg_score = 50; // Mean may lie outside central sample quantiles.
    expect(validatedModelledPatterns(data, 7)).toBe(data);
    expect(validatedModelledPatterns(data, 30)).toBeNull();
  });
  it.each(['observed', 'mixed-basis', 'wrong-timezone', 'confidence-claim', 'missing-hours', 'invalid-range', 'infinite', 'mismatched-counts', 'invalid-date', 'empty-with-values'])(
    'withholds incoherent detailed statistics: %s', reason => {
      const data = fixture();
      if (reason === 'observed') Object.assign(data, { observed: true });
      if (reason === 'mixed-basis') Object.assign(data, { basis: 'mixed_history' });
      if (reason === 'wrong-timezone') Object.assign(data, { timezone: 'UTC' });
      if (reason === 'confidence-claim') Object.assign(data, { spread_interpretation: 'prediction_confidence' });
      if (reason === 'missing-hours') data.day_profiles.all.hourly_profile.pop();
      if (reason === 'invalid-range') data.day_profiles.all.hourly_profile[0].p10_score = 90;
      if (reason === 'infinite') data.day_profiles.all.weighted_mean_score = Infinity;
      if (reason === 'mismatched-counts') data.coverage.sample_count += 1;
      if (reason === 'invalid-date') data.coverage.first_sample_at = 'yesterday';
      if (reason === 'empty-with-values') data.day_profiles.weekend.hourly_profile[0].sample_count = 0;
      expect(validatedModelledPatterns(data, 7)).toBeNull();
    },
  );
});

describe('modelled weekday and weekend comparisons', () => {
  it('switches peak, average, sample coverage and daily values together', async () => {
    const view = await render();
    expect(view.textContent).toContain('3 modelled days in the 7-day window');
    expect(view.querySelectorAll('.modelled-patterns__daily button')).toHaveLength(3);
    const buttons = view.querySelectorAll<HTMLButtonElement>('[aria-label="Modelled day type"] button');
    await act(async () => buttons[1].click());
    expect(view.querySelector('.modelled-patterns__metrics')?.textContent).toContain('8am');
    expect(view.querySelector('.modelled-patterns__metrics')?.textContent).toContain('48 modelled samples');
    expect(view.querySelectorAll('.modelled-patterns__daily button')).toHaveLength(2);
    await act(async () => buttons[2].click());
    expect(view.querySelector('.modelled-patterns__metrics')?.textContent).toContain('2pm');
    expect(view.querySelector('.modelled-patterns__metrics')?.textContent).toContain('24 modelled samples');
    expect(view.querySelectorAll('.modelled-patterns__daily button')).toHaveLength(1);
    expect(view.textContent).toContain('not a prediction confidence interval');
    expect(view.textContent).not.toMatch(/accuracy|live forecast:|measured samples/);
  });
  it('keeps the chart on a fixed 0–100 scale and exposes keyboard-readable scenario values', async () => {
    const view = await render();
    const buttons = view.querySelectorAll<HTMLButtonElement>('[aria-label="Modelled day type"] button');
    await act(async () => buttons[1].click());
    const hourly = view.querySelector('[aria-label^="Hourly modelled values"]');
    expect(hourly?.querySelectorAll('button')).toHaveLength(24);
    expect(hourly?.querySelector('button:nth-child(9)')?.getAttribute('aria-label')).toContain('8am: average 60.0, middle 80% 55.0 to 65.0');
    expect(hourly?.querySelector<HTMLElement>('button:nth-child(9) .modelled-patterns__mean')?.style.bottom).toBe('60%');
    expect(view.querySelector('.modelled-patterns__y-axis')?.textContent).toBe('100500');
  });
  it('shows unavailable for an empty day type instead of zero pressure', async () => {
    const data = fixture();
    data.day_profiles.weekend = { sample_count: 0, day_count: 0, weighted_mean_score: null,
      hourly_profile: Array.from({ length: 24 }, (_, hour) => ({ hour, avg_score: null, p10_score: null, p90_score: null, sample_count: 0, day_count: 0 })),
    };
    const view = await render(data);
    await act(async () => view.querySelectorAll<HTMLButtonElement>('[aria-label="Modelled day type"] button')[2].click());
    expect(view.textContent).toContain('No weekend scenarios in this window');
    expect(view.querySelector('.modelled-patterns__metrics')?.textContent).toContain('Unavailable');
    expect(view.querySelector('.modelled-patterns__hourly')).toBeNull();
  });
});
