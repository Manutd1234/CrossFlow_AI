/* @vitest-environment jsdom */
/* jsdom refuses localStorage on an opaque origin, which is what the default
   about:blank document has. Give the document a real origin so the session
   persistence under test behaves as it does in a browser. */
/* @vitest-environment-options { "url": "http://localhost:3000" } */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const SUPABASE_URL = 'https://wtednggryrikyvkuhqjo.supabase.co';
const PUBLISHABLE_KEY = 'sb_publishable_test';
const TEST_ADMIN_EMAIL = 'admin@test.local';
const TEST_ADMIN_PASSWORD = 'test-admin-password';

vi.stubEnv('VITE_SUPABASE_URL', SUPABASE_URL);
vi.stubEnv('VITE_SUPABASE_PUBLISHABLE_KEY', PUBLISHABLE_KEY);
vi.stubEnv('VITE_TEST_ADMIN_EMAIL', TEST_ADMIN_EMAIL);
vi.stubEnv('VITE_TEST_ADMIN_PASSWORD', TEST_ADMIN_PASSWORD);

const {
  AuthError, clearStoredSession, fetchAuthStatus, fetchSession, projectMismatch, readStoredSession, refreshSession,
  sessionGeneration, SessionChangedError, SessionRejectedError, signIn, signInAsTestAdmin, signOut, supabaseConfigured, testAdminConfigured,
  validSession,
} = await import('./auth');

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

const TOKEN_PAYLOAD = {
  access_token: 'access-token-abc',
  refresh_token: 'refresh-token-xyz',
  expires_in: 3600,
};

/**
 * Own the storage under test.
 *
 * Node 25 exposes an experimental native `localStorage` that shadows jsdom's
 * and is inert without `--localstorage-file`, so the ambient implementation
 * differs by Node version. An explicit in-memory stub keeps these assertions
 * about our code rather than about the runtime.
 */
function installMemoryStorage(): Storage {
  const entries = new Map<string, string>();
  const storage: Storage = {
    get length() { return entries.size; },
    clear: () => entries.clear(),
    getItem: (key: string) => entries.get(key) ?? null,
    key: (index: number) => Array.from(entries.keys())[index] ?? null,
    removeItem: (key: string) => { entries.delete(key); },
    setItem: (key: string, value: string) => { entries.set(key, String(value)); },
  };
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    value: storage,
  });
  return storage;
}

beforeEach(() => {
  installMemoryStorage();
});

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe('supabase sign-in', () => {
  it('is configured from the Vite environment', () => {
    expect(supabaseConfigured()).toBe(true);
  });

  it('sends the password to Supabase and never to the CrossFlow API', async () => {
    const fetchMock = vi.fn<FetchLike>(async () => jsonResponse(TOKEN_PAYLOAD));
    vi.stubGlobal('fetch', fetchMock);

    await signIn('driver@example.com', 'hunter2');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${SUPABASE_URL}/auth/v1/token?grant_type=password`);
    // The password must only ever appear in the Supabase request.
    expect(String(init.body)).toContain('hunter2');
    const calledHosts = fetchMock.mock.calls.map(call => String(call[0]));
    expect(calledHosts.every(target => target.startsWith(SUPABASE_URL))).toBe(true);
  });

  it('persists the session so a reload stays signed in', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(TOKEN_PAYLOAD)));
    await signIn('driver@example.com', 'hunter2');

    const stored = readStoredSession();
    expect(stored?.accessToken).toBe('access-token-abc');
    expect(stored?.refreshToken).toBe('refresh-token-xyz');
    expect(stored?.expiresAtMs).toBeGreaterThan(Date.now());
  });

  it('offers the configured test admin without sending its password to CrossFlow', async () => {
    const fetchMock = vi.fn<FetchLike>(async () => jsonResponse(TOKEN_PAYLOAD));
    vi.stubGlobal('fetch', fetchMock);

    expect(testAdminConfigured()).toBe(true);
    await signInAsTestAdmin();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${SUPABASE_URL}/auth/v1/token?grant_type=password`);
    expect(JSON.parse(String(init.body))).toEqual({
      email: TEST_ADMIN_EMAIL,
      password: TEST_ADMIN_PASSWORD,
    });
    expect(String(url)).not.toContain('/api/');
  });

  it('surfaces Supabase\'s own message instead of a generic failure', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(
      { error_description: 'Email not confirmed' }, 400,
    )));

    // A generic "sign-in failed" would hide the one thing the user must do.
    await expect(signIn('driver@example.com', 'hunter2'))
      .rejects.toThrow('Email not confirmed');
  });

  it('does not write a session when sign-in fails', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ msg: 'Invalid login credentials' }, 400)));
    await expect(signIn('driver@example.com', 'wrong')).rejects.toThrow(AuthError);
    expect(readStoredSession()).toBeNull();
  });

  it('ignores corrupt stored sessions rather than throwing', () => {
    window.localStorage.setItem('crossflow.session', '{not json');
    expect(readStoredSession()).toBeNull();

    window.localStorage.setItem('crossflow.session', JSON.stringify({ accessToken: '' }));
    expect(readStoredSession()).toBeNull();

    window.localStorage.setItem('crossflow.session', '{"accessToken":"a","expiresAtMs":1e309}');
    expect(readStoredSession()).toBeNull();
  });

  it('rejects a malformed session expiry instead of scheduling a refresh loop', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ ...TOKEN_PAYLOAD, expires_in: -1 })));
    await expect(signIn('driver@example.com', 'hunter2')).rejects.toThrow('invalid session expiry');
    expect(readStoredSession()).toBeNull();
  });
});

