/* @vitest-environment jsdom */

import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AuthSession, AuthStatus, StoredSession } from '../../types';

const authMocks = vi.hoisted(() => ({
  fetchSession: vi.fn(),
  signIn: vi.fn(),
  signInAsTestAdmin: vi.fn(),
  signOut: vi.fn(),
  sessionGeneration: vi.fn(() => 0),
  clearStoredSession: vi.fn(),
  startGoogleSignIn: vi.fn(),
  signUp: vi.fn(), verifySignUpOtp: vi.fn(), resendSignUpOtp: vi.fn(),
  requestEmailSignInOtp: vi.fn(), verifyEmailSignInOtp: vi.fn(),
}));

vi.mock('../../services/auth', () => ({
  AuthError: class AuthError extends Error {},
  SessionChangedError: class SessionChangedError extends Error {},
  fetchSession: authMocks.fetchSession,
  projectMismatch: () => null,
  sessionGeneration: authMocks.sessionGeneration,
  clearStoredSession: authMocks.clearStoredSession,
  startGoogleSignIn: authMocks.startGoogleSignIn,
  signUp: authMocks.signUp,
  verifySignUpOtp: authMocks.verifySignUpOtp,
  resendSignUpOtp: authMocks.resendSignUpOtp,
  requestEmailSignInOtp: authMocks.requestEmailSignInOtp,
  verifyEmailSignInOtp: authMocks.verifyEmailSignInOtp,
  signIn: authMocks.signIn,
  signInAsTestAdmin: authMocks.signInAsTestAdmin,
  signOut: authMocks.signOut,
  supabaseConfigured: () => true,
  testAdminConfigured: () => true,
}));

import { SignInPanel } from './SignInPanel';
import { AuthError } from '../../services/auth';

const STATUS: AuthStatus = {
  mode: 'supabase',
  enabled: true,
  configured: true,
  project_origin: 'https://test.supabase.co',
  sign_in: 'supabase_auth_direct',
  notes: '',
};

const SESSION: StoredSession = {
  accessToken: 'test-admin-token',
  refreshToken: 'test-admin-refresh',
  expiresAtMs: Date.now() + 3_600_000,
};

const ADMIN: AuthSession = {
  user_id: 'admin-id',
  role: 'admin',
  display_name: 'Test Admin',
  expires_at: null,
  role_source: 'crossflow_profiles',
};

let root: Root | undefined;

function renderPanel(
  onSessionChange = vi.fn(),
  onIdentityChange = vi.fn(),
  onClose = vi.fn(),
) {
  const container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
  act(() => root?.render(
    <SignInPanel
      status={STATUS}
      session={null}
      identity={null}
      onSessionChange={onSessionChange}
      onIdentityChange={onIdentityChange}
      onClose={onClose}
    />,
  ));
  return { container, onSessionChange, onIdentityChange, onClose };
}

beforeEach(() => {
  authMocks.sessionGeneration.mockReturnValue(0);
  authMocks.fetchSession.mockResolvedValue(ADMIN);
  authMocks.signInAsTestAdmin.mockResolvedValue(SESSION);
  authMocks.signOut.mockResolvedValue(undefined);
  authMocks.startGoogleSignIn.mockResolvedValue(undefined);
  authMocks.signUp.mockResolvedValue({ email: 'person@example.com', needsVerification: true, session: null });
  authMocks.verifySignUpOtp.mockResolvedValue(SESSION);
  authMocks.resendSignUpOtp.mockResolvedValue(undefined);
  authMocks.requestEmailSignInOtp.mockResolvedValue(undefined);
  authMocks.verifyEmailSignInOtp.mockResolvedValue(SESSION);
});

afterEach(() => {
  if (root) act(() => root?.unmount());
  root = undefined;
  document.body.replaceChildren();
  vi.clearAllMocks();
  vi.useRealTimers();
});

