# Local verification — 23 October 2026

The repository is checked out in `CrossFlow_AI`. The main local application is
running at http://localhost:3000 and the FastAPI backend at
http://localhost:8000. Repository edits remain local; no push or deployment was
made.

Latest follow-up: [frontend standardisation and hardening](FRONTEND_AND_HARDENING_2026-10-23.md).
The shared local font and History disclosure fix passed browser checks for both
roles; 422 frontend tests, 471 backend/auth tests with 412 subtests, 153 standalone
backend checks and 26 local SQL checks passed. Seven existing local SQLite
databases passed read-only integrity checks. The follow-up documents the scope
and unchanged model/storage limitations.

**Demo readiness: LIMITED.** Local features and builds pass. The local app now
uses real TomTom speeds for all five corridors through the owner's configured
direct TomTom key; the earlier public Vercel relay remains an optional local
fallback. The Codex browser shows **Live** with no console errors. Hotspots
remain modelled, the forest remains synthetically trained, and historical
field accuracy remains unverified.

The current analytics API derives congestion and bottlenecks from fresh
validated TomTom representative-point measurements, with actual snapshot time,
age and coverage counts. Provider failure leaves values unavailable. Default
history uses only canonical observed spatial records and remains empty when no
measurements have been stored. Fixed fleet splits, illustrative hourly curves,
fabricated alert ages and unsupported observed-emissions figures are excluded
from the operating dashboard. Ferry departures remain published timetable
calculations, never live operating status.

Useful model analytics are separate from those observations. The bounded
operator endpoint `POST /api/analytics/simulation` runs the actual serving
Random Forest for the next 24 hours, across up to five selected corridors,
using the current WIB clock/calendar, chosen weather and published ferry
proximity. It returns P10/mean/P90 forecasts and clear/rain/storm comparisons,
labelled simulation rather than measured history. Modelled history remains an
explicit `include_modelled=true` engineering preview.

The optional local `CROSSFLOW_ARCHIVE_LIVE_TRAFFIC=1` collector begins recording
fresh validated TomTom samples at startup and every 120 seconds. It preserves
original observation times, deduplicates provider snapshots and writes only
canonical observed records. Reads do not trigger collection. The collector is
disabled by default and in serverless/shared-store mode; its status reports
start/last-sample times and current-process counters. New measured history
begins with collection, never a backfilled synthetic archive.

The new measured-only training path excludes synthetic, simulated, unsupported
and future records. It uses purged fit/calibration/final-test timestamp groups,
prior-only persistence/history-blend baselines and explicit promotion gates.
The latest [readiness snapshot](verification/observed-training-readiness.json)
has **110 TomTom rows across 0.6922 hours**; it remains insufficient, with no
model replacement. A licensed historical export and successful future holdout
are still needed. Current Flow returned 200; historical Traffic Stats returned
403. MyTomTom account setup succeeded, but MOVE still reports **Failed to
fetch**. The owner-approved HERE Traffic Analytics trial registration succeeded,
ending 22 November 2026. After the initial SoC-alias message did not arrive,
the owner requested `e1525902@u.nus.edu` and a new submission succeeded; delivery
to the corrected address is not verified yet. HERE dashboard access, Batam coverage and specific ML/storage
rights remain unverified. No provider Batam historical key/export or serving observed-model
replacement is verified yet.
[Provider probe](verification/tomtom-access-check.json),
[HERE trial confirmation](verification/here-trial-u-nus-success.png),
[alternative-source review](HISTORICAL_TRAFFIC_SOURCES.md).
[Observed training and import guide](OBSERVED_ML_TRAINING.md).

### Free historical data acquired and tested

The owner limited further acquisition to free/academic sources. A dated **48-row Batam road-speed survey (16–18 February 2023)** has been transcribed and visually checked against its primary PDF, with original directions, intervals, attribution, licence, hashes and a flagged speed/time discrepancy. The single site has no verified canonical corridor mapping and is too sparse to promote a Batam network model.

A separate **Singapore LTA academic archive** has 397,341 speed-band values across 53 road links and 27 calendar dates (8 March–3 April 2021). An offline chronological Random Forest backtest used zero synthetic training rows, gap exclusions, purged fit/calibration/test targets and fixed model parameters. Forest MAEs at 15/30/60 minutes are 0.528920 / 0.551534 / 0.562846 **band units**, beating persistence and previous-day comparisons in the saved report. These are old Singapore observations, not exact speeds or Batam field accuracy; source timezone and availability delays are unknown. No serving model or canonical app history was replaced.

Local **Analytics → History** now reads the bundled survey and compact benchmark through `GET /api/analytics/research-history`. The endpoint returns `private, no-store`, does no network scraping, accepts no file path, and cannot initiate training or import records. It rejects failed survey checksums and overlapping benchmark chronology. The browser filters actual rows by survey date and original direction and displays explicit historical dates and geographic limits. The existing measured-history chart separately shows today's collected Batam samples.

Fresh checks after this change: **412 frontend tests / 41 files**, lint, TypeScript and production build passed. **335 non-HTTP backend unit tests**, **32 local HTTP contract tests**, **46 authentication unit tests**, and **16 function-style pytest tests** passed. The first backend run could not bind ephemeral loopback ports in the sandbox; the 32 HTTP tests passed when rerun with the approved local-network permission. New integrity/filter/chronology tests are included in those totals. Operator and driver console error logs are empty. The real driver session survived reload and dispatch code **AHMKAQM** loaded its persisted ROAD → FERRY → ROAD map after the backend restart. [Machine-readable verification](verification/historical-research-local-verification.json), [driver screenshot](verification/historical-driver-route-regression.png).

[Sources and acquisition audit](HISTORICAL_TRAFFIC_SOURCES.md), [full benchmark](verification/singapore-academic-backtest.json), [API evidence](verification/historical-research-api.json), [Batam browser evidence](verification/historical-batam-survey-local.png), [benchmark browser evidence](verification/historical-singapore-backtest-local.png).

