import { useCallback, useEffect, useRef, useState } from 'react';
import { History, RefreshCw, TriangleAlert } from 'lucide-react';
import { ICON_SIZE } from '../../theme/iconSizes';
import { ApiRequestError, fetchRouteRunHistory } from '../../services/api';
import type { RouteRunHistory, RouteRunSummary } from '../../types';
import './RouteRunHistoryPanel.css';

interface RouteRunHistoryPanelProps {
  /** Required: the server never returns history to an unauthenticated caller. */
  accessToken: string;
  /** Load a past run back into the planner by its dispatch code. */
  onSelectRun: (routeCode: string) => void;
  /** Highlights the run currently shown in the result pane. */
  activeRouteCode?: string | null;
}

const HISTORY_LIMIT = 20;

/** Absolute local time; a "3 hours ago" label would go stale without a tick. */
function formatRunTime(isoTimestamp: string): string {
  const parsed = new Date(isoTimestamp);
  if (Number.isNaN(parsed.getTime())) return 'Unknown time';
  return parsed.toLocaleString('en-SG', {
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  });
}

function runLabel(run: RouteRunSummary): string {
  const origin = run.origin_name?.trim();
  const destination = run.destination_name?.trim();
  if (origin && destination) return `${origin} → ${destination}`;
  // Named-corridor runs carry no free-text endpoints, so fall back to the kind
  // rather than rendering a bare arrow.
  return run.route_kind.replace(/-/g, ' ').replace(/^optimize /, '');
}

export function RouteRunHistoryPanel({
  accessToken,
  onSelectRun,
  activeRouteCode = null,
}: RouteRunHistoryPanelProps) {
  const [runs, setRuns] = useState<RouteRunSummary[]>([]);
  const [scope, setScope] = useState<'all_accounts' | 'own_account'>('own_account');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadedToken, setLoadedToken] = useState<string | null>(null);
  const [storage, setStorage] = useState<RouteRunHistory['history_storage']>();
  const requestRef = useRef(0);

  const load = useCallback(async () => {
    const requestId = ++requestRef.current;
    setLoading(true);
    setError(null);
    try {
      const history = await fetchRouteRunHistory(accessToken, { limit: HISTORY_LIMIT });
      if (requestId !== requestRef.current) return;
      setRuns(history.runs);
      setScope(history.scope);
      setStorage(history.history_storage);
      setLoadedToken(accessToken);
    } catch (caught) {
      if (requestId !== requestRef.current) return;
      setRuns([]);
      setLoadedToken(accessToken);
      // The 503 body explains which server setting is missing, so surface it
      // verbatim rather than replacing it with a generic failure.
      setError(caught instanceof ApiRequestError
        ? caught.message
        : 'Route history could not be loaded.');
    } finally {
      if (requestId === requestRef.current) setLoading(false);
    }
  }, [accessToken]);

  useEffect(() => {
    let cancelled = false;
    void Promise.resolve().then(() => { if (!cancelled) return load(); });
    return () => {
      cancelled = true;
      requestRef.current += 1;
    };
  }, [load, activeRouteCode]);

  // Never show another account's rows while its replacement request resolves.
  const currentRuns = loadedToken === accessToken ? runs : [];
  const currentScope = loadedToken === accessToken ? scope : 'own_account';
  const currentError = loadedToken === accessToken ? error : null;
  const currentLoading = loading || loadedToken !== accessToken;

  return (
    <section className="glass-panel route-run-history" aria-label="Route solver history">
      <header className="route-run-history__header">
        <h3 className="route-run-history__title">
          <History size={ICON_SIZE.large} aria-hidden="true" />
          Recent runs
        </h3>
        <button
          type="button"
          className="route-run-history__refresh"
          onClick={() => void load()}
          disabled={currentLoading}
          aria-label="Refresh route history"
        >
          <RefreshCw size={ICON_SIZE.medium} aria-hidden="true" />
        </button>
      </header>

      <p className="route-run-history__scope">
        {currentScope === 'all_accounts'
          ? 'Every account’s runs, newest first.'
          : 'Your own runs, newest first.'}
      </p>

      {loadedToken === accessToken && storage ? (
        <p className="route-run-history__scope">
          {storage.shared ? 'Shared Supabase history.' : 'History is stored on this server only.'}
          {!storage.durable ? ' Temporary storage may be cleared when the server restarts.' : ''}
        </p>
      ) : null}

      {currentError ? (
        <p className="route-run-history__error" role="alert">
          <TriangleAlert size={ICON_SIZE.medium} aria-hidden="true" /> {currentError}
        </p>
      ) : currentLoading && currentRuns.length === 0 ? (
        <p className="route-run-history__empty" role="status">Loading route history…</p>
      ) : currentRuns.length === 0 ? (
        <p className="route-run-history__empty" role="status">
          No runs recorded yet. Plan a journey and it will appear here.
        </p>
      ) : (
        <ul className="route-run-history__list">
          {currentRuns.map((run) => (
            <li key={run.route_id}>
              <button
                type="button"
                className="route-run-history__item"
                aria-current={run.route_code === activeRouteCode ? 'true' : undefined}
                onClick={() => onSelectRun(run.route_code)}
              >
                <span className="route-run-history__route">{runLabel(run)}</span>
                <span className="route-run-history__meta">
                  <code className="route-run-history__code">{run.route_code}</code>
                  {run.vehicle_type ? <span>{run.vehicle_type.replace(/_/g, ' ')}</span> : null}
                  <span>{formatRunTime(run.created_at)}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