function changeInput(input: HTMLInputElement, value: string) {
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

function field(container: HTMLElement, label: string): HTMLInputElement {
  return Array.from(container.querySelectorAll('label'))
    .find(element => element.querySelector('span')?.textContent === label)!.querySelector('input')!;
}

function button(container: HTMLElement, text: string): HTMLButtonElement {
  return Array.from(container.querySelectorAll('button')).find(element => element.textContent?.trim() === text
    || element.getAttribute('aria-label') === text)!;
}

async function submit(container: HTMLElement) {
  await act(async () => container.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
}

function fillSignup(container: HTMLElement) {
  act(() => button(container, 'Create an account').click());
  changeInput(field(container, 'Name'), 'Sample Person');
  changeInput(field(container, 'Email address'), 'person@example.com');
  changeInput(field(container, 'Password'), 'example-account-password');
  changeInput(field(container, 'Confirm password'), 'example-account-password');
}

function fillEmailCodeRequest(container: HTMLElement, email = 'otp@example.com') {
  act(() => button(container, 'Email me a sign-in code').click());
  changeInput(field(container, 'Email address'), email);
}

describe('email sign-in code', () => {
  it('requests a code without a password and resolves the verified account role through the server', async () => {
    authMocks.fetchSession.mockResolvedValueOnce({ ...ADMIN, role: 'driver' });
    const { container, onSessionChange, onIdentityChange } = renderPanel();
    fillEmailCodeRequest(container);
    expect(container.querySelector('input[type="password"]')).toBeNull();
    expect(container.textContent).toContain('No password is required');
    expect(document.activeElement).toBe(field(container, 'Email address'));
    await submit(container);
    expect(authMocks.requestEmailSignInOtp).toHaveBeenCalledExactlyOnceWith('otp@example.com');
    expect(authMocks.signUp).not.toHaveBeenCalled();
    expect(authMocks.signIn).not.toHaveBeenCalled();
    expect(onSessionChange).not.toHaveBeenCalled();
    expect(authMocks.fetchSession).not.toHaveBeenCalled();
    expect(container.querySelector('h2')?.textContent).toContain('Check your email');
    const code = field(container, 'Sign-in code');
    expect(code.autocomplete).toBe('one-time-code');
    expect(code.inputMode).toBe('numeric');
    expect(code.maxLength).toBe(10);
    expect(document.activeElement).toBe(code);
    changeInput(code, '12345678');
    await submit(container);
    expect(authMocks.verifyEmailSignInOtp).toHaveBeenCalledExactlyOnceWith('otp@example.com', '12345678');
    expect(authMocks.verifySignUpOtp).not.toHaveBeenCalled();
    expect(authMocks.fetchSession).toHaveBeenCalledWith(SESSION);
    expect(onSessionChange).toHaveBeenCalledWith(SESSION);
    expect(onIdentityChange).toHaveBeenCalledWith(expect.objectContaining({ role: 'driver' }));
  });

  it('preserves email/password sign-in when returning from code mode', async () => {
    authMocks.signIn.mockResolvedValueOnce(SESSION);
    const { container } = renderPanel();
    changeInput(field(container, 'Email address'), 'password@example.com');
    changeInput(field(container, 'Password'), 'ordinary-account-password');
    act(() => button(container, 'Email me a sign-in code').click());
    expect(field(container, 'Email address').value).toBe('password@example.com');
    act(() => button(container, 'Back to sign in').click());
    expect(field(container, 'Password').value).toBe('');
    expect(field(container, 'Password').autocomplete).toBe('current-password');
    changeInput(field(container, 'Password'), 'ordinary-account-password');
    await submit(container);
    expect(authMocks.signIn).toHaveBeenCalledExactlyOnceWith('password@example.com', 'ordinary-account-password');
    expect(authMocks.requestEmailSignInOtp).not.toHaveBeenCalled();
  });

  it('keeps the email form available after delivery fails and clears its error on edit', async () => {
    authMocks.requestEmailSignInOtp.mockRejectedValueOnce(new AuthError('SMTP delivery failed. Please try again.'));
    const { container, onSessionChange, onIdentityChange } = renderPanel();
    fillEmailCodeRequest(container);
    await submit(container);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('SMTP delivery failed');
    expect(field(container, 'Email address').value).toBe('otp@example.com');
    expect(button(container, 'Send sign-in code').disabled).toBe(false);
    expect(onSessionChange).not.toHaveBeenCalled();
    expect(onIdentityChange).not.toHaveBeenCalled();
    changeInput(field(container, 'Email address'), 'retry@example.com');
    expect(container.querySelector('[role="alert"]')).toBeNull();
    await submit(container);
    expect(container.textContent).toContain('retry@example.com');
    expect(container.querySelector('h2')?.textContent).toContain('Check your email');
  });

  it('enforces its resend cooldown, preserves a code on rate-limit failure, and never calls signup resend', async () => {
    vi.useFakeTimers();
    const { container } = renderPanel();
    fillEmailCodeRequest(container);
    await submit(container);
    expect(button(container, 'Resend sign-in code in 60s').disabled).toBe(true);
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    await act(async () => button(container, 'Resend sign-in code').click());
    expect(authMocks.requestEmailSignInOtp).toHaveBeenCalledTimes(2);
    expect(authMocks.resendSignUpOtp).not.toHaveBeenCalled();
    expect(button(container, 'Resend sign-in code in 60s').disabled).toBe(true);
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    changeInput(field(container, 'Sign-in code'), '123456');
    authMocks.requestEmailSignInOtp.mockRejectedValueOnce(new AuthError('Email rate limit reached. Please wait.'));
    await act(async () => button(container, 'Resend sign-in code').click());
    expect(field(container, 'Sign-in code').value).toBe('123456');
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Email rate limit');
    expect(button(container, 'Resend sign-in code').disabled).toBe(false);
  });

  it('retains the same-email cooldown on edit while allowing a different recipient', async () => {
    vi.useFakeTimers();
    const { container } = renderPanel();
    fillEmailCodeRequest(container);
    await submit(container);
    act(() => button(container, 'Use a different email').click());
    expect(button(container, 'Send code in 60s').disabled).toBe(true);
    await submit(container);
    expect(authMocks.requestEmailSignInOtp).toHaveBeenCalledOnce();
    changeInput(field(container, 'Email address'), 'other@example.com');
    expect(button(container, 'Send sign-in code').disabled).toBe(false);
    await submit(container);
    expect(authMocks.requestEmailSignInOtp).toHaveBeenLastCalledWith('other@example.com');
    expect(container.textContent).toContain('other@example.com');
    act(() => button(container, 'Use a different email').click());
    changeInput(field(container, 'Email address'), '  OTP@EXAMPLE.COM  ');
    expect(button(container, 'Send code in 60s').disabled).toBe(true);
    await submit(container);
    expect(authMocks.requestEmailSignInOtp).toHaveBeenCalledTimes(2);
  });

  it('expires the same-email cooldown while another sign-in mode is open', async () => {
    vi.useFakeTimers();
    const { container } = renderPanel();
    fillEmailCodeRequest(container);
    await submit(container);
    act(() => button(container, 'Back to sign in').click());
    await act(async () => vi.advanceTimersByTimeAsync(61_000));
    act(() => button(container, 'Email me a sign-in code').click());
    expect(field(container, 'Email address').value).toBe('otp@example.com');
    expect(button(container, 'Send sign-in code').disabled).toBe(false);
    await submit(container);
    expect(authMocks.requestEmailSignInOtp).toHaveBeenCalledTimes(2);
  });

  it('ignores a resend completion after returning to password sign-in', async () => {
    vi.useFakeTimers();
    const { container, onSessionChange } = renderPanel();
    fillEmailCodeRequest(container);
    await submit(container);
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    let finish!: () => void;
    authMocks.requestEmailSignInOtp.mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve; }));
    await act(async () => button(container, 'Resend sign-in code').click());
    act(() => button(container, 'Back to sign in').click());
    await act(async () => finish());
    expect(container.querySelector('h2')?.textContent).toContain('Sign in to CrossFlow');
    expect(container.querySelector('[role="status"]')).toBeNull();
    expect(onSessionChange).not.toHaveBeenCalled();
    expect(authMocks.clearStoredSession).toHaveBeenCalledOnce();
  });

  it('validates code length and permits correction of expired codes', async () => {
    const { container } = renderPanel();
    fillEmailCodeRequest(container);
    await submit(container);
    changeInput(field(container, 'Sign-in code'), '123');
    await submit(container);
    expect(authMocks.verifyEmailSignInOtp).not.toHaveBeenCalled();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('6–10 digit sign-in code');
    changeInput(field(container, 'Sign-in code'), '12a3456');
    authMocks.verifyEmailSignInOtp.mockRejectedValueOnce(new AuthError('The sign-in code has expired.'));
    await submit(container);
    expect(authMocks.verifyEmailSignInOtp).toHaveBeenCalledWith('otp@example.com', '123456');
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('code has expired');
    changeInput(field(container, 'Sign-in code'), '987654');
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it.each(['back', 'cancel', 'account switch'])('ignores a late email delivery response after %s', async (reason) => {
    let finish!: () => void;
    authMocks.requestEmailSignInOtp.mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve; }));
    const { container, onSessionChange, onIdentityChange, onClose } = renderPanel();
    fillEmailCodeRequest(container);
    await submit(container);
    if (reason === 'back') act(() => button(container, 'Back to sign in').click());
    else if (reason === 'cancel') act(() => button(container, 'Close sign-in').click());
    else authMocks.sessionGeneration.mockReturnValue(1);
    await act(async () => finish());
    expect(container.querySelector('h2')?.textContent).not.toContain('Check your email');
    expect(authMocks.fetchSession).not.toHaveBeenCalled();
    expect(onSessionChange).not.toHaveBeenCalled();
    expect(onIdentityChange).not.toHaveBeenCalled();
    expect(authMocks.clearStoredSession).toHaveBeenCalledTimes(reason === 'account switch' ? 0 : 1);
    if (reason === 'cancel') expect(onClose).toHaveBeenCalledOnce();
  });

  it.each(['edit email', 'account switch', 'unmount'])('ignores a late sign-in-code verification after %s', async (reason) => {
    let finish!: (session: StoredSession) => void;
    const { container, onSessionChange, onIdentityChange } = renderPanel();
    fillEmailCodeRequest(container);
    await submit(container);
    changeInput(field(container, 'Sign-in code'), '123456');
    authMocks.verifyEmailSignInOtp.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    await submit(container);
    if (reason === 'edit email') act(() => button(container, 'Use a different email').click());
    else if (reason === 'account switch') authMocks.sessionGeneration.mockReturnValue(1);
    else { act(() => root!.unmount()); root = undefined; }
    await act(async () => finish(SESSION));
    expect(authMocks.fetchSession).not.toHaveBeenCalled();
    expect(authMocks.signOut).not.toHaveBeenCalled();
    expect(onSessionChange).not.toHaveBeenCalled();
    expect(onIdentityChange).not.toHaveBeenCalled();
  });

  it('does not carry an email-sign-in cooldown or code into password signup', async () => {
    vi.useFakeTimers();
    const { container } = renderPanel();
    fillEmailCodeRequest(container);
    await submit(container);
    changeInput(field(container, 'Sign-in code'), '123456');
    act(() => button(container, 'Back to sign in').click());
    fillSignup(container);
    await submit(container);
    expect(container.querySelector('h2')?.textContent).toContain('Verify your email');
    expect(field(container, 'Verification code').value).toBe('');
    expect(button(container, 'Resend code in 60s').disabled).toBe(true);
    expect(authMocks.signUp).toHaveBeenCalledOnce();
    expect(authMocks.verifyEmailSignInOtp).not.toHaveBeenCalled();
  });

  it('explains recovery if a verified sign-in code cannot open the backend account', async () => {
    const { container, onIdentityChange } = renderPanel();
    fillEmailCodeRequest(container);
    await submit(container);
    changeInput(field(container, 'Sign-in code'), '123456');
    authMocks.fetchSession.mockRejectedValueOnce(new AuthError('Could not reach the sign-in service.'));
    await submit(container);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Request a new sign-in code');
    expect(field(container, 'Sign-in code').value).toBe('');
    expect(authMocks.signOut).toHaveBeenCalledWith(SESSION);
    expect(onIdentityChange).toHaveBeenCalledExactlyOnceWith(null);
  });

  it('keeps the accepted email-code session when success immediately unmounts the gate', async () => {
    const onIdentityChange = vi.fn(() => { root!.unmount(); root = undefined; });
    const { container, onSessionChange } = renderPanel(vi.fn(), onIdentityChange);
    fillEmailCodeRequest(container);
    await submit(container);
    changeInput(field(container, 'Sign-in code'), '123456');
    await submit(container);
    expect(onSessionChange).toHaveBeenCalledWith(SESSION);
    expect(onIdentityChange).toHaveBeenCalledWith(ADMIN);
    expect(authMocks.clearStoredSession).not.toHaveBeenCalled();
  });
});

