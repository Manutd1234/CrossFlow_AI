import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { identityKey, isDriverView } from './utils/access';
import { useSessionRefresh } from './hooks/useSessionRefresh';
import { Header } from './components/app-shell/Header';
import type { AppTab } from './components/app-shell/Navigation';

import {
  AuthSession, AuthStatus, Corridor, CorridorRoute, DataSource, FerrySchedule, Fetched, LiveTrafficData,
  FerryRefreshReport, FerryTimetableMetadata, OperationsSummary, PortStatus, Provenance,
  RouteLocation, RouteOptimizationResult, StoredSession, VehicleType,
} from './types';
import {
  AuthError, SessionRejectedError, clearStoredSession, completeOAuthSignIn, fetchAuthStatus, fetchSession,
  hasOAuthCallback, readStoredSession, sessionGeneration, supabaseConfigured, validSession,
} from './services/auth';
import {
  fetchCorridorRoutes, fetchCorridors, fetchFerries, fetchOperationsSummary,
  fetchLiveTraffic, fetchPortIntelligence, fetchRouteLocations,
  refreshOfficialFerrySources, scheduleInformedPortSeed,
} from './services/api';
import {
  INITIAL_CORRIDORS, INITIAL_FERRIES,
  PUBLISHED_FERRY_TIMETABLE_METADATA, ROUTE_LOCATIONS,
} from './data/mockData';
import { UNAVAILABLE_OPERATIONS } from './data/unavailableOperations';

const POLL_INTERVAL_MS = 30_000;
const OPERATIONS_POLL_INTERVAL_MS = 10_000;
const OPERATIONS_EXPIRY_MARGIN_MS = 2_000;
const OPERATIONS_MIN_POLL_INTERVAL_MS = 1_000;
const DRIVER_TABS: readonly AppTab[] = ['map', 'route'];

function operationsRefreshDelay(snapshot: Fetched<OperationsSummary> | null): number {
  const deadline = snapshot?.data.camera_traffic_summary?.valid_until;
  const remaining = deadline ? Date.parse(deadline) - Date.now() : NaN;
  // A frame may already be old when its numeric summary reaches the browser.
  // Check before that deadline, then back off normally once it has expired.
  return remaining > 0
    ? Math.min(OPERATIONS_POLL_INTERVAL_MS,
      Math.max(OPERATIONS_MIN_POLL_INTERVAL_MS, remaining - OPERATIONS_EXPIRY_MARGIN_MS))
    : OPERATIONS_POLL_INTERVAL_MS;
}

function rememberGuest(value?: boolean): boolean {
  try {
    if (value === true) window.sessionStorage.setItem('crossflow.guest', '1');
    if (value === false) window.sessionStorage.removeItem('crossflow.guest');
    return window.sessionStorage.getItem('crossflow.guest') === '1';
  } catch {
    return false;
  }
}

const MapView = lazy(() => import('./components/corridor-map/MapView').then(module => ({ default: module.MapView })));
const RouteOptimizer = lazy(() => import('./components/route-planner/RouteOptimizer').then(module => ({ default: module.RouteOptimizer })));
const SignInPanel = lazy(() => import('./components/auth/SignInPanel').then(module => ({ default: module.SignInPanel })));
const FerryPortTracker = lazy(() => import('./components/ferry-port/FerryPortTracker').then(module => ({ default: module.FerryPortTracker })));
const OperationsAnalytics = lazy(() => import('./components/operations/OperationsAnalytics').then(module => ({ default: module.OperationsAnalytics })));

function LoadingPanel({ message = 'Loading workspace…' }: { message?: string }) {
  return (
    <div className="glass-panel app-loading-panel" role="status" aria-live="polite">
      <span className="app-loading-spinner" aria-hidden="true" />
      <span>{message}</span>
    </div>
  );
}

const VIEW_META: Record<AppTab, { title: string; description: string }> = {
  map: {
    title: 'Batam Congestion Watch',
    description: 'Explore 30 planning hotspots across Batam.',
  },
  route: {
    title: 'Cross-Border Route Planner',
    description: 'Plan your road and ferry journey.',
  },
  analytics: {
    title: 'Ferry & Operations Analytics',
    description: 'Traffic, ferries and daily trends.',
  },
};

