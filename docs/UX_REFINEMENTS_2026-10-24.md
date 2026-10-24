# Local UX refinements — 24 October 2026

## Latest revision: honest model evaluation

Model now shows prospective Batam forecasts saved before their outcomes, matched observations and test days. The old small retrospective window is explicitly called development evidence. Separate source-labelled tables show Grand Batam Mall and Guangzhou benchmarks, their MAE against persistence, tolerance hit rates, sample/day counts and collapsed confidence intervals. Error reduction is never labelled percentage accuracy, and foreign data is never presented as Batam field validation. See [training results and limits](OBSERVED_ML_TRAINING.md).

The check run passed 562 frontend tests, lint, TypeScript and production build; 101 distinct targeted backend checks passed across the main suite and final evaluation-module run. Browser proof is saved locally.

## Latest revision: real traffic, live weather and measured context

This revision supersedes the synthetic Model panel and 24-hour scenario UI
below. Trends and Model now use the protected real-only forecast endpoint for
15/30/60-minute forecasts, current monitored-point charts, a fresh/stale weather
card, mapped road context and actual chronological training evidence. The UI
shows the selected strategy beside forest and persistence errors, actual fitted
tree/feature counts, archive coverage, training time and background-trainer state.
A credential change clears old results; a failed refresh preserves prior results
with an explicit stale/error message.

The checked browser showed 1,064 real archived observations over 9.6 hours,
three fitted forests with 300 trees and 43 features, and fresh regional
Open-Meteo weather (25.9°C, overcast, 0 mm rain, 3.6 km/h wind, valid 03:15 SGT).
The model has zero synthetic rows. Weather-enriched training pairs initially
remain zero because only weather captured before a forecast origin is eligible.
The forests currently lose validation to persistence. Production-release checks
remain provisional; sparse intervals are withheld. The road table explicitly
labels nearby OSM context and unverified provider direction, with unknown tags
and capacity left unknown.

See [training design, actual results and limits](OBSERVED_ML_TRAINING.md) and
[aggregate evidence](verification/real-traffic-evaluation-2026-10-24.json).
Verification: 548 frontend tests across 51 files, ESLint, TypeScript, production
build and 92 targeted backend tests passed. Local browser checks confirm Model
and Trends render actual responses after a backend restart. The route request
returned HTTP 200 (9.11 km, 19.6 minutes road time); its corrected observed
reference badge and beyond-one-hour limitation rendered. The checked console
had no errors. [Route proof](verification/observed-route-reference-2026-10-24.jpg). The local collector
and hourly real-data retraining are enabled while the backend runs. No deployment
or push was made. [Browser proof](verification/real-traffic-engine-2026-10-24.jpg).

## Earlier revision: typography, route sizing and visible inference

This revision supersedes the earlier Inter typography and Model-panel layout
described below. Outfit headings/navigation and Manrope body text are bundled
locally with their OFL licences and source/hash manifest. Page introductions,
overview-card explanations and forecast copy are shorter.

- The Search layout no longer reserves an empty history row. Sidebar and route
  result now share a bottom edge. The journey map grows into the remaining
  panel height; the overview sits beneath it within the panel's padding.
  At 1280 × 720, both outer panels ended at y=680 and the overview ended at
  y=663. The active map panel's client and scroll heights both measured 342px.
  Compact displays keep the form controls scrollable and the action pinned.
  Leaflet now observes its container size and refits the route after resizing.
