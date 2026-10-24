/* @vitest-environment jsdom */

import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BUNDLED_RF_VALIDATION } from '../../data/modelManifest';
import { INITIAL_CORRIDORS } from '../../data/mockData';
import { fetchHistoricalCongestion, fetchModelStatus } from '../../services/api';
import type { ApiHistoryMetadata, Fetched, HistoricalProfile, ModelMetrics } from '../../types';
import { AIModelPanel } from './AIModelPanel';
import { CongestionHeatmap } from './CongestionHeatmap';

vi.mock('../../services/api', () => ({
  fetchHistoricalCongestion: vi.fn(), fetchModelStatus: vi.fn(),
}));

const model = vi.mocked(fetchModelStatus);
const history = vi.mocked(fetchHistoricalCongestion);
const NOW = new Date('2026-10-23T01:30:00Z');
const TRAINED_AT = '2026-10-22T01:30:00Z';
let root: Root | undefined;

function modelResult(score = 0.71): Fetched<ModelMetrics> {
  return {
    data: { ...BUNDLED_RF_VALIDATION, r2_score: score, last_trained_at: TRAINED_AT },
    source: 'simulated', fetchedAt: NOW.toISOString(),
  };
}

function historyResult(corridor = 'corridor-1', days = 7): Fetched<HistoricalProfile> {
  return {
    source: 'simulated', fetchedAt: NOW.toISOString(),
    data: {
      corridor_id: corridor, days_requested: days,
      hourly_profile: Array.from({ length: 24 }, (_, hour) => ({ hour, avg_score: null, sample_count: 0 })),
      weekly_trend: [],
      history_metadata: {
        window_days: days, observed: false, contains_observed_samples: false,
        selection_scope: 'observed_only', sample_count: 0,
        source_counts: {}, sources: {}, latest_sample_at: null,
        latest_sample_age_seconds: null, freshness: 'empty',
        freshness_basis: 'latest observed sample',
        storage: { engine: 'sqlite', durability: 'persistent_file', durable: true, shared_across_instances: false, fallback_to_memory: false },
        synthetic_seed: { source: 'synthetic_profile_seed_v1', version: 1, days: 30, timezone: 'WIB', generated_for_date: null, observed: false },
      },
    },
  };
}

function replayResult(corridor = 'corridor-1', days = 7): Fetched<HistoricalProfile> {
  const result = historyResult(corridor, days);
  result.data.hourly_profile = Array.from({ length: 24 }, (_, hour) => ({
    hour, avg_score: 30 + hour, sample_count: days,
  }));
  result.data.weekly_trend = Array.from({ length: days }, (_, index) => ({
    date: new Date(NOW.getTime() - (days - index - 1) * 86_400_000).toISOString().slice(0, 10),
    avg_score: 40 + index, sample_count: 24,
  }));
  result.data.history_metadata = {
    ...result.data.history_metadata as ApiHistoryMetadata,
    selection_scope: 'legacy_modelled_preview', sample_count: 24 * days,
    source_counts: { synthetic_profile_seed_v1: 24 * days },
    sources: { synthetic_profile_seed_v1: { observed: false, sample_count: 24 * days } },
  };
  return result;
}

async function render(element: ReactElement): Promise<HTMLElement> {
  const container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root?.render(element));
  return container;
}

function button(container: HTMLElement, label: string): HTMLButtonElement {
  const match = Array.from(container.querySelectorAll<HTMLButtonElement>('button'))
    .find(candidate => candidate.textContent?.includes(label));
  expect(match).toBeDefined();
  return match!;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  vi.resetAllMocks();
  model.mockResolvedValue(modelResult());
  history.mockImplementation(async (corridor, days) => historyResult(corridor, days));
});

afterEach(() => {
  act(() => root?.unmount());
  root = undefined;
  document.body.replaceChildren();
  vi.useRealTimers();
});

