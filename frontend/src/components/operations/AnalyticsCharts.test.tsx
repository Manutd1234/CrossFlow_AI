/* @vitest-environment jsdom */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it } from 'vitest';
import type { AnalyticsSimulation } from '../../services/api';
import { ForecastCharts, ObservedTrafficCharts } from './AnalyticsCharts';
import type { TrafficPoint } from './analyticsChartData';

let root: Root | undefined;
afterEach(() => { act(() => root?.unmount()); root = undefined; document.body.replaceChildren(); });

function observedPoint(id: string, score: number): TrafficPoint {
  return {
    id, name: id, score, current_speed_kmh: 60 * (1 - score / 100), free_flow_speed_kmh: 60,
    as_of: '2026-10-24T08:00:00Z', age_seconds: 0, provider_confidence: 0.9,
    condition: score <= 5 ? 'FREE_FLOW' : score < 20 ? 'SLOWING' : score < 40 ? 'HEAVY' : 'CRITICAL',
    condition_policy: 'observed_speed_loss_bands_v2',
  };
}

function conditionsView(samples: TrafficPoint[]): HTMLElement {
  const page = document.createElement('div');
  document.body.append(page);
  root = createRoot(page);
  act(() => root?.render(<ObservedTrafficCharts samples={samples} expectedCount={5} />));
  return page.querySelectorAll<HTMLElement>('figure')[1];
}

describe('analytics chart presentation', () => {
  it('shows unavailable measured charts instead of a fabricated distribution', () => {
    const html = renderToStaticMarkup(<ObservedTrafficCharts samples={[]} expectedCount={5} />);
    expect(html).toContain('No fresh measured speeds');
    expect(html).toContain('0 of 5 sampled road points');
    expect(html).not.toContain('analytics-chart-donut');
  });
  it('distinguishes a verified 15-index slowdown from a genuinely clear provider point', () => {
    const chart = conditionsView([observedPoint('clear', 0), observedPoint('slowing', 15)]);
    const labels = Array.from(chart.querySelectorAll('.analytics-chart-legend li'), item => item.textContent);
    expect(labels).toEqual(['Free flow1', 'Slowing1', 'Heavy0', 'Critical0', 'Unavailable0']);
    const slowing = chart.querySelectorAll<HTMLElement>('.analytics-chart-legend li')[1];
    expect(slowing.querySelector<HTMLElement>('.analytics-chart-swatch')?.style.background).toBe('var(--accent-amber)');
    expect(chart.textContent).toContain('Conditions cover sampled road points only.');
    expect(chart.textContent).not.toMatch(/Smooth|smooth <40|Congestion index:/);
  });
  it('uses the sensitive measured warning bands at indices 20 and 40', () => {
    const chart = conditionsView([observedPoint('heavy', 20), observedPoint('critical', 40)]);
    const labels = Array.from(chart.querySelectorAll('.analytics-chart-legend li'), item => item.textContent);
    expect(labels).toEqual(['Free flow0', 'Slowing0', 'Heavy1', 'Critical1', 'Unavailable0']);
  });
  it('renders neutral unavailable points when qualification evidence is missing', () => {
    const unknown = observedPoint('old-backend', 0);
    delete unknown.condition_policy;
    const chart = conditionsView([unknown, { ...observedPoint('low-confidence', 0), provider_confidence: 0.49 }]);
    const legend = Array.from(chart.querySelectorAll('.analytics-chart-legend li'), item => item.textContent);
    expect(legend).toEqual(['Free flow0', 'Slowing0', 'Heavy0', 'Critical0', 'Unavailable2']);
    expect(chart.textContent).toContain('Measurements unavailable');
    expect(chart.textContent).toContain('2 of 5 sampled road points');
    expect(chart.querySelectorAll<HTMLElement>('.analytics-chart-legend li')[4]
      .querySelector<HTMLElement>('.analytics-chart-swatch')?.style.background).toBe('var(--text-muted)');
  });
  it('shows all free flow when all five qualified observations really are clear', () => {
    const chart = conditionsView([0, 1, 2, 3, 5].map((score, index) => observedPoint(String(index), score)));
    const legend = Array.from(chart.querySelectorAll('.analytics-chart-legend li'), item => item.textContent);
    expect(legend).toEqual(['Free flow5', 'Slowing0', 'Heavy0', 'Critical0', 'Unavailable0']);
    expect(chart.textContent).toContain('5 of 5 sampled road points');
  });
  it('supports keyboard inspection without adding every heatmap cell to the tab order', () => {
    const first = { timestamp: '2026-10-24T00:00:00+07:00', mean_score: 25, p10_score: 15, p90_score: 35 };
    const next = { ...first, timestamp: '2026-10-24T01:00:00+07:00', mean_score: 72 };
    const simulation: AnalyticsSimulation = { horizon_hours: 24, step_minutes: 60, timezone: 'Asia/Jakarta', weather: 0,
      hourly_forecast: [first, next], corridors: [{ corridor_id: 'road-a', corridor_name: 'Road A', hourly_forecast: [first, next] }],
      weather_comparison: [], model: {}, limitations: [] };
    const container = document.createElement('div'); document.body.append(container); root = createRoot(container);
    act(() => root?.render(<ForecastCharts simulation={simulation} />));
    const cells = container.querySelectorAll<HTMLButtonElement>('.analytics-heatmap-cell');
    expect(container.querySelectorAll('.analytics-heatmap-cell[tabindex="0"]')).toHaveLength(1);
    act(() => { cells[0].focus(); cells[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })); });
    expect(document.activeElement).toBe(cells[1]);
    expect(container.querySelector('.analytics-heatmap-inspection')?.textContent).toContain('01:00 WIB · 72.0 / 100');
    expect(container.querySelectorAll('.analytics-heatmap-cell[tabindex="0"]')).toHaveLength(1);
    act(() => root?.render(<ForecastCharts simulation={{ ...simulation, corridors: [{ ...simulation.corridors[0],
      hourly_forecast: [first, { ...next, mean_score: 81 }] }] }} />));
    expect(container.querySelector('.analytics-heatmap-inspection')?.textContent).toContain('01:00 WIB · 81.0 / 100');
    expect(container.querySelector('.analytics-heatmap-inspection')?.textContent).not.toContain('72.0');
  });
});
