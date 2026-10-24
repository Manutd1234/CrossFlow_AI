/* @vitest-environment jsdom */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { INITIAL_CORRIDORS } from '../../data/mockData';
import { fetchHistoricalCongestion } from '../../services/api';
import type { ApiHistoryMetadata, Fetched, HistoricalProfile } from '../../types';
import { CongestionHeatmap } from './CongestionHeatmap';

vi.mock('../../services/api', () => ({ fetchHistoricalCongestion: vi.fn() }));
const history = vi.mocked(fetchHistoricalCongestion);
let root: Root | undefined;

function modelledResult(corridor = 'corridor-1', days = 7): Fetched<HistoricalProfile> {
  return {
    source: 'simulated', fetchedAt: '2026-10-24T08:00:00Z',
    data: {
      corridor_id: corridor, days_requested: days,
      hourly_profile: Array.from({ length: 24 }, (_, hour) => ({ hour, avg_score: hour + 20, sample_count: days })),
      weekly_trend: Array.from({ length: days }, (_, day) => ({ date: `2026-10-${String(day + 1).padStart(2, '0')}`, avg_score: day + 20, sample_count: 24 })),
      history_metadata: {
        selection_scope: 'modelled_only', window_days: days, observed: false, contains_observed_samples: false,
        source_counts: { synthetic: 24 * days },
        sources: { synthetic: { observed: false, sample_count: 24 * days } },
        latest_sample_at: '2026-10-24T08:00:00Z', latest_sample_age_seconds: 0,
        freshness: 'current', freshness_basis: 'latest modelled archive sample',
        storage: { engine: 'sqlite', durability: 'persistent_file', durable: true, shared_across_instances: false, fallback_to_memory: false },
        synthetic_seed: { source: 'synthetic_profile_seed_v1', version: 1, days: 30, timezone: 'WIB', generated_for_date: null, observed: false },
      },
    },
  };
}

async function render() {
  const view = document.createElement('div'); document.body.append(view); root = createRoot(view);
  await act(async () => root?.render(<CongestionHeatmap corridors={INITIAL_CORRIDORS} mode="modelled" />));
  return view;
}

beforeEach(() => { history.mockReset(); history.mockImplementation(async (corridor, days) => modelledResult(corridor, days)); });
afterEach(() => { act(() => root?.unmount()); root = undefined; document.body.replaceChildren(); });

describe('backend modelled traffic-flow analysis', () => {
  it('uses only the explicit backend model-only query and labels the plots as scenarios', async () => {
    const view = await render();
    expect(history).toHaveBeenCalledWith('corridor-1', 7, false, true);
    expect(view.textContent).toContain('MODELLED TRAFFIC FLOW');
    expect(view.textContent).toContain('168 modelled samples');
    expect(view.textContent).toContain('not measured speeds, vehicle counts or a live forecast');
    expect(view.textContent).toContain('Modelled peak hour');
    expect(view.querySelector('[aria-label^="Modelled 24-hour congestion profile"]')).not.toBeNull();
    expect(view.querySelector('[aria-label^="Modelled daily pattern"]')).not.toBeNull();
    expect(view.querySelector('[aria-label="Historical data source"]')).toBeNull();
    expect(view.textContent).not.toMatch(/Measured history|Synthetic replay|OBSERVED HISTORY|observed samples/);
  });

  it('keeps model-only queries when corridor and time-window filters change', async () => {
    const view = await render();
    await act(async () => view.querySelector<HTMLButtonElement>('[aria-label="Show the last 14 days"]')?.click());
    expect(history).toHaveBeenLastCalledWith('corridor-1', 14, false, true);
    await act(async () => view.querySelectorAll<HTMLButtonElement>('[aria-label="Corridor history"] button')[1]?.click());
    expect(history).toHaveBeenLastCalledWith('corridor-2', 14, false, true);
    expect(view.textContent).toContain('336 modelled samples');
    expect(view.querySelector('[aria-label^="Modelled 24-hour congestion profile"]')?.getAttribute('aria-label'))
      .toContain(INITIAL_CORRIDORS[1].name);
  });

  it.each(['legacy-mixed', 'observed-flag', 'observed-source', 'browser-fallback'] as const)(
    'withholds data that cannot establish model-only provenance: %s', async reason => {
      const result = modelledResult();
      const metadata = result.data.history_metadata as ApiHistoryMetadata;
      if (reason === 'legacy-mixed') metadata.selection_scope = 'legacy_modelled_preview';
      if (reason === 'observed-flag') metadata.contains_observed_samples = true;
      if (reason === 'observed-source') metadata.sources.tomtom_live = { observed: true, sample_count: 1 };
      if (reason === 'browser-fallback') result.data.history_metadata = {
        source: 'history_unavailable', observed: false, model: 'crossflow_local_forecast_equation_v1',
        methodology: 'No archive available.', limitations: [], reference_time: result.fetchedAt,
      };
      history.mockResolvedValueOnce(result);
      const view = await render();
      expect(view.querySelector('[role="alert"]')?.textContent).toContain('verified model-only source is required');
      expect(view.textContent).toContain('ANALYSIS UNAVAILABLE');
      expect(view.querySelector('.operations-history__summary')).toBeNull();
      expect(view.querySelectorAll('[role="img"]')).toHaveLength(0);
    },
  );

  it('shows source failure without a browser substitute and recovers through the update button', async () => {
    history.mockRejectedValueOnce(new Error('Analysis archive unavailable.'));
    const view = await render();
    expect(view.querySelector('[role="alert"]')?.textContent).toContain('Analysis archive unavailable');
    expect(view.querySelectorAll('[role="img"]')).toHaveLength(0);
    await act(async () => view.querySelector<HTMLButtonElement>('.operations-history__status button')?.click());
    expect(history).toHaveBeenLastCalledWith('corridor-1', 7, false, true);
    expect(view.querySelector('[role="alert"]')).toBeNull();
    expect(view.textContent).toContain('MODELLED TRAFFIC FLOW');
    expect(view.querySelector('[aria-label^="Modelled 24-hour congestion profile"]')).not.toBeNull();
  });
});
