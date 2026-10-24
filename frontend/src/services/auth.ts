/**
 * Supabase-backed sign-in for CrossFlow.
 *
 * The password goes to Supabase Auth directly and never to the CrossFlow API,
 * so Supabase's own rate limiting, lockout and password-reset flows apply
 * (backend roadmap decision D2). CrossFlow only ever receives the resulting
 * access token as a Bearer credential.
 *
 * The role shown to the user is the one `/api/auth/session` resolved from
 * `crossflow_profiles` with the caller's own token — never a claim decoded
 * from the token here, which the client could trivially forge.
 */

import type { AuthSession, AuthStatus, StoredSession } from '../types';
import type { SupabaseClient } from '@supabase/supabase-js';

const SUPABASE_URL = import.meta.env.VITE_SUPABASE_URL?.trim().replace(/\/$/, '') ?? '';
const SUPABASE_KEY = (
  import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY
  ?? import.meta.env.VITE_SUPABASE_ANON_KEY
  ?? ''
).trim();

const configuredApiBase = import.meta.env.VITE_API_BASE_URL?.trim().replace(/\/$/, '');
const API_BASE = configuredApiBase ?? '';

// Disposable local-development credentials only. Keep each environment read
// behind Vite's static DEV guard so production builds eliminate the values,
// even when a deployment mistakenly configures these variables.
const TEST_ADMIN_EMAIL = import.meta.env.DEV
  ? (import.meta.env.VITE_TEST_ADMIN_EMAIL?.trim() ?? '') : '';
const TEST_ADMIN_PASSWORD = import.meta.env.DEV
  ? (import.meta.env.VITE_TEST_ADMIN_PASSWORD ?? '') : '';

const STORAGE_KEY = 'crossflow.session';
const AUTH_TIMEOUT_MS = 15_000;
/** Refresh this long before expiry so an in-flight request cannot age out. */
const REFRESH_SKEW_S = 60;
let generation = 0;
const OAUTH_STORAGE_KEY = 'crossflow.oauth.pkce';
const OAUTH_PENDING_KEY = 'crossflow.oauth.pending';
const OAUTH_PENDING_TTL_MS = 10 * 60_000;
let oauthClient: Promise<SupabaseClient> | null = null;
let pendingOAuthCompletion: Promise<StoredSession | null> | null = null;

interface PendingOAuth {
  startedAtMs: number;
  redirectTo: string;
  projectOrigin: string;
  flowId?: string;
}

export class AuthError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'AuthError';
  }
}

/** A server authoritatively rejected this saved credential. */
export class SessionRejectedError extends AuthError {
  constructor(message: string) {
    super(message);
    this.name = 'SessionRejectedError';
  }
}

/** An account change superseded this request; its caller must ignore it. */
export class SessionChangedError extends AuthError {
  constructor() {
    super('The signed-in account changed while this request was in progress.');
    this.name = 'SessionChangedError';
  }
}

/** Snapshot before async identity work; normal token refreshes keep it stable. */
export function sessionGeneration(): number {
  return generation;
}

export function supabaseConfigured(): boolean {
  return SUPABASE_URL.length > 0 && SUPABASE_KEY.length > 0;
}

/** Whether local development offers its disposable one-click test account. */
export function testAdminConfigured(): boolean {
  return TEST_ADMIN_EMAIL.length > 0 && TEST_ADMIN_PASSWORD.length > 0;
}

/**
 * Detect the browser and the API authenticating against different projects.
 *
 * This is worth its own check because the symptom is so misleading: sign-in
 * succeeds, Supabase issues a perfectly valid token, and then every API call
 * returns a bare 401 because the server is validating it against a different
 * project's keys. Nothing in that sequence points at the real cause.
 */
export function projectMismatch(status: AuthStatus | null): string | null {
  const serverOrigin = status?.project_origin?.trim().replace(/\/$/, '');
  if (!serverOrigin || !SUPABASE_URL) return null;
  if (serverOrigin === SUPABASE_URL) return null;
  return (
    `This build signs in to ${SUPABASE_URL}, but the API validates tokens `
    + `against ${serverOrigin}. A token from one project is never valid for `
    + 'the other, so sign-in would fail with an unexplained 401. Point '
    + 'VITE_SUPABASE_URL and the server\'s SUPABASE_URL at the same project.'
  );
}

