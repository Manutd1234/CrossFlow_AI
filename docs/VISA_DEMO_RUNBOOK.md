# CrossFlow AI — Visa team demo runbook

**Readiness: LIMITED.** Local planning, driver lookup, analytics and ML flows
have been exercised. The local app now receives real TomTom corridor speeds
directly through the owner's saved server key; the public Vercel relay remains
an earlier optional fallback. Current operating congestion uses those
timestamped point observations; unsupported fleet/emissions figures stay
unavailable, and recorded observed history stays empty until measurements
exist. Hotspot priorities and the synthetically trained forecast remain modelled.

Trends can use the actual model's 24-hour simulation and clear/rain/storm
comparisons, explicitly labelled with quantiles and synthetic training scope.
Observed history is separate. When enabled locally, the background collector
adds fresh validated traffic points every 120 seconds, preserving their
original times and deduplicating repeated snapshots; it never invents a past
archive. Modelled historical preview is an explicit opt-in.

**Historical research demo:** open **Analytics → History**. The top evidence panel shows 48 published Batam speeds from 16–18 February 2023; change survey date and original direction to inspect actual rows. Below it, the historical Singapore ML benchmark shows 53 links, 397,341 speed-band values and chronological held-out results at 15/30/60 minutes, with zero synthetic training rows. This benchmark is separate from the serving Batam model. Open the source/licence disclosures; do not present the old Singapore results as Batam km/h accuracy. The measured-history chart further below shows independently archived current Batam observations. See [historical source audit](HISTORICAL_TRAFFIC_SOURCES.md).

The approved demo mapping is:

| Identity | Role and demo purpose | Current state |
|---|---|---|
| `ianwangsa2@gmail.com` | Operator/admin, Google sign-in | Fresh localhost Google OAuth succeeded; backend resolved Ian Wangsa as admin; secret rotation unverified |
| `ian1234@comp.nus.edu.sg` | Driver, password signup + OTP | Outlook receipt, original-address signup OTP, owner-entered password sign-in and reload restoration verified as CrossFlow Driver Demo / DRIVER |
| `2021.ian.abiel.wangsa@ejc.edu.sg` | Driver, passwordless email code | New-account and confirmed-account UI verification succeeded as CrossFlow OTP Demo / driver; session survived reload |

Google and custom SMTP settings are saved. Google has the correct Client ID,
a nonempty masked secret and Enabled state. Which Brevo key label is saved,
including **CrossFlow AI v2**, is unverified; both usage labels remain `--`.
An original Google secret appeared once in a copy-button label in tool output;
replacement and old-secret revocation remain unverified. Never display
credentials in the demo; owners enter new secrets and passwords directly.

The driver workspace offers Congestion and assigned-code lookup in Route.
The operator workspace adds planning and **Analytics → Ferry & Ports**. A role
change in the database is not proof of a refreshed operator session; this
operator role was also verified during a fresh Google login. NUS password
sign-in and reload restoration have also passed with the backend-resolved
driver role and zero console errors.

In Supabase mode the API requires the verified operator/admin session to plan
routes; drivers retain public retrieval of an issued dispatch code. Check
`dispatch_available` before presenting a code. Failed storage writes produce a
preview without a code, and `route_storage` distinguishes current-host local
storage from shared durable storage.

This is a local engineering demo. The synthetic historical replay currently
underperforms latest-speed persistence and its intervals under-cover. No claim
of perfect forecasts, production readiness or measured impact is supported.

The story: an operator needs to get a delivery from an industrial area to the
right terminal, while a driver needs one clear, retrievable journey. CrossFlow
combines mapped roads, congestion scenarios and published ferry departures to
explain the plan and its limitations.

## Before the meeting

Run from the repository root:

```bash
./scripts/dev.sh
```

The launcher installs missing dependencies, loads server settings from the
gitignored root `.env`, and starts the API on port 8000 and the browser app on
port 3000. Browser settings belong in `frontend/.env.local`. Restart the
relevant server after changing either file.

- Open <http://localhost:3000> and let the first map load finish before sharing.
- Confirm the backend is available at <http://localhost:8000/docs>.
- Keep the frontend and backend Supabase URLs pointed at the same project.
  Only the publishable/anon key belongs in browser configuration.