describe('saved session rejection versus temporary service failure', () => {
  const saved = { accessToken: 'saved', refreshToken: 'saved-refresh', expiresAtMs: 1 };

  it.each([
    [400, { error: 'invalid_grant', error_description: 'Refresh credential was revoked' }],
    [400, { code: 'refresh_token_not_found', msg: 'Refresh token missing' }],
    [400, { error_code: 'refresh_token_already_used', message: 'Refresh token already used' }],
    [400, { msg: 'Invalid Refresh Token: Refresh Token Not Found' }],
    [401, { message: 'Session is no longer valid' }],
  ])('classifies authoritative refresh rejection (%s) for the lifecycle to clear', async (status, payload) => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(payload, status as number)));
    await expect(refreshSession(saved)).rejects.toBeInstanceOf(SessionRejectedError);
  });

  it.each([400, 403, 429, 500, 503])('keeps a temporary/non-token refresh failure (%s) recoverable', async (status) => {
    window.localStorage.setItem('crossflow.session', JSON.stringify(saved));
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ message: 'Service temporarily unavailable' }, status)));
    const error = await refreshSession(saved).catch(error => error);
    expect(error).toBeInstanceOf(AuthError);
    expect(error).not.toBeInstanceOf(SessionRejectedError);
    expect(readStoredSession()).toEqual(saved);
  });

  it('preserves credentials after a network failure during backend identity resolution', async () => {
    window.localStorage.setItem('crossflow.session', JSON.stringify(saved));
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('connection reset')));
    const error = await fetchSession(saved).catch(error => error);
    expect(error).toBeInstanceOf(AuthError);
    expect(error).not.toBeInstanceOf(SessionRejectedError);
    expect(readStoredSession()).toEqual(saved);
  });

  it.each([403, 500, 503])('does not revoke a saved credential because the API returns %s', async (status) => {
    window.localStorage.setItem('crossflow.session', JSON.stringify(saved));
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'Role service is unavailable' }, status)));
    const error = await fetchSession(saved).catch(error => error);
    expect(error).toBeInstanceOf(AuthError);
    expect(error).not.toBeInstanceOf(SessionRejectedError);
    expect(readStoredSession()).toEqual(saved);
  });

  it('marks an authoritative API401 as a rejected saved session', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({}, 401)));
    await expect(fetchSession(saved)).rejects.toBeInstanceOf(SessionRejectedError);
  });
});

