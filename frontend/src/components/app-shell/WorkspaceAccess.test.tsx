/* @vitest-environment jsdom */

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AuthSession } from '../../types';
import { WorkspaceAccess } from './WorkspaceAccess';

let root: Root | undefined;
const identity = (role: string): AuthSession => ({ user_id: 'user', role, display_name: 'Sample User',
  expires_at: null, role_source: 'crossflow_profiles' });

function render(account: AuthSession | null, driverAccess: boolean, signInAvailable = true) {
  const onOpenAccount = vi.fn();
  const container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
  act(() => root!.render(<WorkspaceAccess identity={account} driverAccess={driverAccess}
    signInAvailable={signInAvailable} onOpenAccount={onOpenAccount} />));
  return { container, onOpenAccount };
}

afterEach(() => {
  if (root) act(() => root!.unmount());
  root = undefined;
  document.body.replaceChildren();
});

describe('workspace access discoverability', () => {
  it('explains the guest driver workspace and opens the existing operator sign-in', () => {
    const { container, onOpenAccount } = render(null, true);
    expect(container.textContent).toContain('Driver workspace');
    expect(container.textContent).toContain('Guest session');
    expect(container.textContent).toContain('You continued as a driver');
    expect(container.textContent).toContain('authorized operator account');
    const button = container.querySelector('button')!;
    expect(button.textContent).toContain('Operator sign-in');
    expect(button.getAttribute('aria-haspopup')).toBe('dialog');
    act(() => button.click());
    expect(onOpenAccount).toHaveBeenCalledOnce();
  });

  it('shows a signed-in driver role without offering a privilege switch', () => {
    const { container, onOpenAccount } = render(identity('driver'), true);
    expect(container.textContent).toContain('Account role: driver');
    expect(container.textContent).toContain('An administrator can grant operator access');
    expect(container.textContent).not.toContain('Guest session');
    const buttons = Array.from(container.querySelectorAll('button'));
    expect(buttons).toHaveLength(1);
    expect(buttons[0].textContent).toContain('Account details');
    act(() => buttons[0].click());
    expect(onOpenAccount).toHaveBeenCalledOnce();
  });

  it('locates ferry and port tools inside Analytics for the full workspace', () => {
    const { container } = render(identity('admin'), false);
    expect(container.textContent).toContain('Full workspace');
    expect(container.textContent).toContain('Account role: admin');
    expect(container.textContent).toContain('Analytics → Ports');
    expect(container.textContent).not.toContain('An administrator can grant operator access');
  });

  it('explains unavailable operator sign-in without adding an unusable action', () => {
    const { container } = render(null, true, false);
    expect(container.textContent).toContain('Operator sign-in unavailable');
    expect(container.querySelector('button')).toBeNull();
  });

  it('keeps account details reachable for an existing session when sign-in is unavailable', () => {
    const { container, onOpenAccount } = render(identity('driver'), true, false);
    act(() => container.querySelector('button')!.click());
    expect(onOpenAccount).toHaveBeenCalledOnce();
    expect(container.textContent).not.toContain('Operator sign-in unavailable');
  });
});
