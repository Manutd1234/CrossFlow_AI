/* @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const cryptoModuleName = 'node:crypto' as string;
const { webcrypto } = await import(/* @vite-ignore */ cryptoModuleName) as { webcrypto: Crypto };

const sdk = vi.hoisted(() => ({
  createClient: vi.fn(), signInWithOAuth: vi.fn(), exchangeCodeForSession: vi.fn(),
}));
vi.mock('@supabase/supabase-js', () => ({ createClient: sdk.createClient }));

const PROJECT = 'https://wtednggryrikyvkuhqjo.supabase.co';
const FLOW_ID = 'cb07540b-b1da-475a-8b5a-31851c5cfe29';
const AUTHORIZE_URL = `${PROJECT}/auth/v1/authorize?provider=google&redirect_to=http%3A%2F%2Flocalhost%3A3000%2F`;
const TOKEN_PAYLOAD = {
  access_token: 'supabase-access', refresh_token: 'supabase-refresh', expires_in: 3600,
  provider_token: 'google-token-must-not-be-used',
};

function storage(): Storage {
  const entries = new Map<string, string>();
  return {
    get length() { return entries.size; },
    getItem: key => entries.get(key) ?? null,
    setItem: (key, value) => { entries.set(key, String(value)); },
    removeItem: key => { entries.delete(key); },
    clear: () => entries.clear(),
    key: index => Array.from(entries.keys())[index] ?? null,
  };
}

let browser: {
  location: { href: string; origin: string; assign: ReturnType<typeof vi.fn> };
  localStorage: Storage;
  sessionStorage: Storage;
  history: { state: null; replaceState: ReturnType<typeof vi.fn> };
  setTimeout: typeof window.setTimeout;
  clearTimeout: typeof window.clearTimeout;
};
let auth: typeof import('./auth');

beforeEach(async () => {
  vi.resetModules();
  vi.clearAllMocks();
  vi.stubEnv('VITE_SUPABASE_URL', PROJECT);
  vi.stubEnv('VITE_SUPABASE_PUBLISHABLE_KEY', 'sb_publishable_test');
  browser = {
    location: { href: 'http://localhost:3000/', origin: 'http://localhost:3000', assign: vi.fn() },
    localStorage: storage(), sessionStorage: storage(),
    history: {
      state: null,
      replaceState: vi.fn((_state, _title, url) => {
        browser.location.href = new URL(String(url), browser.location.origin).href;
      }),
    },
    setTimeout: window.setTimeout.bind(window), clearTimeout: window.clearTimeout.bind(window),
  };
  vi.stubGlobal('window', browser);
  vi.stubGlobal('crypto', webcrypto);
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, json: async () => ({ external: { google: true, email: true } }),
  })));
  sdk.createClient.mockReturnValue({ auth: {
    signInWithOAuth: sdk.signInWithOAuth, exchangeCodeForSession: sdk.exchangeCodeForSession,
  } });
  sdk.signInWithOAuth.mockImplementation(async () => {
    const options = sdk.createClient.mock.calls[sdk.createClient.mock.calls.length - 1]?.[2];
    options.auth.storage.setItem('crossflow.oauth.pkce-code-verifier', 'proof-key');
    return { data: { url: AUTHORIZE_URL, flowId: FLOW_ID }, error: null };
  });
  sdk.exchangeCodeForSession.mockResolvedValue({ data: { session: TOKEN_PAYLOAD }, error: null });
  auth = await import('./auth');
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe('Google OAuth initiation', () => {
  it('uses SDK PKCE with a same-origin root and keeps only verifier storage', async () => {
    browser.location.href = 'http://localhost:3000/?next=https://attacker.example/#route';
    await auth.startGoogleSignIn();
    expect(sdk.signInWithOAuth).toHaveBeenCalledWith({
      provider: 'google', options: { redirectTo: 'http://localhost:3000/', skipBrowserRedirect: true },
    });
    const options = sdk.createClient.mock.calls[0][2];
    expect(options.auth).toMatchObject({
      flowType: 'pkce', autoRefreshToken: false, detectSessionInUrl: false, persistSession: true,
    });
    expect(browser.sessionStorage.getItem('crossflow.oauth.pkce-code-verifier')).toBe('proof-key');
    options.auth.storage.setItem('crossflow.oauth.pkce', 'sdk-session-should-not-persist');
    expect(browser.sessionStorage.getItem('crossflow.oauth.pkce')).toBeNull();
    expect(browser.location.assign).toHaveBeenCalledWith(AUTHORIZE_URL);
  });

  it('reports provider setup instead of redirecting when Google is disabled', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ external: { google: false } }) })));
    await expect(auth.startGoogleSignIn()).rejects.toThrow('Google sign-in is not enabled');
    expect(sdk.createClient).not.toHaveBeenCalled();
    expect(browser.location.assign).not.toHaveBeenCalled();
    expect(browser.sessionStorage.getItem('crossflow.oauth.pending')).toBeNull();
  });

  it('does not follow an unexpected authorization URL', async () => {
    sdk.signInWithOAuth.mockResolvedValue({ data: { url: 'https://attacker.example/login' }, error: null });
    await expect(auth.startGoogleSignIn()).rejects.toThrow('unexpected Google sign-in URL');
    expect(browser.location.assign).not.toHaveBeenCalled();
    expect(browser.sessionStorage.getItem('crossflow.oauth.pending')).toBeNull();
  });

  it('does not accept an authorization URL with an external redirect', async () => {
    sdk.signInWithOAuth.mockResolvedValue({
      data: { url: `${PROJECT}/auth/v1/authorize?provider=google&redirect_to=https://attacker.example` }, error: null,
    });
    await expect(auth.startGoogleSignIn()).rejects.toThrow('unexpected Google sign-in URL');
    expect(browser.location.assign).not.toHaveBeenCalled();
  });

  it('ignores initiation if the account changed while provider settings loaded', async () => {
    let finish!: (response: unknown) => void;
    vi.stubGlobal('fetch', vi.fn(() => new Promise(resolve => { finish = resolve; })));
    const pending = auth.startGoogleSignIn();
    const rejected = expect(pending).rejects.toBeInstanceOf(auth.SessionChangedError);
    auth.clearStoredSession();
    finish({ ok: true, json: async () => ({ external: { google: true } }) });
    await rejected;
    expect(browser.location.assign).not.toHaveBeenCalled();
  });
});

