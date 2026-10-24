# CrossFlow local website audit — 23 October 2026

The local driver and operator workflows pass the checks below. A real-data
Batam research forest has now been trained and backtested, but the **serving
network model remains the synthetic baseline**. There is still no adequate,
verified, free historical speed archive for the five supported Batam corridors.
This is a tested local demonstration with material data limitations, not a
claim of production readiness, perfect forecasting, or an exhaustive security
penetration test. No push, deployment, provider purchase, account creation or
credential change was performed in this audit.

## Data acquisition and actual training

Eight further source checks are recorded in
[the acquisition audit](verification/website-audit-research-sources.json).
Three additional public PDFs were downloaded and inspected: the Gajah Mada
diversion study, the Gajah Mada U-turn closure study, and the Tengku Sulung
Botania performance study. They do not establish a usable additional dated
speed series. The UIB Ahmad Yani thesis describes relevant measurements, but
its results chapter is restricted to repository staff. Restricted material was
not accessed. The previously identified Universitas Batam paper has 56
weekday/hour/road-state speed means, without established calendar dates.
Undated observations, calculated free-flow references, volume/capacity ratios,
simulated outcomes and other cities' speeds were not relabelled as Batam
chronological training data. **New eligible speed labels: zero.**

The existing [Kurniawan–Anwar survey](https://www.journal.unrika.ac.id/index.php/sigmateknika/article/view/5642)
contains **48 published measured-speed aggregates**, two original directions,
one 100 m site and 16–18 February 2023, under the article's CC BY-SA 4.0
licence. The new [offline runner](../scripts/backtest_batam_survey.py) validates
the reviewed CSV checksum, uses only time phase and direction as predictors,
and fits fixed Random Forest parameters without using either test day for
tuning. It never reads target travel time or derived density as a predictor.

| Evaluation day | Earlier training rows | Held-out interval means |
| --- | ---: | ---: |
| 17 February 2023 | 16 | 16 |
| 18 February 2023 | 32 | 16 |

All training intervals end before midnight at the forecast origin. These are
next-survey-day forecasts, not operational 15/30/60-minute forecasts.

| Model/baseline | MAE (km/h) | RMSE (km/h) |
| --- | ---: | ---: |
| Random forest | 2.637907 | 3.224252 |
| Previous survey day, same time and direction | 3.207188 | 3.819249 |
| Historical same-time/direction mean | 2.907969 | 3.416295 |

There are **zero synthetic training rows** in this experiment. The 32 forecast
errors are correlated aggregates from only two test days, not independent
vehicle samples. One published speed/inverse-mean-time discrepancy remains
flagged and unaltered. Exact geometry and canonical corridor mapping are still
unverified. These results are insufficient for network model promotion.

[Full predictions and fold audit](verification/batam-site-backtest.json),
[reviewed API artifact](../backend/data/research/batam-site-backtest.json).
The local **Analytics → History** panel displays this benchmark, its baselines,
original survey rows and limitations through a validated read-only endpoint.
Refreshing or logging in does not alter historical measurement dates.

The separate 397,341-value Singapore LTA academic benchmark was rerun from
its checksum-verified files. Its results exactly reproduce the previous
15/30/60-minute scores. Those are ordinal speed-band errors, not km/h and not
Batam results. [Rerun report](verification/singapore-site-audit-retest.json).

## Fixed findings

| Priority | Finding | Implemented correction |
| --- | --- | --- |
| P1 | Optional Google route benchmark could reach a paid provider without verifying the operator when enabled. | Backend checks the current server-resolved role before key lookup, graph work or provider requests; frontend forwards the operator session. Disabled remains disabled. Missing and driver identities are tested before any work. |
| P2 | Malformed OSRM route/step/coordinate containers and non-finite values could escape parsing and crash a journey. | Bound reads to 2 MB, validate metrics/coordinates/legs/steps and count limits, and use the existing explicitly estimated fallback on failure. Missing directions no longer masquerade as provider steps. Verified TLS is retained and provider exception contents are not logged. |
| P2 | Tomorrow's scheduled journey displayed “Depart now” and only a clock time. | Both roles show a scheduled-departure label with absolute date and source timezone. Old driver assignments retain their passed-departure notice. Clock state updates on focus and every 30 seconds. |
| P2 | A road-only result displayed a ferry-timetable warning. | The warning applies to journeys containing a ferry leg. |
| P2 | Corridor model estimates could be confused with live provider points. | Explicit modelled-estimate text accompanies corridor details. Driver map action opens assigned routes with an appropriate label. |
| P2 | Selected vehicle's comparison CO₂ estimate could disagree with the selected route's summary. | The active row uses the saved route's road time and CO₂ estimate. Other vehicles remain labelled profile estimates; “Road + buffer” explains exclusions from the full journey ETA. |
| P2 | Selecting an alternative preview left the recommended journey's driver code on screen without explaining the distinction. | An alternative preview explicitly states that the code retrieves Recommended. Selecting an alternative is not represented as publishing a changed assignment. |
| P2 | CI installed pytest after unittest discovery, although discovery imports pytest modules. | Install the test runner before discovery, while keeping it out of production runtime dependencies. |
| P3 | New backtest table/footer needed more spacing, and MAE used an uncertainty-style ± sign. | Added consistent section spacing and clearer heading size; MAE is shown as an error magnitude. Existing inset select arrows and history footer spacing are retained. |

Regression coverage includes malformed external payloads and oversized reads,
role spoofing and provider-work prevention, source integrity, whole-day splits,
future-label independence, chronology rejection, actual rendered historical
filters, selected route comparisons and future-departure advice.

## Verification performed

| Check | Result |
| --- | --- |
| `npm run check` in `frontend` | 422 tests / 41 files, ESLint, TypeScript and production build passed. |
| `.venv/bin/python -m pytest backend/tests backend/auth/tests -q` | 458 tests and 399 subtests passed. Includes local HTTP, authentication and data-boundary checks. |
| `.venv/bin/python backend/test_backend.py` | 153/153 checks passed. Overlaps some modular test coverage; counts are not an exhaustive feature guarantee. |
| `npm test` in `backend/auth` | 26 PostgreSQL schema, migration and row-level-policy checks passed locally. This does not assert remote migrations were applied. |
| Frontend `npm audit --json` | Zero reported vulnerabilities across all severities for the installed dependency tree. |
| Python dependency consistency | `pip check`: no broken requirements. A complete Python vulnerability scan was not performed. |
| Calculation stress | 1,000,000 production-forest inference/router-cost scenarios; zero invariant failures, 19.416 seconds. Not one million full journey solves or measured labels. |
| API read-through | Public planning metadata and telemetry returned 200. Private route listing returned 401 without credentials; machine-protected status endpoints returned 403. Research evidence was validated and no-store. |
| Source/format checks | Python compilation, launcher syntax and `git diff --check` passed. |

The stress run also evaluated the currently serving forest against 404 actual
archived TomTom points without modifying that forest. This differs from fitting
the new, private observed-data candidate. In that short archive window:

| Horizon | Forecasts | Serving forest MAE (index points) | Persistence MAE (index points) |
| --- | ---: | ---: | ---: |
| 15 minutes | 79 | 4.8725 | 2.3035 |
| 30 minutes | 40 | 5.1394 | 3.2217 |
| 60 minutes | 74 | 5.0395 | 4.8606 |

Persistence performed better by MAE in this window. These dependent,
short-window results do not establish general field performance. Assumptions
include clear weather, unknown ferry proximity and unknown ingestion delays.
[Full stress and observed evaluation](verification/website-audit-engine-validation.json).

## Real Codex browser verification

Only the Codex in-app browser was used, with separate localhost operator and
127.0.0.1 driver origins. Existing real Supabase sessions were reused. No new
OTP was sent, password entered, or Google consent flow repeated in this pass;
the automated email, signup, OAuth and session boundary tests were rerun.

Verified operator congestion/corridor controls, single road routing,
cross-border planning, three-stop Batam routing, alternative selection,
eight-vehicle comparison, saved route history and restoring a stored journey.
Checked all six analytics tabs: Overview, Ferry & Ports, Trends, Alerts,
History and Model; also Terminals, Departures and Cargo. Rain selection
recomputed the 24-hour forecast. Historical date/direction filters changed the
actual returned rows; the new Batam diagnostic appeared with API-derived
metrics. Current observations and published schedules retain distinct labels.
No live cargo/queue/berth measurements were invented for empty states.

Verified driver invalid-code recovery, lowercase code normalization, the old
AHMKAQM assignment's expiry notice, all four journey result sections and
account-panel Escape dismissal. A newly created operator cross-border route,
**3B55FVN**, appeared in operator history and loaded in the separate driver
account: **24 October 2026, 18:05 WIB**, ROAD → FERRY → ROAD. A separate
three-stop road test produced **5868AKD** and distinct alternative paths.
Both are single-server saved demonstration journeys, not bookings.

Visual review used the available Codex browser viewports, including the earlier
775×858 view and the final wider desktop view. Small-phone
and screen-reader testing beyond automated accessibility assertions remains
outside this manual pass. Browser evidence and screenshots accompany this
report; the [detailed browser evidence](verification/website-audit-browser.json)
identifies each checked view. Captured console error lists are empty for both
roles. Map-picker cancellation also preserved the existing waypoint and route.

## Remaining material limits

1. **P1 — Observed network model not ready.** At the 21:05 SGT browser check,
   candidate evaluation saw 409 eligible raw points, 60 selected originals,
   about 3.39 hours, one local date and five local hour bins. Minimum seven-day,
   temporal-coverage, partition-size and corridor-coverage gates failed. It
   correctly refused promotion. The serving model still reports 4,000 synthetic
   training rows. Keep collecting while the local backend runs; this does not
   backfill dates before collection started.
2. **P2 — Unconnected operations feeds.** Cargo tracking, live queues, berth
   occupancy and measured emissions remain unavailable. BP Batam period
   aggregates and published ferry schedules are useful references, not live
   terminal or vessel telemetry. The six-source ferry-check report displayed in
   this pass was the previously completed report, not a new six-source scrape.
3. **P2 — Local persistence scope.** Route/history storage is durable SQLite on
   this API instance and is labelled accordingly. Shared Supabase route/history
   migrations and cross-deployment retrieval were not verified or changed.
4. **Dispatch design boundary.** A seven-character code is a capability to read
   its saved recommended journey. Account-wide listing, planning and operator
   evaluations are separately authenticated. Alternative selection previews a
   path; it does not republish the dispatch code with a different assignment.

Reproduction commands and source files are kept with the repository. The
latest machine-readable summary is
[website-audit-summary.json](verification/website-audit-summary.json).