async function supabaseFetch(
  path: string,
  body: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  if (!supabaseConfigured()) {
    throw new AuthError(
      'Sign-in is not configured: VITE_SUPABASE_URL and VITE_SUPABASE_PUBLISHABLE_KEY are unset.',
    );
  }
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), AUTH_TIMEOUT_MS);
  try {
    const response = await fetch(`${SUPABASE_URL}${path}`, {
      method: 'POST',
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        apikey: SUPABASE_KEY,
        Authorization: `Bearer ${SUPABASE_KEY}`,
      },
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch((error) => {
      if (controller.signal.aborted) throw error;
      return {};
    });
    if (!response.ok) {
      // Supabase distinguishes rejected credentials from unconfirmed email.
      const detail = typeof payload?.error_description === 'string'
        ? payload.error_description
        : typeof payload?.msg === 'string'
          ? payload.msg
          : typeof payload?.message === 'string'
            ? payload.message
            : 'Sign-in failed.';
      const errorCode = typeof payload?.error_code === 'string' ? payload.error_code
        : typeof payload?.code === 'string' ? payload.code
          : typeof payload?.error === 'string' ? payload.error : '';
      const rejectedRefresh = path === '/auth/v1/token?grant_type=refresh_token'
        && (response.status === 401 || (response.status === 400 && (
          ['invalid_grant', 'refresh_token_not_found', 'refresh_token_already_used',
            'session_not_found', 'session_expired', 'bad_jwt'].includes(errorCode)
          || /^Invalid Refresh Token:/i.test(detail)
        )));
      if (rejectedRefresh) throw new SessionRejectedError(detail);
      throw new AuthError(detail);
    }
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
      throw new AuthError('Supabase returned an invalid session response.');
    }
    return payload as Record<string, unknown>;
  } catch (error) {
    if (error instanceof AuthError) throw error;
    throw new AuthError(
      (error as Error)?.name === 'AbortError'
        ? 'Supabase did not respond in time. Check your connection and try again.'
        : 'Could not reach Supabase. Check your connection and try again.',
    );
  } finally {
    window.clearTimeout(timer);
  }
}

function toStoredSession(payload: Record<string, unknown>): StoredSession {
  const accessToken = typeof payload.access_token === 'string' ? payload.access_token : '';
  const refreshToken = typeof payload.refresh_token === 'string' ? payload.refresh_token : '';
  const expiresIn = typeof payload.expires_in === 'number' ? payload.expires_in : 3600;
  const expiresAtMs = Date.now() + expiresIn * 1000;
  if (!accessToken) throw new AuthError('Supabase returned no access token.');
  if (!Number.isFinite(expiresIn) || expiresIn <= 0 || !Number.isFinite(expiresAtMs)) {
    throw new AuthError('Supabase returned an invalid session expiry.');
  }
  return {
    accessToken,
    refreshToken,
    expiresAtMs,
  };
}

export function readStoredSession(): StoredSession | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<StoredSession>;
    if (typeof parsed?.accessToken !== 'string' || !parsed.accessToken) return null;
    if (typeof parsed?.expiresAtMs !== 'number' || !Number.isFinite(parsed.expiresAtMs)) return null;
    return {
      accessToken: parsed.accessToken,
      refreshToken: typeof parsed.refreshToken === 'string' ? parsed.refreshToken : '',
      expiresAtMs: parsed.expiresAtMs,
    };
  } catch {
    // Corrupt or unavailable storage must not break the public views.
    return null;
  }
}

function writeStoredSession(session: StoredSession | null): void {
  try {
    if (session) window.localStorage.setItem(STORAGE_KEY, JSON.stringify(session));
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Private-mode storage failures are not fatal; the session stays in memory.
  }
}

/** Forget a rejected credential without making another network request. */
export function clearStoredSession(): void {
  generation += 1;
  writeStoredSession(null);
  clearOAuthData();
}

