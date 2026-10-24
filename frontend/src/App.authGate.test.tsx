/* @vitest-environment jsdom */

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AuthSession, StoredSession } from './types';

const authMocks = vi.hoisted(() => ({
  readStoredSession: vi.fn<() => StoredSession | null>(() => null),
  validSession: vi.fn(), fetchSession: vi.fn(),
  hasOAuthCallback: vi.fn(() => false), completeOAuthSignIn: vi.fn(),
  clearStoredSession: vi.fn(), sessionGeneration: vi.fn(() => 0),
  refreshHook: vi.fn(),
}));

vi.mock('./hooks/useSessionRefresh', () => ({ useSessionRefresh: authMocks.refreshHook }));
vi.mock('./services/auth', async (importOriginal) => ({
  ...await importOriginal<typeof import('./services/auth')>(),
  ...authMocks,
  supabaseConfigured: () => true,
  fetchAuthStatus: async () => ({ enabled: true, configured: true, mode: 'supabase' }),
}));
vi.mock('./services/api', async (importOriginal) => {
  const original = await importOriginal<typeof import('./services/api')>();
  const data = await import('./data/mockData');
  const fetched = <T,>(value: T) => ({ data: value, source: 'offline', fetchedAt: '2026-10-23T05:00:00Z' });
  return {
    ...original,
    fetchCorridors: async () => fetched(data.INITIAL_CORRIDORS),
    fetchFerries: async () => ({ ...fetched(data.INITIAL_FERRIES), timetable: data.PUBLISHED_FERRY_TIMETABLE_METADATA }),
    fetchOperationsSummary: async () => fetched(data.MOCK_OPERATIONS),
    fetchLiveTraffic: async () => fetched({ zones: [] }),
    fetchPortIntelligence: async () => fetched([]),
    fetchCorridorRoutes: async () => fetched([]),
    fetchRouteLocations: async () => fetched(data.ROUTE_LOCATIONS),
  };
});
vi.mock('./components/app-shell/Header', () => ({
  Header: ({ onOpenSignIn, identity, allowedTabs }: { onOpenSignIn: () => void; identity: AuthSession | null; allowedTabs?: string[] }) => (
    <header data-testid="workspace-header" data-role={identity?.role ?? 'guest'} data-allowed-tabs={allowedTabs?.join(',')}><button onClick={onOpenSignIn}>Open account</button></header>
  ),
}));
vi.mock('./components/corridor-map/MapView', () => ({ MapView: () => <div>Public congestion map</div> }));
vi.mock('./components/auth/SignInPanel', () => ({
  SignInPanel: ({ variant, identity, onContinueAsGuest, onClose, onIdentityChange, onSessionChange, initialError, onRetrySession }: {
    variant?: string; onContinueAsGuest?: () => void; onClose: () => void;
    identity: AuthSession | null; onIdentityChange: (identity: AuthSession | null) => void;
    onSessionChange: (session: StoredSession | null) => void;
    initialError?: string | null;
    onRetrySession?: () => void;
  }) => (
    <div role={variant === 'page' ? 'region' : 'dialog'} aria-label="Test sign-in">
      {initialError ? <p role="alert">{initialError}</p> : null}
      {onRetrySession ? <button onClick={onRetrySession}>Retry saved session</button> : null}
      <button onClick={variant === 'page' ? onContinueAsGuest : onClose}>
        {variant === 'page' ? 'Continue as Driver' : 'Close account'}
      </button>
      <button onClick={() => {
        onSessionChange(identity ? null : { accessToken: 'memory-access', refreshToken: 'memory-refresh', expiresAtMs: Date.now() + 3600_000 });
        onIdentityChange(identity ? null : {
          user_id: 'admin', role: 'admin', display_name: 'Admin', expires_at: null,
          role_source: 'crossflow_profiles',
        });
      }}>{identity ? 'Test sign out' : 'Test sign in'}</button>
    </div>
  ),
}));

import { App } from './App';
import { AuthError, SessionRejectedError } from './services/auth';
let root: Root | undefined;