describe('session lifetime', () => {
  it('shares a concurrent refresh and cannot restore tokens after sign-out', async () => {
    let finish!: (response: Response) => void;
    const fetchMock = vi.fn<FetchLike>(() => new Promise(resolve => { finish = resolve; }));
    vi.stubGlobal('fetch', fetchMock);
    const old = {
      accessToken: 'old', refreshToken: 'r', expiresAtMs: Date.now(),
    };
    window.localStorage.setItem('crossflow.session', JSON.stringify(old));
    const first = refreshSession(old);
    const second = refreshSession(old);
    const firstRejected = expect(first).rejects.toBeInstanceOf(SessionChangedError);
    const secondRejected = expect(second).rejects.toBeInstanceOf(SessionChangedError);
    expect(fetchMock).toHaveBeenCalledOnce();
    clearStoredSession();
    finish(jsonResponse(TOKEN_PAYLOAD));
    await Promise.all([firstRejected, secondRejected]);
    expect(readStoredSession()).toBeNull();
  });

  it('does not replace another account when an old refresh completes', async () => {
    let finish!: (response: Response) => void;
    vi.stubGlobal('fetch', vi.fn(() => new Promise(resolve => { finish = resolve; })));
    const pending = refreshSession({ accessToken: 'old', refreshToken: 'old-r', expiresAtMs: 0 });
    const rejected = expect(pending).rejects.toBeInstanceOf(SessionChangedError);
    const other = { accessToken: 'other', refreshToken: 'other-r', expiresAtMs: 100 };
    window.localStorage.setItem('crossflow.session', JSON.stringify(other));
    finish(jsonResponse(TOKEN_PAYLOAD));
    await rejected;
    expect(readStoredSession()).toEqual(other);
  });

  it('does not restore a sign-in request that finishes after sign-out', async () => {
    let finish!: (response: Response) => void;
    vi.stubGlobal('fetch', vi.fn(() => new Promise(resolve => { finish = resolve; })));
    const pending = signIn('driver@example.com', 'hunter2');
    const rejected = expect(pending).rejects.toBeInstanceOf(SessionChangedError);
    await signOut(null);
    finish(jsonResponse(TOKEN_PAYLOAD));
    await rejected;
    expect(readStoredSession()).toBeNull();
  });

  it('keeps the account generation stable across a successful token refresh', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(TOKEN_PAYLOAD)));
    const generation = sessionGeneration();
    await refreshSession({ accessToken: 'a', refreshToken: 'r', expiresAtMs: 0 });
    expect(sessionGeneration()).toBe(generation);
  });

  it('bounds a stalled session check instead of leaving sign-in busy forever', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn((_url, init: RequestInit) => new Promise((_resolve, reject) => {
      init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    })));
    const check = expect(fetchSession({ accessToken: 'a', refreshToken: 'r', expiresAtMs: 1 }))
      .rejects.toThrow('Could not reach the sign-in service');
    await vi.advanceTimersByTimeAsync(15_000);
    await check;
    vi.useRealTimers();
  });

  it('also times out a session response whose headers arrived but body stalled', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn(async (_url, init: RequestInit) => ({
      ...jsonResponse({}),
      json: () => new Promise((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
      }),
    })));
    const check = expect(fetchSession({ accessToken: 'a', refreshToken: 'r', expiresAtMs: 1 }))
      .rejects.toThrow('Could not reach the sign-in service');
    await vi.advanceTimersByTimeAsync(15_000);
    await check;
    vi.useRealTimers();
  });

  it('also times out a Supabase response whose body stalled', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn(async (_url, init: RequestInit) => ({
      ...jsonResponse({}),
      json: () => new Promise((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
      }),
    })));
    const check = expect(signIn('driver@example.com', 'hunter2')).rejects.toThrow('did not respond in time');
    await vi.advanceTimersByTimeAsync(15_000);
    await check;
    vi.useRealTimers();
  });

  it('ignores an identity response that finishes after the account changed', async () => {
    let finish!: (response: Response) => void;
    vi.stubGlobal('fetch', vi.fn(() => new Promise(resolve => { finish = resolve; })));
    const pending = fetchSession({ accessToken: 'a', refreshToken: 'r', expiresAtMs: 1 });
    const rejected = expect(pending).rejects.toBeInstanceOf(SessionChangedError);
    clearStoredSession();
    finish(jsonResponse({ user_id: 'old', role: 'admin' }));
    await rejected;
  });

  it('returns unavailable when the auth-status service stalls', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn((_url, init: RequestInit) => new Promise((_resolve, reject) => {
      init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    })));
    const status = fetchAuthStatus();
    await vi.advanceTimersByTimeAsync(15_000);
    expect(await status).toBeNull();
    vi.useRealTimers();
  });

  it('refreshes a session that is close to expiry', async () => {
    const fetchMock = vi.fn<FetchLike>(async () => jsonResponse({
      ...TOKEN_PAYLOAD, access_token: 'refreshed-token',
    }));
    vi.stubGlobal('fetch', fetchMock);

    const nearlyExpired = {
      accessToken: 'old', refreshToken: 'refresh-token-xyz',
      expiresAtMs: Date.now() + 5_000,
    };
    const fresh = await validSession(nearlyExpired);

    expect(fresh.accessToken).toBe('refreshed-token');
    expect(String(fetchMock.mock.calls[0][0])).toContain('grant_type=refresh_token');
  });

  it('leaves a session that is still comfortably valid untouched', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    const healthy = {
      accessToken: 'still-good', refreshToken: 'r',
      expiresAtMs: Date.now() + 3_600_000,
    };
    expect((await validSession(healthy)).accessToken).toBe('still-good');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('refuses to refresh without a refresh token', async () => {
    await expect(refreshSession({
      accessToken: 'a', refreshToken: '', expiresAtMs: 0,
    })).rejects.toThrow('cannot be refreshed');
  });

  it('clears local credentials even when the server revoke fails', async () => {
    window.localStorage.setItem('crossflow.session', JSON.stringify({
      accessToken: 'a', refreshToken: 'r', expiresAtMs: Date.now() + 1000,
    }));
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('network down'); }));

    await signOut({ accessToken: 'a', refreshToken: 'r', expiresAtMs: Date.now() + 1000 });

    // Being unable to reach Supabase must not strand the user signed in.
    expect(readStoredSession()).toBeNull();
  });
});