- The **CrossFlow AI** Google Cloud project (`crossflow-ai-512506`) has been
  created with the user-approved **My Billing Account** link; no **Upgrade**
  action was selected. The external-testing OAuth app is saved following the
  user's explicit Google User Data Policy acceptance. Identity scopes
  `openid`/`email`/`profile` are saved and **CrossFlow AI Local Web** was created.
  Supabase's saved Google credentials and **Enabled** state supported a fresh
  successful localhost Google login as Ian Wangsa, with **admin** resolved by
  the backend. [Login evidence](verification/google-login-admin-success.png).
  Secret replacement/revocation remains unverified.
  The owner's Google test account was added after authorization and verified
  in the grid. Google's [basic-identity scope exception](https://support.google.com/cloud/answer/15549945?hl=en)
  means this `openid`/`email`/`profile` sign-in does not need a test-user
  allowlist; additional scopes or account restrictions can change access.
- The user is signed in to Brevo Free (300 sends/day), reports successful
  phone verification, and its blocking banner/dialog are gone. The existing
  `ianwangsa2@gmail.com` sender is Verified with display name **CrossFlow AI**;
  its freemail warning remains. Saved custom SMTP delivered the EJC numeric
  confirmation email after one `/otp` request at 16:13 returned 200/no session.
  Both templates are saved with `{{ .Token }}` and explicit `{{ .Email }}`
  recipient notices. Both updates were independently reloaded with Email/Token
  persisted and Save disabled.
  the sign-in subject is **Your CrossFlow sign-in code**. At 16:22, **Email me
  a sign-in code** requested/verified another confirmation code with type
  `email`; the backend resolved CrossFlow OTP Demo as **driver** without an
  agent-set password. The driver session survived reload and its modal
  confirmed the role source as `crossflow_profiles`. A repeat confirmed-account
  email arrived at 16:23, subject **Your CrossFlow sign-in code**; its UI
  verification also succeeded as driver with zero console errors. Full frontend
  check: **403 tests / 39 files**, lint, TypeScript and
  build, including 41 SignInPanel and 31 email-OTP service tests.
  Follow [signup email setup](SUPABASE_SIGNUP_EMAIL_SETUP.md)
  for the resolved NUS mail trace, verified password sign-in and
  credential-rotation limits.
- For account-scoped local history, set `CROSSFLOW_LOCAL_ROUTE_HISTORY=1` and
  use a writable, persistent `CROSSFLOW_ROUTE_DB`. History is labelled as local
  SQLite, and is not represented as a shared hosted database.
- The final real Google-admin → NUS-driver handoff used **`AHMKAQM`**, generated
  after the complete official-source check. Lowercase/spaced code input loaded
  the same 62-point ROAD → FERRY → ROAD journey, 12 directions and Journey map.
  Public code/full-ID retrieval returned identical geometry, navigation and
  legs with confirmed persistent-local/single-host storage and no-store headers.
  Earlier road `9H2WQHF` and ferry `V5D9FB8` also remain retrievable after restart.
  [Final handoff proof](verification/final-operator-driver-dispatch.json).
- Keep an already-tested route code available. On the audited local database,
  `AHMKAQM` is the latest cross-border example; `9H2WQHF` is the earlier road plan.
  Verify those codes again before presenting; they belong to that local
  database and are not portable fixtures.
- Use a future departure. Display Batam times as WIB (UTC+7) and Singapore
  times as SGT (UTC+8).

Public health checks do not require passwords or access tokens:

```bash
curl --fail --silent --show-error http://localhost:8000/api/auth/status
curl --fail --silent --show-error http://localhost:8000/api/model-status
```

`auth/status` being configured does not prove that Google credentials, an OAuth
redirect, email templates or SMTP delivery work. Test the actual user journey
before describing those integrations as ready.

## The 5–7 minute story

Use the verified Google operator and separate NUS driver accounts. A fresh
session must still resolve the correct server role; if either account becomes
unavailable, use the fallback below and recorded evidence for protected views.