describe('analytics freshness and missing observations', () => {
  it.each([
    ['waiting_for_eligible_model', false, 'waiting for an eligible observed model'],
    ['restore_failed_previous_model_retained', false, 'Observed model restoration failed'],
    ['restored', true, 'An observed model was restored'],
  ] as const)('keeps artifact status %s out of the simplified model view', async (status, restored, message) => {
    const response = modelResult();
    response.data.observed_artifact_status = { configured: true, status, restored };
    model.mockResolvedValueOnce(response);
    const container = await render(<AIModelPanel />);
    expect(container.textContent).not.toContain(message);
    expect(container.textContent).not.toContain('Vehicle counting preview');
    expect(container.textContent).not.toContain('Observed-data chronological backtest');
    expect(container.textContent).not.toContain('sha256');
  });

  it('keeps training scope and its timestamp while removing polling prose', async () => {
    const container = await render(<AIModelPanel />);
    expect(container.textContent).toContain('0.7100');
    expect(container.textContent).toContain('raw forest before the published-timetable adjustment');
    expect(container.textContent).toContain('do not measure the separate timetable adjustment');
    expect(container.textContent).toContain('refreshing does not retrain the model');
    expect(container.querySelector(`time[datetime="${TRAINED_AT}"]`)).not.toBeNull();
    expect(container.querySelector(`time[datetime="${NOW.toISOString()}"]`)).toBeNull();
    expect(container.textContent).not.toContain('API snapshot');
  });

  it('checks model status on focus, every minute and manually without inventing new scores', async () => {
    const container = await render(<AIModelPanel />);
    await act(async () => window.dispatchEvent(new Event('focus')));
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(model).toHaveBeenCalledTimes(3);
    expect(container.textContent).toContain('0.7100');
    model.mockResolvedValueOnce(modelResult(0.62));
    await act(async () => button(container, 'Refresh model status').click());
    expect(model).toHaveBeenCalledTimes(4);
    expect(container.textContent).toContain('0.6200');
    expect(container.textContent).not.toContain('0.7100');
  });

  it('does not display bundled constants as operator telemetry when the API is unavailable', async () => {
    model.mockResolvedValueOnce({ data: BUNDLED_RF_VALIDATION, source: 'offline', fetchedAt: NOW.toISOString() });
    const container = await render(<AIModelPanel />);
    expect(container.textContent).toContain('MODEL STATUS UNAVAILABLE');
    expect(container.textContent).toContain('MODEL API UNAVAILABLE');
    expect(container.textContent).not.toContain('0.9490');
    expect(container.textContent).not.toContain('4,000');
    expect(container.querySelector('[role="progressbar"]')).toBeNull();
    expect(container.textContent).not.toContain('BUNDLED RF');
  });

  it('renders MAE and training rows from each API model version rather than fixed display values', async () => {
    const response = modelResult();
    response.data = { ...response.data, mae: 7.13, total_samples: 1234 };
    model.mockResolvedValueOnce(response);
    const container = await render(<AIModelPanel />);
    expect(container.querySelector('.operations-model-metric__value--cyan')?.textContent).toContain('7.13');
    expect(container.querySelector('.operations-model-metric__value--indigo')?.textContent).toContain('1,234');
    expect(container.querySelector('[aria-label="Saved training run"]')?.textContent).toContain('Live traffic updates separately');
    model.mockResolvedValueOnce({ ...response, data: { ...response.data, mae: 1.24, total_samples: 9876 } });
    await act(async () => button(container, 'Refresh model status').click());
    expect(container.querySelector('.operations-model-metric__value--cyan')?.textContent).toContain('1.24');
    expect(container.querySelector('.operations-model-metric__value--indigo')?.textContent).toContain('9,876');
    expect(container.querySelector('.operations-model-metric__value--cyan')?.textContent).not.toContain('7.13');
  });

  it('coalesces focus/poll checks while model status is pending and cancels on unmount', async () => {
    let finish!: (value: Fetched<ModelMetrics>) => void;
    model.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    await render(<AIModelPanel />);
    await act(async () => {
      window.dispatchEvent(new Event('focus'));
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(model).toHaveBeenCalledTimes(1);
    act(() => root?.unmount());
    root = undefined;
    await act(async () => finish(modelResult()));
    await act(async () => vi.advanceTimersByTimeAsync(120_000));
    expect(model).toHaveBeenCalledTimes(1);
  });

  it('keeps an empty observed snapshot empty with an explicit measurement scope', async () => {
    const container = await render(<CongestionHeatmap corridors={INITIAL_CORRIDORS} />);
    expect(container.textContent).toContain('NO OBSERVED HISTORY');
    expect(container.querySelector('[aria-label="History provenance"]')).toBeNull();
    expect(container.textContent).toContain('No historical samples are available');
    expect(container.querySelector('[aria-label^="24-hour congestion profile"]')).toBeNull();
    expect(container.querySelector('[aria-label^="Daily congestion trend"]')).toBeNull();
    expect(container.textContent).not.toContain('API-MODELLED HISTORY');
    expect(container.textContent).not.toContain('modelled samples only');
    expect(container.querySelector('[aria-label="Partial history window"]')).toBeNull();
  });

  it('discloses when the bounded snapshot cannot represent the full requested history window', async () => {
    const limited = historyResult();
    limited.data.history_metadata = {
      ...limited.data.history_metadata as ApiHistoryMetadata,
      snapshot_may_be_truncated: true, snapshot_loaded_rows: 10_000,
      snapshot_max_rows: 10_000,
      window_basis: 'Batam calendar days including the current day',
    };
    history.mockResolvedValueOnce(limited);
    const container = await render(<CongestionHeatmap corridors={INITIAL_CORRIDORS} />);
    const disclosure = container.querySelector('[aria-label="Partial history window"]');
    expect(disclosure?.textContent).toContain('snapshot limit of 10,000 rows');
    expect(disclosure?.textContent).toContain('selected 7-day window may be incomplete');
    expect(disclosure?.textContent).toContain('No missing samples were filled in');
    expect(disclosure?.textContent).toContain('Batam calendar days including the current day');
    expect(container.querySelector('[aria-label^="24-hour congestion profile"]')).toBeNull();
  });

  it('rechecks history on focus, polling and explicit refresh without creating samples', async () => {
    const container = await render(<CongestionHeatmap corridors={INITIAL_CORRIDORS} />);
    await act(async () => window.dispatchEvent(new Event('focus')));
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    await act(async () => button(container, 'Refresh history').click());
    expect(history).toHaveBeenCalledTimes(4);
    expect(history).toHaveBeenLastCalledWith('corridor-1', 7, false);
    expect(container.textContent).toContain('NO OBSERVED HISTORY');
    expect(container.querySelector('[aria-label^="24-hour congestion profile"]')).toBeNull();
  });

  it('cannot restore a previous corridor when its slower history request finishes late', async () => {
    let finishFirst!: (value: Fetched<HistoricalProfile>) => void;
    history.mockImplementationOnce(() => new Promise(resolve => { finishFirst = resolve; }));
    const container = await render(<CongestionHeatmap corridors={INITIAL_CORRIDORS} />);
    await act(async () => root?.render(<CongestionHeatmap corridors={[INITIAL_CORRIDORS[1]]} />));
    expect(history).toHaveBeenLastCalledWith('corridor-2', 7, false);
    await act(async () => finishFirst(historyResult('corridor-1')));
    expect(container.textContent).toContain('NO OBSERVED HISTORY');
    expect(container.querySelector('[aria-label="Corridor history"] [aria-pressed="true"]')?.textContent).toContain(
      INITIAL_CORRIDORS[1].name.split('->')[0].trim().split(' ').slice(0, 2).join(' '),
    );
  });

  it('reports an unreachable history API without silently drawing a browser simulation', async () => {
    const unavailable = historyResult();
    unavailable.source = 'offline';
    unavailable.data.history_metadata = {
      source: 'history_unavailable', observed: false,
      model: 'crossflow_local_forecast_equation_v1',
      methodology: 'The observed-history API is unavailable.', limitations: [],
      reference_time: NOW.toISOString(),
    };
    history.mockResolvedValueOnce(unavailable);
    const container = await render(<CongestionHeatmap corridors={INITIAL_CORRIDORS} />);
    expect(container.textContent).toContain('HISTORY UNAVAILABLE');
    expect(container.querySelector('[aria-label="History provenance"]')).toBeNull();
    expect(container.textContent).not.toContain('Browser continuity model');
    expect(container.querySelector('[aria-label^="24-hour congestion profile"]')).toBeNull();
  });

  it('loads simulated hourly and daily charts only after explicitly selecting Synthetic replay', async () => {
    history.mockImplementation(async (corridor, days, includeModelled) => (
      includeModelled ? replayResult(corridor, days) : historyResult(corridor, days)
    ));
    const container = await render(<CongestionHeatmap corridors={INITIAL_CORRIDORS} />);
    expect(history).toHaveBeenLastCalledWith('corridor-1', 7, false);
    expect(button(container, 'Measured history').getAttribute('aria-pressed')).toBe('true');
    expect(container.querySelector('[aria-label^="24-hour congestion profile"]')).toBeNull();
    await act(async () => button(container, 'Synthetic replay').click());
    expect(history).toHaveBeenLastCalledWith('corridor-1', 7, true);
    expect(container.textContent).toContain('SYNTHETIC REPLAY');
    expect(container.querySelector('.badge')?.textContent).toContain('SYNTHETIC REPLAY');
    expect(container.querySelector('[aria-label="History provenance"]')).toBeNull();
    expect(container.querySelector('[aria-label^="24-hour congestion profile"]')).not.toBeNull();
    expect(container.querySelector('[aria-label^="Daily congestion trend"]')).not.toBeNull();
    expect(container.textContent).toContain('168 modelled stored samples');
    await act(async () => button(container, 'Measured history').click());
    expect(history).toHaveBeenLastCalledWith('corridor-1', 7, false);
    expect(container.textContent).toContain('NO OBSERVED HISTORY');
    expect(container.querySelector('[aria-label^="24-hour congestion profile"]')).toBeNull();
  });

  it('ignores a replay response arriving after the user switches back to measurements', async () => {
    let finishReplay!: (value: Fetched<HistoricalProfile>) => void;
    history.mockImplementation(async (corridor, days, includeModelled) => (
      includeModelled ? new Promise(resolve => { finishReplay = resolve; }) : historyResult(corridor, days)
    ));
    const container = await render(<CongestionHeatmap corridors={INITIAL_CORRIDORS} />);
    await act(async () => button(container, 'Synthetic replay').click());
    await act(async () => button(container, 'Measured history').click());
    await act(async () => finishReplay(replayResult()));
    expect(button(container, 'Measured history').getAttribute('aria-pressed')).toBe('true');
    expect(container.textContent).toContain('NO OBSERVED HISTORY');
    expect(container.querySelector('[aria-label^="24-hour congestion profile"]')).toBeNull();
    expect(container.textContent).not.toContain('168 modelled stored samples');
  });

  it.each([
    { allObserved: false, label: 'MIXED ARCHIVE REPLAY', summary: '84 observed of 168 stored samples' },
    { allObserved: true, label: 'OBSERVED LEGACY ARCHIVE', summary: '168 observed samples' },
  ])('preserves actual legacy source declarations in replay: $label', async ({ allObserved, label, summary }) => {
    const replay = replayResult();
    const count = allObserved ? 168 : 84;
    replay.data.history_metadata = {
      ...replay.data.history_metadata as ApiHistoryMetadata,
      observed: allObserved, contains_observed_samples: true,
      source_counts: allObserved ? { probe_gps: count } : { probe_gps: count, synthetic_profile_seed_v1: 84 },
      sources: allObserved
        ? { probe_gps: { observed: true, sample_count: count } }
        : { probe_gps: { observed: true, sample_count: count }, synthetic_profile_seed_v1: { observed: false, sample_count: 84 } },
    };
    history.mockImplementation(async (corridor, days, includeModelled) => includeModelled ? replay : historyResult(corridor, days));
    const container = await render(<CongestionHeatmap corridors={INITIAL_CORRIDORS} />);
    await act(async () => button(container, 'Synthetic replay').click());
    expect(container.textContent).toContain(label);
    expect(container.textContent).toContain(summary);
    expect(container.querySelector('[aria-label="History provenance"]')).toBeNull();
    expect(container.textContent).not.toContain('Synthetic archive replay — not observed traffic');
  });
});