export async function signIn(email: string, password: string): Promise<StoredSession> {
  const requestGeneration = ++generation;
  clearOAuthData();
  const payload = await supabaseFetch('/auth/v1/token?grant_type=password', {
    email: email.trim(),
    password,
  }).catch((error) => {
    if (requestGeneration !== generation) throw new SessionChangedError();
    throw error;
  });
  if (requestGeneration !== generation) throw new SessionChangedError();
  const session = toStoredSession(payload);
  writeStoredSession(session);
  return session;
}

export interface SignUpResult {
  email: string;
  needsVerification: boolean;
  session: StoredSession | null;
}

async function signupRequest(
  path: string,
  body: Record<string, unknown>,
  requestGeneration: number,
): Promise<Record<string, unknown>> {
  try {
    const payload = await supabaseFetch(path, body);
    if (requestGeneration !== generation) throw new SessionChangedError();
    return payload;
  } catch (error) {
    if (requestGeneration !== generation) throw new SessionChangedError();
    throw error;
  }
}

function confirmedEmail(payload: Record<string, unknown>): string | null {
  const user = payload.user && typeof payload.user === 'object'
    ? payload.user as Record<string, unknown> : payload;
  const confirmedAt = user.email_confirmed_at;
  return typeof user.email === 'string'
    && typeof confirmedAt === 'string' && Number.isFinite(Date.parse(confirmedAt))
    ? user.email : null;
}

/** Register with Supabase directly; its confirmation template sends the OTP. */
export async function signUp(email: string, password: string, displayName: string): Promise<SignUpResult> {
  const requestGeneration = ++generation;
  clearOAuthData();
  const normalizedEmail = email.trim();
  if (!normalizedEmail || !password) throw new AuthError('Enter your email and password to create an account.');
  const payload = await signupRequest('/auth/v1/signup', {
    email: normalizedEmail,
    password,
    data: { display_name: displayName.trim().slice(0, 120) },
  }, requestGeneration);
  // With email confirmation enabled Supabase returns a user, never a session.
  // Even an unexpected access token cannot bypass confirmation in this client.
  const confirmed = confirmedEmail(payload);
  if (typeof payload.access_token !== 'string' || !payload.access_token || !confirmed) {
    return { email: normalizedEmail, needsVerification: true, session: null };
  }
  if (confirmed.toLowerCase() !== normalizedEmail.toLowerCase()) {
    throw new AuthError('Supabase returned a session for a different email address.');
  }
  const session = toStoredSession(payload);
  writeStoredSession(session);
  return { email: normalizedEmail, needsVerification: false, session };
}

/** Verify the code from the signup confirmation email, then store its session. */
export async function verifySignUpOtp(email: string, code: string): Promise<StoredSession> {
  const requestGeneration = ++generation;
  clearOAuthData();
  const normalizedEmail = email.trim();
  const token = code.trim();
  if (!normalizedEmail || !/^\d{6,10}$/.test(token)) {
    throw new AuthError('Enter the numeric verification code from your email.');
  }
  const payload = await signupRequest('/auth/v1/verify', {
    email: normalizedEmail, token, type: 'signup',
  }, requestGeneration);
  const confirmed = confirmedEmail(payload);
  if (!confirmed || confirmed.toLowerCase() !== normalizedEmail.toLowerCase()) {
    throw new AuthError('Supabase did not confirm this email address. Request a new verification code and try again.');
  }
  const session = toStoredSession(payload);
  if (!session.refreshToken) throw new AuthError('Supabase returned an incomplete verified session. Please sign in again.');
  writeStoredSession(session);
  return session;
}

/** Resend the signup code without signing in or starting a passwordless login. */
export async function resendSignUpOtp(email: string): Promise<void> {
  const requestGeneration = generation;
  const normalizedEmail = email.trim();
  if (!normalizedEmail) throw new AuthError('Enter the email address used to create your account.');
  await signupRequest('/auth/v1/resend', {
    email: normalizedEmail, type: 'signup',
  }, requestGeneration);
}