- Forecast Weather, Corridor and Recompute controls share the same top and
  bottom edges and 44px height. The alert title, count and Check traffic action
  have matching vertical centres (within the button's 1px hover translation).
- Removed the vehicle-count preview, observed backtest entry, polling/API
  paragraph and model-persistence paragraph from the Model tab. Those backend
  research tools remain available to development workflows. Training scope is
  available in a collapsed disclosure.
- Added **Live forecast check** to the Model tab. Clicking Run live forecast
  sends the current weather assumption and authenticated operator credential
  to `/api/analytics/simulation`. Its graph and numerical cards use that
  response; no local forecast constants are substituted. Changing weather or
  account discards prior or in-flight results.
- The simulation API now returns actual fitted estimator metadata, including
  estimator class, tree count, input-feature count and training timestamp.
  The browser returned **RandomForestRegressor, 100 fitted trees, 26 input
  features and 135 predictions** for five corridors. Clear-weather inference
  at 01:29 WIB returned a current mean of 13.7 and a 24-hour peak of 52.1.
  A subsequent Storm run returned 35.1 and 74.5 respectively, with the same
  actual tree/feature/prediction counts. The checked browser console had no errors.
  Training metrics remain separate from inference results: the same seeded
  synthetic baseline produces the same training scores until the model changes.
- A direct operations API check returned five fresh TomTom points with indices
  0–2.4 and zero alerts. Check traffic subsequently refreshed to a highest
  index of 0. The actual trigger remains **greater than 60/100**; zero alerts
  is a legitimate measured state. Trigger creation, freshness rejection and
  acknowledgement paths are covered by backend/frontend fixtures.

The engine already uses the actual clock/calendar, selected weather and
published ferry proximity. Real-world calibration, measured per-road spatial
features and a validated future-weather feed are still data gaps. The serving
model remains trained on synthetic profiles; these UI changes do not establish
real-world Batam forecast accuracy or turn tree dispersion into a calibrated
network confidence interval. The forecast scope disclosure retains those limits.

Verification: **485 frontend tests across 47 files**, ESLint, TypeScript and
production build passed. **33 targeted backend tests** passed across congestion
model, analytics simulation and operations analytics. `git diff --check` passed.
Browser checks used only the Codex in-app browser at 1280 × 720 and 778 × 988;
the latter uses the stacked responsive route layout. No deployment was made.

Screenshots: [route alignment](verification/route-aligned-map-2026-10-24.jpg),
[live ML inference](verification/live-model-forecast-2026-10-24.jpg),
[forecast controls](verification/forecast-controls-aligned-2026-10-24.jpg),
[traffic alerts](verification/alerts-checked-aligned-2026-10-24.jpg).

## Earlier revision: annotated review cleanup

The fifteen browser comments were addressed locally:

- Removed the workspace-access banner from the app shell, so it no longer
  appears on any main tab. The header account control and role-based routing
  remain in place. Removed the hotspot “About this watch” disclosure.
- Departure and arrival cards now use equal two-row controls. Hours/minutes
  are separate from the full-width AM/PM selector. In the 1376px browser view,
  both cards measured 161 × 145px; all four period buttons measured
  68.5 × 44px. Selecting each period also selected its corresponding time mode.
- Published operating records use three numerical cards and a proportional
  passenger-movement graphic. The live scraped period supplied 51,733 vessel
  calls, 3,075,059 domestic passenger movements and 3,643,016 international
  passenger movements. The graphic uses the two passenger totals only: 45.8%
  domestic and 54.2% international. Vessel calls are not mixed into that ratio.
  Repeated metadata and empty reporting-period disclosures were removed.
- Both forecast disclosures use matching chevrons at the far right. Their
  measured vertical-centre offset was zero, and their right edges matched.
  Opening forecast values still rendered all 24 hourly rows.
- History refresh uses the shared glass-button style: bundled CrossFlow Inter,
  14px text and 44px height. Removed the two introductory provenance paragraphs
  and the detailed History provenance block. Data-source badges, sample counts,
  genuine errors and partial-window warnings remain. The survey's site-method
  and source dropdowns were removed; the numerical site backtest and compact
  journal/PDF/licence links remain.
- Alerts now distinguishes active monitoring with no alerts from a missing
  feed. It shows monitored-point count, the backend trigger threshold, highest
  observed index and snapshot time, with an explicit “Check traffic” action.
  The backend returned five fresh measurements with scores from 0 to 2.5 and
  no alerts; the trigger is greater than 60. A subsequent browser refresh showed
  a highest score of 2.4, confirming that the panel updates from its source.
- The model panel prominently identifies saved training results and their
  training time. Direct reads of `/api/model-status` returned MAE 2.87,
  total_samples 4000, source `synthetic_profile_generator` and training time
  `2026-10-23T13:34:05.387781+00:00`. Those are computed metrics from the same
  saved synthetic model, not hard-coded frontend display values or live
  traffic counters. Refreshing status does not retrain or promote a model.

Verification: **440 frontend tests across 44 files**, ESLint, TypeScript and
production build passed, as did `git diff --check`. Added tests verify that MAE
and row count change with API model versions, that passenger shares exclude
vessel calls, and that healthy/no-data alert states and refresh actions differ.
Existing authentication tests now assert the verified identity and allowed
tabs rather than removed banner text. Browser checks covered time controls,
report totals, history refresh, survey disclosures, alert refresh and model
refresh. The checked operator console had no errors; relevant API requests
returned HTTP 200. Backend/database runtime code and model training were not
changed. No push or deployment was performed.

The real saved driver session was reopened in the Codex browser. Its banner was
absent, analytics navigation remained unavailable, and route 3B55FVN reloaded
successfully (Batamindo to HarbourFront, 111-minute total ETA). Its console had
no errors. Congestion still rendered 30 hotspot cards with no watch disclosure.

Screenshots: [time controls](verification/review-time-controls-2026-10-24.jpg),
[graphical reports](verification/review-published-records-2026-10-24.jpg),
[alert monitoring](verification/review-alert-monitoring-2026-10-24.jpg),
[saved model results](verification/review-model-results-2026-10-24.jpg),
[disclosure arrows](verification/review-disclosures-2026-10-24.jpg),
[clean History](verification/review-history-2026-10-24.jpg).

## Latest revision: visual analytics

The deployed reference was reviewed in the Codex in-app browser, using its
existing demo sign-in. Its two-column donut, bar and area presentation informed
six data-driven local charts:

- **Overview:** measured road speeds versus free-flow reference; a donut of
  smooth, heavy and critical conditions across the actual observed points.
- **Trends:** the next 24 hours with average corridor P10–P90 dispersion;
  clear/rain/storm counterfactual bars; first-hour versus peak-hour pressure by
  corridor; and an hourly corridor heatmap.

Charts use existing operations and simulation responses, with no extra provider
requests or invented emissions outcomes. Weather and corridor controls update
all forecast charts together. Missing corridor hours stay unavailable rather
than becoming zero. Measured point coverage and modelled forecast scope are
labelled separately. The serving model's declared source and limitations remain
available; no model training or backend/database runtime changes were made.

The chart grid uses two columns on desktop and stacks in narrower panes.
Tooltips show values and units; measured speed and hourly forecast tables remain
available. Heatmap cells have one tab stop, arrow-key navigation and a visible
inspection label derived from the current response, so it updates when refreshed.
Chart animations are disabled to keep polling updates steady.

**Verification:** 436 frontend tests in 44 files, ESLint, TypeScript and the
production build passed. Six added tests cover measured-data boundaries,
empty/invalid data, missing-hour alignment, Batam clock conversion, unavailable
chart states and keyboard inspection with refreshed values. Existing simulation
tests still cover access errors, late-response rejection, polling, retry and
retention of the last computed snapshot on failure.

The authenticated local operator browser rendered two Overview charts and four
Trends charts. The measured snapshot contained five points. Rain returned a
new computed curve; selecting Mukakuning reduced the heatmap from 120 cells
across five corridors to 24 cells for one corridor. Right Arrow moved the
inspection from the first forecast hour to the next. The 783px Codex pane
stacked the charts without page-wide horizontal overflow. Browser errors: zero.
Operations and simulation requests returned HTTP 200. The separate driver
still has no analytics navigation, and its assigned code remains 3B55FVN.
Nothing was pushed or deployed.

Screenshots: [measured Overview charts](verification/charts-overview-2026-10-24.jpg),
[forecast and weather](verification/charts-trends-forecast-2026-10-24.jpg),
[corridor pressure and heatmap](verification/charts-trends-corridors-2026-10-24.jpg).

## Latest revision: six-tab classic presentation

The follow-up requested at 01:01 restores **Overview, Ports, Trends, Alerts,
History and Model**. It supersedes the four-section arrangement documented
below, while preserving the current backend integrations and truthful data
labels. Changes are local; no deployment or credential changes were made.

- History now has **Observed traffic, Batam speed survey, Intersection study
  and Singapore benchmark** subtabs. Only one dataset is visible, and changing
  tabs preserves survey filters. Source units, dates and model-promotion
  limitations remain attached to their respective datasets.
- Hotspot cards have consistent photo frames, ranks, softer status pills,
  aligned scores and compact photo attribution. All 30 backend-ranked places,
  map selection and 60 attribution/licence links remain available.
- The route title and tab rail remain visible while the active content scrolls
  on desktop. A conflicting short-screen rule was corrected after browser
  testing at 1280 × 720. The three route-choice cards have equal dimensions.
  Dispatch-code notes now use a full-width row rather than narrow columns.
- Journey-map overviews are a concise sentence derived from the selected
  route's endpoints, distance and principal named roads or ferry connection.
  Complete turn instructions remain in Directions. The checked Alternative 2
  overview is 114 characters, rather than a paragraph of turn instructions.
- Removed the ferry **Sources & freshness** disclosure, the persistent
  source-check results panel and the general unavailable-operations paragraph.
  Schedule checking still runs, and genuine failures show a concise error.
  Terminal cards retain their actual last-verification time, published next
  departure, unavailable measurement labels and photo credits.

Verification: **430 frontend tests in 42 files passed**, followed by ESLint,
TypeScript and a production build. Tests cover retained history filters,
dataset separation, concise route summaries, route-selection consistency,
refresh coalescing and the removed ferry diagnostics. A final CSS-only
alignment adjustment was followed by another production build.

The Codex in-app browser checked the real operator and driver sessions:
all six analytics tabs; all four History subtabs; the survey-date filter;
schedule refresh; terminal filtering; Departures and Cargo; weather-dependent
backend forecasts; model status; route alternatives; directions; and assigned
route lookup. The operator restored **QG7TDQJ** and the driver successfully
reloaded **3B55FVN**, including road, ferry and onward-road legs. The driver
still has no analytics navigation. Both checked browser consoles had no errors.
Relevant ferry refresh, simulation, research-history, model-status and route
retrieval requests returned HTTP 200. See the
[browser evidence](verification/classic-browser-2026-10-24.json).

The serving Batam network model remains the explicitly labelled synthetic
baseline. The real 48-row Batam survey/site backtest and Singapore research
benchmark remain separate; this UI revision does not claim new training,
network-wide measured coverage or live terminal operations. Backend and
database runtime code was unchanged in this pass; earlier audit results are
recorded in the linked hardening reports, not counted as freshly rerun here.

Screenshots: [History navigation](verification/classic-history-tabs-2026-10-24.jpg),
[hotspots](verification/classic-hotspots-2026-10-24.jpg),
[equal route options](verification/classic-route-alternatives-2026-10-24.jpg),
[concise route overview](verification/classic-route-overview-2026-10-24.jpg),
[ferry cards](verification/classic-ferry-2026-10-24.jpg),
[driver journey](verification/classic-driver-route-2026-10-24.jpg).

## Earlier revision: four-section presentation

The location search and analytics presentation were refined using the supplied
screenshots and deployed CrossFlow reference. Code changes remain local.

## Changes

- **Location search:** the suggestions float outside the sidebar's clipping
  area, follow the input during scrolling/resizing, and fit above or below it.
  Map actions, search results and local suggestions share one scroll surface.
  Pointer highlighting no longer triggers automatic scrolling. Keyboard
  selection scrolls only the suggestions, and typing reopens a dismissed menu.
  Outside click, Escape and Tab dismissal remain supported.
- **Freshness banner:** status, timestamp, refresh action and source details
  have distinct alignment and spacing. Expanded provenance uses spaced
  paragraphs instead of a cramped nested scroll box.
- **Analytics:** six main tabs become **Overview, Ferry & Ports, History and
  Forecasts**. Alerts are available in Overview; model details and backtesting
  expand within Forecasts. Historical filters and model state are retained
  across section changes. The metric grid uses four desktop columns, two
  tablet columns and one narrow-screen column.
- **Ferry header:** one concise verification line replaces the repeated large
  timestamp badges. Source-check and retrieval times remain available under
  **Sources & freshness**, with their separate meanings preserved.

No measurement values, provider definitions, model provenance or authentication
permissions were changed. Missing observations still appear as unavailable.

## Verification

`npm run check` passed: **425 tests across 41 files**, ESLint, TypeScript and
the production build. Three new interaction tests cover the floating menu,
pointer/keyboard scrolling and reopening suggestions while typing. Existing
navigation, alert acknowledgement, retained history/model state and ferry
provenance tests were updated for the revised presentation.

The real Codex browser verified ten checks recorded in
[the browser evidence](verification/ux-browser-2026-10-24.json). The menu
scrolled 158px while its input and sidebar stayed still. A third waypoint's
menu opened above its input, entirely within the viewport. Typed search and
both pointer and keyboard selection worked.

The operator planned **FQN37PQ** from Harbour Bay Ferry Terminal to Batam
Centre, and the separate authenticated driver account retrieved the same
summary. Server logs confirmed successful planning and route retrieval.
The existing ferry assignment **3B55FVN** was restored in the driver tab.

All four analytics sections were checked. Rain selection recomputed the
actual backend forecast; expanded model tools loaded API status and retained
backtest controls. History continued to show the original dated Batam survey.
Planning, route retrieval, simulation, model status and research-history API
requests returned 200. Both browser console error lists were empty.

The operator tab is left on Overview and the driver tab on its ferry journey
map. No push, deployment or credential change was performed. The existing
model/data limitations in the full website audit still apply. This pass
verifies the changed frontend flows rather than repeating the complete backend
test suite or certifying every device size.

Screenshots: [overview](verification/ux-analytics-overview-2026-10-24.jpg),
[scrolling suggestions](verification/ux-location-menu-2026-10-24.jpg),
[ferry header](verification/ux-ferry-header-2026-10-24.jpg),
[driver journey](verification/ux-driver-route-2026-10-24.jpg).