describe('email signup and verification', () => {
  it('validates confirmation, sends a signup code, then resolves the verified account through the server', async () => {
    const { container, onSessionChange, onIdentityChange } = renderPanel();
    fillSignup(container);
    expect(field(container, 'Password').autocomplete).toBe('new-password');
    changeInput(field(container, 'Confirm password'), 'different-confirmation');
    await submit(container);
    expect(authMocks.signUp).not.toHaveBeenCalled();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('passwords do not match');
    changeInput(field(container, 'Confirm password'), 'example-account-password');
    expect(container.querySelector('[role="alert"]')).toBeNull();
    await submit(container);
    expect(authMocks.signUp).toHaveBeenCalledWith('person@example.com', 'example-account-password', 'Sample Person');
    expect(container.querySelector('h2')?.textContent).toContain('Verify your email');
    expect(container.textContent).toContain('person@example.com');
    const code = field(container, 'Verification code');
    expect(code.autocomplete).toBe('one-time-code');
    expect(code.maxLength).toBe(10);
    expect(document.activeElement).toBe(code);
    expect(authMocks.fetchSession).not.toHaveBeenCalled();
    expect(onSessionChange).not.toHaveBeenCalled();
    changeInput(code, '123456789');
    await submit(container);
    expect(authMocks.verifySignUpOtp).toHaveBeenCalledWith('person@example.com', '123456789');
    expect(authMocks.fetchSession).toHaveBeenCalledWith(SESSION);
    expect(onSessionChange).toHaveBeenCalledWith(SESSION);
    expect(onIdentityChange).toHaveBeenCalledWith(ADMIN);
  });

  it('preserves signup fields when email delivery or its template fails', async () => {
    authMocks.signUp.mockRejectedValueOnce(new AuthError('The verification email could not be sent. Check the Supabase email template or SMTP settings.'));
    const { container } = renderPanel();
    fillSignup(container);
    await submit(container);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('SMTP settings');
    expect(container.querySelector('h2')?.textContent).toContain('Create your CrossFlow account');
    expect(field(container, 'Name').value).toBe('Sample Person');
    expect(field(container, 'Email address').value).toBe('person@example.com');
    expect(field(container, 'Password').value).toBe('example-account-password');
    expect(field(container, 'Confirm password').value).toBe('example-account-password');
    expect(button(container, 'Create account and send code').disabled).toBe(false);
    expect(button(container, 'Close sign-in').disabled).toBe(false);
  });

  it('enforces the resend cooldown and preserves the code when a resend fails', async () => {
    vi.useFakeTimers();
    const { container } = renderPanel();
    fillSignup(container);
    await submit(container);
    expect(button(container, 'Resend code in 60s').disabled).toBe(true);
    await act(async () => vi.advanceTimersByTimeAsync(59_000));
    expect(button(container, 'Resend code in 1s').disabled).toBe(true);
    expect(authMocks.resendSignUpOtp).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    await act(async () => button(container, 'Resend code').click());
    expect(authMocks.resendSignUpOtp).toHaveBeenCalledExactlyOnceWith('person@example.com');
    expect(button(container, 'Resend code in 60s').disabled).toBe(true);
    expect(container.textContent).toContain('A new verification code was requested');
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    changeInput(field(container, 'Verification code'), '123456');
    authMocks.resendSignUpOtp.mockRejectedValueOnce(new AuthError('Email rate limit reached. Please wait before trying again.'));
    await act(async () => button(container, 'Resend code').click());
    expect(field(container, 'Verification code').value).toBe('123456');
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Email rate limit reached');
    expect(button(container, 'Resend code').disabled).toBe(false);
  });

  it('lets an expired code be corrected and lets the email be edited', async () => {
    const { container } = renderPanel();
    fillSignup(container);
    await submit(container);
    changeInput(field(container, 'Verification code'), '12a3456');
    expect(field(container, 'Verification code').value).toBe('123456');
    authMocks.verifySignUpOtp.mockRejectedValueOnce(new AuthError('The verification code has expired. Request a new code.'));
    await submit(container);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('code has expired');
    changeInput(field(container, 'Verification code'), '987654');
    expect(container.querySelector('[role="alert"]')).toBeNull();
    act(() => button(container, 'Edit email').click());
    expect(field(container, 'Email address').value).toBe('person@example.com');
    expect(field(container, 'Password').value).toBe('');
    expect(field(container, 'Confirm password').value).toBe('');
    expect(container.querySelector('[role="alert"]')).toBeNull();
    act(() => button(container, 'Already have an account? Sign in').click());
    expect(field(container, 'Password').autocomplete).toBe('current-password');
  });

  it('cancels pending signup when returning to sign in', async () => {
    let finish!: (result: { email: string; needsVerification: boolean; session: null }) => void;
    authMocks.signUp.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const { container, onSessionChange } = renderPanel();
    fillSignup(container);
    await submit(container);
    act(() => button(container, 'Already have an account? Sign in').click());
    expect(authMocks.clearStoredSession).toHaveBeenCalledOnce();
    await act(async () => finish({ email: 'person@example.com', needsVerification: true, session: null }));
    expect(container.querySelector('h2')?.textContent).toContain('Sign in to CrossFlow');
    expect(onSessionChange).not.toHaveBeenCalled();
    expect(authMocks.fetchSession).not.toHaveBeenCalled();
  });

  it.each(['edit email', 'account switch'])('ignores late OTP verification after %s', async (reason) => {
    let finish!: (session: StoredSession) => void;
    const { container, onSessionChange, onIdentityChange } = renderPanel();
    fillSignup(container);
    await submit(container);
    changeInput(field(container, 'Verification code'), '123456');
    authMocks.verifySignUpOtp.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    await submit(container);
    if (reason === 'edit email') act(() => button(container, 'Edit email').click());
    else authMocks.sessionGeneration.mockReturnValue(1);
    await act(async () => finish(SESSION));
    expect(authMocks.fetchSession).not.toHaveBeenCalled();
    expect(authMocks.signOut).not.toHaveBeenCalled();
    expect(onSessionChange).not.toHaveBeenCalled();
    expect(onIdentityChange).not.toHaveBeenCalled();
  });

  it('keeps driver entry available when authentication is unavailable', () => {
    const onContinueAsGuest = vi.fn();
    const container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
    act(() => root!.render(<SignInPanel status={{ ...STATUS, configured: false }} session={null} identity={null}
      onSessionChange={vi.fn()} onIdentityChange={vi.fn()} onClose={vi.fn()} variant="page"
      onContinueAsGuest={onContinueAsGuest} />));
    expect(container.textContent).toContain('sign-in is unavailable');
    act(() => button(container, 'Continue as Driver').click());
    expect(onContinueAsGuest).toHaveBeenCalledOnce();
  });

  it('explains how to recover when a verified email cannot resolve its CrossFlow account', async () => {
    const { container, onIdentityChange } = renderPanel();
    fillSignup(container);
    await submit(container);
    changeInput(field(container, 'Verification code'), '123456');
    authMocks.fetchSession.mockRejectedValueOnce(new AuthError('Could not reach the sign-in service.'));
    await submit(container);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Your email was verified');
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Use Back to sign in');
    expect(field(container, 'Verification code').value).toBe('');
    expect(authMocks.signOut).toHaveBeenCalledWith(SESSION);
    expect(onIdentityChange).toHaveBeenCalledExactlyOnceWith(null);
  });
});