/** Send a passwordless email code for a new or existing Supabase account. */
export async function requestEmailSignInOtp(email: string, displayName?: string): Promise<void> {
  const requestGeneration = ++generation;
  clearOAuthData();
  const normalizedEmail = email.trim();
  if (!normalizedEmail) throw new AuthError('Enter your email address to receive a sign-in code.');
  const name = displayName?.trim().slice(0, 120);
  // Supabase handles initial confirmation for new accounts and email login for
  // existing accounts. This client never chooses or sends a password here.
  await signupRequest('/auth/v1/otp', {
    email: normalizedEmail,
    create_user: true,
    ...(name ? { data: { display_name: name } } : {}),
  }, requestGeneration);
}

/** Verify either an initial confirmation or an existing account's email code. */
export async function verifyEmailSignInOtp(email: string, code: string): Promise<StoredSession> {
  const requestGeneration = ++generation;
  clearOAuthData();
  const normalizedEmail = email.trim();
  const token = code.trim();
  if (!normalizedEmail || !/^\d{6,10}$/.test(token)) {
    throw new AuthError('Enter the numeric sign-in code from your email.');
  }
  const payload = await signupRequest('/auth/v1/verify', {
    email: normalizedEmail, token, type: 'email',
  }, requestGeneration);
  const confirmed = confirmedEmail(payload);
  if (!confirmed || confirmed.toLowerCase() !== normalizedEmail.toLowerCase()) {
    throw new AuthError('Supabase did not confirm this email address. Request a new sign-in code and try again.');
  }
  if (typeof payload.access_token !== 'string' || !payload.access_token.trim()
    || typeof payload.refresh_token !== 'string' || !payload.refresh_token.trim()) {
    throw new AuthError('Supabase returned an incomplete verified session. Request a new sign-in code and try again.');
  }
  const session = toStoredSession(payload);
  writeStoredSession(session);
  return session;
}

/**
 * Sign in with the explicitly configured local-development test administrator.
 *
 * The credentials still go directly to Supabase through `signIn`; they never
 * reach the CrossFlow API. The UI separately verifies that the server resolves
 * this account as an admin before it accepts the session.
 */
export async function signInAsTestAdmin(): Promise<StoredSession> {
  if (!testAdminConfigured()) {
    throw new AuthError('Test admin access is not configured for this build.');
  }
  return signIn(TEST_ADMIN_EMAIL, TEST_ADMIN_PASSWORD);
}

const pendingRefreshes = new Map<string, Promise<StoredSession>>();

export async function refreshSession(session: StoredSession): Promise<StoredSession> {
  if (!session.refreshToken) throw new SessionRejectedError('This session cannot be refreshed.');
  const requestGeneration = generation;
  const storedAtStart = readStoredSession();
  if (storedAtStart && storedAtStart.refreshToken !== session.refreshToken) {
    throw new SessionChangedError();
  }
  const refreshKey = `${requestGeneration}:${session.refreshToken}`;
  const pending = pendingRefreshes.get(refreshKey);
  if (pending) return pending;
  const request = (async () => {
    const payload = await supabaseFetch('/auth/v1/token?grant_type=refresh_token', {
      refresh_token: session.refreshToken,
    }).catch((error) => {
      if (requestGeneration !== generation) throw new SessionChangedError();
      throw error;
    });
    const storedNow = readStoredSession();
    if (requestGeneration !== generation
      || (storedAtStart && storedNow?.refreshToken !== session.refreshToken)
      || (storedNow && storedNow.refreshToken !== session.refreshToken)) {
      throw new SessionChangedError();
    }
    const refreshed = toStoredSession(payload);
    // A response arriving after sign-out or an account switch cannot put the
    // previous user's tokens back into storage.
    if (storedNow?.refreshToken === session.refreshToken) {
      writeStoredSession(refreshed);
    }
    return refreshed;
  })();
  pendingRefreshes.set(refreshKey, request);
  try {
    return await request;
  } finally {
    pendingRefreshes.delete(refreshKey);
  }
}

/** Return a session that is still valid, refreshing it first when it is close to expiry. */
export async function validSession(session: StoredSession): Promise<StoredSession> {
  if (Date.now() < session.expiresAtMs - REFRESH_SKEW_S * 1000) return session;
  return refreshSession(session);
}