| Time | Actor and action | Evidence to show | What to say |
| --- | --- | --- | --- |
| 0:00–0:50 | Sign in with the tested Google operator account. Show the EJC email-code driver workspace and restored session separately. | Fresh Google admin login, EJC new/confirmed-account email-code login and reload, role explanation and restricted driver navigation. | “Supabase verifies identity. The server resolves the role from the protected profile table; a driver account cannot unlock operator tools.” |
| 0:50–1:35 | Operator opens Corridor Map and inspects an industrial or terminal area. | Five corridors, 30 modelled hotspots, the source label and 30/60-minute forecasts. | “This is a planning scenario. Watch-list priority and current modelled congestion are different fields.” |
| 1:35–2:40 | Operator plans Batamindo Industrial Park → Batam Centre Ferry Terminal with `CARGO_TRUCK`, a future WIB departure and clear weather. Then compare storm weather if time permits. | Road geometry, ETA, vehicle assumptions, weather/congestion cost breakdown and dispatch code. | “The forecast informs a constrained A* search over mapped roads. Missing physical road tags remain unknown.” |
| 2:40–3:40 | Operator switches to `COMMUTER` and plans Batamindo → HarbourFront. | Batam road access → published ferry → Singapore road access, with local time zones and per-leg sources. Latest verified code: `AHMKAQM`, departing 18:15 WIB. | “The composer chooses a published terminal corridor. Ferry geometry is a planning line and does not establish a live vessel, seat or gate.” |
| 3:40–4:35 | NUS driver uses the issued dispatch code to retrieve the journey. Operator opens recent runs while signed in. | `AHMKAQM` loaded with lowercase/spaces, 12 directions and the Journey map; public code/full-ID geometry and navigation match. Account history is scoped to the caller. | “The driver receives one plan. Account-wide history requires a verified session; drivers see their own runs.” |
| 4:35–5:40 | An authorized operator opens Analytics, then Model; Ferry & Ports is available in the same workspace. | Current measured-point congestion, snapshot age/coverage, computed 24-hour model/weather scenarios, newly collected observed points, opt-in modelled history and parsed official reporting-period aggregates. | “Current measurements and forecasts have separate sources. The synthetic replay currently loses to persistence; field accuracy and realised carbon savings remain unmeasured.” |
| 5:40–6:30 | Discuss the next validation step. | Source confidence, chronological holdout, current calibration shortfall and optional telemetry adapters. | “The next milestone is verified traversal/speed data and field validation, with the same provenance and role boundaries.” |

The operator is a demonstration persona, not an automatic administrator role.
New email and Google accounts are created as drivers. Ian Wangsa's Google
account now has the user-approved admin/operator role, confirmed by an
independent SQL read and a fresh successful Google admin session. Verify again
after replacing the Google secret. Demonstrate any
administrator-only action only with an account that already has an authorized
admin profile. Do not change a role without approval, disable email
confirmation, or substitute
a machine token to make the demo proceed.

For the cross-border step, use `COMMUTER`. A heavy or light truck requested on
the published passenger-ferry services is deliberately rejected; an authorized
cargo or vehicle-carriage integration is a separate requirement.

## Verified AI and routing claims

The live backend builds a deterministic, 100-tree `RandomForestRegressor`
baseline at startup. It does not load the optional training scripts' model
files into this API. Inputs include cyclical time/day features, weather,
categorical corridor and road class, capacity, terminal distance and free-flow
speed. Current spatial defaults are declared assumptions.

The model publishes the ensemble mean, standard deviation and empirical
P10/P90 tree quantiles. These describe tree disagreement; they are not a
guaranteed 80% predictive interval. Published ferry proximity is a separate
post-model adjustment, capped at 15 points with a 45-minute exponential decay.

The route engine uses a vehicle-constrained, turn-aware A* search over the
committed OpenStreetMap graph. Its objective exposes time, distance, congestion
and road-quality components. Tests exercise access rules, motorcycle/car
differences, width, truck height/weight constraints and preference costs. It
cannot certify restrictions absent from the map or provide certified navigation.

