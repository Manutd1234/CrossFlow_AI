import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { ArrowRight, CarFront, LogIn, LogOut, Mail, Route, ShieldCheck, TriangleAlert, X } from 'lucide-react';
import { ICON_SIZE } from '../../theme/iconSizes';
import type { AuthSession, AuthStatus, StoredSession } from '../../types';
import {
  AuthError,
  SessionChangedError,
  clearStoredSession,
  fetchSession,
  projectMismatch,
  requestEmailSignInOtp,
  resendSignUpOtp,
  sessionGeneration,
  signIn,
  signInAsTestAdmin,
  signOut,
  signUp,
  startGoogleSignIn,
  supabaseConfigured,
  testAdminConfigured,
  verifyEmailSignInOtp,
  verifySignUpOtp,
} from '../../services/auth';
import './SignInPanel.css';

interface SignInPanelProps {
  status: AuthStatus | null;
  session: StoredSession | null;
  identity: AuthSession | null;
  onSessionChange: (session: StoredSession | null) => void;
  onIdentityChange: (identity: AuthSession | null) => void;
  onClose: () => void;
  /** 'page' is the full-screen gate shown before the workspace loads. */
  variant?: 'modal' | 'page';
  /** Gate only: proceed without an account. */
  onContinueAsGuest?: () => void;
  /** Provider callback failures resolved before this gate is mounted. */
  initialError?: string | null;
  onRetrySession?: () => void;
}

type AccountStage = 'signin' | 'signup' | 'verify' | 'email-code' | 'email-code-verify';