beforeEach(() => {
  vi.clearAllMocks();
  authMocks.readStoredSession.mockReset().mockReturnValue(null);
  authMocks.validSession.mockReset();
  authMocks.fetchSession.mockReset();
  authMocks.hasOAuthCallback.mockReturnValue(false);
  authMocks.sessionGeneration.mockReturnValue(0);
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  root = undefined;
  window.sessionStorage.clear();
  document.body.replaceChildren();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('authentication gate isolation', () => {
  it('recovers the latest in-memory credential when browser storage is blocked and verifies the role again', async () => {
    const actualAuth = await vi.importActual<typeof import('./services/auth')>('./services/auth');
    vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => { throw new DOMException('Storage blocked', 'SecurityError'); });
    authMocks.readStoredSession.mockImplementation(actualAuth.readStoredSession);
    const rotated = { accessToken: 'rotated-access', refreshToken: 'rotated-refresh', expiresAtMs: Date.now() + 3600_000 };
    authMocks.validSession.mockResolvedValue(rotated);
    let resolveRole!: (identity: AuthSession) => void;
    authMocks.fetchSession.mockReturnValueOnce(new Promise(resolve => { resolveRole = resolve; }));
    const container = document.createElement('div'); document.body.append(container); root = createRoot(container);
    await act(async () => root!.render(<App />));
    await act(async () => [...container.querySelectorAll('button')].find(button => button.textContent === 'Test sign in')!.click());
    const [, updateSession, updateIdentity, recover] = authMocks.refreshHook.mock.calls[authMocks.refreshHook.mock.calls.length - 1];
    await act(async () => { updateSession(rotated); updateIdentity(null); recover(new AuthError('Backend restarting')); });
    expect(authMocks.validSession).toHaveBeenCalledWith(rotated);
    expect(authMocks.fetchSession).toHaveBeenCalledWith(rotated);
    expect(authMocks.clearStoredSession).not.toHaveBeenCalled();
    expect(container.querySelector('[data-testid="workspace-header"]')).toBeNull();
    await act(async () => resolveRole({ user_id: 'admin', role: 'driver', display_name: 'Driver',
      expires_at: null, role_source: 'crossflow_profiles' }));
    expect(container.querySelector('[data-testid="workspace-header"]')?.getAttribute('data-role')).toBe('driver');
    expect(container.querySelector('.workspace-access')).toBeNull();
  });

  it('does not retry an in-memory credential invalidated by a newer account operation', async () => {
    const container = document.createElement('div'); document.body.append(container); root = createRoot(container);
    await act(async () => root!.render(<App />));
    await act(async () => [...container.querySelectorAll('button')].find(button => button.textContent === 'Test sign in')!.click());
    const [, , updateIdentity, recover] = authMocks.refreshHook.mock.calls[authMocks.refreshHook.mock.calls.length - 1];
    authMocks.sessionGeneration.mockReturnValue(1);
    await act(async () => { updateIdentity(null); recover(new AuthError('Backend restarting')); });
    expect(authMocks.validSession).not.toHaveBeenCalled();
    expect(authMocks.fetchSession).not.toHaveBeenCalled();
    expect(container.querySelector('[data-testid="workspace-header"]')).toBeNull();
    expect(container.querySelector('[aria-label="Test sign-in"]')).not.toBeNull();
    expect(container.textContent).not.toContain('Retry saved session');
  });

  it('cannot accept an older in-memory recovery after another account has replaced it', async () => {
    let resolveOld!: (identity: AuthSession) => void;
    authMocks.validSession.mockImplementation(async (value: StoredSession) => value);
    authMocks.fetchSession.mockReturnValueOnce(new Promise(resolve => { resolveOld = resolve; }));
    const container = document.createElement('div'); document.body.append(container); root = createRoot(container);
    await act(async () => root!.render(<App />));
    await act(async () => [...container.querySelectorAll('button')].find(button => button.textContent === 'Test sign in')!.click());
    const [, updateSession, updateIdentity, recover] = authMocks.refreshHook.mock.calls[authMocks.refreshHook.mock.calls.length - 1];
    await act(async () => { updateIdentity(null); recover(new AuthError('Backend restarting')); });
    authMocks.sessionGeneration.mockReturnValue(1);
    const replacement = { accessToken: 'new-account', refreshToken: 'new-refresh', expiresAtMs: Date.now() + 3600_000 };
    await act(async () => {
      updateSession(replacement);
      updateIdentity({ user_id: 'new-driver', role: 'driver', display_name: 'New Driver', expires_at: null, role_source: 'crossflow_profiles' });
      resolveOld({ user_id: 'admin', role: 'admin', display_name: 'Old Admin', expires_at: null, role_source: 'crossflow_profiles' });
    });
    expect(container.querySelector('[data-testid="workspace-header"]')?.getAttribute('data-role')).toBe('driver');
    expect(container.textContent).not.toContain('Old Admin');
    expect(authMocks.clearStoredSession).not.toHaveBeenCalled();
  });

  it('preserves credentials through a transient backend failure and restores only a verified role on retry', async () => {
    vi.useFakeTimers();
    const stored = { accessToken: 'stored', refreshToken: 'refresh', expiresAtMs: Date.now() + 3600_000 };
    authMocks.readStoredSession.mockReturnValue(stored);
    authMocks.validSession.mockResolvedValue(stored);
    authMocks.fetchSession.mockRejectedValueOnce(new AuthError('Backend unavailable')).mockResolvedValueOnce({
      user_id: 'driver', role: 'driver', display_name: 'Driver', expires_at: null, role_source: 'crossflow_profiles',
    });
    const container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
    await act(async () => root!.render(<App />));
    expect(container.textContent).toContain('Restoring saved sign-in');
    expect(container.querySelector('[data-testid="workspace-header"]')).toBeNull();
    expect(authMocks.clearStoredSession).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(container.querySelector('[data-testid="workspace-header"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="workspace-header"]')?.getAttribute('data-role')).toBe('driver');
    expect(authMocks.clearStoredSession).not.toHaveBeenCalled();
  });

  it('keeps an exhausted restoration gated and offers a manual retry without erasing the saved sign-in', async () => {
    vi.useFakeTimers();
    const stored = { accessToken: 'stored', refreshToken: 'refresh', expiresAtMs: Date.now() + 3600_000 };
    authMocks.readStoredSession.mockReturnValue(stored);
    authMocks.validSession.mockResolvedValue(stored);
    authMocks.fetchSession.mockRejectedValue(new AuthError('Backend unavailable'));
    const container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
    await act(async () => root!.render(<App />));
    await act(async () => vi.advanceTimersByTimeAsync(4000));
    expect(authMocks.fetchSession).toHaveBeenCalledTimes(3);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('saved sign-in is preserved');
    expect(container.querySelector('[data-testid="workspace-header"]')).toBeNull();
    expect(authMocks.clearStoredSession).not.toHaveBeenCalled();
    authMocks.fetchSession.mockResolvedValue({ user_id: 'driver', role: 'driver', display_name: 'Driver',
      expires_at: null, role_source: 'crossflow_profiles' });
    await act(async () => Array.from(container.querySelectorAll('button'))
      .find(button => button.textContent === 'Retry saved session')!.click());
    expect(container.querySelector('[data-testid="workspace-header"]')).not.toBeNull();
    expect(authMocks.clearStoredSession).not.toHaveBeenCalled();
  });

  it('erases an authoritatively rejected session instead of retrying it', async () => {
    const stored = { accessToken: 'rejected', refreshToken: 'refresh', expiresAtMs: Date.now() + 3600_000 };
    authMocks.readStoredSession.mockReturnValue(stored);
    authMocks.validSession.mockResolvedValue(stored);
    authMocks.fetchSession.mockRejectedValue(new SessionRejectedError('Your session has expired. Please sign in again.'));
    const container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
    await act(async () => root!.render(<App />));
    expect(authMocks.clearStoredSession).toHaveBeenCalledOnce();
    expect(authMocks.fetchSession).toHaveBeenCalledOnce();
    expect(container.querySelector('[data-testid="workspace-header"]')).toBeNull();
    expect(container.textContent).not.toContain('Retry saved session');
  });

  it('cancels restoration backoff on unmount without discarding credentials', async () => {
    vi.useFakeTimers();
    const stored = { accessToken: 'stored', refreshToken: 'refresh', expiresAtMs: Date.now() + 3600_000 };
    authMocks.readStoredSession.mockReturnValue(stored);
    authMocks.validSession.mockResolvedValue(stored);
    authMocks.fetchSession.mockRejectedValue(new AuthError('Backend unavailable'));
    const container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
    await act(async () => root!.render(<App />));
    act(() => root!.unmount()); root = undefined;
    await act(async () => vi.advanceTimersByTimeAsync(4000));
    expect(authMocks.fetchSession).toHaveBeenCalledOnce();
    expect(authMocks.clearStoredSession).not.toHaveBeenCalled();
  });

  it('completes a Google callback before resolving the account role through the API', async () => {
    const session = { accessToken: 'google-access', refreshToken: 'google-refresh', expiresAtMs: Date.now() + 3600_000 };
    authMocks.hasOAuthCallback.mockReturnValue(true);
    authMocks.completeOAuthSignIn.mockResolvedValueOnce(session);
    authMocks.validSession.mockResolvedValueOnce(session);
    authMocks.fetchSession.mockResolvedValueOnce({ user_id: 'google-user', role: 'driver',
      display_name: 'Gmail User', expires_at: null, role_source: 'crossflow_profiles' });
    const container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
    await act(async () => { root!.render(<App />); });
    expect(authMocks.completeOAuthSignIn).toHaveBeenCalledOnce();
    expect(authMocks.fetchSession).toHaveBeenCalledWith(session);
    expect(container.querySelector('[data-testid="workspace-header"]')).not.toBeNull();
    expect(container.querySelector('[role="region"]')).toBeNull();
  });

  it('shows actionable OAuth callback failures at the gate even after a guest visit', async () => {
    window.sessionStorage.setItem('crossflow.guest', '1');
    authMocks.hasOAuthCallback.mockReturnValue(true);
    authMocks.completeOAuthSignIn.mockRejectedValueOnce(new AuthError('Google sign-in was cancelled. Please try again.'));
    const container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
    await act(async () => { root!.render(<App />); });
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Google sign-in was cancelled. Please try again.');
    expect(container.querySelector('[data-testid="workspace-header"]')).toBeNull();
    expect(window.sessionStorage.getItem('crossflow.guest')).toBeNull();
  });

  it('keeps the workspace hidden while a remembered account is being resolved', async () => {
    let finish!: (identity: AuthSession) => void;
    const session = { accessToken: 'stored', refreshToken: 'refresh', expiresAtMs: Date.now() + 3600_000 };
    authMocks.readStoredSession.mockReturnValue(session);
    authMocks.validSession.mockResolvedValue(session);
    authMocks.fetchSession.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
    await act(async () => { root!.render(<App />); });
    expect(container.textContent).toContain('Loading workspace');
    expect(container.querySelector('[data-testid="workspace-header"]')).toBeNull();
    expect(container.querySelector('#main-content')).toBeNull();
    await act(async () => finish({ user_id: 'admin', role: 'admin', display_name: 'Admin',
      expires_at: null, role_source: 'crossflow_profiles' }));
    expect(container.querySelector('[data-testid="workspace-header"]')).not.toBeNull();
  });

  it('can continue as a driver after signing out without reopening the old account modal', async () => {
    const container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
    await act(async () => { root!.render(<App />); });
    const click = async (label: string) => act(async () => Array.from(container.querySelectorAll('button'))
      .find(button => button.textContent === label)!.click());
    await click('Test sign in');
    await click('Open account');
    await click('Test sign out');
    expect(container.querySelector('[role="region"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="workspace-header"]')).toBeNull();
    await click('Continue as Driver');
    expect(container.querySelector('[data-testid="workspace-header"]')).not.toBeNull();
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    expect(container.querySelector('[inert]')).toBeNull();
  });

  it('renders the gate exclusively and makes the workspace inert behind the account modal', async () => {
    const container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
    await act(async () => { root!.render(<App />); });
    expect(container.querySelector('[aria-label="Test sign-in"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="workspace-header"]')).toBeNull();
    expect(container.querySelector('#main-content')).toBeNull();

    await act(async () => container.querySelector<HTMLButtonElement>('button')!.click());
    expect(container.querySelector('[data-testid="workspace-header"]')).not.toBeNull();
    expect(container.textContent).toContain('Public congestion map');
    expect(container.querySelector('.workspace-access')).toBeNull();
    expect(container.querySelector('[data-testid="workspace-header"]')?.getAttribute('data-allowed-tabs')).toBe('map,route');

    await act(async () => Array.from(container.querySelectorAll('button'))
      .find(button => button.textContent?.trim() === 'Open account')!.click());
    expect(container.querySelector('[role="dialog"]')).not.toBeNull();
    const workspace = container.querySelector('[data-testid="workspace-header"]')!.parentElement!;
    expect(workspace.hasAttribute('inert')).toBe(true);
    expect(workspace.getAttribute('aria-hidden')).toBe('true');
    await act(async () => Array.from(container.querySelectorAll('button'))
      .find(button => button.textContent === 'Close account')!.click());
    expect(workspace.hasAttribute('inert')).toBe(false);
    expect(workspace.hasAttribute('aria-hidden')).toBe(false);
  });
});