The measured-only path uses server-owned source policies, purged chronological
fit/calibration/test groups, prior-only 15/30/60-minute baselines and explicit
promotion gates. Read-only operator evaluation never publishes a model. A
separate protected machine operation can atomically activate an eligible
candidate, persisting safe numeric JSON first when a trusted path is configured.
The real archive is too short; no real-data replacement is active. Current Flow
access works, but historical Traffic Stats returned 403. MyTomTom account setup
succeeded; MOVE still reports **Failed to fetch**. The alternative HERE Traffic
Analytics trial registration succeeded, ending 22 November 2026. A corrected
submission to the owner-requested `e1525902@u.nus.edu` also succeeded after the
first message did not arrive. Credentials are pending delivery verification; dashboard access, Batam coverage, export and ML reuse
rights remain unverified. No historical export or measured model replacement
is verified. [Training/import guide](OBSERVED_ML_TRAINING.md),
[source review](HISTORICAL_TRAFFIC_SOURCES.md),
[HERE trial confirmation](verification/here-trial-u-nus-success.png).

A suitable short claim is: “We combine a random-forest congestion scenario
with an auditable route optimizer and published ferry evidence. The demo
validates the software flow; field accuracy and operational impact still need
measurement.”

## Audit evidence — 23 October 2026

These are samples from the audited local environment, not service guarantees.

| Evidence | Result |
| --- | --- |
| Current training data | 4,000 synthetic rows; 3,200 fit / 800 holdout; **zero observed training rows** |
| Raw-forest synthetic holdout fit | R² **0.9490**, MAE **2.87** points, RMSE **3.61** points; excludes the external post-model ferry adjustment and is not field accuracy |
| Raw-forest synthetic P10–P90 coverage | Empirical **66.13% against an 80% target**; undercoverage remains unresolved |
| Other uncertainty evidence | Mean interval width **7.873** points; empirical ensemble CRPS **2.223** |
| Reproducibility | Two fresh model instances produced identical metrics and forecasts across five tested scenarios |
| Scenario sensitivity, corridor 1 | Clear weekday midday **29.7**; clear weekday evening peak **71.7**; storm evening peak **85.6**; clear weekend evening peak **46.7** |
| Ferry adjustment example | Imminent sailing changed the midday score from **29.7** to **44.7**, an explicit 15-point adjustment |
| First measured archive and official reports | Collector inserted five real points; actual-snapshot SQLite replay/reopen deduplicated 5/5. BP Batam parser retained Jan–Aug 2026: 51,733 vessel calls, 3,075,059 domestic and 3,643,016 international passenger movements, not live terminal counts. [Proof](verification/local-live-archive-and-reports.json) |
| Earlier engineering emissions scenario | **405.3 kg/day** projected avoidable-idling opportunity; not a measured saving and excluded from the current operating dashboard |
| Emissions assumptions | 40 advised trips/corridor/hour; 35% of modelled queue delay avoidable; 1.8 kg/hour idle burn |
| API read samples | Model **0.010s**, traffic **0.078s**, operations **0.040s**, corridors **0.038s**; all HTTP 200 |
| Current analytics after final restart | Two API samples used 5/5 real TomTom points; response timestamps advanced and trained-model metrics remained stable. This comparison preceded collector activation, so observed history had zero rows; four published-reference ports returned unavailable operating measures. [Proof](verification/local-current-analytics-api-check.json) |
| Cargo road-plan sample | **2.609s** first request, **0.052s** repeat, Batamindo → Batam Centre; route/code remained stable |
| Focused ML/vehicle checks | **39 tests passed**, including calibration/source weighting, retraining continuity and vehicle policies |
| Baseline API coverage | Full backend/auth **418 tests/397 subtests** passed; final holdout-metadata correction passed **22 focused tests/11 subtests**. Standalone: **152/153**, then the corrected source-date assertion **1/1 separately**; no fresh 153/153 full command. |
| Latest frontend full run | **403 tests in 39 files**, ESLint, TypeScript and production build passed, including measured-backtest contracts/UI, model scope, restored analytics and session recovery. Includes 41 SignInPanel tests, 31 email-OTP service tests and 4 email-isolation tests. Earlier production-build credential sentinels were absent. |
| Auth/history boundary evidence | Earlier auth service/HTTP/SQL runners passed; **26 PostgreSQL tests** passed for the current local shared-history atomic fix. Integrated backend/auth passed 418 tests/397 subtests, followed by the metadata-focused 22/11 pass. Standalone had 152 passes plus the corrected source-date case passing separately. The new history migration was not applied remotely. |
| Randomized engine stress | Latest **2,000,000 scenarios**, zero inference/router-cost invariant failures, **40.48s**, on the running **synthetic baseline**; not itinerary solves or measured accuracy. [Report](verification/observed-task-engine-validation.json) |
| Measured-only promotion readiness | Latest saved snapshot: **110 TomTom rows / 0.6922 hours**, insufficient. The new operator backtest requires at least 7 days plus fit/calibration/test, calendar, corridor, baseline and interval gates. No observed model has replaced the synthetic baseline. [Guide](OBSERVED_ML_TRAINING.md) |
| Latest real operator/driver repeat | Same request retained `AHMKAQM`; NUS driver normalized code input loaded 62 points, 12 directions, ROAD → FERRY → ROAD, the published connection/countdown and 25/25 map tiles. History displayed 24 observed samples per corridor collected today. [Runtime](verification/observed-task-runtime.json), [map](verification/driver-observed-task-assigned-map.png), [backtest](verification/operator-observed-backtest-local.png) |
| Synthetic replay, 15-minute horizon | 3,360 synthetic input rows, 675 outer-holdout samples; forest MAE **6.6282** vs persistence **4.6786** points; the forest performed worse. P10–P90 coverage **37.19%**. Running model unchanged. |