**Fresh Google OAuth succeeded from `http://localhost:3000`.** The app showed
**Ian Wangsa**, and the backend resolved the protected profile as **admin**.
This follows the user's explicitly approved operator/admin promotion of
`ianwangsa2@gmail.com`; the earlier driver session is superseded by this fresh
operator session. See [Google login evidence](verification/google-login-admin-success.png).

Custom SMTP is enabled and saved. At 16:13, one actual Supabase Auth `/otp`
request for `2021.ian.abiel.wangsa@ejc.edu.sg` returned HTTP 200 with no session.
The EJC inbox received a numeric confirmation email from **CrossFlow AI** with
subject **Verify your CrossFlow account**. At 16:22, the first-class **Email me
a sign-in code** UI requested another numeric confirmation email, verified it
with type `email`, and the backend resolved **CrossFlow OTP Demo** as
**driver**. No agent-set password was used. EJC's first passwordless login is
verified and survived a reload. The account modal showed **DRIVER**, with
`crossflow_profiles` as the server role source.
[Restored driver-session evidence](verification/otp-login-driver-restored.png).
A repeat confirmed-account code arrived at 16:23 with subject **Your CrossFlow
sign-in code** and its UI verification also succeeded as **driver**, with zero
console errors. Both first-account confirmation and confirmed-account sign-in
are verified. The owner submitted the separate NUS password signup at 16:26
from `http://127.0.0.1:3000/`, preserving the localhost EJC session. Supabase
created a distinct NUS user ID with confirmation sent at **16:26:45 SGT**.
The authorized NUS-only resend at 16:49 was marked Sent/Delivered to exactly
`ian1234@comp.nus.edu.sg`. The owner later found it in the Outlook mailbox
`e1525902@u.nus.edu`. Codex independently opened the actual 16:49 message:
its header showed **To: ian1234@comp.nus.edu.sg**, and its recipient-aware body
named that same address. The code was verified through the original 127.0.0.1
signup screen using `verifySignUpOtp`; the backend resolved **CrossFlow Driver
Demo** as **driver**. **NUS receipt and signup OTP verification are verified.**
The canonical CrossFlow account stays `ian1234@comp.nus.edu.sg`; no account
email was changed or merged. NUS reload restoration also passed; its modal
showed **CrossFlow Driver Demo / DRIVER**, with `crossflow_profiles` as role
source. The owner then entered the chosen password for a separate localhost
sign-in. That login, reload restoration and the server-resolved DRIVER modal
all passed, with zero console errors.
[NUS restored-session evidence](verification/nus-signup-driver-restored.png).
[NUS password-login evidence](verification/nus-password-login-restored.png).
[Recipient-specific delivery events](verification/recipient-specific-smtp-delivery.png),
[NUS-bound verification screen](verification/nus-recipient-bound-verification.png),
[16:49 delivery trace](verification/nus-email-delivery-trace.json).

