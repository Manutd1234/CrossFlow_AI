/* @vitest-environment jsdom */
import { act, lazy, Suspense, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { WorkspaceErrorBoundary } from './WorkspaceErrorBoundary';

let root: Root | undefined;

function render(element: ReactElement): HTMLElement {
  const view = document.createElement('div');
  document.body.append(view);
  root = createRoot(view);
  act(() => root?.render(<WorkspaceErrorBoundary>{element}</WorkspaceErrorBoundary>));
  return view;
}

function BrokenWorkspace(): never {
  throw new Error('Internal render exception with implementation details');
}

function preventExpectedError(event: ErrorEvent): void {
  event.preventDefault();
}

beforeEach(() => {
  // React reports caught render failures to the developer console in tests.
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
  window.addEventListener('error', preventExpectedError);
});

afterEach(() => {
  act(() => root?.unmount());
  root = undefined;
  window.removeEventListener('error', preventExpectedError);
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.replaceChildren();
});

describe('workspace load recovery', () => {
  it('renders a healthy workspace without replacing its content or draft', () => {
    const view = render(<section><h1>Route planner</h1><input defaultValue="Unsaved route draft" /></section>);
    expect(view.textContent).toContain('Route planner');
    expect(view.querySelector('input')?.value).toBe('Unsaved route draft');
    expect(view.querySelector('[role="alert"]')).toBeNull();
  });

  it('shows a concise recovery state after a workspace render fails', () => {
    const view = render(<BrokenWorkspace />);
    const alert = view.querySelector('[role="alert"]');
    expect(alert?.textContent).toContain('Unable to load the workspace');
    expect(alert?.textContent).toContain('Check your connection');
    expect(alert?.textContent).toContain('latest version of CrossFlow');
    expect(alert?.textContent).not.toMatch(/Internal render exception|implementation details|stack/i);
    expect(view.querySelector('button')?.textContent).toBe('Reload workspace');
  });

  it('recovers from a rejected lazy workspace chunk instead of leaving a blank application', async () => {
    let rejectChunk!: (reason: Error) => void;
    const LazyWorkspace = lazy(() => new Promise<{ default: () => ReactElement }>((_resolve, reject) => {
      rejectChunk = reject;
    }));
    const view = render(<Suspense fallback={<p>Loading workspace</p>}><LazyWorkspace /></Suspense>);
    expect(view.textContent).toBe('Loading workspace');

    await act(async () => rejectChunk(new Error('Failed to fetch dynamically imported module /assets/retired-chunk.js')));

    expect(view.querySelector('[role="alert"]')?.textContent).toContain('Unable to load the workspace');
    expect(view.textContent).not.toMatch(/assets|retired-chunk|dynamically imported|Error:/);
    expect(view.querySelector('button')).not.toBeNull();
  });

  it('reloads only after the user explicitly chooses to reload', async () => {
    const reload = vi.fn();
    vi.stubGlobal('location', { reload });
    const view = render(<BrokenWorkspace />);
    await act(async () => {
      window.dispatchEvent(new Event('focus'));
      await Promise.resolve();
    });
    expect(reload).not.toHaveBeenCalled();

    act(() => view.querySelector<HTMLButtonElement>('button')?.click());

    expect(reload).toHaveBeenCalledOnce();
  });
});