Earlier broad planner/analytics UI evidence came from the temporary public-mode
server, now stopped. Fresh Google admin login and the operator Ferry & Ports
workspace were subsequently verified in the configured main app; EJC driver
email-code login, reload and repeat sign-in also succeeded. A fresh
**Continue as Driver** check in Codex's in-app
browser exposed Congestion Map and assigned-code lookup; `H6SNDCM` loaded its
cross-border Journey map. This establishes restricted guest access, not access
to the planner, Operations or account history.
[Guest Journey map evidence](verification/codex-driver-route.png).
The cleaned signed-in driver shell has a visible workspace explanation and
account action; full planner/analytics access remains role-gated.
[Cleaned driver workspace evidence](verification/workspace-driver-cleaned.png).

The [congestion-engine validation artifact](verification/congestion-engine-validation.json)
records the stress and backtest results above. The stress proves sampled
invariants only. The synthetic replay's worse-than-persistence result and zero
observed rows keep field-accuracy claims unsupported. Retrieval resilience,
protected integration diagnostics and the read-only backtest API passed the
completed backend suite, with a further 28-test/74-subtest relay/diagnostics/math
check. Their protected/HTTP and fallback contracts have automated coverage;
this does not establish configured hosted-store availability.

The sample API evidence is available locally at
`/tmp/crossflow-demo-api-evidence.json`; focused ML/vehicle output is at
`/tmp/crossflow-demo-ml-checks.log`. Temporary evidence files are not deployment
artifacts.

## Authentication and external-source limits

