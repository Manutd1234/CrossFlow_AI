/* @vitest-environment jsdom */
/* @vitest-environment-options { "url": "http://localhost:3000" } */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const SUPABASE_URL = 'https://wtednggryrikyvkuhqjo.supabase.co';
const PUBLISHABLE_KEY = 'sb_publishable_email_otp_fixture';
vi.stubEnv('VITE_SUPABASE_URL', SUPABASE_URL);
vi.stubEnv('VITE_SUPABASE_PUBLISHABLE_KEY', PUBLISHABLE_KEY);
const auth = await import('./auth');

const EMAIL = 'driver@example.test';
const CONFIRMED = {
  id: '11111111-1111-4111-8111-111111111111',
  email: EMAIL,
  email_confirmed_at: '2026-10-23T10:00:00Z',
};
const VERIFIED = {
  access_token: 'email-verified-access',
  refresh_token: 'email-verified-refresh',
  expires_in: 3600,
  user: CONFIRMED,
};

function response(payload: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => payload } as Response;
}

function memoryStorage(): Storage {
  const entries = new Map<string, string>();
  return {
    get length() { return entries.size; },
    clear: () => entries.clear(),
    getItem: key => entries.get(key) ?? null,
    setItem: (key, value) => { entries.set(key, value); },
    removeItem: key => { entries.delete(key); },
    key: index => Array.from(entries.keys())[index] ?? null,
  };
}

