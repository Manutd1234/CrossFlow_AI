/* @vitest-environment jsdom */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fetchHistoricalResearch } from '../../services/api';
import { batamBenchmarkFixture, researchFixture, saturationFixture } from '../../test/fixtures/historicalResearch';
import { PublishedTrafficHistory } from './PublishedTrafficHistory';

vi.mock('../../services/api', () => ({ fetchHistoricalResearch: vi.fn() }));
const fetchEvidence = vi.mocked(fetchHistoricalResearch);
let root: Root | undefined;
async function render() {
  const container = document.createElement('div'); document.body.append(container); root = createRoot(container);
  await act(async () => root?.render(<PublishedTrafficHistory />));
  return container;
}
beforeEach(() => { fetchEvidence.mockReset(); fetchEvidence.mockResolvedValue(researchFixture()); });
afterEach(() => { act(() => root?.unmount()); root = undefined; document.body.replaceChildren(); });

describe('published historical traffic evidence', () => {
  it('shows one dataset at a time and retains the survey filter when changing subtabs', async () => {
    fetchEvidence.mockResolvedValue({ ...researchFixture(), saturation_survey: saturationFixture() });
    const view = await render();
    const visible = () => view.querySelector('[role="tabpanel"]:not([hidden])')!;
    expect(view.querySelectorAll('[role="tabpanel"]:not([hidden])')).toHaveLength(1);
    const date = visible().querySelector('select')!;
    await act(async () => { date.value = '2023-02-17'; date.dispatchEvent(new Event('change', { bubbles: true })); });
    const select = async (name: string) => {
      const tab = [...view.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find(item => item.textContent === name)!;
      await act(async () => tab.click());
    };
    await select('Intersection study');
    expect(visible().textContent).toContain('Published intersection saturation');
    expect(visible().textContent).not.toContain('Published road-speed survey');
    await select('Singapore benchmark');
    expect(visible().textContent).toContain('speed-band units');
    await select('Batam speed survey');
    expect(visible().querySelector('select')?.value).toBe('2023-02-17');
    expect(visible().querySelector('tbody')?.textContent).toContain('31.00');
    expect(fetchEvidence).toHaveBeenCalledOnce();
  });
  it('shows the Batam site diagnostic with explicit horizon and promotion limits', async () => {
    fetchEvidence.mockResolvedValue({ ...researchFixture(), batam_benchmark: batamBenchmarkFixture() });
    const view = await render();
    const result = view.querySelector('[aria-label="Exploratory Batam site backtest"]');
    expect(result?.textContent).toContain('0 synthetic training rows');
    expect(result?.textContent).toContain('not 15/30/60-minute network accuracy');
    expect(result?.querySelector('tbody')?.rows).toHaveLength(3);
    expect(result?.textContent).toContain('Historical time-slot mean');
  });
  it('keeps dated saturation ratios separate from speed labels when filtering the second Batam study', async () => {
    fetchEvidence.mockResolvedValue({ ...researchFixture(), saturation_survey: saturationFixture() });
    const view = await render();
    const survey = view.querySelectorAll('article')[1];
    expect(survey.querySelector('tbody')?.textContent).toContain('0.900');
    const date = survey.querySelector('select')!;
    await act(async () => { date.value = '2023-06-13'; date.dispatchEvent(new Event('change', { bubbles: true })); });
    expect(survey.querySelector('tbody')?.textContent).toContain('1.200');
    expect(survey.textContent).toContain('not vehicle speeds');
    expect(survey.textContent).toContain('0 speed-training labels');
  });
  it('filters the actual returned survey rows by date and keeps original units and scope', async () => {
    const view = await render();
    const survey = view.querySelector('article')!;
    expect(survey.querySelector('tbody')?.textContent).toContain('21.00');
    const date = survey.querySelector('select')!;
    await act(async () => { date.value = '2023-02-17'; date.dispatchEvent(new Event('change', { bubbles: true })); });
    expect(survey.querySelector('tbody')?.textContent).toContain('31.00');
    expect(survey.querySelector('tbody')?.textContent).not.toContain('21.00');
    expect(survey.textContent).toContain('cross-check differs');
    expect(view.textContent).toContain('has not replaced the Batam serving model');
    expect(view.textContent).toContain('speed-band units');
    expect(view.querySelectorAll('tbody')[1]?.rows).toHaveLength(3);
  });
  it('shows an actionable unavailable state and recovers without invented observations', async () => {
    fetchEvidence.mockRejectedValueOnce(new Error('Source checksum mismatch'));
    const view = await render();
    expect(view.querySelector('table')).toBeNull();
    expect(view.querySelector('[role="alert"]')?.textContent).toContain('Source checksum mismatch');
    await act(async () => view.querySelector('button')?.click());
    expect(view.querySelectorAll('table')).toHaveLength(2);
  });
  it('retains previously checked historical evidence when a later check fails', async () => {
    const view = await render();
    fetchEvidence.mockRejectedValueOnce(new Error('Service unavailable'));
    await act(async () => view.querySelector('button')?.click());
    expect(view.querySelectorAll('table')).toHaveLength(2);
    expect(view.querySelector('[role="alert"]')?.textContent).toContain('current check failed');
  });
});