export async function signOut(session: StoredSession | null): Promise<void> {
  clearStoredSession();
  if (!session?.accessToken || !supabaseConfigured()) return;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), AUTH_TIMEOUT_MS);
  try {
    await fetch(`${SUPABASE_URL}/auth/v1/logout`, {
      method: 'POST',
      signal: controller.signal,
      headers: {
        apikey: SUPABASE_KEY,
        Authorization: `Bearer ${session.accessToken}`,
      },
    });
  } catch {
    // The local credential is already gone, which is what signing out means
    // here. A failed server-side revoke must not strand the user signed in.
  } finally {
    window.clearTimeout(timer);
  }
}

export async function fetchAuthStatus(): Promise<AuthStatus | null> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), AUTH_TIMEOUT_MS);
  try {
    const response = await fetch(`${API_BASE}/api/auth/status`, { signal: controller.signal });
    if (!response.ok) return null;
    return (await response.json()) as AuthStatus;
  } catch {
    return null;
  } finally {
    window.clearTimeout(timer);
  }
}

/**
 * Ask the API who it thinks the caller is.
 *
 * The returned role is authoritative. Reading it from the token client-side
 * would let anyone hand themselves an admin badge.
 */
export async function fetchSession(session: StoredSession): Promise<AuthSession> {
  const requestGeneration = generation;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), AUTH_TIMEOUT_MS);
  try {
    const response = await fetch(`${API_BASE}/api/auth/session`, {
      signal: controller.signal,
      headers: { Authorization: `Bearer ${session.accessToken}` },
    });
    if (requestGeneration !== generation) throw new SessionChangedError();
    if (response.status === 401) {
      throw new SessionRejectedError('Your session has expired. Please sign in again.');
    }
    if (!response.ok) {
      const detail = await response.json().catch((error) => {
        if (controller.signal.aborted) throw error;
        return {};
      });
      if (requestGeneration !== generation) throw new SessionChangedError();
      throw new AuthError(
        typeof detail?.detail === 'string' ? detail.detail : 'Could not load your session.',
      );
    }
    const identity = await response.json() as AuthSession;
    if (requestGeneration !== generation) throw new SessionChangedError();
    return identity;
  } catch (error) {
    if (requestGeneration !== generation) throw new SessionChangedError();
    if (error instanceof AuthError) throw error;
    throw new AuthError('Could not reach the sign-in service. Please try again.');
  } finally {
    window.clearTimeout(timer);
  }
}

function clearOAuthData(): void {
  try {
    const storage = window.sessionStorage;
    storage.removeItem(OAUTH_PENDING_KEY);
    for (let index = storage.length - 1; index >= 0; index -= 1) {
      const key = storage.key(index);
      if (key?.startsWith(OAUTH_STORAGE_KEY)) storage.removeItem(key);
    }
  } catch {
    // Unavailable storage is reported when Google sign-in starts.
  }
}

/** Bound the SDK's token exchange, including its response body. */
async function oauthFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (init?.signal?.aborted) controller.abort();
  init?.signal?.addEventListener('abort', abort, { once: true });
  const timer = window.setTimeout(abort, AUTH_TIMEOUT_MS);
  try {
    const response = await fetch(input, { ...init, signal: controller.signal });
    const body = await response.arrayBuffer();
    return new Response(body, {
      status: response.status, statusText: response.statusText, headers: response.headers,
    });
  } finally {
    window.clearTimeout(timer);
    init?.signal?.removeEventListener('abort', abort);
  }
}

async function googleClient(): Promise<SupabaseClient> {
  if (!oauthClient) {
    oauthClient = import('@supabase/supabase-js').then(({ createClient }) => createClient(
      SUPABASE_URL, SUPABASE_KEY, {
        auth: {
          flowType: 'pkce',
          autoRefreshToken: false,
          detectSessionInUrl: false,
          persistSession: true,
          storageKey: OAUTH_STORAGE_KEY,
          // Persist only PKCE verifier slots through this tab's redirect. The
          // existing CrossFlow session lifecycle owns all access/refresh tokens.
          storage: {
            getItem: key => key.endsWith('-code-verifier')
              ? window.sessionStorage.getItem(key) : null,
            setItem: (key, value) => {
              if (key.endsWith('-code-verifier')) window.sessionStorage.setItem(key, value);
            },
            removeItem: key => { window.sessionStorage.removeItem(key); },
          },
        },
        global: { fetch: oauthFetch },
      },
    ));
  }
  return oauthClient;
}

