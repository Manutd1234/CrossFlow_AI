import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchHistoricalCongestion } from './api';

function payload(source = 'synthetic', observed = false, scope = 'modelled_only') {
  return {
    generated_at: '2026-10-24T12:00:00+07:00', data_source: 'simulated',
    corridor_id: 'corridor-1', days_requested: 7,
    hourly_profile: Array.from({ length: 24 }, (_, hour) => ({ hour, avg_score: hour, sample_count: 1 })),
    weekly_trend: [{ date: '2026-10-24', avg_score: 12, sample_count: 24 }],
    history_metadata: {
      window_days: 7, observed, contains_observed_samples: observed, selection_scope: scope,
      source_counts: { [source]: 24 }, sources: { [source]: { sample_count: 24, observed } },
      latest_sample_at: '2026-10-24T12:00:00+07:00', latest_sample_age_seconds: 0,
      freshness: 'current', freshness_basis: 'Modelled samples in the selected window',
      storage: { engine: 'sqlite', durability: 'ephemeral_instance_file', durable: false,
        shared_across_instances: false, fallback_to_memory: false },
      synthetic_seed: { source: 'synthetic', version: 3, days: 14, timezone: 'WIB (UTC+07:00)',
        generated_for_date: '2026-10-24', observed: false },
    },
  };
}

afterEach(() => vi.unstubAllGlobals());

describe('backend modelled-only traffic-flow analysis', () => {
  it('requests an explicit modelled-only source and preserves its calculated values', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(payload()), { status: 200 }));
    vi.stubGlobal('fetch', fetch);
    const result = await fetchHistoricalCongestion('corridor-1', 7, true, true);
    expect(String(fetch.mock.calls[0][0])).toContain('modelled_only=true');
    expect(String(fetch.mock.calls[0][0])).not.toContain('include_modelled');
    expect(result.data.hourly_profile[0].avg_score).toBe(0);
    expect(result.data.history_metadata).toMatchObject({ selection_scope: 'modelled_only', observed: false });
  });

  it.each([
    ['tomtom_live', true, 'modelled_only'],
    ['simulated', false, 'modelled_only'],
    ['unknown_source', false, 'modelled_only'],
    ['synthetic', false, 'legacy_modelled_preview'],
  ])('withholds %s/%s/%s rather than substitute mixed history', async (source, observed, scope) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify(payload(String(source), Boolean(observed), String(scope))), { status: 200 },
    )));
    const result = await fetchHistoricalCongestion('corridor-1', 7, false, true);
    expect(result.source).toBe('offline');
    expect(result.data.history_metadata).toMatchObject({ source: 'history_unavailable' });
    expect(result.data.hourly_profile.every(bucket => bucket.avg_score === null)).toBe(true);
  });
});