describe('Google and email sign-in choices', () => {
  it('starts Google sign-in without submitting a password or revoking an accepted redirect', async () => {
    const { container, onSessionChange, onIdentityChange } = renderPanel();
    const button = Array.from(container.querySelectorAll<HTMLButtonElement>('button'))
      .find(candidate => candidate.textContent?.includes('Continue with Google'))!;
    const email = container.querySelector<HTMLInputElement>('input[type="email"]')!;
    expect(email.placeholder).toBe('you@example.com');
    expect(email.hasAttribute('pattern')).toBe(false);
    await act(async () => button.click());
    expect(authMocks.startGoogleSignIn).toHaveBeenCalledOnce();
    expect(authMocks.signIn).not.toHaveBeenCalled();
    expect(authMocks.fetchSession).not.toHaveBeenCalled();
    expect(onSessionChange).not.toHaveBeenCalled();
    expect(onIdentityChange).not.toHaveBeenCalled();
    act(() => root!.unmount());
    root = undefined;
    expect(authMocks.clearStoredSession).not.toHaveBeenCalled();
  });

  it('explains a disabled Google provider and leaves email/password sign-in available', async () => {
    authMocks.startGoogleSignIn.mockRejectedValueOnce(new AuthError(
      'Google sign-in is not configured in Supabase. Enable the Google provider and set its client ID and client secret.',
    ));
    const { container } = renderPanel();
    await act(async () => Array.from(container.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Continue with Google'))!.click());
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('Enable the Google provider');
    expect(container.querySelector<HTMLInputElement>('input[type="email"]')!.disabled).toBe(false);
    expect(container.querySelector<HTMLInputElement>('input[type="password"]')!.disabled).toBe(false);
    expect(authMocks.clearStoredSession).not.toHaveBeenCalled();
  });
});

describe('test admin access', () => {
  it('invalidates a pending sign-in when the dialog is dismissed and ignores its late identity', async () => {
    let finish!: (identity: AuthSession) => void;
    authMocks.fetchSession.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const { container, onSessionChange, onIdentityChange, onClose } = renderPanel();
    const button = Array.from(container.querySelectorAll<HTMLButtonElement>('button'))
      .find(candidate => candidate.textContent?.includes('Sign in as test admin'))!;
    await act(async () => button.click());
    act(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
    expect(onClose).toHaveBeenCalledOnce();
    expect(authMocks.clearStoredSession).toHaveBeenCalledOnce();
    await act(async () => finish(ADMIN));
    expect(onSessionChange).not.toHaveBeenCalled();
    expect(onIdentityChange).not.toHaveBeenCalled();
  });

  it.each([false, true])('cancels a password request on unmount (newer account: %s)', async (newerAccount) => {
    let finish!: (session: StoredSession) => void;
    authMocks.signInAsTestAdmin.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const { container, onSessionChange } = renderPanel();
    await act(async () => Array.from(container.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Sign in as test admin'))!.click());
    if (newerAccount) authMocks.sessionGeneration.mockReturnValue(1);
    act(() => root!.unmount());
    root = undefined;
    await act(async () => finish(SESSION));
    expect(authMocks.clearStoredSession).toHaveBeenCalledTimes(newerAccount ? 0 : 1);
    expect(authMocks.fetchSession).not.toHaveBeenCalled();
    expect(onSessionChange).not.toHaveBeenCalled();
  });

  it('does not clear an accepted session when success immediately unmounts the gate', async () => {
    const onIdentityChange = vi.fn(() => { root!.unmount(); root = undefined; });
    const { container } = renderPanel(vi.fn(), onIdentityChange);
    await act(async () => Array.from(container.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Sign in as test admin'))!.click());
    expect(onIdentityChange).toHaveBeenCalledWith(ADMIN);
    expect(authMocks.clearStoredSession).not.toHaveBeenCalled();
  });

  it('clears rejected identity before waiting for remote revocation', async () => {
    let finish!: () => void;
    authMocks.fetchSession.mockResolvedValueOnce({ ...ADMIN, role: 'driver' });
    authMocks.signOut.mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve; }));
    const { container, onIdentityChange } = renderPanel();
    await act(async () => Array.from(container.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Sign in as test admin'))!.click());
    expect(onIdentityChange).toHaveBeenCalledExactlyOnceWith(null);
    authMocks.sessionGeneration.mockReturnValue(2);
    await act(async () => finish());
    expect(onIdentityChange).toHaveBeenCalledExactlyOnceWith(null);
  });

  it('does not revive an account or revoke a newer one after role resolution loses a race', async () => {
    let finish!: (identity: AuthSession) => void;
    authMocks.fetchSession.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const { container, onSessionChange, onIdentityChange } = renderPanel();
    const button = Array.from(container.querySelectorAll<HTMLButtonElement>('button'))
      .find(candidate => candidate.textContent?.includes('Sign in as test admin'));
    await act(async () => button?.click());
    authMocks.sessionGeneration.mockReturnValue(1);
    await act(async () => finish(ADMIN));
    expect(onSessionChange).not.toHaveBeenCalled();
    expect(onIdentityChange).not.toHaveBeenCalled();
    expect(authMocks.signOut).not.toHaveBeenCalled();
  });
  it('signs in with the shared account and accepts its server-resolved admin role', async () => {
    const { container, onSessionChange, onIdentityChange } = renderPanel();
    const button = Array.from(container.querySelectorAll<HTMLButtonElement>('button'))
      .find(candidate => candidate.textContent?.includes('Sign in as test admin'));

    expect(button).toBeDefined();
    await act(async () => button?.click());

    expect(authMocks.signInAsTestAdmin).toHaveBeenCalledOnce();
    expect(authMocks.fetchSession).toHaveBeenCalledWith(SESSION);
    expect(onSessionChange).toHaveBeenCalledWith(SESSION);
    expect(onIdentityChange).toHaveBeenCalledWith(ADMIN);
  });

  it('rejects a configured test account that the server resolves as a driver', async () => {
    authMocks.fetchSession.mockResolvedValue({ ...ADMIN, role: 'driver' });
    const { container, onSessionChange, onIdentityChange } = renderPanel();
    const button = Array.from(container.querySelectorAll<HTMLButtonElement>('button'))
      .find(candidate => candidate.textContent?.includes('Sign in as test admin'));

    await act(async () => button?.click());

    expect(authMocks.signOut).toHaveBeenCalledWith(SESSION);
    expect(onSessionChange).not.toHaveBeenCalledWith(SESSION);
    expect(onIdentityChange).not.toHaveBeenCalledWith(expect.objectContaining({ role: 'driver' }));
    expect(container.textContent).toContain('configured test account is not an admin');
  });
});

describe('sign-in dialog keyboard access', () => {
  it('traps focus, locks background scrolling, and restores the account trigger on close', () => {
    const trigger = document.createElement('button');
    document.body.append(trigger);
    trigger.focus();
    document.body.style.overflow = 'scroll';
    const { container, onClose } = renderPanel();
    const inputs = Array.from(container.querySelectorAll('input'));
    const buttons = Array.from(container.querySelectorAll('button'));
    expect(document.activeElement).toBe(inputs[0]);
    expect(document.body.style.overflow).toBe('hidden');
    act(() => buttons[0].focus());
    act(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true })));
    expect(document.activeElement).toBe(buttons[buttons.length - 1]);
    act(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })));
    expect(document.activeElement).toBe(buttons[0]);
    act(() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
    expect(onClose).toHaveBeenCalledOnce();
    act(() => root!.unmount());
    root = undefined;
    expect(document.activeElement).toBe(trigger);
    expect(document.body.style.overflow).toBe('scroll');
    document.body.style.overflow = '';
  });
});

describe('signed-in account panel', () => {
  it('shows the role the server resolved, with a right-aligned primary sign-out action', () => {
    const container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);

    act(() => root?.render(
      <SignInPanel
        status={null}
        session={{ accessToken: 'access', refreshToken: 'refresh', expiresAtMs: 1 }}
        identity={{
          user_id: 'user-1',
          display_name: 'Test User',
          role: 'driver',
          expires_at: 1,
          role_source: 'crossflow_profiles',
        }}
        onSessionChange={vi.fn()}
        onIdentityChange={vi.fn()}
        onClose={vi.fn()}
      />,
    ));

    // This identity is a driver, so the badge must say driver. Labelling every
    // signed-in user an admin would tell them they hold access the API will
    // then refuse, and the role is only meaningful because the server read it
    // from crossflow_profiles rather than trusting the client.
    expect(container.querySelector('.signin-role')?.textContent).toContain('driver');
    expect(container.querySelector('.signin-role')?.textContent).not.toContain('Admin');
    expect(container.textContent).toContain('Your workspace access is linked to your account.');
    expect(container.textContent).not.toContain('crossflow_profiles');
    expect(container.textContent).not.toContain('Re-check role');

    const signOut = Array.from(container.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Sign out'));
    expect(signOut?.classList.contains('ui-button-primary')).toBe(true);
    expect(signOut?.classList.contains('signin-panel__signout')).toBe(true);
  });
});


describe('verification recipient isolation across demo accounts', () => {
  const DRIVER_EMAIL = 'driver@school.example';
  const SHOWCASE_EMAIL = 'showcase@school.example';

  it('keeps password signup verification and resend on its recipient after using another OTP account', async () => {
    vi.useFakeTimers();
    authMocks.signUp.mockResolvedValueOnce({ email: DRIVER_EMAIL, needsVerification: true, session: null });
    const { container } = renderPanel();
    fillEmailCodeRequest(container, SHOWCASE_EMAIL);
    await submit(container);
    act(() => button(container, 'Back to sign in').click());
    fillSignup(container);
    changeInput(field(container, 'Email address'), DRIVER_EMAIL);
    await submit(container);
    expect(authMocks.signUp).toHaveBeenCalledExactlyOnceWith(DRIVER_EMAIL, 'example-account-password', 'Sample Person');
    expect(container.querySelector('strong')?.textContent).toBe(DRIVER_EMAIL);
    expect(container.textContent).toContain(`Resend destination: ${DRIVER_EMAIL}`);
    expect(container.querySelector('[role="status"]')?.textContent).toContain(DRIVER_EMAIL);
    expect(container.querySelector('[role="status"]')?.textContent).not.toContain(SHOWCASE_EMAIL);
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    await act(async () => button(container, 'Resend code').click());
    expect(authMocks.resendSignUpOtp).toHaveBeenCalledExactlyOnceWith(DRIVER_EMAIL);
    expect(container.querySelector('[role="status"]')?.textContent).toContain(DRIVER_EMAIL);
    changeInput(field(container, 'Verification code'), '123456');
    await submit(container);
    expect(authMocks.verifySignUpOtp).toHaveBeenCalledExactlyOnceWith(DRIVER_EMAIL, '123456');
    expect(authMocks.verifyEmailSignInOtp).not.toHaveBeenCalled();
  });

  it('binds passwordless resend and verification to the edited email, never the earlier email', async () => {
    vi.useFakeTimers();
    const { container } = renderPanel();
    fillEmailCodeRequest(container, DRIVER_EMAIL);
    await submit(container);
    act(() => button(container, 'Use a different email').click());
    changeInput(field(container, 'Email address'), SHOWCASE_EMAIL);
    await submit(container);
    expect(container.querySelector('strong')?.textContent).toBe(SHOWCASE_EMAIL);
    expect(container.textContent).toContain(`Resend destination: ${SHOWCASE_EMAIL}`);
    expect(container.querySelector('[role="status"]')?.textContent).toContain(SHOWCASE_EMAIL);
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    await act(async () => button(container, 'Resend sign-in code').click());
    expect(authMocks.requestEmailSignInOtp.mock.calls.map(call => call[0])).toEqual([
      DRIVER_EMAIL, SHOWCASE_EMAIL, SHOWCASE_EMAIL,
    ]);
    changeInput(field(container, 'Sign-in code'), '654321');
    await submit(container);
    expect(authMocks.verifyEmailSignInOtp).toHaveBeenCalledExactlyOnceWith(SHOWCASE_EMAIL, '654321');
    expect(authMocks.verifySignUpOtp).not.toHaveBeenCalled();
  });

  it('cannot restore an old passwordless recipient after a different signup has reached verification', async () => {
    let finish!: () => void;
    authMocks.requestEmailSignInOtp.mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve; }));
    authMocks.signUp.mockResolvedValueOnce({ email: DRIVER_EMAIL, needsVerification: true, session: null });
    const { container } = renderPanel();
    fillEmailCodeRequest(container, SHOWCASE_EMAIL);
    await submit(container);
    act(() => button(container, 'Back to sign in').click());
    fillSignup(container);
    changeInput(field(container, 'Email address'), DRIVER_EMAIL);
    await submit(container);
    await act(async () => finish());
    expect(container.querySelector('h2')?.textContent).toContain('Verify your email');
    expect(container.querySelector('strong')?.textContent).toBe(DRIVER_EMAIL);
    expect(container.querySelector('[role="status"]')?.textContent).toContain(DRIVER_EMAIL);
    expect(container.querySelector('[role="status"]')?.textContent).not.toContain(SHOWCASE_EMAIL);
    changeInput(field(container, 'Verification code'), '123456');
    await submit(container);
    expect(authMocks.verifySignUpOtp).toHaveBeenCalledExactlyOnceWith(DRIVER_EMAIL, '123456');
  });
});
