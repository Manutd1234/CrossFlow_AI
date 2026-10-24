/* @vitest-environment jsdom */
/* @vitest-environment-options { "url": "http://localhost:3000" } */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const SUPABASE_URL = 'https://wtednggryrikyvkuhqjo.supabase.co';
vi.stubEnv('VITE_SUPABASE_URL', SUPABASE_URL);
vi.stubEnv('VITE_SUPABASE_PUBLISHABLE_KEY', 'sb_publishable_isolation_fixture');
const auth = await import('./auth');

const EMAIL_A = 'university-a@example.test';
const EMAIL_B = 'school-b@example.test';
const CODE_A = '111111';
const CODE_B = '222222';
const VERIFIED_B = {
  access_token: 'verified-b-access', refresh_token: 'verified-b-refresh', expires_in: 3600,
  user: { id: 'account-b', email: EMAIL_B, email_confirmed_at: '2026-10-23T12:00:00Z' },
};

function response(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

function memoryStorage(): Storage {
  const entries = new Map<string, string>();
  return {
    get length() { return entries.size; },
    getItem: key => entries.get(key) ?? null,
    setItem: (key, value) => { entries.set(key, value); },
    removeItem: key => { entries.delete(key); },
    clear: () => entries.clear(),
    key: index => Array.from(entries.keys())[index] ?? null,
  };
}

beforeEach(() => {
  Object.defineProperty(window, 'localStorage', { configurable: true, value: memoryStorage() });
  Object.defineProperty(window, 'sessionStorage', { configurable: true, value: memoryStorage() });
});

afterEach(() => {
  auth.clearStoredSession();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('email recipient and verification isolation', () => {
  it('keeps passwordless delivery and rejected cross-email code verification bound to each explicit address', async () => {
    // Supabase owns code generation and email+token validation. This test
    // checks our exact outgoing recipient trace and handling of its rejection;
    // it does not claim to emulate or live-test the provider's cryptography.
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({}))
      .mockResolvedValueOnce(response({}))
      .mockResolvedValueOnce(response({ msg: 'Token has expired or is invalid' }, 403))
      .mockResolvedValueOnce(response(VERIFIED_B));
    vi.stubGlobal('fetch', fetchMock);
    await auth.requestEmailSignInOtp(` ${EMAIL_A} `);
    await auth.requestEmailSignInOtp(` ${EMAIL_B} `);
    await expect(auth.verifyEmailSignInOtp(EMAIL_B, CODE_A)).rejects.toThrow('Token has expired or is invalid');
    expect(auth.readStoredSession()).toBeNull();
    const accepted = await auth.verifyEmailSignInOtp(EMAIL_B, CODE_B);
    expect(auth.readStoredSession()).toEqual(accepted);
    expect(accepted.accessToken).toBe('verified-b-access');
    expect(fetchMock.mock.calls.map(([url, init]) => [url, JSON.parse(String(init.body))])).toEqual([
      [`${SUPABASE_URL}/auth/v1/otp`, { email: EMAIL_A, create_user: true }],
      [`${SUPABASE_URL}/auth/v1/otp`, { email: EMAIL_B, create_user: true }],
      [`${SUPABASE_URL}/auth/v1/verify`, { email: EMAIL_B, token: CODE_A, type: 'email' }],
      [`${SUPABASE_URL}/auth/v1/verify`, { email: EMAIL_B, token: CODE_B, type: 'email' }],
    ]);
  });

  it('keeps a new signup, resend and rejected code from another signup on the selected address', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ email: EMAIL_A, email_confirmed_at: null }))
      .mockResolvedValueOnce(response({ email: EMAIL_B, email_confirmed_at: null }))
      .mockResolvedValueOnce(response({}))
      .mockResolvedValueOnce(response({ msg: 'Token has expired or is invalid' }, 403))
      .mockResolvedValueOnce(response(VERIFIED_B));
    vi.stubGlobal('fetch', fetchMock);
    const first = await auth.signUp(EMAIL_A, 'fixture-password-a', 'Account A');
    const second = await auth.signUp(` ${EMAIL_B} `, 'fixture-password-b', 'Account B');
    expect(first.email).toBe(EMAIL_A);
    expect(second.email).toBe(EMAIL_B);
    expect(second.session).toBeNull();
    await auth.resendSignUpOtp(second.email);
    await expect(auth.verifySignUpOtp(second.email, CODE_A)).rejects.toThrow('Token has expired or is invalid');
    expect(auth.readStoredSession()).toBeNull();
    const accepted = await auth.verifySignUpOtp(second.email, CODE_B);
    expect(auth.readStoredSession()).toEqual(accepted);
    expect(fetchMock.mock.calls.map(([url, init]) => [url, JSON.parse(String(init.body))])).toEqual([
      [`${SUPABASE_URL}/auth/v1/signup`, {
        email: EMAIL_A, password: 'fixture-password-a', data: { display_name: 'Account A' },
      }],
      [`${SUPABASE_URL}/auth/v1/signup`, {
        email: EMAIL_B, password: 'fixture-password-b', data: { display_name: 'Account B' },
      }],
      [`${SUPABASE_URL}/auth/v1/resend`, { email: EMAIL_B, type: 'signup' }],
      [`${SUPABASE_URL}/auth/v1/verify`, { email: EMAIL_B, token: CODE_A, type: 'signup' }],
      [`${SUPABASE_URL}/auth/v1/verify`, { email: EMAIL_B, token: CODE_B, type: 'signup' }],
    ]);
  });

  it.each([
    ['signup', auth.verifySignUpOtp], ['email sign-in', auth.verifyEmailSignInOtp],
  ])('rejects a confirmed session response for the wrong %s email', async (_flow, verify) => {
    const fetchMock = vi.fn(async () => response(VERIFIED_B));
    vi.stubGlobal('fetch', fetchMock);
    await expect(verify(EMAIL_A, CODE_A)).rejects.toThrow('did not confirm this email address');
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(String(init.body)).email).toBe(EMAIL_A);
    expect(auth.readStoredSession()).toBeNull();
  });
});
