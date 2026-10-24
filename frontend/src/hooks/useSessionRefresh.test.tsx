/* @vitest-environment jsdom */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AuthSession, StoredSession } from '../types';

const auth = vi.hoisted(() => ({
  clearStoredSession: vi.fn(), fetchSession: vi.fn(), validSession: vi.fn(),
  sessionGeneration: vi.fn(), SessionChangedError: class SessionChangedError extends Error {},
  SessionRejectedError: class SessionRejectedError extends Error {},
}));
vi.mock('../services/auth', () => auth);
import { useSessionRefresh } from './useSessionRefresh';

let root: Root;
const setSession = vi.fn();
const setIdentity = vi.fn();
const recover = vi.fn();
const identity: AuthSession = {
  user_id: 'one', role: 'driver', display_name: 'Driver',
  role_source: 'crossflow_profiles', expires_at: null,
};

function Harness({ session }: { session: StoredSession | null }) {
  useSessionRefresh(session, setSession, setIdentity, recover);
  return null;
}

function render(session: StoredSession | null) {
  act(() => root.render(<Harness session={session} />));
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  const container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
  auth.fetchSession.mockResolvedValue(identity);
  auth.sessionGeneration.mockReturnValue(1);
});
afterEach(() => {
  act(() => root.unmount());
  document.body.replaceChildren();
  vi.useRealTimers();
});

describe('open workspace session lifetime', () => {
  it('refreshes before expiry and updates the server-resolved identity', async () => {
    const old = { accessToken: 'old', refreshToken: 'refresh', expiresAtMs: Date.now() + 120_000 };
    const fresh = { ...old, accessToken: 'fresh', expiresAtMs: Date.now() + 3_600_000 };
    auth.validSession.mockResolvedValue(fresh);
    render(old);
    await act(async () => vi.advanceTimersByTimeAsync(59_999));
    expect(auth.validSession).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(auth.fetchSession).toHaveBeenCalledWith(fresh);
    expect(setSession).toHaveBeenCalledWith(fresh);
    expect(setIdentity).toHaveBeenCalledWith(identity);
  });

  it('clears rejected credentials so reload does not restore them', async () => {
    auth.validSession.mockRejectedValue(new auth.SessionRejectedError('Refresh token revoked'));
    render({ accessToken: 'old', refreshToken: 'refresh', expiresAtMs: Date.now() });
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(auth.clearStoredSession).toHaveBeenCalledOnce();
    expect(setSession).toHaveBeenCalledWith(null);
    expect(setIdentity).toHaveBeenCalledWith(null);
    expect(recover).not.toHaveBeenCalled();
  });

  it('preserves saved credentials but withdraws the role while a backend outage recovers', async () => {
    const old = { accessToken: 'old', refreshToken: 'refresh', expiresAtMs: Date.now() };
    const fresh = { ...old, accessToken: 'fresh', refreshToken: 'rotated', expiresAtMs: Date.now() + 3_600_000 };
    auth.validSession.mockResolvedValue(fresh);
    const outage = new Error('API is restarting');
    auth.fetchSession.mockRejectedValue(outage);
    render(old);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(auth.clearStoredSession).not.toHaveBeenCalled();
    expect(setSession).toHaveBeenCalledWith(fresh);
    expect(setSession).not.toHaveBeenCalledWith(null);
    expect(setIdentity).toHaveBeenCalledWith(null);
    expect(recover).toHaveBeenCalledWith(outage);
  });

  it('preserves the existing pair when the token refresh itself has a network failure', async () => {
    const old = { accessToken: 'old', refreshToken: 'refresh', expiresAtMs: Date.now() };
    auth.validSession.mockRejectedValue(new Error('network unavailable'));
    render(old);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(auth.clearStoredSession).not.toHaveBeenCalled();
    expect(setSession).toHaveBeenCalledWith(old);
    expect(setIdentity).toHaveBeenCalledWith(null);
    expect(recover).toHaveBeenCalledOnce();
  });

  it('ignores a refresh that completes after signing out', async () => {
    let finish!: (session: StoredSession) => void;
    auth.validSession.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const old = { accessToken: 'old', refreshToken: 'refresh', expiresAtMs: Date.now() };
    render(old);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    render(null);
    await act(async () => finish({ ...old, accessToken: 'late' }));
    expect(setSession).not.toHaveBeenCalled();
    expect(auth.fetchSession).not.toHaveBeenCalled();
  });

  it('ignores a refresh after sign-out begins before React rerenders', async () => {
    let finish!: (session: StoredSession) => void;
    auth.validSession.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const old = { accessToken: 'old', refreshToken: 'refresh', expiresAtMs: Date.now() };
    render(old);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    auth.sessionGeneration.mockReturnValue(2);
    await act(async () => finish({ ...old, accessToken: 'late' }));
    expect(setSession).not.toHaveBeenCalled();
    expect(auth.fetchSession).not.toHaveBeenCalled();
  });

  it('cannot clear a newer account when an old refresh fails', async () => {
    let fail!: (error: Error) => void;
    auth.validSession.mockImplementation(() => new Promise((_resolve, reject) => { fail = reject; }));
    render({ accessToken: 'old', refreshToken: 'refresh', expiresAtMs: Date.now() });
    await act(async () => vi.advanceTimersByTimeAsync(1));
    auth.sessionGeneration.mockReturnValue(2);
    await act(async () => fail(new Error('old refresh rejected')));
    expect(auth.clearStoredSession).not.toHaveBeenCalled();
    expect(setSession).not.toHaveBeenCalled();
  });
});