The signup UI supports code entry, verification, resend and visible errors.
The new passwordless email-code flow is implemented; its first live UI request
and verification succeeded as CrossFlow OTP Demo / driver. A fresh Google
OAuth/admin session and actual EJC numeric email delivery are also verified.
The EJC session also survived reload and its account modal confirmed DRIVER
from `crossflow_profiles`.
[Driver restoration evidence](verification/otp-login-driver-restored.png).
The operator Ferry & Ports workspace opened under the admin session with zero
console errors. [Operator workspace evidence](verification/operator-ferry-workspace.png).
The EJC driver Route view displays **Assigned Routes** with dispatch guidance.
[Driver Route evidence](verification/otp-driver-assigned-routes.png).
The confirmed-account repeat verification also succeeded as driver, with zero
console errors. The owner submitted NUS password signup from
`http://127.0.0.1:3000/` at 16:26. The 16:49 NUS-only resend was found in
`e1525902@u.nus.edu` Outlook with **To: ian1234@comp.nus.edu.sg** and that same
address in the body. Its code verified the original NUS signup; the backend
resolved **CrossFlow Driver Demo / driver**. Reload restoration passed, and
the modal showed **DRIVER** with role source `crossflow_profiles`.
[NUS restored-session evidence](verification/nus-signup-driver-restored.png).
A separate localhost password sign-in using the owner-entered password also
passed, followed by reload restoration and the server-resolved DRIVER modal,
with zero console errors.
[NUS password-login evidence](verification/nus-password-login-restored.png).
No account email was changed or merged. EJC remains separate. The mail issue
is resolved and no support trace is required. The exact institutional routing
rule/full Received chain was not inspected.
[Resolved delivery trace](SUPABASE_SIGNUP_EMAIL_SETUP.md#nus-delivery-trace-and-handoff).
Codes are verified together with the requested email; do not promise globally
unique numeric digits across accounts.
[Exact-recipient SMTP events](verification/recipient-specific-smtp-delivery.png),
[NUS-bound code entry](verification/nus-recipient-bound-verification.png).
Keep readiness **LIMITED** while credential rotation and observed-data
validation remain pending.

The tested minimal
[`002_harden_auth_role_guard.sql`](../backend/auth/migrations/002_harden_auth_role_guard.sql)
upgrade was applied to the hosted project through Codex's in-app browser. An
independent clean SQL query confirmed schema version 2, RLS enabled,
authenticated read access, authenticated insert denied and an invoker role
guard (`SECURITY DEFINER` false). The three existing profiles were unchanged.
The full schema and its backfill were not rerun. The later real Ian Wangsa
session initially resolved as driver. The user then explicitly approved its
operator/admin role, and the update plus independent read confirmed it. A fresh
Google login now also resolved that admin role through the backend. Secret
replacement and old-secret revocation remain unverified.
[Hosted v2 verification evidence](verification/hosted-auth-v2.png).

Email signup must have Supabase **Confirm Email** enabled. With confirmation
enabled, an unverified signup does not receive a usable session; a profile row
alone does not sign anyone into CrossFlow. The backend verifies access tokens
with Supabase before profile lookup. A focused regression rejects a locally
forged token even when it claims a verified email. Disabling Confirm Email
implicitly confirms users and would defeat the intended signup policy.
[Supabase user semantics](https://supabase.com/docs/guides/auth/users),
[confirmation configuration](https://supabase.com/docs/guides/auth/general-configuration).

Supabase's default SMTP sends only to addresses belonging to the project's
organization team, currently allows two emails per project per hour, and has
no delivery SLA. Testing two addresses requires actual delivery eligibility
and remaining quota, or custom SMTP. Do not add test recipients to the
organization merely to obtain email delivery, since that would grant project
access. Signup/OTP resend also has a default per-user 60-second cooldown.
[SMTP restrictions](https://supabase.com/docs/guides/auth/auth-smtp),
[rate limits](https://supabase.com/docs/guides/auth/rate-limits).

The signup confirmation template now contains `{{ .Token }}` for the code-entry
flow and was saved and reloaded with its code preview. The separate Magic link
or OTP template is saved with **Your CrossFlow sign-in code** and `{{ .Token }}`.
Both now include `{{ .Email }}` with instructions to use the code only for that
address; both updated templates are independently saved/reload-verified with
Email/Token persisted and Save disabled.
The first actual EJC message used the confirmation subject **Verify your
CrossFlow account**. Its inbox delivery and first email-code UI verification
and reload restoration are verified; repeat confirmed-account verification also
succeeded after the **Your CrossFlow sign-in code** email arrived at 16:23.
New free-tier projects created from 3 June 2026
cannot customize templates
while using default SMTP; custom SMTP or the applicable paid plan removes that
template restriction. Further recipients and credential changes require their
own delivery checks.
[Email template variables](https://supabase.com/docs/guides/auth/auth-email-templates),
[template customization policy](https://supabase.com/changelog/46599-changes-to-email-template-customisation-on-free-tier).

The final official ferry refresh returned HTTP 200 with **all six sources
checked, zero needing review and zero permission-gated** at 17:54:46 SGT.
All six supported Sindo route cards were reviewed against its
[official timetable](https://app.sindoferry.com.sg/schedule/); the snapshot now
includes the daily 20:30 SGT HarbourFront → Sekupang service and the reverse
17:00/20:30 WIB slots. Daily and weekend qualifiers remain distinct. The
runtime complete-source verification time advances after actual validation;
only Sindo's committed review dates changed in this scoped review. Published
schedules do not establish live departures, seat availability or queues.
[Source-check evidence](verification/final-official-source-checks.json),
[Sindo review evidence](verification/sindo-supported-routes-reviewed.json).

TomTom speeds were verified through ordinary public Vercel API access and the
local relay; all five corridor segments were live. The optional Google metric
benchmark, constrained remote pgRouting and shared Supabase intelligence/history
stores still lack live round-trip proof in this audit. Their fallback paths
have tests, and their configuration is not availability proof.

## If a dependency is unavailable during the demo

- If a future Google or email-code sign-in fails,
  keep readiness **LIMITED**. The
  approved Ian Wangsa operator account can demonstrate the full workspace only
  after a fresh successful session. The separate NUS driver account has verified
  signup, password sign-in and reload restoration. If neither account is available, demonstrate
  **Continue as Driver**: Congestion Map, assigned-code
  lookup and the loaded Journey map. Use recorded operator screenshots or
  read-only API evidence for the remaining story. State that the planner,
  Operations and account history require sign-in. Do not bypass a protected
  view, role or email confirmation.
- If a geocoder is unavailable, choose a named location or map point. The
  backend now permits a fresh explicit retry after a provider outage.
- If an online road provider is unavailable, the journey may carry a labelled
  continuity estimate. Explain that limitation before showing the plan.
- If a ferry source changes, show the refresh warning and retained snapshot.
  Verify a real journey with the operator rather than inventing a replacement.
- If route computation is slow, wait for its result or use a route code already
  verified in the same local database. Say when a stored plan is being shown.
- If the backend is unavailable, restart it and check `/api/model-status`.
  Any browser simulations reachable after an authorized sign-in remain visibly
  labelled offline/modelled; they are not proof that the backend or
  authentication succeeded and do not open the sign-in gate.

## Current engineering checks

Frontend: 403 tests/39 files, lint, TypeScript and production build. PostgreSQL:
26 tests. Backend/auth: **418 tests/397 subtests** in the integrated run, then
**22 focused tests/11 subtests** after the approved holdout metadata correction.
Standalone: **152/153**, followed by the corrected source-date assertion
**1 passed / 152 deselected**, not a fresh 153/153 full command. Latest stress:
2,000,000 scenarios on the running **synthetic baseline**, zero invariant
failures in 40.48s; these are not itinerary solves or measured accuracy. The
operator observed backtest uses fixed 15/30/60-minute horizons and reports
insufficient history until its measured-only evidence gates pass. Historical
Traffic Stats entitlement is still pending. See [observed training](OBSERVED_ML_TRAINING.md)
and [engine evaluation](CONGESTION_ENGINE_VALIDATION.md).

The local-only DEV guard prevents test-admin credentials from entering new
production bundles. Existing deployed credentials still need owner rotation
and removal; do not treat a local build as revocation. New shared-history and
stopped-speed SQL upgrades remain unapplied to the hosted database.

## Latest tested local demo route and real Batam ML diagnostic

The latest audited cross-border demonstration code is **3B55FVN**, departing **24 October 2026, 18:05 WIB**. It was created by the real operator, restored from Saved → Recent runs, and loaded by the separate driver account. Its itinerary is road → ferry → road. Both roles show the absolute planned date; this is a saved estimate, not a booking or live vessel confirmation. Route **5868AKD** exercises a three-stop Batam road journey via Nagoya Hill Shopping Mall. Alternative selection previews a path; its code still retrieves Recommended, as the UI explains.

Analytics → History now also shows the **exploratory Batam site backtest**: 48 published measured-speed aggregates, 32 next-survey-day forecasts, zero synthetic training rows; Random Forest MAE **2.638 km/h**, compared with previous-day **3.207** and historical-slot-mean **2.908**. This demonstrates real Batam research training at one surveyed site. It does not establish network-wide or 15/30/60-minute forecasting accuracy. The serving network model still uses its labelled synthetic baseline. [Full audit and remaining limits](WEBSITE_AUDIT_2026-10-23.md).