async function requireGoogleProvider(): Promise<void> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), AUTH_TIMEOUT_MS);
  try {
    const response = await fetch(`${SUPABASE_URL}/auth/v1/settings`, {
      signal: controller.signal, headers: { apikey: SUPABASE_KEY },
    });
    if (!response.ok) throw new AuthError('Could not check Google sign-in. Please try again.');
    const settings = await response.json();
    if (settings?.external?.google !== true) {
      throw new AuthError(
        'Google sign-in is not enabled for this Supabase project. '
        + 'Enable the Google provider and configure its client ID and secret in Supabase.',
      );
    }
  } catch (error) {
    if (error instanceof AuthError) throw error;
    throw new AuthError('Could not reach Google sign-in. Check your connection and try again.');
  } finally {
    window.clearTimeout(timer);
  }
}

/** Start same-tab Google OAuth using the official SDK's PKCE implementation. */
export async function startGoogleSignIn(): Promise<void> {
  const requestGeneration = ++generation;
  clearOAuthData();
  if (!supabaseConfigured()) throw new AuthError('Supabase sign-in is not configured for this build.');
  if (!globalThis.crypto?.subtle || typeof globalThis.crypto.getRandomValues !== 'function') {
    throw new AuthError('Google sign-in needs a secure browser context. Open localhost or an HTTPS address.');
  }
  // The root is the configured Supabase Site URL. Never take a redirect from
  // query parameters or move between localhost and 127.0.0.1 after PKCE starts.
  const redirectTo = new URL('/', window.location.origin).href;
  try {
    await requireGoogleProvider();
    if (requestGeneration !== generation) throw new SessionChangedError();
    const marker: PendingOAuth = { startedAtMs: Date.now(), redirectTo, projectOrigin: SUPABASE_URL };
    window.sessionStorage.setItem(OAUTH_PENDING_KEY, JSON.stringify(marker));
    if (!window.sessionStorage.getItem(OAUTH_PENDING_KEY)) {
      throw new AuthError('Allow browser session storage to use Google sign-in.');
    }
    const client = await googleClient();
    if (requestGeneration !== generation) throw new SessionChangedError();
    const { data, error } = await client.auth.signInWithOAuth({
      provider: 'google', options: { redirectTo, skipBrowserRedirect: true },
    });
    if (requestGeneration !== generation) throw new SessionChangedError();
    if (error) throw new AuthError(error.message);
    if (!data.url) throw new AuthError('Supabase returned no Google sign-in URL.');
    const destination = new URL(data.url);
    if (destination.origin !== SUPABASE_URL || destination.pathname !== '/auth/v1/authorize'
      || destination.searchParams.get('provider') !== 'google'
      || destination.searchParams.get('redirect_to') !== redirectTo) {
      throw new AuthError('Supabase returned an unexpected Google sign-in URL.');
    }
    marker.flowId = data.flowId ?? undefined;
    window.sessionStorage.setItem(OAUTH_PENDING_KEY, JSON.stringify(marker));
    window.location.assign(destination.href);
  } catch (error) {
    if (requestGeneration !== generation) throw new SessionChangedError();
    clearOAuthData();
    if (error instanceof AuthError) throw error;
    throw new AuthError('Could not start Google sign-in. Allow browser session storage and try again.');
  }
}

function oauthCallbackParameters(): URLSearchParams {
  const url = new URL(window.location.href);
  const query = url.searchParams;
  const fragment = new URLSearchParams(url.hash.slice(1));
  // Supabase reports provider failures in the fragment even for PKCE flows.
  // Access/refresh tokens in a fragment are never accepted by this client.
  for (const key of ['error', 'error_code', 'error_description']) {
    if (!query.has(key) && fragment.has(key)) query.set(key, fragment.get(key) ?? '');
  }
  return query;
}

export function hasOAuthCallback(): boolean {
  const query = oauthCallbackParameters();
  const fragment = new URLSearchParams(new URL(window.location.href).hash.slice(1));
  return pendingOAuthCompletion !== null || query.has('code') || query.has('error')
    || query.has('error_code') || query.has('error_description')
    || fragment.has('access_token') || fragment.has('refresh_token');
}

