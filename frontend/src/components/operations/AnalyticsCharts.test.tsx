/* @vitest-environment jsdom */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it } from 'vitest';
import type { AnalyticsSimulation } from '../../services/api';
import { ForecastCharts, ObservedTrafficCharts } from './AnalyticsCharts';

let root: Root | undefined;
afterEach(() => { act(() => root?.unmount()); root = undefined; document.body.replaceChildren(); });

describe('analytics chart presentation', () => {
  it('shows unavailable measured charts instead of a fabricated distribution', () => {
    const html = renderToStaticMarkup(<ObservedTrafficCharts samples={[]} expectedCount={5} />);
    expect(html).toContain('No fresh measured speeds');
    expect(html).toContain('0 of 5 representative road points');
    expect(html).not.toContain('analytics-chart-donut');
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