The EJC 16:22/16:23 messages were separate intentional tests. Its latest
confirmation-subject message is still the 16:22 email; no NUS-to-EJC delivery
was observed, and the 16:49 resend was NUS-only. The evidence
does not show NUS being sent to EJC. Four email-isolation tests and three
cross-account UI tests now cover recipient binding. The UI explicitly names
the verification/resend destination without changing the active NUS stage.
Verification uses the requested email together with the numeric code; no
promise of globally unique digits across accounts is made.
Earlier NUS/EJC Gmail exact-ID searches were negative; Gmail settings were
unchanged. The [resolved delivery trace](verification/nus-email-delivery-trace.json)
preserves those searches and now records Outlook receipt, verification and
reload restoration. Provider Opened alone was not
inbox proof; the actual Outlook message now supplies that proof. An
institutional forward/gateway is plausible, but the full Received chain and
exact routing rule were not inspected. The earlier webmail timeout was a
connectivity result. The [historical trace handoff](SUPABASE_SIGNUP_EMAIL_SETUP.md#nus-delivery-trace-and-handoff)
is resolved; no support contact is needed for this message and none was sent.

An original Google client secret appeared once in a copy-button accessibility
label in tool output. Replacement and old-secret revocation are **unverified**;
the successful login does not establish which secret is active or prove
rotation. No secret value is written in repository files or screenshots.
The **CrossFlow AI** Google Cloud project (`crossflow-ai-512506`) was created
with the user's approved **My Billing Account** link; no **Upgrade** action was
selected. The external-testing OAuth app was saved after explicit acceptance
of Google's User Data Policy. Identity scopes `openid`, `email` and `profile`
were saved and confirmed after reload. **CrossFlow AI Local Web** uses origin
`http://localhost:3000` and the hosted Supabase callback. Supabase has the
correct Client ID, a nonempty masked saved secret and Google **Enabled**.
The fresh local Google OAuth and backend admin-role round trip succeeded;
replacement-secret status remains unverified. The owner's Google test account was added after
explicit authorization, and the [test-user grid was verified](verification/google-test-user-added.png).

Google's [basic-identity scope exception](https://support.google.com/cloud/answer/15549945?hl=en)
means `openid`/`email`/`profile` sign-in does not require a test-user allowlist.
Adding that test account is not a prerequisite for this scope set. Additional
scopes or account restrictions can change access behavior.

The user completed Brevo setup and reports successful phone verification. Its
dashboard showed Free with 300 sends/day. The sender `ianwangsa2@gmail.com`
was **Verified** with display name **CrossFlow AI**; its freemail warning
remains. Saved custom SMTP delivered the EJC confirmation email described
above. The exact stored Brevo key label cannot be determined: both key rows
still show `Last used --`, so use of **CrossFlow AI v2** is unverified.
Both numeric templates are saved with a recipient notice using
`<strong>{{ .Email }}</strong>` and instructions to use the code only with
that address. Both updated templates were independently reloaded with the
recipient notice and numeric token persisted and Save disabled. The sign-in subject
remains **Your CrossFlow sign-in code** and both retain `{{ .Token }}`.
See [signup email setup](SUPABASE_SIGNUP_EMAIL_SETUP.md) and
[the recipient-aware signup template](verification/signup-recipient-template-saved.png),
[the recipient-aware sign-in template](verification/signin-recipient-template-saved.png).

## Authentication connection

The gitignored root `.env` and `frontend/.env.local` target Supabase project
`wtednggryrikyvkuhqjo` with its browser-safe publishable key. Human sign-in does
not need a service-role secret. Passwords go directly to Supabase; the backend
verifies the access token and resolves the current role from the caller's
profile. The hosted project's Auth settings returned HTTP 200, email auth is
enabled, existing profile rows are present, and anonymous profile access
returned HTTP 401.

A fresh real Google sign-in showed **Ian Wangsa** in the configured main app,
and the backend returned the protected profile's **admin** role. The earlier
driver session and approved role update were separately observed. EJC email-code
verification succeeded as driver, survived reload, and succeeded again for the
confirmed account. Earlier browser checks
also reached real Supabase Auth and displayed “Invalid login credentials”
for a synthetic nonexistent account.
The UI provides email/password sign-in, **Create account**, signup OTP
verification/resend and **Continue with Google**, using the official Supabase
SDK's PKCE flow. Gmail addresses use a CrossFlow account password in the
password form; Google passwords are entered only on Google's screen.
Password-recovery and MFA screens remain outside this implementation.
The first-class **Email me a sign-in code** flow is implemented and its first
live request/verification succeeded. The full frontend check passed **403 tests
in 39 files**, lint, TypeScript and build, including **41 SignInPanel tests**
and **31 email-OTP service tests**. Four email-isolation and three cross-account
UI regressions are included. EJC reload restoration and repeat confirmed-account
verification are verified.

The signup implementation does not store a session from an unconfirmed signup
response. Verification checks the expected email and the returned access and
refresh tokens. Resend and verification failures remain visible, and account
changes invalidate stale requests. A backend regression confirms that a
missing session or a forged token claiming email verification cannot bypass
Supabase token verification or reach profile lookup.

The earlier disabled-provider state has been superseded by saved credentials,
Google **Enabled**, and a successful fresh local Google sign-in. The recorded
Site URL is `http://localhost:3000`.

The local Google flow passed real-SDK PKCE contract tests and its real
disabled-provider browser check. Tests cover same-tab initiation, S256 verifier
exchange, callback cleanup, cancellations in query/fragment URLs, expired or
unsolicited codes, account changes and StrictMode duplicate callbacks. A
successful Google sign-in is recorded above. See
[Google Auth Setup](GOOGLE_AUTH_SETUP.md).

Actual EJC confirmation-email delivery, UI code verification and the resulting
backend-resolved driver session and restoration after reload are **verified**.
A repeat code reached the confirmed account with the sign-in subject and its
verification also succeeded as driver, with zero console errors.
Both templates contain numeric `{{ .Token }}` and explicit `{{ .Email }}`
recipient notices; both latest updates are saved and reload-verified.
Supabase's default SMTP is restricted to organization-team addresses, currently permits
two project emails/hour and has no delivery SLA; it is not sufficient evidence
that arbitrary test recipients will receive a code. Do not add recipients to
the organization merely to obtain delivery, or disable Confirm Email to get a
successful-looking signup. [SMTP restrictions](https://supabase.com/docs/guides/auth/auth-smtp),
[rate limits](https://supabase.com/docs/guides/auth/rate-limits).

The local schema v2 in `backend/auth/schema.sql` fixes a critical privilege
bug: the old SECURITY DEFINER profile guard evaluated `current_user` as its
privileged owner and allowed driver self-promotion. The fixed SECURITY INVOKER
guard rejects it with SQLSTATE 42501. It also backfills missing profiles as
drivers without changing existing roles. The hosted project was upgraded using
the tested minimal migration
[`002_harden_auth_role_guard.sql`](../backend/auth/migrations/002_harden_auth_role_guard.sql),
applied through the Codex in-app browser. This changed the role guard to
`SECURITY INVOKER`, updated the Google display-name signup handler and upgraded
the health function. **The full schema and its profile backfill were not run.**

An independent clean SQL query after the migration confirmed:

| Hosted verification | Result |
| --- | --- |
| `schema_version` | `2` |
| `rls_enabled` | `true` |
| `authenticated_can_read` | `true` |
| `authenticated_cannot_insert` | `true` |
| Role guard `SECURITY DEFINER` flag | `false` — now invoker |
| Existing profile count | `3`, unchanged |

This verifies the deployed function mode and health metadata at migration time.
Subsequent role checks verified the Google account's initial driver session
and explicitly approved promotion to admin. A fresh Google operator UI session
has since succeeded; secret
replacement/revocation remains unverified.
The separate email-code UI checks above establish OTP login; a role update
alone would not establish that journey.

![Hosted auth v2 verification](verification/hosted-auth-v2.png)

## Changes

- Guarded session refresh, initial restoration and dismissed sign-ins against
  account-change races; refresh open workspaces before expiry; bound network
  requests including response bodies; clear signed-out UI immediately.
- Added Google OAuth with same-tab PKCE verifier storage, callback code
  exchange, URL cleanup, expected-request checks, cancellation handling and
  server-resolved roles; email/password login remains available.
- Added email/password signup, OTP entry, verification and resend, with
  explicit unconfirmed-session handling and stale-request invalidation. Live
  inbox delivery and verification remain separate live checks after the saved
  SMTP/template configuration.
- Rendered the sign-in gate exclusively and added modal focus trapping,
  background isolation, focus restoration and correct accessible role labels.
- Passed the user's token to all planner requests so history records have an
  owner. Cancelled stale route/history results and refreshed history after a
  plan. Added explicit local SQLite history with account filtering and actual
  durability labels. Serverless deployments retain shared storage requirements.
- Corrected UTC-to-Batam conversion, exact departure minutes, arrival deadlines
  and fallback ferry selection. Removed a discarded later-departure graph solve
  from fixed-departure requests.
- Fixed geocoder malformed responses and recovery after upstream outages.
- Replaced CARTO's rendered “API KEY REQUIRED” tiles across all three maps with
  a shared configurable OpenStreetMap tile layer and visible attribution.
  Disabled CSS zoom transitions that could call into a removed Leaflet map.
- Explained guest, driver and full-workspace access in the shell, with a visible
  account action and the existing operator sign-in path. Standardized shell and
  workspace sub-tab spacing, selected states, focus rings and 44px controls;
  retained mobile navigation labels. Renamed the analytics sub-tab to **Ferry &
  Ports**. These changes do not grant roles or expose protected tools to drivers.
- Updated compatible frontend toolchain dependencies; audit reports zero
  vulnerabilities. Added SQL policy checks to CI and updated setup examples.
- Added atomic per-account shared history, local-read/shared-fallback retrieval,
  protected integration diagnostics and a read-only chronological backtest API.
  Stopped measurements now persist without censoring the most severe congestion.
  SQLite upgrades are atomic; shared SQL remains local and unapplied.
- Unified batch inference and speed-ratio scoring, invalidated emissions caches
  by model revision, and connected the local backend to the exact public Vercel
  traffic endpoint with bounded requests, strict validation and memory-only cache.
- Disabled browser-shared test-admin credentials in production builds. Harmless
  sentinels were absent from all 23 built files. Existing deployments may already
  contain the old configured credentials; a new local build does not revoke them.

## Test results

These runners overlap; their counts must not be added into one total. The
following results identify the completed integrated runs and focused follow-up
checks that produced the evidence.

| Command / runner | Result |
|---|---|
| `cd frontend && npm run check` | **39 files / 403 tests**, ESLint, TypeScript and production build passed after measured-backtest UI/contracts, model scope, restored analytics, official reports, session recovery and the canonical Sindo timetable; includes 41 SignInPanel tests, 31 email-OTP service tests and 4 email-isolation tests. Log: `/tmp/crossflow-observed-backtest-full-check.log`. |
| Later auth form scrolling CSS | **46 existing auth/gate tests**, ESLint, TypeScript and production build passed. A single outer scrollbar replaces nested scrolling; hooks, recipient state and code entry are unchanged. Log: `/tmp/crossflow-auth-short-viewport-check.log`. Pixel verification at the short native viewport remains inconclusive because its captured dimensions differed from the DOM viewport; this is not recorded as a visual pass. |
| `cd frontend && npm audit` | **0 vulnerabilities** after compatible lockfile updates |
| `.venv/bin/python -m pytest backend/tests backend/auth/tests -q` | **418 tests / 397 subtests passed** in 137.00s including measured-only candidate/import/API/auth hardening; `/tmp/crossflow-observed-final-all-backend-verified.log`. After the approved holdout metadata correction, 22 focused tests / 11 subtests passed; `/tmp/crossflow-observed-metadata-final.log` |
| `.venv/bin/python backend/test_backend.py` | Latest full runner: **152/153 passed**; only the stale all-sources-August review-date assertion failed. The corrected source-specific case passed separately: **1 passed / 152 deselected**, `/tmp/crossflow-shared-ferry-source-metadata-regression.log`. This is not a fresh 153/153 full command. Full-run log: `/tmp/crossflow-observed-final-standalone.log`. |
| `npm --prefix backend/auth test` | Fresh **26/26 passed**, zero failures, 3.129s; `/tmp/crossflow-auth-sql-final.log`. New history/stopped-traffic migrations remain unapplied remotely. |
| Earlier relay, diagnostics and congestion-math focused follow-up | **28 tests / 74 subtests passed** in 3.30s; `/tmp/crossflow-final-upstream-and-diagnostics.log` |
| Later forecast-simulation/current archive integration | **36 tests / 31 subtests passed** in 3.73s; `/tmp/crossflow-archive-simulation-final.log`. Simulation real-HTTP/role/bounds checks also passed **9 tests / 11 subtests** in 21.70s; `/tmp/crossflow-analytics-simulation-http.log`. These focused runners overlap and are included in the later integrated suites. |
| `.venv/bin/python -m unittest backend.tests.test_congestion_model backend.tests.test_vehicle_routing -q` | **39 focused ML/vehicle tests** passed in the earlier model audit; overlaps backend runners |
| Congestion-engine stress harness | Latest: **2,000,000 scenarios**, zero invariant failures, **40.48s**, `/tmp/crossflow-observed-final-stress.log`. This exercises the running **synthetic baseline**, not real accuracy or two million itinerary solves. |
| Python compile, launcher syntax, `git diff --check` | Passed after backend integration; documentation diff check passed again after the status updates |

The SQL behavioral checks cover signup/backfill, row-level security, driver
isolation, admin access, denied self-promotion, allowed renaming, denied
inserts/deletes, role changes, immutable IDs, schema reruns and account-deletion
cascade. Migration cases cover idempotence, preservation of existing
rows/ACLs/owners/policies/triggers, role enforcement, Google naming and
transaction rollback on failure. The new shared-history fix remains local.

## Feature coverage

| Feature | Evidence |
|---|---|
| Congestion watch | Automated hotspot/photo/overlay/keyboard tests; browser selected a hotspot and displayed its modelled pressure tooltip |
| Corridor telemetry and forecasts | API contract tests and browser corridor panel with five selectable corridors and 30/60-minute estimates |
| Batam road planning | Graph, eight-vehicle, five-objective, weather, alternatives and maneuver tests; browser completed Batamindo → Batam Centre |
| Route result tabs | Browser verified summary, map, directions and vehicle comparison |
| Dispatch and driver lookup | Final Google admin issued `AHMKAQM`; the real NUS driver entered it with lowercase/spaces and loaded its 12 directions and Journey map. Public code/full-ID retrieval matched all geometry/navigation/legs. Driver “Try again” retries lookup rather than invoking the planner. [Proof](verification/final-operator-driver-dispatch.json) |
| Cross-border planning | Final operator planned Batamindo → HarbourFront, `COMMUTER`, 18:15 WIB departure; code `AHMKAQM` contains ROAD → FERRY → ROAD. Earlier `H6SNDCM` and `V5D9FB8` journeys also passed browser checks. |
| Map rendering | Visual inspection caught the tile-provider failure; repaired map verified and captured below |
| Multi-stop, saved/free places, map picker | Automated UI/keyboard/payload tests and real HTTP multi-stop/free-route tests |
| Departure/arrival and offline fallback | Automated timezone, exact-minute, ferry boarding and arrival-feasibility regressions |
| Route history | HTTP/service/React tests for auth attribution, driver/admin scope, local persistence, shared failure behavior and stale-account response isolation |
| Ferry terminals/departures/cargo | Browser verified terminal cards, departure filtering and labelled demo cargo cards; automated detail tests |
| Official schedule refresh | Final browser and cached API check: **6 pages checked, 0 needing review, 0 permission-gated**, at 17:54:46 SGT. All six supported Sindo cards and 51 departure slots match the reviewed snapshot. [API evidence](verification/final-official-source-checks.json) |
| Operations overview/trends | Browser verified headline metrics and chart summaries; API and continuity tests passed |
| Alerts | Browser exercised acknowledgement; automated acknowledgement and continuity checks passed |
| Historical congestion | Latest UI showed 24 real samples per corridor collected today and no earlier measured daily trend. Observed-only history, source/persistence validation and the distinct modelled opt-in have regressions. |
| Model diagnostics | Serving model provenance stays synthetic until a qualified replacement exists. New operator-only measured backtest reports source/window counts, 15/30/60-minute baselines and explicit insufficient-data reasons; model/artifact/leakage regressions passed. |
| Sign-in/session/sign-out | Automated service, component, hook, HTTP-boundary and PostgreSQL tests; real-SDK Google PKCE contract; fresh Google OAuth from localhost succeeded as Ian Wangsa with backend-resolved admin role. Secret replacement/revocation remains unverified. |
| Email signup and OTP | EJC verification, reload and repeat email-code login succeeded as driver. NUS Outlook receipt, original-address signup OTP, owner-entered password sign-in and reload restoration succeeded as CrossFlow Driver Demo / DRIVER from crossflow_profiles, with zero console errors. No account merge/email change. |
| Workspace/role discoverability | Driver shell and restricted navigation have UI coverage. The explicitly approved Google-account admin/operator role was resolved by the backend during a fresh successful Google login. |
| Hosted auth hardening | Minimal v1→v2 migration applied; independent SQL confirmed v2 health, invoker role guard and 3 unchanged profiles; full schema/backfill not run |
| Protected machine operations | Unauthorized API requests and configured fixture workflows tested; no ingestion/retraining performed against the hosted project |

![Verified cross-border map](verification/cross-border-route.png)

Fresh verification in the configured main app used **Continue as Driver**.
Guest mode exposes Congestion Map and the Route tab's assigned-code lookup.
Code `H6SNDCM` loaded the persisted cross-border journey and its Journey map;
the planner, Operations and account history remain inaccessible to guests.

![Configured app guest route and Journey map](verification/codex-driver-route.png)

The cleaned main app also displayed the real **Ian Wangsa** account with its
server-resolved **driver** role. Drivers receive Congestion and dispatch-code
lookup. Full planning and Analytics → Ferry & Ports require an authorized
non-driver account; the new account/workspace explanation makes that boundary
visible. Ian's approved operator/admin role was verified in a fresh successful
Google login; credential rotation remains unverified.
The current admin session also opened the operator Ferry & Ports workspace
with zero console errors. [Operator workspace evidence](verification/operator-ferry-workspace.png).
The EJC driver Route view now shows **Assigned Routes** and dispatch-code
instructions, verified in the browser after a copy-only change.
[Assigned Routes evidence](verification/otp-driver-assigned-routes.png).

The backend now enforces the operator role on all three planning endpoints and
their aliases in Supabase mode, before computation or persistence. Public
dispatch retrieval stays open. A route code is issued only when local or shared
storage confirms a write; failed writes return an explicitly unavailable
preview without a dispatch code. Retrieval metadata states the actual storage
source and single-host/shared durability boundary.

After the final restart and complete six-source check, the real Google admin
created **`AHMKAQM`**, Batamindo → HarbourFront, `COMMUTER`, departing at
18:15 WIB. The real NUS driver entered the code with lowercase and spaces and
loaded the same ROAD → FERRY → ROAD journey, 12 directions and Journey map.
Independent public API checks returned HTTP 200 by both code and full ID, with
identical 62-point geometry, navigation and legs, `dispatch_available=true`,
and confirmed persistent-local/single-host storage. Both responses use
`private, no-store`. Unknown code `2222222` returned 404/no-store; all six
planner paths rejected valid unauthenticated requests with 401/no-store.
Earlier road code `9H2WQHF` (55 points) and ferry code `V5D9FB8` (62 points)
also remain retrievable after restart.
[Final operator/driver API proof](verification/final-operator-driver-dispatch.json).

The earlier [post-restart analytics proof](verification/local-current-analytics-api-check.json)
confirms two current analytics samples with five measured points,
advancing response timestamps, stable model metrics without retraining,
zero observed-history rows before collector activation and four reference-only ports with unavailable
operating measures. The final browser handoff above followed the session-recovery
changes and final restart. Successful local handoff is not a claim that every
external dependency or shared production store works.

After the measured-training changes and direct-key restart, the real Google
operator replanned the same request and received the same idempotent code
**AHMKAQM**. The NUS driver again loaded padded/lowercase input, 62 geometry
points, 12 directions, ROAD → FERRY → ROAD, and all 25 map tiles. Connections
showed the published 19:00 WIB → 20:55 SGT sailing and its current countdown;
this is not a live-vessel or seat claim. The History UI showed 24 real samples
per corridor, all from today's collection, with no earlier measured trend.
The new operator backtest correctly reports insufficient history. Runtime
checks confirmed **five direct TomTom segments**, a configured artifact path
waiting for eligible data, zero observed fit rows, anonymous backtest **401**
and anonymous retraining **403**.
[Runtime proof](verification/observed-task-runtime.json),
[driver map](verification/driver-observed-task-assigned-map.png),
[operator backtest](verification/operator-observed-backtest-local.png).

![Cleaned signed-in driver workspace](verification/workspace-driver-cleaned.png)

The measured fixed-departure CARGO_TRUCK route took **2.609 s** initially and
**0.052 s** on repeat in this local environment. These timings are a sample,
not a performance guarantee.

## ML evidence and bounded claims

The running 100-tree random-forest baseline uses **4,000 synthetic rows**
(3,200 fit / 800 holdout), with **zero observed training rows**. Its synthetic
holdout R² is **0.9490**, MAE **2.87** points and RMSE **3.61** points. Two fresh
model instances reproduced the same metrics and forecasts across five audited
scenarios.

The empirical P10–P90 interval covered **66.13%** of synthetic holdout values
against an **80% target**; mean interval width was **7.873** points and empirical
ensemble CRPS **2.223**. The interval undercoverage remains unresolved. These
are experimental tree-ensemble statistics, not validated real-world Batam
accuracy or a guaranteed 80% predictive interval.

The [congestion-engine validation artifact](verification/congestion-engine-validation.json)
records **2,000,000** randomized inference/router-cost scenarios with zero
invariant failures in **36.841 seconds**. It checks finite outputs, ordered
quantiles, bounded speed ratios, nonnegative delays, monotonicity and ferry
surge limits. It does not establish measured traffic accuracy or exercise two
million complete journey solves.

Before the collector was enabled, the same artifact recorded **zero observed database rows**, so the observed
chronological backtest returned `insufficient_observations` (at least 150
eligible unique observations are required). Its separate **synthetic replay**
used 3,360 synthetic rows and a 675-row outer holdout. At a 15-minute horizon,
the forest MAE was **6.6282** points versus **4.6786** for persistence; the
forest performed worse on that replay. Replay P10–P90 coverage was **37.19%**.
These results do not support an accuracy or model-superiority claim. The replay
did not replace the running model. Protected diagnostic/backtest API integration and retrieval resilience are now
implemented and passed real HTTP contract tests plus focused failure regressions.

The earlier engineering scenario audit returned a **405.3 kg/day** modelled avoidable-idling
projection. It is excluded from the current operating dashboard, where
measured emissions are unavailable. It is an audited sample under stated assumptions, not a fixed
540 kg/day, a measured carbon saving or a demonstrated customer benefit. The
forecast and carbon outputs remain visibly modelled.

See [the Visa demo runbook](VISA_DEMO_RUNBOOK.md) for the timed operator/driver
story, current evidence and safe fallbacks while credential rotation and observed
data validation remain pending.

## External limits

- The Sindo discrepancy was resolved by reviewing all six supported route cards
  on the [official timetable](https://app.sindoferry.com.sg/schedule/): the
  snapshot now includes the daily 20:30 SGT HarbourFront → Sekupang sailing and
  replaces the reverse direction's old 17:40/18:30 WIB slots with the published
  17:00/20:30 WIB slots. Daily/weekend qualifiers remain distinct. Only Sindo's
  source/service review dates advanced in the committed data; the global
  snapshot review date was preserved. The final complete six-source runtime
  check passed and records **23 October, 16:54:46 WIB / 17:54:46 SGT** in the
  current process. Local route authority explicitly remains the committed
  timetable simulation rather than claiming shared durable freshness.
  [Scoped review](verification/sindo-supported-routes-reviewed.json),
  [complete source check](verification/final-official-source-checks.json).
  Timetables still do not establish seat availability or actual departures.
- Current TomTom segment speeds were verified through the public Vercel relay.
  The optional Google metric comparison, constrained Supabase pgRouting and
  shared Supabase persistence lack live round-trip proof; their contracts and
  failure paths have fixture coverage. Hotspots and forecasts remain modelled;
  unsupported terminal queues and measured CO₂ remain unavailable.
- Real device geolocation permission and every external photo host were not
  exercised. Permission/error behavior and photo fallback have automated tests.
- Earlier planning/analytics browser checks used a temporary frontend on port
  3001 in the app's public configuration; that server is now stopped. Those
  checks did not establish an administrator identity or authenticated hosted history.
  Fresh Google admin login and EJC email-code driver login/reload/repeat were
  subsequently verified in the configured main app. The admin also opened
  Ferry & Ports successfully. Later authenticated operator/driver checks verified
  persisted road code `9H2WQHF`, ferry code `V5D9FB8` and the final fresh
  operator-to-driver handoff `AHMKAQM`; authenticated hosted
  shared-history persistence remains unverified.
  The main frontend on port 3000 retains the real Supabase configuration and a
  sign-in gate before guest continuation.
  **Continue as Driver** exposes Congestion Map and assigned-code lookup in
  Route; it does not unlock the planner, Operations or account history. The
  fresh guest `H6SNDCM` lookup and Journey map were verified in Codex's in-app
  browser. If no verified account can sign in, limit the live demonstration to
  these guest surfaces and recorded operator evidence rather than bypassing
  the protected views.

To restart the configured main app, run `./scripts/dev.sh` from the repository.
Local route/history databases and environment files are ignored by Git.

## Latest integrated evidence

- Frontend: 403 tests in 39 files after restored model analytics, official reports,
  session recovery and the canonical timetable review, plus
  ESLint, TypeScript and production build; includes the earlier Assigned Routes
  copy changes.
- PostgreSQL: 26 tests pass, including account associations and stopped-speed
  migration preservation/rollback.
- Python: integrated backend/auth suite **418 tests/397 subtests passed** in
  137.00s; `/tmp/crossflow-observed-final-all-backend-verified.log`. After the
  approved final holdout-metadata fix, **22 focused tests/11 subtests passed**;
  `/tmp/crossflow-observed-metadata-final.log`. Standalone was **152/153** plus
  the corrected source-date case **1/1 separately**; do not call this a fresh
  153/153 full command. Logs: `/tmp/crossflow-observed-final-standalone.log` and
  `/tmp/crossflow-shared-ferry-source-metadata-regression.log`.
  The relay/diagnostics/math subset also passed 28 tests/74 subtests.
- Post-restart current analytics: two real TomTom point snapshots, 5/5
  corridor coverage, advancing response timestamps and stable model metrics.
  Before collector activation, observed history was empty. The first live
  collector poll at 17:40:23 SGT inserted five real TomTom observations, one
  per corridor; each observed-history response showed its original time and
  excluded synthetic seeds. An isolated replay of those actual points inserted
  five rows, then zero rows/five duplicates after reopening SQLite.
  [Live archive/report proof](verification/local-live-archive-and-reports.json).
  The same artifact confirms both dispatch codes survived the collector/scraper restart.
- Final browser and API handoff: Google admin issued `AHMKAQM` after the final
  source check; the NUS driver loaded it with normalized code input. Code and
  full ID return matching geometry/navigation and confirmed local persistence.
  [Proof](verification/final-operator-driver-dispatch.json).
- Complete official-source check: 6 verified, 0 failed, 0 permission-gated;
  Sindo's six supported route cards match 51 departure slots.
  [Proof](verification/final-official-source-checks.json).
- Latest stress report: 2,000,000 scenarios on the running **synthetic baseline**,
  zero invariant failures, 40.48 seconds; measured-history backtest explicitly
  insufficient. [Report](verification/observed-task-engine-validation.json).
- Normal public Vercel access returned live measurements without an auth bypass.
  The protected-fetch action was rejected because it could create a bypass link;
  no such link was created and it was unnecessary.
- Earlier current-traffic checks used the public relay. The latest runtime
  check returned five `tomtom_live` segments with `direct_tomtom` transport;
  30 hotspots remain modelled. [Proof](verification/observed-task-runtime.json).
- Hosted read-only table check: legacy shared routes exist; measured spatial
  history and the new account-association table do not yet exist. No shared
  rollout or production deployment was performed.

See [engine evaluation](CONGESTION_ENGINE_VALIDATION.md) for methods and limits.

![Local Vercel-fed live traffic](verification/local-vercel-live-traffic.png)
![Approved operator role saved](verification/operator-role-saved.png)

## Analytics and additional Batam evidence — follow-up

The History speed-survey and benchmark cards now have an explicit 16 px content rhythm and footer gap, consistent inset padding, left-aligned table captions, and padded native dropdowns with chevrons inset 16 px from the edge. Narrow screens allow controls to wrap. The new seven-day Batam intersection card shares the same layout. Native select behaviour and forced-colour system arrows are preserved. Ferry departure and arrival clocks now include WIB/SGT labels, preventing an apparently earlier Batam arrival from being mistaken for a negative travel time.

Another 28 dated historical Batam saturation ratios (12–18 June 2023) are bundled with a source/hash/licence manifest and served read-only. Their units remain volume/capacity ratio; they are not converted into fabricated speed targets. Six area-level 2024 speed ranges are separately transcribed with blank exact timestamps and excluded from fitting. The additional Gajah Mada 2026 and Bunga Raya/Raden Patah papers do not establish the missing dated raw speed archive. See the renewed source audit in `HISTORICAL_TRAFFIC_SOURCES.md`.

Fresh automated checks: **416 frontend tests / 41 files**, lint, TypeScript and production build passed. **102 focused backend tests** passed, covering research integrity, observed training and backtesting, operations, simulation, history, ports and official reports. Source/type tests explicitly reject attempts to treat saturation values as speed labels. Earlier complete backend/authentication regressions remain recorded above; this follow-up does not claim they were all repeated.

In the real operator session, observed-data evaluation returned **315 eligible real rows**, **45 selected originals**, **2.75 hours**, **one local date** and **four observed hour bins**. The purged partitions were insufficient; no observed model was activated and no field-accuracy score was invented. The app cannot responsibly replace its labelled synthetic network forest with this archive or with the differently measured academic summaries. New live collection continues locally; a sufficient dated speed archive still remains unresolved.

Final follow-up verification after the driver notice: **418 frontend tests / 41 files**, lint, TypeScript and production build passed. Driver code entry normalizes lowercase `ahmkaqm` to `AHMKAQM`, retrieves the saved journey after a backend restart and session reload, and renders its ROAD → FERRY → ROAD map, 12 directions and saved ferry connection. The departed ferry is labelled departed; the Summary explicitly warns when the saved departure has passed. That warning refreshes on focus and every 30 seconds with a one-minute tolerance. It does not silently alter the dispatched itinerary.

All six Analytics tabs were exercised in the real admin session: Overview displayed five current measured corridor points and a period-labelled BP Batam report; Ferry & Ports rendered four terminal references, future departures and the truthful no-cargo-feed state; its fresh official-source check completed **6/6** with **zero sources needing review**; Trends recomputed after choosing Rain; Alerts correctly had zero qualifying observed incidents; History filtered both Batam studies and rendered current collected history plus the separate Singapore benchmark; Model refreshed status and completed the real-observation evaluation. Missing queue, berth, customs, shipment and measured-emissions feeds remain unavailable rather than being filled with estimates. Both browser console error logs were empty.

[Browser verification](verification/analytics-browser-recheck.json), [API boundary checks](verification/analytics-api-recheck.json), [History spacing and dropdowns](verification/analytics-history-alignment-after.png), [survey dropdowns](verification/analytics-history-dropdown-after.png), [ferry timezones](verification/analytics-ferry-timezones-after.png), [driver notice](verification/analytics-driver-assigned-warning.png), [driver map](verification/analytics-driver-map-final.png), [observed readiness](verification/analytics-observed-model-readiness.png).

**Unresolved requested outcome:** no adequate free Batam network speed archive was established, and the serving forecast model remains explicitly synthetic. The renewed search adds genuine historical records but not enough eligible speed labels to justify replacing that model. No model promotion, paid account, external request, purchase, push or deployment was performed in this follow-up.

## Latest full local website audit, 23 October 2026, 21:15 SGT

[Complete audit](WEBSITE_AUDIT_2026-10-23.md) and [machine-readable summary](verification/website-audit-summary.json). Final checks: **422 frontend tests / 41 files**, lint, TypeScript and build; **458 backend/auth tests / 399 subtests**; **153 standalone backend checks**; **26 local PostgreSQL policy/migration checks**; zero reported frontend dependency vulnerabilities and no broken Python requirements. A further **1,000,000** numerical/router-cost scenarios passed with zero invariant failures. Counts overlap some semantic coverage and do not prove perfection.

The real operator and driver sessions exercised all six analytics tabs, ferry sub-tabs, code lookup/recovery, full saved journey sections, three-stop solving, alternatives, vehicle comparison, map-picker cancellation and route-history restoration. The new operator code **3B55FVN** loaded in the separate driver account, with departure **24 October 2026, 18:05 WIB**. Old **AHMKAQM** retains its expired-departure notice. The 48-row published Batam speed survey was actually trained and chronologically backtested with zero synthetic rows; the broader serving model remains synthetic because observed network promotion gates still fail. See the audit for baseline comparisons and missing feeds. No deployment or credential change was performed.


## Website, operator and driver data-flow audit — 24 October 2026

The local operator workspace and guest driver workflow were exercised from the browser through the actual API and persistent route store. All six operator analytics tabs, three ferry sub-tabs, four historical datasets, road planning, Saved history restoration, missing-code recovery, dated-journey warnings and road/ferry maps were checked. Operator dispatch **XK7RWH4** was created and retrieved by the driver flow; the earlier ferry dispatch **3B55FVN** retained its three-leg itinerary after the backend restart. Keyboard Home/End navigation also worked. The existing layout and font family were preserved, and the UI contains no CCTV video or upload controls.

The audit fixed actual data-flow defects: old alert acknowledgements hid later observations; cached failed ferry checks could appear successful; model status reported candidate metrics while persistence was serving; readable historical evaluation required writes; complete live provider data unnecessarily depended on a warm forecast model; and the 30-second page poll could outlast a camera aggregate's original 30-second TTL. Numeric summaries now load independently, normally every 10 seconds and sooner before expiry, while unchanged/stale data still disappears on time. Six fake-time regressions and five real browser observations spanning 32 seconds verified the refresh behavior. No transient unavailable estimate appeared in those final observations, and values/timestamps changed.

All 28 registered Matanya playlist sources now receive bounded availability/recovery checks. Availability alone cannot authorize inference, calibrate road regions, validate accuracy or enable routing. Source clocks are rechecked at read time, lifecycle restarts remain bounded, and playlist checks reject known media bodies without fetching segments. Camera 51 recovered during the final check; five camera estimates were fresh at the final API read. These counts are dynamic. Only the currently stale camera 15 has a provisional directed-edge binding, so the active camera routing-edge count remains **zero**. Directional calibration of the other fresh views remains required.

Historical inputs are connected according to their measurements and dates: genuine archived provider speeds drive observed history and forecast evaluation; dated Batam speed/intersection surveys and foreign benchmarks remain scoped research evidence; current camera counts/occupancy remain separate from measured road speeds. The current serving forecast strategy is persistence. Ferry timetable checks completed against six official sources and the successful cached recheck preserved its verification time. Live vessel status, passenger/berth queues, cargo operations and measured emissions reductions remain unavailable where no matching feed exists.

Final checks passed **600 frontend tests across 55 files**, full lint, TypeScript and production build. Separate runs passed **34 HTTP contract tests with 35 subtests**, **131 auth/history/operations tests with 148 subtests**, **15 CCTV API tests with 14 subtests**, and **34 focused independent availability/data checks with 20 subtests**; backend runs overlap and are not additive unique-test counts. The two audited browser consoles recorded zero errors. Nine real public API reads returned 200; anonymous operator actions/history/forecast/CCTV status returned 401 with no-store headers.

The API process stopped during a reload, and saved-session recovery correctly failed closed. It was restored as detached PID **5526** with private ignored logs, then verified after launcher exit and independent commands. The existing operator session restored through its normal retry, with no new sign-in email, credential/role change or exported browser token. The genuine archiver, observed-model service, route dispatches, forecast ledger and ferry verification survived. No orphaned or duplicate capture sessions remained. The signed-in driver login itself was not repeated: browser driver testing used the existing guest driver entry, while signed-in role boundaries were covered by isolated HTTP/auth tests. The actual operator-authenticated availability diagnostic read was not performed; its protected contract was verified in isolated tests.

[Full machine-readable audit](verification/website-full-audit-2026-10-24.json), [source-to-output matrix](verification/analytics-source-integration-audit-2026-10-24.json), [API evidence](verification/website-dataflow-api-2026-10-24.json), [browser observations](verification/website-workspace-browser-audit-2026-10-24.json), [detached service recovery](verification/cctv-detached-api-restore-2026-10-24.json).

![Operator analytics](verification/website-operator-overview-2026-10-24.png)

![Driver saved ferry map](verification/website-driver-ferry-map-2026-10-24.png)