function cleanOAuthCallbackUrl(): void {
  const url = new URL(window.location.href);
  for (const key of ['code', 'error', 'error_code', 'error_description', 'sb_flow_id']) {
    url.searchParams.delete(key);
  }
  const fragment = new URLSearchParams(url.hash.slice(1));
  let fragmentChanged = false;
  for (const key of ['error', 'error_code', 'error_description', 'access_token', 'refresh_token',
    'provider_token', 'provider_refresh_token', 'token_type', 'expires_at', 'expires_in']) {
    if (fragment.has(key)) {
      fragment.delete(key);
      fragmentChanged = true;
    }
  }
  if (fragmentChanged) url.hash = fragment.toString();
  window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`);
}

/** Complete only an OAuth flow initiated in this tab; ignore ordinary visits. */
export function completeOAuthSignIn(): Promise<StoredSession | null> {
  if (pendingOAuthCompletion) return pendingOAuthCompletion;
  if (!hasOAuthCallback()) return Promise.resolve(null);
  const query = oauthCallbackParameters();
  const fragment = new URLSearchParams(new URL(window.location.href).hash.slice(1));
  cleanOAuthCallbackUrl();
  let marker: PendingOAuth | null = null;
  try {
    const raw = window.sessionStorage.getItem(OAUTH_PENDING_KEY);
    marker = raw ? JSON.parse(raw) as PendingOAuth : null;
  } catch {
    // A missing/corrupt verifier is actionable, never a reason to accept a code.
  }
  if (!marker || marker.projectOrigin !== SUPABASE_URL
    || marker.redirectTo !== new URL('/', window.location.origin).href
    || !Number.isFinite(marker.startedAtMs)
    || Date.now() - marker.startedAtMs < 0
    || Date.now() - marker.startedAtMs > OAUTH_PENDING_TTL_MS) {
    clearOAuthData();
    // Keep failures shareable too: StrictMode's first bootstrap is cancelled
    // after the URL is cleaned, and the second must still receive this error.
    pendingOAuthCompletion = Promise.reject(new AuthError(
      'Google sign-in was not started in this tab or has expired. Please try again.',
    )).finally(() => { pendingOAuthCompletion = null; });
    return pendingOAuthCompletion;
  }
  const requestGeneration = ++generation;
  const expectedMarker = marker;
  pendingOAuthCompletion = (async () => {
    try {
      if (query.has('error') || query.has('error_description')) {
        const detail = query.get('error_description')?.slice(0, 240);
        throw new AuthError(query.get('error') === 'access_denied'
          ? 'Google sign-in was cancelled. Please try again.'
          : detail || 'Google sign-in failed. Please try again.');
      }
      if (fragment.has('access_token') || fragment.has('refresh_token')) {
        throw new AuthError('Google sign-in returned an unsupported response. Please try again.');
      }
      const code = query.get('code');
      if (!code) throw new AuthError('Google sign-in returned no authorization code. Please try again.');
      const flowId = query.get('sb_flow_id');
      if (flowId && flowId !== expectedMarker.flowId) {
        throw new AuthError('Google sign-in did not match the request started in this tab. Please try again.');
      }
      const client = await googleClient();
      if (requestGeneration !== generation) throw new SessionChangedError();
      const { data, error } = await client.auth.exchangeCodeForSession(
        code, expectedMarker.flowId ? { flowId: expectedMarker.flowId } : undefined,
      );
      if (requestGeneration !== generation) throw new SessionChangedError();
      if (error) throw new AuthError(`Google sign-in failed: ${error.message}`);
      if (!data.session) throw new AuthError('Google sign-in returned no session. Please try again.');
      const stored = toStoredSession(data.session as unknown as Record<string, unknown>);
      writeStoredSession(stored);
      return stored;
    } catch (error) {
      if (requestGeneration !== generation) throw new SessionChangedError();
      if (error instanceof AuthError) throw error;
      throw new AuthError('Could not complete Google sign-in. Please try again.');
    } finally {
      if (requestGeneration === generation) clearOAuthData();
    }
  })().finally(() => { pendingOAuthCompletion = null; });
  return pendingOAuthCompletion;
}
