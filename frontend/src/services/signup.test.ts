/* @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.stubEnv('VITE_SUPABASE_URL', 'https://wtednggryrikyvkuhqjo.supabase.co');
vi.stubEnv('VITE_SUPABASE_PUBLISHABLE_KEY', 'sb_publishable_test');
const auth = await import('./auth');
const EMAIL = 'driver@example.test';
const CONFIRMED_USER = {
  id: '11111111-1111-4111-8111-111111111111', email: EMAIL,
  email_confirmed_at: '2026-10-23T05:00:00Z',
};
const VERIFIED = {
  access_token: 'verified-access', refresh_token: 'verified-refresh', expires_in: 3600,
  user: CONFIRMED_USER,
};

function response(payload: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => payload } as Response;
}

beforeEach(() => {
  const entries = new Map<string, string>();
  const storage: Storage = {
    get length() { return entries.size; },
    clear: () => entries.clear(),
    getItem: key => entries.get(key) ?? null,
    setItem: (key, value) => { entries.set(key, value); },
    removeItem: key => { entries.delete(key); },
    key: index => Array.from(entries.keys())[index] ?? null,
  };
  Object.defineProperty(window, 'localStorage', { configurable: true, value: storage });
});

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe('email and password signup', () => {
  it('sends only Supabase the password and waits for email confirmation', async () => {
    const fetchMock = vi.fn(async () => response({ ...CONFIRMED_USER, email_confirmed_at: null }));
    vi.stubGlobal('fetch', fetchMock);
    const generation = auth.sessionGeneration();
    const result = await auth.signUp(` ${EMAIL} `, 'fixture-password', '  Test Driver  ');
    expect(result).toEqual({ email: EMAIL, needsVerification: true, session: null });
    expect(auth.sessionGeneration()).toBe(generation + 1);
    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('https://wtednggryrikyvkuhqjo.supabase.co/auth/v1/signup');
    expect(JSON.parse(String(init.body))).toEqual({
      email: EMAIL, password: 'fixture-password', data: { display_name: 'Test Driver' },
    });
    expect(auth.readStoredSession()).toBeNull();
  });

  it('cannot turn an unconfirmed signup response into a stored login', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({
      ...VERIFIED,
      user: { ...CONFIRMED_USER, email_confirmed_at: null, confirmed_at: '2026-10-23T05:00:00Z' },
    })));
    expect(await auth.signUp(EMAIL, 'fixture-password', 'Driver')).toEqual({
      email: EMAIL, needsVerification: true, session: null,
    });
    expect(auth.readStoredSession()).toBeNull();
  });

  it('supports an explicitly confirmed session if Supabase autoconfirm is configured', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response(VERIFIED)));
    const result = await auth.signUp(EMAIL, 'fixture-password', 'Driver');
    expect(result.needsVerification).toBe(false);
    expect(result.session?.accessToken).toBe('verified-access');
    expect(auth.readStoredSession()).toEqual(result.session);
  });

  it('surfaces SMTP delivery failures instead of claiming a code was sent', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ message: 'Error sending confirmation email' }, 500)));
    await expect(auth.signUp(EMAIL, 'fixture-password', 'Driver')).rejects.toThrow('Error sending confirmation email');
    expect(auth.readStoredSession()).toBeNull();
  });

  it('cannot restore a signup session after signing out', async () => {
    let finish!: (value: Response) => void;
    vi.stubGlobal('fetch', vi.fn(() => new Promise(resolve => { finish = resolve; })));
    const signup = auth.signUp(EMAIL, 'fixture-password', 'Driver');
    const rejected = expect(signup).rejects.toBeInstanceOf(auth.SessionChangedError);
    auth.clearStoredSession();
    finish(response(VERIFIED));
    await rejected;
    expect(auth.readStoredSession()).toBeNull();
  });
});

describe('signup verification codes', () => {
  it('verifies the signup code directly with Supabase and persists its confirmed session', async () => {
    const fetchMock = vi.fn(async () => response(VERIFIED));
    vi.stubGlobal('fetch', fetchMock);
    const session = await auth.verifySignUpOtp(` ${EMAIL} `, ' 12345678 ');
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('https://wtednggryrikyvkuhqjo.supabase.co/auth/v1/verify');
    expect(JSON.parse(String(init.body))).toEqual({ email: EMAIL, token: '12345678', type: 'signup' });
    expect(session.accessToken).toBe('verified-access');
    expect(auth.readStoredSession()).toEqual(session);
  });

  it('rejects malformed codes before sending a request', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    for (const code of ['', '12345', '12345678901', '123a56']) {
      await expect(auth.verifySignUpOtp(EMAIL, code)).rejects.toThrow('numeric verification code');
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each(['Token has expired or is invalid', 'Email link is invalid or has expired'])(
    'reports an invalid or expired code: %s', async message => {
      vi.stubGlobal('fetch', vi.fn(async () => response({ msg: message }, 403)));
      await expect(auth.verifySignUpOtp(EMAIL, '123456')).rejects.toThrow(message);
      expect(auth.readStoredSession()).toBeNull();
    },
  );

  it('rejects tokens for an email that remains unconfirmed', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({
      ...VERIFIED, user: { ...CONFIRMED_USER, email_confirmed_at: null },
    })));
    await expect(auth.verifySignUpOtp(EMAIL, '123456')).rejects.toThrow('did not confirm this email');
    expect(auth.readStoredSession()).toBeNull();
  });

  it('rejects a verified response for a different account', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({
      ...VERIFIED, user: { ...CONFIRMED_USER, email: 'other@example.test' },
    })));
    await expect(auth.verifySignUpOtp(EMAIL, '123456')).rejects.toThrow('did not confirm this email');
    expect(auth.readStoredSession()).toBeNull();
  });

  it('does not store a response missing the session refresh token', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ ...VERIFIED, refresh_token: '' })));
    await expect(auth.verifySignUpOtp(EMAIL, '123456')).rejects.toThrow('incomplete verified session');
    expect(auth.readStoredSession()).toBeNull();
  });

  it('cannot overwrite a newer account when an old verification completes', async () => {
    let finish!: (value: Response) => void;
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
      .mockResolvedValueOnce(response({ ...VERIFIED, access_token: 'new-account-access' }));
    vi.stubGlobal('fetch', fetchMock);
    const verify = auth.verifySignUpOtp(EMAIL, '123456');
    const rejected = expect(verify).rejects.toBeInstanceOf(auth.SessionChangedError);
    await auth.signIn('other@example.test', 'fixture-password');
    finish(response(VERIFIED));
    await rejected;
    expect(auth.readStoredSession()?.accessToken).toBe('new-account-access');
  });
});

describe('resending signup codes', () => {
  it('resends a signup confirmation without creating a passwordless session', async () => {
    const fetchMock = vi.fn(async () => response({}));
    vi.stubGlobal('fetch', fetchMock);
    const generation = auth.sessionGeneration();
    await auth.resendSignUpOtp(` ${EMAIL} `);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('https://wtednggryrikyvkuhqjo.supabase.co/auth/v1/resend');
    expect(JSON.parse(String(init.body))).toEqual({ email: EMAIL, type: 'signup' });
    expect(auth.sessionGeneration()).toBe(generation);
    expect(auth.readStoredSession()).toBeNull();
  });

  it('preserves the server rate-limit message', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ msg: 'For security purposes, you can only request this after 60 seconds.' }, 429)));
    await expect(auth.resendSignUpOtp(EMAIL)).rejects.toThrow('after 60 seconds');
  });

  it('ignores an old resend error after the account changes', async () => {
    let finish!: (value: Response) => void;
    vi.stubGlobal('fetch', vi.fn(() => new Promise(resolve => { finish = resolve; })));
    const resend = auth.resendSignUpOtp(EMAIL);
    const rejected = expect(resend).rejects.toBeInstanceOf(auth.SessionChangedError);
    auth.clearStoredSession();
    finish(response({ msg: 'Email rate limit exceeded' }, 429));
    await rejected;
  });
});