describe('role resolution', () => {
  it('takes the role from the API, not from the token', async () => {
    const fetchMock = vi.fn<FetchLike>(async () => jsonResponse({
      user_id: 'user-1', role: 'DRIVER', display_name: 'Test Driver',
      expires_at: 1, role_source: 'crossflow_profiles',
    }));
    vi.stubGlobal('fetch', fetchMock);

    const identity = await fetchSession({
      accessToken: 'token', refreshToken: 'r', expiresAtMs: Date.now() + 1000,
    });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain('/api/auth/session');
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer token');
    expect(identity.role).toBe('DRIVER');
    // The badge is only trustworthy because the server resolved it from the
    // database with the caller's own token.
    expect(identity.role_source).toBe('crossflow_profiles');
  });

  it('reports an expired session clearly on 401', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'nope' }, 401)));
    await expect(fetchSession({
      accessToken: 'stale', refreshToken: 'r', expiresAtMs: Date.now() + 1000,
    })).rejects.toThrow('session has expired');
  });
});

describe('project mismatch guard', () => {
  const status = (origin: string | null) => ({
    mode: 'supabase', enabled: true, configured: true,
    project_origin: origin, sign_in: 'supabase_auth_direct', notes: '',
  });

  it('stays silent when both halves target the same project', () => {
    expect(projectMismatch(status(SUPABASE_URL))).toBeNull();
  });

  it('tolerates a trailing slash on the server origin', () => {
    expect(projectMismatch(status(`${SUPABASE_URL}/`))).toBeNull();
  });

  it('names both projects when they differ', () => {
    // The real symptom is a bare 401 after an apparently successful sign-in,
    // which points nowhere near the actual cause.
    const message = projectMismatch(status('https://otherprojectref000.supabase.co'));
    expect(message).toContain(SUPABASE_URL);
    expect(message).toContain('https://otherprojectref000.supabase.co');
    expect(message).toContain('VITE_SUPABASE_URL');
  });

  it('says nothing when the server has not reported an origin', () => {
    expect(projectMismatch(status(null))).toBeNull();
    expect(projectMismatch(null)).toBeNull();
  });
});