function deferredResponse() {
  let resolve!: (value: Response) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<Response>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

beforeEach(() => {
  Object.defineProperty(window, 'localStorage', { configurable: true, value: memoryStorage() });
  Object.defineProperty(window, 'sessionStorage', { configurable: true, value: memoryStorage() });
});

afterEach(() => {
  auth.clearStoredSession();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('requesting a passwordless email sign-in code', () => {
  it('permits new accounts, sends only Supabase an optional name, and advances generation synchronously', async () => {
    const pending = deferredResponse();
    const fetchMock = vi.fn(() => pending.promise);
    vi.stubGlobal('fetch', fetchMock);
    const generation = auth.sessionGeneration();
    const request = auth.requestEmailSignInOtp(` ${EMAIL} `, '  Test Driver  ');
    expect(auth.sessionGeneration()).toBe(generation + 1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${SUPABASE_URL}/auth/v1/otp`);
    expect(init.method).toBe('POST');
    expect(init.headers).toMatchObject({ apikey: PUBLISHABLE_KEY });
    expect(JSON.parse(String(init.body))).toEqual({
      email: EMAIL, create_user: true, data: { display_name: 'Test Driver' },
    });
    expect(String(init.body)).not.toContain('password');
    expect(auth.readStoredSession()).toBeNull();
    pending.resolve(response({}));
    await expect(request).resolves.toBeUndefined();
    expect(auth.readStoredSession()).toBeNull();
  });

  it('supports existing accounts and a repeat request without metadata or password changes', async () => {
    const fetchMock = vi.fn(async () => response({}));
    vi.stubGlobal('fetch', fetchMock);
    await auth.requestEmailSignInOtp(EMAIL);
    await auth.requestEmailSignInOtp(EMAIL, '   ');
    expect(fetchMock).toHaveBeenCalledTimes(2);
    for (const call of fetchMock.mock.calls) {
      const [, init] = call as unknown as [string, RequestInit];
      expect(JSON.parse(String(init.body))).toEqual({ email: EMAIL, create_user: true });
    }
  });

  it('cannot accept an unexpected session while merely requesting a code', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(VERIFIED)));
    await auth.requestEmailSignInOtp(EMAIL);
    expect(auth.readStoredSession()).toBeNull();
  });

  it('bounds optional profile metadata and rejects a blank email before network access', async () => {
    const fetchMock = vi.fn(async () => response({}));
    vi.stubGlobal('fetch', fetchMock);
    await expect(auth.requestEmailSignInOtp('   ')).rejects.toThrow('Enter your email');
    expect(fetchMock).not.toHaveBeenCalled();
    await auth.requestEmailSignInOtp(EMAIL, 'a'.repeat(200));
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(String(init.body)).data.display_name).toHaveLength(120);
  });

  it.each([
    [429, 'For security purposes, request another code after 60 seconds.'],
    [500, 'Error sending confirmation email'],
  ])('surfaces provider error %s instead of claiming delivery', async (status, message) => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ msg: message }, status)));
    await expect(auth.requestEmailSignInOtp(EMAIL)).rejects.toThrow(message);
    expect(auth.readStoredSession()).toBeNull();
  });

  it('reports transport failure without exposing provider exception text', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('fixture private provider detail'); }));
    await expect(auth.requestEmailSignInOtp(EMAIL)).rejects.toThrow('Could not reach Supabase');
  });

  it('bounds a stalled request with the existing auth timeout', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn((_input: unknown, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    })));
    const request = auth.requestEmailSignInOtp(EMAIL);
    const rejected = expect(request).rejects.toThrow('did not respond in time');
    await vi.advanceTimersByTimeAsync(15_000);
    await rejected;
  });

  it.each([false, true])('rejects a cancelled request completion (server failure=%s)', async failure => {
    const pending = deferredResponse();
    vi.stubGlobal('fetch', vi.fn(() => pending.promise));
    const request = auth.requestEmailSignInOtp(EMAIL);
    const rejected = expect(request).rejects.toBeInstanceOf(auth.SessionChangedError);
    auth.clearStoredSession();
    pending.resolve(response(failure ? { msg: 'Email rate limit exceeded' } : {}, failure ? 429 : 200));
    await rejected;
    expect(auth.readStoredSession()).toBeNull();
  });

  it('supersedes an older request when the user requests a different email', async () => {
    const pending = deferredResponse();
    vi.stubGlobal('fetch', vi.fn()
      .mockImplementationOnce(() => pending.promise)
      .mockResolvedValueOnce(response({})));
    const oldRequest = auth.requestEmailSignInOtp(EMAIL);
    const rejected = expect(oldRequest).rejects.toBeInstanceOf(auth.SessionChangedError);
    await auth.requestEmailSignInOtp('new-account@example.test');
    pending.reject(new Error('old request disconnected'));
    await rejected;
  });
});

describe('verifying passwordless email codes', () => {
  it('verifies confirmation or login codes through type email and persists a complete session', async () => {
    const fetchMock = vi.fn(async () => response(VERIFIED));
    vi.stubGlobal('fetch', fetchMock);
    const generation = auth.sessionGeneration();
    const verification = auth.verifyEmailSignInOtp(` ${EMAIL.toUpperCase()} `, ' 12345678 ');
    expect(auth.sessionGeneration()).toBe(generation + 1);
    const stored = await verification;
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${SUPABASE_URL}/auth/v1/verify`);
    expect(JSON.parse(String(init.body))).toEqual({
      email: EMAIL.toUpperCase(), token: '12345678', type: 'email',
    });
    expect(stored.accessToken).toBe(VERIFIED.access_token);
    expect(stored.refreshToken).toBe(VERIFIED.refresh_token);
    expect(auth.readStoredSession()).toEqual(stored);
  });

  it.each(['', '12345', '12345678901', '123a56'])('rejects malformed code %s before network access', async code => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    await expect(auth.verifyEmailSignInOtp(EMAIL, code)).rejects.toThrow('numeric sign-in code');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each(['Token has expired or is invalid', 'Email rate limit exceeded'])(
    'preserves a failed verification message: %s', async message => {
      vi.stubGlobal('fetch', vi.fn(async () => response({ msg: message }, 403)));
      await expect(auth.verifyEmailSignInOtp(EMAIL, '123456')).rejects.toThrow(message);
      expect(auth.readStoredSession()).toBeNull();
    },
  );

  it.each([
    { ...CONFIRMED, email_confirmed_at: null, confirmed_at: '2026-10-23T10:00:00Z' },
    { ...CONFIRMED, email_confirmed_at: 'invalid-date' },
    { ...CONFIRMED, email: 'another@example.test' },
  ])('rejects an unconfirmed or different email response', async user => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ ...VERIFIED, user })));
    await expect(auth.verifyEmailSignInOtp(EMAIL, '123456')).rejects.toThrow('did not confirm this email');
    expect(auth.readStoredSession()).toBeNull();
  });

  it.each([
    { access_token: '' }, { access_token: '  ' }, { access_token: null },
    { refresh_token: '' }, { refresh_token: '  ' }, { refresh_token: null },
  ])('never stores an incomplete access/refresh pair', async missing => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ ...VERIFIED, ...missing })));
    await expect(auth.verifyEmailSignInOtp(EMAIL, '123456')).rejects.toThrow('incomplete verified session');
    expect(auth.readStoredSession()).toBeNull();
  });

  it('rejects invalid expiry without storing otherwise valid verified credentials', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ ...VERIFIED, expires_in: -1 })));
    await expect(auth.verifyEmailSignInOtp(EMAIL, '123456')).rejects.toThrow('invalid session expiry');
    expect(auth.readStoredSession()).toBeNull();
  });

  it('cannot restore a session after a verification is cancelled', async () => {
    const pending = deferredResponse();
    vi.stubGlobal('fetch', vi.fn(() => pending.promise));
    const verification = auth.verifyEmailSignInOtp(EMAIL, '123456');
    const rejected = expect(verification).rejects.toBeInstanceOf(auth.SessionChangedError);
    auth.clearStoredSession();
    pending.resolve(response(VERIFIED));
    await rejected;
    expect(auth.readStoredSession()).toBeNull();
  });

  it.each([false, true])('never overwrites a newer account when old verification completes (server failure=%s)', async failure => {
    const pending = deferredResponse();
    vi.stubGlobal('fetch', vi.fn()
      .mockImplementationOnce(() => pending.promise)
      .mockResolvedValueOnce(response({ ...VERIFIED, access_token: 'newer-account-access' })));
    const verification = auth.verifyEmailSignInOtp(EMAIL, '123456');
    const rejected = expect(verification).rejects.toBeInstanceOf(auth.SessionChangedError);
    await auth.signIn('another@example.test', 'fixture-password');
    pending.resolve(response(failure ? { msg: 'Expired old code' } : VERIFIED, failure ? 403 : 200));
    await rejected;
    expect(auth.readStoredSession()?.accessToken).toBe('newer-account-access');
  });
});