export function SignInPanel({
  status,
  session,
  identity,
  onSessionChange,
  onIdentityChange,
  onClose,
  variant = 'modal',
  onContinueAsGuest,
  initialError = null,
  onRetrySession,
}: SignInPanelProps) {
  const isGate = variant === 'page';
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [accountStage, setAccountStage] = useState<AccountStage>('signin');
  const [displayName, setDisplayName] = useState('');
  const [passwordConfirmation, setPasswordConfirmation] = useState('');
  const [verificationEmail, setVerificationEmail] = useState('');
  const [verificationCode, setVerificationCode] = useState('');
  const [notice, setNotice] = useState<string | null>(null);
  const [resendAvailableAt, setResendAvailableAt] = useState(0);
  const [resendClock, setResendClock] = useState(() => Date.now());
  const [emailCodeEmail, setEmailCodeEmail] = useState('');
  const [emailCodeRecipient, setEmailCodeRecipient] = useState('');
  const [emailCode, setEmailCode] = useState('');
  const [emailCodeCooldowns, setEmailCodeCooldowns] = useState<Record<string, number>>({});
  const [emailCodeClock, setEmailCodeClock] = useState(() => Date.now());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(initialError);
  const emailId = useId();
  const passwordId = useId();
  const nameId = useId();
  const confirmationId = useId();
  const codeId = useId();
  const titleId = useId();
  const emailRef = useRef<HTMLInputElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const codeRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const mountedRef = useRef(false);
  const attemptRef = useRef<{ generation: number; pending: boolean } | null>(null);
  const isEmailCodeVerification = accountStage === 'email-code-verify';
  const emailCodeLookupEmail = (isEmailCodeVerification ? emailCodeRecipient : emailCodeEmail).trim().toLowerCase();
  const emailCodeAvailableAt = emailCodeCooldowns[emailCodeLookupEmail] ?? 0;

  const cancelPendingAuthentication = useCallback(() => {
    const attempt = attemptRef.current;
    if (!attempt?.pending) return;
    attempt.pending = false;
    attemptRef.current = null;
    // Only invalidate this dialog's request. Another account may have signed
    // in elsewhere while its response was waiting for the network.
    if (attempt.generation === sessionGeneration()) clearStoredSession();
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      cancelPendingAuthentication();
    };
  }, [cancelPendingAuthentication]);

  const requestClose = useCallback(() => {
    cancelPendingAuthentication();
    onClose();
  }, [cancelPendingAuthentication, onClose]);

  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const dialog = dialogRef.current;
    const focusable = () => Array.from(dialog?.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ) ?? []);
    (emailRef.current ?? focusable()[0] ?? dialog)?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !isGate) {
        event.preventDefault();
        requestClose();
      }
      if (event.key !== 'Tab') return;
      const elements = focusable();
      const first = elements[0];
      const last = elements[elements.length - 1];
      const outside = !dialog?.contains(document.activeElement);
      if (!first) {
        event.preventDefault();
        dialog?.focus();
      } else if (event.shiftKey && (document.activeElement === first || outside)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || outside)) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.body.style.overflow = previousOverflow;
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, [isGate, requestClose]);

  useEffect(() => {
    if (accountStage === 'verify' || accountStage === 'email-code-verify') codeRef.current?.focus();
    if (accountStage === 'signup') nameRef.current?.focus();
    if (accountStage === 'signin' || accountStage === 'email-code') emailRef.current?.focus();
  }, [accountStage]);

  useEffect(() => {
    if (accountStage !== 'verify' || resendAvailableAt <= Date.now()) return;
    const timer = window.setInterval(() => {
      const now = Date.now();
      setResendClock(now);
      if (now >= resendAvailableAt) window.clearInterval(timer);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [accountStage, resendAvailableAt]);

  useEffect(() => {
    if (!accountStage.startsWith('email-code') || emailCodeAvailableAt <= Date.now()) return;
    const timer = window.setInterval(() => {
      const now = Date.now();
      setEmailCodeClock(now);
      if (now >= emailCodeAvailableAt) window.clearInterval(timer);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [accountStage, emailCodeAvailableAt]);

  const resendSeconds = Math.max(0, Math.ceil((resendAvailableAt - resendClock) / 1000));
  const beginResendCooldown = () => {
    const now = Date.now();
    setResendClock(now);
    setResendAvailableAt(now + 60_000);
  };

  const emailCodeSeconds = Math.max(0, Math.ceil((emailCodeAvailableAt - emailCodeClock) / 1000));
  const emailCodeRequestCoolingDown = emailCodeSeconds > 0;
  const isVerificationStage = accountStage === 'verify' || isEmailCodeVerification;
  const activeCode = isEmailCodeVerification ? emailCode : verificationCode;

  const changeAccountStage = (stage: 'signin' | 'signup' | 'email-code') => {
    cancelPendingAuthentication();
    setBusy(false);
    setAccountStage(stage);
    setVerificationCode('');
    setEmailCode('');
    setEmailCodeClock(Date.now());
    if (stage === 'email-code' && accountStage === 'signin' && email.trim()) setEmailCodeEmail(email.trim());
    setPassword('');
    setPasswordConfirmation('');
    setError(null);
    setNotice(null);
  };

  // The server decides whether signing in is even possible. Showing a form
  // when Supabase is unreachable would invite a failure the user cannot fix,
  // while the public corridor views keep working regardless.
  const unavailableReason = !supabaseConfigured()
    ? 'Sign-in is not configured in this build. Set VITE_SUPABASE_URL and VITE_SUPABASE_PUBLISHABLE_KEY.'
    : status && !status.enabled
      ? 'Sign-in is turned off on this server (CROSSFLOW_AUTH_MODE is not "supabase").'
      : status && !status.configured
        ? 'The server cannot reach Supabase, so sign-in is unavailable right now.'
        : projectMismatch(status);

  const authenticate = async (
    requestSession: () => Promise<StoredSession | null>,
    requireAdmin = false,
    onVerificationRequired?: () => void,
  ) => {
    if (busy) return;
    setError(null);
    setNotice(null);
    setBusy(true);
    const attempt = { generation: sessionGeneration(), pending: true };
    attemptRef.current = attempt;
    const stale = () => !mountedRef.current || attemptRef.current !== attempt
      || attempt.generation !== sessionGeneration();
    let stored: StoredSession | null = null;
    try {
      const request = requestSession();
      // signIn increments its generation synchronously before its first await.
      attempt.generation = sessionGeneration();
      stored = await request;
      if (stale()) return;
      if (!stored) {
        if (!onVerificationRequired) throw new AuthError('Supabase returned no authenticated session. Please try again.');
        attempt.pending = false;
        attemptRef.current = null;
        setBusy(false);
        onVerificationRequired();
        return;
      }
      const resolved = await fetchSession(stored);
      if (stale()) return;
      if (requireAdmin && resolved.role.toLowerCase() !== 'admin') {
        throw new AuthError(
          'The configured test account is not an admin. Promote its CrossFlow profile first.',
        );
      }
      // Accept the credential before callbacks unmount this gate or dialog.
      attempt.pending = false;
      attemptRef.current = null;
      setBusy(false);
      setPassword('');
      setPasswordConfirmation('');
      setVerificationCode('');
      setEmailCode('');
      onSessionChange(stored);
      onIdentityChange(resolved);
    } catch (caught) {
      if (caught instanceof SessionChangedError || stale()) return;
      attempt.pending = false;
      // signIn persists before the API resolves the role. Clear that credential
      // if role verification fails so a broken demo account cannot reappear on
      // the next page load as though it had been accepted.
      const message = caught instanceof AuthError
        ? caught.message
        : 'Sign-in failed. Please try again.';
      if (accountStage === 'email-code-verify' && stored) {
        setEmailCode('');
        setError(`The code was verified, but your account could not be opened. Request a new sign-in code and try again. ${message}`);
      } else if (accountStage === 'verify' && stored) {
        setVerificationCode('');
        setError(`Your email was verified, but your account could not be opened. Use Back to sign in to try again. ${message}`);
      } else {
        setError(message);
      }
      onSessionChange(null);
      onIdentityChange(null);
      // Clear rejected identity immediately; never clear a newer account after
      // waiting for the old account's remote token revocation.
      if (stored) await signOut(stored);
    } finally {
      if (attemptRef.current === attempt) {
        attemptRef.current = null;
        if (mountedRef.current) setBusy(false);
      }
    }
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (accountStage === 'email-code') {
      await handleEmailCodeRequest();
      return;
    }
    if (accountStage === 'email-code-verify') {
      if (!/^[0-9]{6,10}$/.test(emailCode)) {
        setError('Enter the 6–10 digit sign-in code from your email.');
        return;
      }
      await authenticate(() => verifyEmailSignInOtp(emailCodeRecipient, emailCode));
      return;
    }
    if (accountStage === 'verify') {
      if (!/^[0-9]{6,10}$/.test(verificationCode)) {
        setError('Enter the 6–10 digit verification code from your email.');
        return;
      }
      await authenticate(() => verifySignUpOtp(verificationEmail, verificationCode));
      return;
    }
    if (accountStage === 'signup') {
      if (!displayName.trim()) {
        setError('Enter your name to create your account.');
        return;
      }
      if (password !== passwordConfirmation) {
        setError('The passwords do not match. Please enter them again.');
        return;
      }
      if (password.length < 8) {
        setError('Use at least 8 characters for your new account password.');
        return;
      }
      let sentTo = email.trim();
      await authenticate(async () => {
        const result = await signUp(email, password, displayName);
        sentTo = result.email;
        return result.session;
      }, false, () => {
        setVerificationEmail(sentTo);
        setAccountStage('verify');
        setPassword('');
        setPasswordConfirmation('');
        setVerificationCode('');
        setNotice(`A verification code was requested for ${sentTo}. Check that inbox and spam folder.`);
        beginResendCooldown();
      });
      return;
    }
    await authenticate(() => signIn(email, password));
  };

  const deliverCode = async (
    requestCode: () => Promise<void>,
    onDelivered: () => void,
    failureMessage: string,
  ) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    const attempt = { generation: sessionGeneration(), pending: true };
    attemptRef.current = attempt;
    try {
      const request = requestCode();
      attempt.generation = sessionGeneration();
      await request;
      if (!mountedRef.current || attemptRef.current !== attempt
        || attempt.generation !== sessionGeneration()) return;
      onDelivered();
    } catch (caught) {
      if (!mountedRef.current || attemptRef.current !== attempt
        || attempt.generation !== sessionGeneration() || caught instanceof SessionChangedError) return;
      setError(caught instanceof AuthError ? caught.message : failureMessage);
    } finally {
      if (attemptRef.current === attempt) {
        attempt.pending = false;
        attemptRef.current = null;
        if (mountedRef.current) setBusy(false);
      }
    }
  };

  const handleResend = async () => {
    if (resendSeconds > 0) return;
    await deliverCode(() => resendSignUpOtp(verificationEmail), () => {
      setVerificationCode('');
      setNotice(`A new verification code was requested for ${verificationEmail}. Check that inbox and spam folder.`);
      beginResendCooldown();
    }, 'The verification email could not be resent. Please try again.');
  };

  const handleEmailCodeRequest = async (resend = false) => {
    const recipient = (resend ? emailCodeRecipient : emailCodeEmail).trim();
    if (!recipient) {
      setError('Enter your email address to request a sign-in code.');
      return;
    }
    if ((emailCodeCooldowns[recipient.toLowerCase()] ?? 0) > Date.now()) return;
    await deliverCode(() => requestEmailSignInOtp(recipient), () => {
      setEmailCodeRecipient(recipient);
      setEmailCode('');
      setAccountStage('email-code-verify');
      setNotice(resend
        ? `A new sign-in code was requested for ${recipient}. Check that inbox and spam folder.`
        : `A sign-in code was requested for ${recipient}. Check that inbox and spam folder.`);
      const now = Date.now();
      setEmailCodeClock(now);
      setEmailCodeCooldowns(current => ({ ...current, [recipient.toLowerCase()]: now + 60_000 }));
    }, 'The sign-in code could not be sent. Please try again.');
  };

  const handleTestAdmin = async () => {
    await authenticate(signInAsTestAdmin, true);
  };

  const handleGoogleSignIn = async () => {
    if (busy) return;
    setError(null);
    setBusy(true);
    const attempt = { generation: sessionGeneration(), pending: true };
    attemptRef.current = attempt;
    try {
      const request = startGoogleSignIn();
      attempt.generation = sessionGeneration();
      await request;
      if (!mountedRef.current || attemptRef.current !== attempt
        || attempt.generation !== sessionGeneration()) return;
      // A successful provider redirect must retain the PKCE verifier when
      // browser navigation removes this dialog.
      attempt.pending = false;
      attemptRef.current = null;
    } catch (caught) {
      if (!mountedRef.current || attemptRef.current !== attempt
        || attempt.generation !== sessionGeneration()
        || caught instanceof SessionChangedError) return;
      setError(caught instanceof AuthError ? caught.message
        : 'Google sign-in could not be started. Try again or sign in with your email and password.');
    } finally {
      if (attemptRef.current === attempt) {
        attempt.pending = false;
        attemptRef.current = null;
        if (mountedRef.current) setBusy(false);
      }
    }
  };

  const handleGuest = () => {
    cancelPendingAuthentication();
    onContinueAsGuest?.();
  };

  const handleSignOut = async () => {
    setBusy(true);
    onSessionChange(null);
    onIdentityChange(null);
    await signOut(session);
    if (!mountedRef.current) return;
    setBusy(false);
    setEmail('');
    setPassword('');
  };

  return (
    <div
      className={isGate ? 'signin-gate' : 'signin-backdrop'}
      role="presentation"
      onMouseDown={(event) => {
        if (!isGate && event.target === event.currentTarget) requestClose();
      }}
    >
      <div className={isGate ? 'signin-layout' : 'signin-layout signin-layout--modal'}>
        {isGate ? (
          <aside className="signin-welcome" aria-label="Welcome to CrossFlow">
            <div className="signin-welcome__brand"><span className="signin-brand-mark"><Route size={25} aria-hidden="true" /></span><span>CrossFlow</span></div>
            <div className="signin-welcome__copy">
              <p className="signin-welcome__eyebrow">Batam &amp; Singapore, connected</p>
              <h1>A little less rush.<br /><em>A better journey.</em></h1>
              <p>Find your way across the city and the strait, all in one place.</p>
            </div>
            <div className="signin-journey" aria-hidden="true">
              <svg viewBox="0 0 360 180" fill="none">
                <path d="M-30 130C70 20 130 185 235 75S370 70 400 5" stroke="currentColor" strokeOpacity=".12" strokeWidth="54" />
                <path d="M54 126C125 139 114 40 195 65S246 123 306 45" stroke="currentColor" strokeWidth="1.5" strokeDasharray="5 7" />
                <circle cx="54" cy="126" r="10" fill="var(--sand-soft)" fillOpacity=".12" />
                <circle cx="54" cy="126" r="4" fill="var(--sand-soft)" />
                <circle cx="306" cy="45" r="10" fill="var(--sand-soft)" fillOpacity=".12" />
                <circle cx="306" cy="45" r="4" fill="var(--sand-soft)" />
                <text x="39" y="159" fill="currentColor">Batam</text><text x="252" y="20" fill="currentColor">Singapore</text>
              </svg>
            </div>
            <p className="signin-welcome__footer">Your route. Your ferry. Your next move.</p>
          </aside>
        ) : null}
      <div
        ref={dialogRef}
        className="glass-panel signin-panel"
        role={isGate ? 'region' : 'dialog'}
        aria-modal={isGate ? undefined : true}
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        {!isGate ? (
          <div className="signin-modal-brand">
            <div className="signin-modal-brand__name">
              <span className="signin-modal-brand__mark"><Route size={20} aria-hidden="true" /></span>
              <span>CrossFlow</span>
            </div>
            <button type="button" className="signin-panel__close" aria-label="Close sign-in" onClick={requestClose}>
              <X size={19} aria-hidden="true" />
            </button>
          </div>
        ) : null}
        {isGate ? (
          <div className="signin-gate__brand"><Route size={19} aria-hidden="true" /><span>CrossFlow</span><span className="signin-gate__region">Batam–Singapore</span></div>
        ) : null}
        <header className="signin-panel__header">
        <p className="signin-panel__eyebrow">{identity ? 'Your account' : isVerificationStage ? 'One last step' : accountStage === 'signup' ? 'Join the journey' : 'Welcome back'}</p>
        <h2 id={titleId} className="signin-panel__title">
          <ShieldCheck aria-hidden="true" size={ICON_SIZE.large} color="var(--accent-cyan)" />
          {identity ? 'Signed in' : isEmailCodeVerification ? 'Check your email'
            : accountStage === 'email-code' ? 'Sign in with an email code'
              : accountStage === 'verify' ? 'Verify your email' : accountStage === 'signup'
                ? 'Create your CrossFlow account' : 'Sign in to CrossFlow'}
        </h2>
        <p className="signin-panel__intro">{identity ? 'You’re ready to continue your journey.'
          : isVerificationStage ? 'Enter your code to continue securely.'
            : accountStage === 'signup' ? 'A few details, and you’re on your way.'
              : accountStage === 'email-code' ? 'Sign in with a code sent to your inbox.'
                : 'Choose how you’d like to continue.'}</p>
        </header>

        {error ? (
          <p className="signin-panel__error" role="alert">
            <TriangleAlert aria-hidden="true" size={ICON_SIZE.medium} /> {error}
          </p>
        ) : null}

        {onRetrySession ? (
          <button type="button" className="signin-panel__secondary" onClick={onRetrySession} disabled={busy}>
            Retry saved session
          </button>
        ) : null}

        {notice ? <p className="signin-panel__notice" role="status">{notice}</p> : null}

        {identity ? (
          <>
            <dl className="signin-identity">
              <dt>Signed in as</dt>
              <dd>{identity.display_name || identity.user_id}</dd>
              <dt>Role</dt>
              <dd>
                <span className={`badge signin-role signin-role--${identity.role.toLowerCase()}`}>
                  {identity.role}
                </span>
              </dd>
            </dl>
            <p className="signin-panel__note">Your workspace access is linked to your account.</p>
            <div className="signin-panel__actions">
              <button
                type="button"
                className="ui-button-primary signin-panel__signout"
                onClick={handleSignOut}
                disabled={busy}
              >
                <LogOut aria-hidden="true" size={ICON_SIZE.medium} /> Sign out
              </button>
            </div>
          </>
        ) : unavailableReason ? (
          <>
            <p className="signin-panel__error" role="status">
              <TriangleAlert aria-hidden="true" size={ICON_SIZE.medium} /> {unavailableReason}
            </p>

          </>
        ) : isVerificationStage ? (
          <form className="signin-form" onSubmit={handleSubmit}>
            <p className="signin-panel__note">
              A {isEmailCodeVerification ? 'sign-in' : 'verification'} code was requested for{' '}
              <strong>{isEmailCodeVerification ? emailCodeRecipient : verificationEmail}</strong>.
              {' '}Enter the code from that inbox{isEmailCodeVerification ? '. No password is required.' : ' to finish creating your account.'}
            </p>
            <label className="signin-field" htmlFor={codeId}>
              <span>{isEmailCodeVerification ? 'Sign-in code' : 'Verification code'}</span>
              <input
                id={codeId}
                ref={codeRef}
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                pattern="[0-9]{6,10}"
                minLength={6}
                maxLength={10}
                required
                value={activeCode}
                onChange={(event) => {
                  const value = event.target.value.replace(/[^0-9]/g, '').slice(0, 10);
                  if (isEmailCodeVerification) setEmailCode(value);
                  else setVerificationCode(value);
                  setError(null);
                }}
                disabled={busy}
              />
            </label>
            <div className="signin-panel__actions">
              <button type="button" className="signin-panel__secondary" onClick={() => changeAccountStage(isEmailCodeVerification ? 'email-code' : 'signup')}>
                {isEmailCodeVerification ? 'Use a different email' : 'Edit email'}
              </button>
              <button type="submit" className="ui-button-primary" disabled={busy || !/^[0-9]{6,10}$/.test(activeCode)}>
                {busy ? 'Verifying…' : isEmailCodeVerification ? 'Verify and sign in' : 'Verify email'}
              </button>
            </div>
            <p className="signin-panel__note">
              Resend destination: <strong>{isEmailCodeVerification ? emailCodeRecipient : verificationEmail}</strong>
            </p>
            <button type="button" className="signin-panel__link"
              aria-label={`Resend ${isEmailCodeVerification ? 'sign-in' : 'verification'} code to ${isEmailCodeVerification ? emailCodeRecipient : verificationEmail}`}
              disabled={busy || (isEmailCodeVerification ? emailCodeSeconds : resendSeconds) > 0}
              onClick={isEmailCodeVerification ? () => handleEmailCodeRequest(true) : handleResend}>
              {isEmailCodeVerification
                ? emailCodeSeconds > 0 ? `Resend sign-in code in ${emailCodeSeconds}s` : 'Resend sign-in code'
                : resendSeconds > 0 ? `Resend code in ${resendSeconds}s` : 'Resend code'}
            </button>
            <div className="signin-panel__verification-footer">
              <button type="button" className="signin-panel__link" onClick={() => changeAccountStage('signin')}>
                Back to sign in
              </button>

            </div>
          </form>
        ) : accountStage === 'email-code' ? (
          <form className="signin-form" onSubmit={handleSubmit}>
            <p className="signin-panel__note">
              Use any email address, including Gmail. No password is required.
            </p>
            <label className="signin-field" htmlFor={emailId}>
              <span>Email address</span>
              <input id={emailId} ref={emailRef} type="email" autoComplete="email" required
                placeholder="you@example.com" value={emailCodeEmail}
                onChange={(event) => { setEmailCodeEmail(event.target.value); setEmailCodeClock(Date.now()); setError(null); }} disabled={busy} />
            </label>
            <div className="signin-panel__actions">
              <button type="button" className="signin-panel__secondary" onClick={() => changeAccountStage('signin')}>
                Back to sign in
              </button>
              <button type="submit" className="ui-button-primary" disabled={busy || emailCodeRequestCoolingDown}>
                {busy ? 'Sending code…' : emailCodeRequestCoolingDown ? `Send code in ${emailCodeSeconds}s` : 'Send sign-in code'}
              </button>
            </div>

          </form>
        ) : (
          <form className="signin-form" onSubmit={handleSubmit}>
            <section className="signin-method" aria-label="Google sign-in">
            <p className="signin-section-label">Google account</p>
            <button
              type="button"
              className="signin-provider signin-provider--google"
              onClick={handleGoogleSignIn}
              disabled={busy}
            >
              <svg className="signin-provider__google-mark" viewBox="0 0 24 24" aria-hidden="true">
                <path fill="#4285F4" d="M21.6 12.23c0-.71-.06-1.39-.18-2.05H12v3.88h5.38a4.6 4.6 0 0 1-2 3.02v2.51h3.24c1.9-1.75 2.98-4.33 2.98-7.36Z" />
                <path fill="#34A853" d="M12 22c2.7 0 4.96-.9 6.62-2.41l-3.24-2.51c-.9.6-2.05.96-3.38.96-2.6 0-4.8-1.76-5.59-4.12H3.07v2.59A10 10 0 0 0 12 22Z" />
                <path fill="#FBBC05" d="M6.41 13.92a6 6 0 0 1 0-3.84V7.49H3.07a10 10 0 0 0 0 9.02l3.34-2.59Z" />
                <path fill="#EA4335" d="M12 5.96c1.47 0 2.79.5 3.82 1.5l2.87-2.87A9.6 9.6 0 0 0 12 2a10 10 0 0 0-8.93 5.49l3.34 2.59C7.2 7.72 9.4 5.96 12 5.96Z" />
              </svg>
              Continue with Google
            </button>
            </section>
            <div className="signin-panel__separator"><span>or continue with email</span></div>
            <section className="signin-credentials" aria-label={accountStage === 'signup' ? 'Account details' : 'Email and password'}>
            <div className="signin-credentials__heading">
            <h3 className="signin-section-label">{accountStage === 'signup' ? 'Your account details' : 'Email & password'}</h3>
            </div>
            {accountStage === 'signup' ? (
              <label className="signin-field" htmlFor={nameId}>
                <span>Name</span>
                <input id={nameId} ref={nameRef} type="text" autoComplete="name" required maxLength={120}
                  value={displayName} onChange={(event) => { setDisplayName(event.target.value); setError(null); }} disabled={busy} />
              </label>
            ) : null}

            <label className="signin-field" htmlFor={emailId}>
              <span>Email address</span>
              <input
                id={emailId}
                ref={emailRef}
                type="email"
                autoComplete="username"
                placeholder="you@example.com"
                required
                value={email}
                onChange={(event) => { setEmail(event.target.value); setError(null); }}
                disabled={busy}
              />
            </label>

            <label className="signin-field" htmlFor={passwordId}>
              <span>Password</span>
              <input
                id={passwordId}
                type="password"
                autoComplete={accountStage === 'signup' ? 'new-password' : 'current-password'}
                placeholder={accountStage === 'signup' ? 'Create a password' : 'Your CrossFlow password'}
                minLength={accountStage === 'signup' ? 8 : undefined}
                required
                value={password}
                onChange={(event) => { setPassword(event.target.value); setError(null); }}
                disabled={busy}
              />
            </label>

            {accountStage === 'signup' ? (
              <>
                <p className="signin-panel__note">Use at least 8 characters for your new account password.</p>
                <label className="signin-field" htmlFor={confirmationId}>
                  <span>Confirm password</span>
                  <input id={confirmationId} type="password" autoComplete="new-password" required
                    value={passwordConfirmation} onChange={(event) => { setPasswordConfirmation(event.target.value); setError(null); }} disabled={busy} />
                </label>
              </>
            ) : null}


            <div className="signin-panel__actions signin-panel__actions--primary">
              <button type="submit" className="ui-button-primary" disabled={busy}>
                <LogIn aria-hidden="true" size={ICON_SIZE.medium} />
                {busy ? accountStage === 'signup' ? 'Sending code…' : 'Signing in…'
                  : accountStage === 'signup' ? 'Create account and send code' : 'Sign in'}
              </button>
            </div>

            </section>
            {accountStage === 'signin' ? (
              <section className="signin-code-option" aria-label="Password-free sign-in">
                <span className="signin-code-option__icon"><Mail size={19} aria-hidden="true" /></span>
                <div className="signin-code-option__copy">
                <h3>Email sign-in code</h3>
                <p>Any email address, including Gmail.</p>
                <button type="button" className="signin-panel__link" onClick={() => changeAccountStage('email-code')}>
                  Email me a sign-in code <ArrowRight size={14} aria-hidden="true" />
                </button>
                </div>
              </section>
            ) : null}
            <div className="signin-account-switch">
              <span>{accountStage === 'signup' ? 'Already a member?' : 'New to CrossFlow?'}</span>
              <button type="button" className="signin-panel__link" onClick={() => changeAccountStage(accountStage === 'signup' ? 'signin' : 'signup')}>
                {accountStage === 'signup' ? 'Already have an account? Sign in' : 'Create an account'}
              </button>
            </div>

            {accountStage === 'signin' && testAdminConfigured() ? (
              <div className="signin-test-admin">
                <p>
                  Want to explore the full workspace? Use the shared test admin
                  for this demo environment.
                </p>
                <button
                  type="button"
                  className="signin-provider signin-provider--test-admin"
                  onClick={handleTestAdmin}
                  disabled={busy}
                >
                  <ShieldCheck aria-hidden="true" size={ICON_SIZE.large} />
                  {busy ? 'Signing in…' : 'Sign in as test admin'}
                </button>
              </div>
            ) : null}

          </form>
        )}
        {isGate && !identity && onContinueAsGuest ? (
          <section className="signin-guest" aria-label="Guest access">
            <CarFront size={22} aria-hidden="true" />
            <div className="signin-guest__copy"><h3>Just exploring?</h3><p>View congestion and assigned routes without an account.</p>
              <button type="button" className="signin-guest__button" onClick={handleGuest}>Continue as Driver <ArrowRight size={16} aria-hidden="true" /></button>
            </div>
          </section>
        ) : null}
      </div>
      </div>
    </div>
  );
}