describe('Google OAuth completion', () => {
  it('uses the real SDK to preserve its S256 verifier and exchange the callback', async () => {
    vi.stubGlobal('BroadcastChannel', undefined);
    const actualSdk = await vi.importActual<typeof import('@supabase/supabase-js')>('@supabase/supabase-js');
    let realClient: ReturnType<typeof actualSdk.createClient> | undefined;
    sdk.createClient.mockImplementation((...args: Parameters<typeof actualSdk.createClient>) => {
      realClient = actualSdk.createClient(...args);
      return realClient;
    });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/auth/v1/settings')) {
        return new Response(JSON.stringify({ external: { google: true } }), { status: 200 });
      }
      expect(url).toBe(`${PROJECT}/auth/v1/token?grant_type=pkce`);
      const body = JSON.parse(String(init?.body));
      expect(body.auth_code).toBe('real-sdk-code');
      expect(body.code_verifier).toBeTruthy();
      const authorize = new URL(browser.location.assign.mock.calls[0][0]);
      expect(authorize.searchParams.get('code_challenge_method')).toBe('s256');
      const digest = await webcrypto.subtle.digest('SHA-256', new TextEncoder().encode(body.code_verifier));
      const challenge = btoa(String.fromCharCode(...new Uint8Array(digest)))
        .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
      expect(authorize.searchParams.get('code_challenge')).toBe(challenge);
      expect(new Headers(init?.headers).get('apikey')).toBe('sb_publishable_test');
      return new Response(JSON.stringify({
        ...TOKEN_PAYLOAD, token_type: 'bearer',
        user: { id: '11111111-1111-4111-8111-111111111111', email: 'driver@example.test' },
      }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    });
    vi.stubGlobal('fetch', fetchMock);
    try {
      await auth.startGoogleSignIn();
      const marker = JSON.parse(browser.sessionStorage.getItem('crossflow.oauth.pending')!);
      expect(marker.flowId).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
      expect(browser.sessionStorage.getItem(`crossflow.oauth.pkce-flow-${marker.flowId}-code-verifier`)).toBeTruthy();
      browser.location.href = 'http://localhost:3000/?code=real-sdk-code';
      const result = await auth.completeOAuthSignIn();
      expect(result?.accessToken).toBe('supabase-access');
      expect(auth.readStoredSession()).toEqual(result);
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(browser.sessionStorage.getItem('crossflow.oauth.pkce')).toBeNull();
      expect(browser.sessionStorage.getItem(`crossflow.oauth.pkce-flow-${marker.flowId}-code-verifier`)).toBeNull();
    } finally {
      await realClient?.auth.dispose();
    }
  });

  it('ignores ordinary page visits', async () => {
    browser.location.href = 'http://localhost:3000/#route';
    expect(auth.hasOAuthCallback()).toBe(false);
    expect(await auth.completeOAuthSignIn()).toBeNull();
    expect(sdk.exchangeCodeForSession).not.toHaveBeenCalled();
    expect(browser.history.replaceState).not.toHaveBeenCalled();
  });

  it('rejects unsolicited codes, cleans the URL and never exchanges them', async () => {
    browser.location.href = 'http://localhost:3000/?code=unrequested&utm=keep#route';
    expect(auth.hasOAuthCallback()).toBe(true);
    await expect(auth.completeOAuthSignIn()).rejects.toThrow('not started in this tab');
    expect(browser.location.href).toBe('http://localhost:3000/?utm=keep#route');
    expect(sdk.exchangeCodeForSession).not.toHaveBeenCalled();
  });

  it('shares a rejected unsolicited callback after StrictMode cleans its URL', async () => {
    browser.location.href = 'http://localhost:3000/?code=unrequested';
    const first = auth.completeOAuthSignIn();
    const firstRejected = expect(first).rejects.toThrow('not started in this tab');
    const second = auth.completeOAuthSignIn();
    expect(first).toBe(second);
    const secondRejected = expect(second).rejects.toThrow('not started in this tab');
    await Promise.all([firstRejected, secondRejected]);
    expect(sdk.exchangeCodeForSession).not.toHaveBeenCalled();
  });

  it('survives the same-tab reload, exchanges once, and stores Supabase tokens', async () => {
    await auth.startGoogleSignIn();
    vi.resetModules();
    auth = await import('./auth');
    browser.location.href = `http://localhost:3000/?code=authorization-code&utm=keep#route`;
    const session = await auth.completeOAuthSignIn();
    expect(sdk.exchangeCodeForSession).toHaveBeenCalledWith('authorization-code', { flowId: FLOW_ID });
    expect(session?.accessToken).toBe('supabase-access');
    expect(auth.readStoredSession()).toEqual(session);
    expect(JSON.stringify(auth.readStoredSession())).not.toContain('google-token');
    expect(browser.location.href).toBe('http://localhost:3000/?utm=keep#route');
    expect(browser.sessionStorage.getItem('crossflow.oauth.pending')).toBeNull();
    expect(browser.sessionStorage.getItem('crossflow.oauth.pkce-code-verifier')).toBeNull();
  });

  it('shares completion after URL cleanup under StrictMode duplicate bootstrap', async () => {
    await auth.startGoogleSignIn();
    let finish!: (value: unknown) => void;
    sdk.exchangeCodeForSession.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    browser.location.href = 'http://localhost:3000/?code=single-use-code';
    const first = auth.completeOAuthSignIn();
    expect(browser.location.href).toBe('http://localhost:3000/');
    expect(auth.hasOAuthCallback()).toBe(true);
    const second = auth.completeOAuthSignIn();
    expect(first).toBe(second);
    await vi.waitFor(() => expect(sdk.exchangeCodeForSession).toHaveBeenCalledOnce());
    finish({ data: { session: TOKEN_PAYLOAD }, error: null });
    expect(await first).toEqual(await second);
    expect(auth.hasOAuthCallback()).toBe(false);
  });

  it('surfaces provider cancellation from URL fragments and removes error fields', async () => {
    await auth.startGoogleSignIn();
    browser.location.href = 'http://localhost:3000/?utm=keep#error=access_denied&error_code=cancelled&error_description=No+thanks&section=route';
    expect(auth.hasOAuthCallback()).toBe(true);
    await expect(auth.completeOAuthSignIn()).rejects.toThrow('Google sign-in was cancelled');
    expect(sdk.exchangeCodeForSession).not.toHaveBeenCalled();
    expect(browser.location.href).toBe('http://localhost:3000/?utm=keep#section=route');
  });

  it('rejects implicit fragment tokens rather than accepting them as a session', async () => {
    await auth.startGoogleSignIn();
    browser.location.href = 'http://localhost:3000/#access_token=forged&refresh_token=forged&section=route';
    expect(auth.hasOAuthCallback()).toBe(true);
    await expect(auth.completeOAuthSignIn()).rejects.toThrow('unsupported response');
    expect(sdk.exchangeCodeForSession).not.toHaveBeenCalled();
    expect(auth.readStoredSession()).toBeNull();
    expect(browser.location.href).toBe('http://localhost:3000/#section=route');
  });

  it('refuses expired requests', async () => {
    await auth.startGoogleSignIn();
    const marker = JSON.parse(browser.sessionStorage.getItem('crossflow.oauth.pending')!);
    browser.sessionStorage.setItem('crossflow.oauth.pending', JSON.stringify({ ...marker, startedAtMs: Date.now() - 11 * 60_000 }));
    browser.location.href = 'http://localhost:3000/?code=expired';
    await expect(auth.completeOAuthSignIn()).rejects.toThrow('has expired');
    expect(sdk.exchangeCodeForSession).not.toHaveBeenCalled();
  });

  it('cannot restore a session when exchange finishes after sign-out', async () => {
    await auth.startGoogleSignIn();
    let finish!: (value: unknown) => void;
    sdk.exchangeCodeForSession.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    browser.location.href = 'http://localhost:3000/?code=late';
    const pending = auth.completeOAuthSignIn();
    const rejected = expect(pending).rejects.toBeInstanceOf(auth.SessionChangedError);
    await vi.waitFor(() => expect(sdk.exchangeCodeForSession).toHaveBeenCalledOnce());
    auth.clearStoredSession();
    finish({ data: { session: TOKEN_PAYLOAD }, error: null });
    await rejected;
    expect(auth.readStoredSession()).toBeNull();
  });
});
