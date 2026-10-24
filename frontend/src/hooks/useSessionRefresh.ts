import { useEffect } from 'react';
import type { AuthSession, StoredSession } from '../types';
import {
  clearStoredSession, fetchSession, sessionGeneration, SessionChangedError,
  SessionRejectedError, validSession,
} from '../services/auth';

/** Keep open workspaces authenticated, and ignore refreshes after account changes. */
export function useSessionRefresh(
  session: StoredSession | null,
  onSessionChange: (session: StoredSession | null) => void,
  onIdentityChange: (identity: AuthSession | null) => void,
  onRecoveryRequired?: (error: Error) => void,
) {
  useEffect(() => {
    if (!session) return;
    let cancelled = false;
    const generation = sessionGeneration();
    const stale = () => cancelled || generation !== sessionGeneration();
    const timer = window.setTimeout(async () => {
      let latestCredential = session;
      try {
        const fresh = await validSession(session);
        if (stale()) return;
        latestCredential = fresh;
        const identity = await fetchSession(fresh);
        if (stale()) return;
        onSessionChange(fresh);
        onIdentityChange(identity);
      } catch (error) {
        if (stale() || error instanceof SessionChangedError) return;
        if (error instanceof SessionRejectedError) {
          clearStoredSession();
          onSessionChange(null);
        } else {
          // A restarting API cannot revoke a Supabase credential. Preserve
          // a rotated refresh pair, but withdraw the role until revalidated.
          onSessionChange(latestCredential);
        }
        onIdentityChange(null);
        if (!(error instanceof SessionRejectedError)) {
          onRecoveryRequired?.(error instanceof Error ? error : new Error('The sign-in service is unavailable.'));
        }
      }
    }, Math.max(0, session.expiresAtMs - Date.now() - 60_000));
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [session, onSessionChange, onIdentityChange, onRecoveryRequired]);
}
