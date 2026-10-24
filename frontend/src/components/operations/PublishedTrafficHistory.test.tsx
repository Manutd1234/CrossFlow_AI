/* @vitest-environment jsdom */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchHistoricalResearch } from '../../services/api';
import { PublishedTrafficHistory } from './PublishedTrafficHistory';

vi.mock('../../services/api', () => ({ fetchHistoricalResearch: vi.fn() }));
let root: Root | undefined;
function render(content?: React.ReactNode) {
  const container = document.createElement('div'); document.body.append(container); root = createRoot(container);
  act(() => root?.render(<PublishedTrafficHistory modelledContent={content} />));
  return container;
}
afterEach(() => { act(() => root?.unmount()); root = undefined; document.body.replaceChildren(); vi.clearAllMocks(); });

describe('single traffic-flow analysis workspace', () => {
  it('shows one modelled analysis panel without research subtabs or research requests', () => {
    const view = render(<p>Possible corridor traffic patterns</p>);
    expect(view.querySelector('section')?.getAttribute('aria-label')).toBe('Modelled traffic-flow analysis');
    expect(view.textContent).toContain('Modelled traffic-flow analysis');
    expect(view.textContent).toContain('separate from live traffic and measured history');
    expect(view.textContent).toContain('Possible corridor traffic patterns');
    expect(view.querySelector('[role="tablist"]')).toBeNull();
    expect(view.textContent).not.toMatch(/Observed traffic|Batam speed survey|Intersection study|Singapore benchmark/);
    expect(fetchHistoricalResearch).not.toHaveBeenCalled();
  });

  it('shows a concise unavailable state when no analysis content is provided', () => {
    const view = render();
    expect(view.querySelector('[role="status"]')?.textContent).toContain('Traffic-flow analysis is unavailable');
    expect(view.querySelector('table')).toBeNull();
    expect(fetchHistoricalResearch).not.toHaveBeenCalled();
  });
});