export function App() {
  const [activeTab, setActiveTab] = useState<AppTab>('map');

  const [isSignInOpen, setIsSignInOpen] = useState<boolean>(false);
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null);
  const [session, setSession] = useState<StoredSession | null>(() => readStoredSession());
  const currentCredentialRef = useRef({ session, generation: sessionGeneration() });
  const handleSessionChange = useCallback((next: StoredSession | null) => {
    // Storage may be blocked, so keep the accepted/rotated credential for this
    // mounted app. Bind it to the account generation to prevent resurrection
    // after sign-out or a different sign-in attempt.
    currentCredentialRef.current = { session: next, generation: sessionGeneration() };
    setSession(next);
  }, []);
  const [identity, setIdentity] = useState<AuthSession | null>(null);
  const [authResolved, setAuthResolved] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);
  const [authRecoveryPending, setAuthRecoveryPending] = useState(false);
  const [authRestoreVersion, setAuthRestoreVersion] = useState(0);
  // Remembered for the tab only: a guest should not be re-prompted on every
  // reload, but a new visit starts at the sign-in screen again.
  const [isGuest, setIsGuest] = useState<boolean>(
    () => rememberGuest(),
  );
  const handleIdentityChange = useCallback((next: AuthSession | null) => {
    setIdentity(next);
    if (next) {
      setAuthRecoveryPending(false);
      rememberGuest(false);
      setIsGuest(false);
      setIsSignInOpen(false);
      setAuthError(null);
    }
  }, []);
  const retrySavedSession = useCallback(() => {
    setAuthError(null);
    setAuthResolved(false);
    setAuthRestoreVersion(version => version + 1);
  }, []);
  const handleSessionRecovery = useCallback(() => {
    setIdentity(null);
    setAuthRecoveryPending(true);
    retrySavedSession();
  }, [retrySavedSession]);
  useSessionRefresh(authResolved && identity ? session : null, handleSessionChange, handleIdentityChange, handleSessionRecovery);
  const [routes, setRoutes] = useState<CorridorRoute[]>([]);
  const [routesLoading, setRoutesLoading] = useState(true);
  const [corridors, setCorridors] = useState<Corridor[]>(INITIAL_CORRIDORS);
  const [ferries, setFerries] = useState<FerrySchedule[]>(INITIAL_FERRIES);
  const [ferryTimetable, setFerryTimetable] = useState<FerryTimetableMetadata>(
    PUBLISHED_FERRY_TIMETABLE_METADATA,
  );
  const [ports, setPorts] = useState<PortStatus[]>(() => scheduleInformedPortSeed());
  const [portSource, setPortSource] = useState<DataSource>('offline');
  const [portsLoading, setPortsLoading] = useState(true);
  const [portsError, setPortsError] = useState<string | null>(null);
  const [isFerryRefreshing, setIsFerryRefreshing] = useState(false);
  const [operationsSnapshot, setOperationsSnapshot] = useState<Fetched<OperationsSummary> | null>(null);
  const [operationsCheckedAt, setOperationsCheckedAt] = useState<string | null>(null);
  const [portsCheckedAt, setPortsCheckedAt] = useState<string | null>(null);
  const [routeLocations, setRouteLocations] = useState<RouteLocation[]>(ROUTE_LOCATIONS);
  const [trafficSnapshot, setTrafficSnapshot] = useState<Fetched<LiveTrafficData> | null>(null);

  const [dataSource, setDataSource] = useState<DataSource>('offline');
  const [ferrySource, setFerrySource] = useState<DataSource>('offline');
  const [provenance, setProvenance] = useState<Provenance | undefined>();
  const [lastUpdated, setLastUpdated] = useState<string>('');

  const [routeOriginId, setRouteOriginId] = useState<string>('mukakuning');
  const [routeDestinationId, setRouteDestinationId] = useState<string>('batam_centre');

  // Solver state lives here rather than inside RouteOptimizer: that component is
  // conditionally rendered, so switching tabs used to unmount it and silently
  // discard a computed route.
  const [routeResult, setRouteResult] = useState<RouteOptimizationResult | null>(null);
  const [routeSource, setRouteSource] = useState<DataSource>('simulated');
  const [vehicleType, setVehicleType] = useState<VehicleType>('CARGO_TRUCK');
  const [weather, setWeather] = useState<number>(0);
  const [departureHour, setDepartureHour] = useState<number>(() => new Date().getHours());
  const ferryRefreshInFlightRef = useRef(false);
  const ferryRequestEpochRef = useRef(0);
  const dataRequestEpochRef = useRef(0);
  const operationsRequestEpochRef = useRef(0);
  const operationsRequestRef = useRef<Promise<void> | null>(null);

  const loadOperations = useCallback((): Promise<void> => {
    // Timer, focus, and manual refreshes share one numeric-summary request.
    if (operationsRequestRef.current) return operationsRequestRef.current;
    const requestEpoch = ++operationsRequestEpochRef.current;
    const request = fetchOperationsSummary().then(snapshot => {
      if (requestEpoch !== operationsRequestEpochRef.current) return;
      setOperationsSnapshot(snapshot);
      setOperationsCheckedAt(new Date().toISOString());
    }).finally(() => {
      if (operationsRequestRef.current === request) operationsRequestRef.current = null;
    });
    operationsRequestRef.current = request;
    return request;
  }, []);

  useEffect(() => {
    void loadOperations();
    return () => {
      operationsRequestEpochRef.current += 1;
      operationsRequestRef.current = null;
    };
  }, [loadOperations]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      // Browser timers can be throttled in background tabs. The existing
      // focus/visibility refresh resumes this cadence on foreground return.
      if (document.visibilityState !== 'hidden') void loadOperations();
    }, operationsRefreshDelay(operationsSnapshot));
    return () => window.clearTimeout(timer);
  }, [loadOperations, operationsSnapshot, operationsCheckedAt]);

  const loadData = useCallback(async () => {
    const dataRequestEpoch = ++dataRequestEpochRef.current;
    // Parallel, not sequential: a slow provider must not delay unrelated
    // ferry and corridor requests one after another. Numeric summaries have
    // their own cadence so provider latency cannot delay local observations.
    const ferryRequestEpoch = ferryRequestEpochRef.current;
    const ferryRequest = ferryRefreshInFlightRef.current
      ? Promise.resolve(null)
      : fetchFerries();
    const [c, f, traffic] = await Promise.all([
      fetchCorridors(), ferryRequest, fetchLiveTraffic(),
    ]);

    if (dataRequestEpoch !== dataRequestEpochRef.current) return;

    setCorridors(c.data);
    // A poll that began before a manual official-source refresh must not write
    // its older response over the coordinated refresh result.
    if (f
      && ferryRequestEpoch === ferryRequestEpochRef.current
      && !ferryRefreshInFlightRef.current) {
      setFerries(f.data);
      setFerryTimetable(f.timetable);
      setFerrySource(f.source);
    }
    setTrafficSnapshot(traffic);
    // The global badge describes road traffic. Ferry schedules and operations
    // identify their own modelled/offline state inside their respective views.
    // This also lets a configured live traffic provider surface as live even
    // while the ferry timetable remains modelled.
    const roadSource = traffic.source === 'offline' ? c.source : traffic.source;
    setDataSource(roadSource);
    setProvenance(traffic.provenance ?? c.provenance);
    setLastUpdated(traffic.source === 'offline' ? c.fetchedAt : traffic.fetchedAt);
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      await loadData();
      if (!cancelled) timer = setTimeout(poll, POLL_INTERVAL_MS);
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [loadData]);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const pollPorts = async () => {
      const requestEpoch = ferryRequestEpochRef.current;
      if (!ferryRefreshInFlightRef.current) {
        try {
          const response = await fetchPortIntelligence();
          if (!cancelled
            && requestEpoch === ferryRequestEpochRef.current
            && !ferryRefreshInFlightRef.current) {
            setPorts(response.data);
            setPortSource(response.source);
            setPortsError(null);
            setPortsCheckedAt(new Date().toISOString());
          }
        } catch (error) {
          if (!cancelled && requestEpoch === ferryRequestEpochRef.current) {
            setPortsError(error instanceof Error
              ? error.message
              : 'Port intelligence could not be refreshed.');
          }
        } finally {
          if (!cancelled && requestEpoch === ferryRequestEpochRef.current) {
            setPortsLoading(false);
          }
        }
      }
      if (!cancelled) timer = setTimeout(pollPorts, 10_000);
    };

    void pollPorts();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  // Road geometry and the route-place catalogue are both static, so fetch each
  // once. The corridor geometry is what lets the map draw the corridor the
  // Corridor tab is reporting congestion for.
  useEffect(() => {
    let cancelled = false;
    Promise.all([fetchCorridorRoutes(), fetchRouteLocations()])
      .then(([corridorRoutes, locations]) => {
        if (cancelled) return;
        setRoutes(corridorRoutes.data);
        setRouteLocations(locations.data);
      })
      .finally(() => {
        if (!cancelled) setRoutesLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  /**
   * Restore a stored session on load and ask the server who it belongs to.
   *
   * A backend outage is not a rejected credential. Keep saved tokens while
   * roles are unverified, retry briefly, and leave the workspace gated.
   */
  useEffect(() => {
    let cancelled = false;
    const waits = new Map<number, () => void>();
    const waitBeforeRetry = (delay: number) => new Promise<void>(resolve => {
      const timer = window.setTimeout(() => { waits.delete(timer); resolve(); }, delay);
      waits.set(timer, resolve);
    });
    fetchAuthStatus().then(status => {
      if (!cancelled) setAuthStatus(status);
    });

    const stored = readStoredSession();
    const oauthCallback = hasOAuthCallback();
    let generation = sessionGeneration();
    const memoryCredential = authRestoreVersion > 0 && currentCredentialRef.current.generation === generation
      ? currentCredentialRef.current.session : null;
    let credential = stored ?? memoryCredential;
    // Always resolve through a promise so the gate's readiness flag is never
    // set synchronously inside the effect, which would cascade renders.
    Promise.resolve()
      .then(async () => {
        if (credential || oauthCallback) {
          setAuthResolved(false);
          setIdentity(null);
        }
        if (oauthCallback) {
          credential = null;
          const completion = completeOAuthSignIn();
          generation = sessionGeneration();
          credential = await completion;
        }
        if (!credential) {
          if (!cancelled && generation === sessionGeneration() && authRestoreVersion > 0) {
            handleSessionChange(null);
            setAuthRecoveryPending(false);
          }
          return;
        }
        for (let attempt = 0; attempt < 3; attempt += 1) {
          if (cancelled || generation !== sessionGeneration()) return;
          try {
            const fresh = await validSession(credential);
            if (cancelled || generation !== sessionGeneration()) return;
            credential = fresh;
            handleSessionChange(fresh);
            const resolved = await fetchSession(fresh);
            if (cancelled || generation !== sessionGeneration()) return;
            setAuthRecoveryPending(false);
            setAuthError(null);
            handleIdentityChange(resolved);
            return;
          } catch (error) {
            if (cancelled || generation !== sessionGeneration()) return;
            if (error instanceof SessionRejectedError || attempt === 2) throw error;
            setAuthRecoveryPending(true);
            await waitBeforeRetry(attempt === 0 ? 1000 : 3000);
          }
        }
      })
      .catch((error: unknown) => {
        if (cancelled || generation !== sessionGeneration()) return;
        setIdentity(null);
        if (error instanceof SessionRejectedError || !credential) {
          clearStoredSession();
          handleSessionChange(null);
          setAuthRecoveryPending(false);
          setAuthError(error instanceof AuthError ? error.message : 'Sign-in could not be completed. Please try again.');
        } else {
          handleSessionChange(readStoredSession() ?? credential);
          setAuthRecoveryPending(true);
          setAuthError(`Your account could not be verified right now. Your saved sign-in is preserved. Retry when the service is available. ${error instanceof Error ? error.message : ''}`);
        }
        if (oauthCallback || credential) {
          setIsGuest(false);
          rememberGuest(false);
        }
      })
      .finally(() => {
        if (!cancelled) setAuthResolved(true);
      });
    return () => {
      cancelled = true;
      waits.forEach((resolve, timer) => { window.clearTimeout(timer); resolve(); });
      waits.clear();
    };
  }, [handleIdentityChange, handleSessionChange, authRestoreVersion]);

  // Offering sign-in needs both halves configured: the browser needs the
  // Supabase project to authenticate against, and the API needs to be able to
  // resolve a role from it.
  const signInAvailable = supabaseConfigured()
    && (authStatus === null || authStatus.enabled);
  const driverAccess = isDriverView(identity, isGuest);

  const visibleActiveTab: AppTab = driverAccess && activeTab === 'analytics'
    ? 'map'
    : activeTab;

  const refreshAnalyticsData = useCallback(async () => {
    const epoch = ferryRequestEpochRef.current;
    const [, response] = await Promise.all([loadData(), fetchPortIntelligence(), loadOperations()]);
    if (epoch === ferryRequestEpochRef.current && !ferryRefreshInFlightRef.current) {
      setPorts(response.data);
      setPortSource(response.source);
      setPortsCheckedAt(new Date().toISOString());
      setPortsError(null);
      setPortsLoading(false);
    }
  }, [loadData, loadOperations]);

  useEffect(() => {
    if (identity || visibleActiveTab === 'analytics') void refreshAnalyticsData();
  }, [identity, visibleActiveTab, refreshAnalyticsData]);

  useEffect(() => {
    const refreshOnReturn = () => {
      if (document.visibilityState !== 'hidden') void refreshAnalyticsData();
    };
    window.addEventListener('focus', refreshOnReturn);
    document.addEventListener('visibilitychange', refreshOnReturn);
    return () => {
      window.removeEventListener('focus', refreshOnReturn);
      document.removeEventListener('visibilitychange', refreshOnReturn);
    };
  }, [refreshAnalyticsData]);

  // Changing who is signed in — including signing out — must not leave the
  // previous account's route on screen. Undefined marks the first run, so
  // restoring a stored session on load is not mistaken for a switch.
  const identityKeyRef = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    const key = identityKey(identity);
    if (identityKeyRef.current === undefined) {
      identityKeyRef.current = key;
      return;
    }
    if (identityKeyRef.current === key) return;
    identityKeyRef.current = key;
    setRouteResult(null);
    setActiveTab('map');
  }, [identity]);

  // Entering or leaving the driver view must start from a clean pane. The
  // driver's only route is the one they load by code, so a journey left over
  // from planning would otherwise appear under a different code than the one
  // in the lookup box.
  const driverAccessRef = useRef<boolean | undefined>(undefined);
  useEffect(() => {
    if (driverAccessRef.current === undefined) {
      driverAccessRef.current = driverAccess;
      return;
    }
    if (driverAccessRef.current === driverAccess) return;
    driverAccessRef.current = driverAccess;
    setRouteResult(null);
    setActiveTab('map');
  }, [driverAccess]);

  const continueAsGuest = useCallback(() => {
    rememberGuest(true);
    setIsGuest(true);
    setIsSignInOpen(false);
    setAuthError(null);
  }, []);
  const closeSignIn = useCallback(() => setIsSignInOpen(false), []);

  // Show the sign-in screen first. Waiting for authResolved avoids flashing it
  // at someone whose stored session is about to restore.
  const showAuthGate = (signInAvailable || Boolean(authError)) && authResolved && !identity && !isGuest;

  const handleSelectCorridorAndSolve = useCallback((corridorId: string) => {
    const corridor = corridors.find(item => item.id === corridorId);
    if (corridor?.origin && corridor.destination) {
      setRouteOriginId(corridor.origin);
      setRouteDestinationId(corridor.destination);
      setRouteResult(null);
    }
    setActiveTab('route');
  }, [corridors]);

  const handleRefreshOfficialFerrySources = useCallback(async (): Promise<FerryRefreshReport> => {
    if (ferryRefreshInFlightRef.current) {
      throw new Error('An official-source refresh is already in progress.');
    }

    ferryRefreshInFlightRef.current = true;
    ferryRequestEpochRef.current += 1;
    setIsFerryRefreshing(true);
    setPortsLoading(true);
    setPortsError(null);
    try {
      const response = await refreshOfficialFerrySources();
      setFerries(response.data);
      setFerryTimetable(response.timetable);
      setFerrySource(response.source);
      setPorts(response.ports);
      setPortSource(response.source);
      setPortsCheckedAt(new Date().toISOString());
      return response.refresh;
    } finally {
      ferryRefreshInFlightRef.current = false;
      setIsFerryRefreshing(false);
      setPortsLoading(false);
    }
  }, []);

  const activeView = visibleActiveTab === 'route' && driverAccess
    ? {
      title: 'Assigned Routes',
      description: 'Load your journey with a dispatch code.',
    }
    : VIEW_META[visibleActiveTab];
  const operations = operationsSnapshot?.data ?? UNAVAILABLE_OPERATIONS;

  if (!authResolved && supabaseConfigured()) return <LoadingPanel message={authRecoveryPending
    ? 'Restoring saved sign-in… The service is unavailable; retrying without discarding your session.' : undefined} />;

  if (showAuthGate) {
    return (
      <Suspense fallback={<LoadingPanel />}>
        <SignInPanel
          variant="page"
          status={authStatus}
          session={session}
          identity={identity}
          onSessionChange={handleSessionChange}
          onIdentityChange={handleIdentityChange}
          onClose={continueAsGuest}
          onContinueAsGuest={continueAsGuest}
          initialError={authError}
          onRetrySession={authRecoveryPending ? retrySavedSession : undefined}
        />
      </Suspense>
    );
  }

  return (
    <>
      <div style={{ display: 'contents' }} {...(isSignInOpen ? { inert: '' } : {})} aria-hidden={isSignInOpen || undefined}>
      <a className="app-skip-link" href="#main-content">Skip to main content</a>

      <Header
        onOpenSignIn={() => setIsSignInOpen(true)}
        identity={identity}
        signInAvailable={signInAvailable}
        activeTab={visibleActiveTab}
        setActiveTab={setActiveTab}
        allowedTabs={driverAccess ? DRIVER_TABS : undefined}
        dataSource={dataSource}
        provenance={provenance}
        lastUpdated={lastUpdated}
      />

      <div className="app-workspace">
        <main
          id="main-content"
          className="app-main"
          role="tabpanel"
          aria-labelledby={`workspace-tab-${visibleActiveTab}`}
          tabIndex={-1}
        >
          <div className="app-view-heading">
            <div>
              <h2 id="active-view-title">{activeView.title}</h2>
              <span>{activeView.description}</span>
            </div>
            <span className="app-track-label">Batam · Riau Islands</span>
          </div>

          <section className="app-view-content" aria-labelledby="active-view-title">
            <Suspense fallback={<LoadingPanel />}>
              {visibleActiveTab === 'map' && (
                <MapView
                  corridors={corridors}
                  routes={routes}
                  routesLoading={routesLoading}
                  trafficSnapshot={trafficSnapshot}
                  canPlanRoutes={!driverAccess}
                  onSelectCorridor={handleSelectCorridorAndSolve}
                />
              )}

              {visibleActiveTab === 'route' && (
                <RouteOptimizer
                  key={identityKey(identity) ?? (isGuest ? 'guest' : 'public')}
                  locations={routeLocations}
                  originId={routeOriginId}
                  setOriginId={setRouteOriginId}
                  destinationId={routeDestinationId}
                  setDestinationId={setRouteDestinationId}
                  result={routeResult}
                  setResult={setRouteResult}
                  resultSource={routeSource}
                  setResultSource={setRouteSource}
                  vehicleType={vehicleType}
                  setVehicleType={setVehicleType}
                  weather={weather}
                  setWeather={setWeather}
                  hour={departureHour}
                  setHour={setDepartureHour}
                  driverAccess={driverAccess}
                  accessToken={session?.accessToken ?? null}
                />
              )}

              {visibleActiveTab === 'analytics' && (
                <div className="analytics-workspace-stack">
                  <OperationsAnalytics
                    operations={operations}
                    operationsSnapshot={operationsSnapshot}
                    checkedAt={operationsCheckedAt}
                    onRefresh={refreshAnalyticsData}
                    accessToken={session?.accessToken ?? null}
                    corridors={corridors}
                    ferryAndPortsContent={(
                      <FerryPortTracker
                        ferries={ferries}
                        dataSource={ferrySource}
                        timetable={ferryTimetable}
                        ports={ports}
                        portSource={portSource}
                        portsLoading={portsLoading}
                        portsCheckedAt={portsCheckedAt}
                        portsError={portsError}
                        isRefreshingOfficialSources={isFerryRefreshing}
                        onRefreshOfficialSources={handleRefreshOfficialFerrySources}
                      />
                    )}
                  />
                </div>
              )}
            </Suspense>
          </section>
        </main>
      </div>

      </div>
      <Suspense fallback={null}>
        {isSignInOpen && (
          <SignInPanel
            status={authStatus}
            session={session}
            identity={identity}
            onSessionChange={handleSessionChange}
            onIdentityChange={handleIdentityChange}
            onClose={closeSignIn}
            initialError={authError}
            onRetrySession={authRecoveryPending ? retrySavedSession : undefined}
          />
        )}
      </Suspense>
    </>
  );
}

export default App;
