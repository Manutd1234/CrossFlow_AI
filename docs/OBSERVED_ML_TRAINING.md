# Observed traffic training and backtesting

## Latest local revision — expanded historical testing, 24 October 2026

The real-only runtime now compares a **fixed five-candidate set**: original and
regularized absolute forests, plus a forest of measured changes blended with
persistence at weights 0.25/0.5/1. Validation alone selects the configuration;
calibration and test labels do not tune it. The selected forest is refitted on
fit plus validation, calibrated separately and tested later. Shifted residuals
are stored as bounded numeric trees and decoded back into score changes.
Safe JSON artifacts retain their exact decoder and the runtime publishes the
artifact SHA-256. Missing evidence still cannot trigger synthetic fallback.

The live five-point Batam archive is still too short for field validation.
Its prior final windows have already been inspected, so the UI now calls them
**chronological development results**, not a newly untouched confirmation test.
Persistence remains the serving choice whenever validation prefers it. A fresh
fit is not a licence to claim that a local forest now beats persistence.

### Additional Batam road-speed evidence

The original public Grand Batam Mall survey PDF was downloaded again and its
hash, dates (PDFp4) and Table5 (PDFp6) checked visually. **66 hourly speed means**
cover Jalan Pembangunan, Jalan Utama Happy Garden and Jalan Baloi Garden1 on
20, 22, 24 and 25 June 2023. The table had already been transcribed in the broader
research workspace and is now normalized, hashed and integrated into this repo's
source-specific testing. These are published aggregates derived from measured
travel times, not individual GPS traces. Direction, sample size and publication
latency are unknown. No measured free-flow reference or canonical corridor
mapping was invented. No explicit dataset licence was located; the manifest
identifies the public repository and factual-table scope rather than assigning
a made-up open licence.

At each forecast origin, only an already completed preceding hour is available.
Non-consecutive intervals are excluded. Fit uses the first two dates, validation
uses 24 June and the final test uses 25 June; the selected forest is refitted on the
first three dates. There are 12 fit, 15 validation and 15 final test pairs.
The selected `residual_025` reduces MAE from **2.4960 to 2.3345 km/h (6.47%)**.
RMSE is 2.9733 km/h; 13/15 forecasts (**86.67%**) are within ±5 km/h, the same tolerance
hit rate as persistence. The previous-survey-day same-hour baseline has MAE
2.4840 km/h on the same 15 cases. One test day cannot support a confidence interval
or a general Batam accuracy claim. [Full survey evaluation](verification/grand-batam-backtest-2026-10-24.json),
[normalized measurements](../backend/data/research/batam-grand-mall-2023.csv).

### Larger new historical speed benchmark — Guangzhou, not Batam

Downloaded **1,855,589 original measured speed records**, 214 anonymous roads,
61 days (1 August–30 September 2016), from Chen, Chen and He's
[Zenodo dataset, DOI 10.5281/zenodo.1205229](https://zenodo.org/records/1205229),
CC BY 4.0. The original publisher's ZIP MD5 and local SHA-256 match. The release
has 24,187 absent road/windows; missing values are not zeros or interpolated
training targets. The source's 10-minute windows support 10/30/60-minute horizons;
we do not manufacture a 15-minute label. Source dates/window-end availability are
retained without asserting an independently verified publication delay or UTC
clock. Real speeds above 100 km/h are preserved, with target-only scaling for the
shared bounded decoder and every result converted back to km/h.

The [frozen protocol](verification/historical-expansion-protocol-2026-10-24.json)
sets 33 fit days, 7 validation days, 7 calibration days and 14 final test days, 60-minute
purges, a 40,000-row initial fit cap per horizon and the fixed candidate set before
successful fitting. Selection never reads final-test errors. The final model is
refitted on fit plus validation. Calibration-only 80% residual radii are evaluated
on future test observations. Previous-day and previous-week baselines are also
reported on common matched subsets. Per-day, per-road and slow-traffic errors
are retained. Whole-day paired bootstrap resamples all roads and intraday errors
together (2,000 replicates); its intervals remain estimates because inter-day
serial dependence may persist.

| Horizon | Test pairs | ML MAE km/h | Persistence MAE km/h | MAE reduction | Within ±5 km/h | 95% day-block interval for MAE reduction, km/h |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 10 min | 429,926 | 2.0073 | 2.1988 | 8.71% | 92.37% | 0.1794–0.2039 |
| 30 min | 429,498 | 2.8062 | 3.2507 | 13.67% | 84.87% | 0.3877–0.4957 |
| 60 min | 428,856 | 3.2552 | 4.2288 | 23.02% | 80.75% | 0.8555–1.0755 |

The tolerance hit rate is a defined percentage metric. It is not 100 minus MAE,
R² as percentage accuracy or Batam field validation. This Chinese model is never
activated for Batam routes, and its rows are never added to canonical Batam
traffic history. [Full new-data backtest](verification/historical-expansion-backtest-2026-10-24.json),
[download/access evidence](verification/historical-expansion-acquisition-2026-10-24.json).

### Prospective confirmation in the running Batam system

The local traffic collector now saves immutable candidate, selected-strategy and
persistence forecasts every collection cycle. Each record includes origin,
actual issue time, target time, monitored location and exact model fingerprint.
The evaluator accepts the first later real provider measurement at or after the
target, within 180 seconds; late, synthetic, pre-issue and geographically different
measurements cannot score it. A result can be written once and cannot rewrite
its issued forecast. Windows with no eligible future observation expire rather
than becoming invented labels. The separate local SQLite ledger is ignored by
Git, survives restarts and cannot train or promote a model. A ledger failure does
not disable traffic forecasts. At least seven measured test days and broader
conditions are still needed before judging field improvement. The Model tab
shows issued forecasts, matched outcomes and test-day count, including pending
states; it keeps the source-specific historical benchmarks separately labelled. The live forecast uses persistence until the predefined release checks pass, while candidate forests are still evaluated and recorded.

The existing TomTom key still returns current Flow 200 and historical Stats 403.
No paid job, account creation, outbound enquiry or cloud deployment was performed.
A licensed multi-week history for the five actual monitored Batam points remains
unverified. Collection and prospective evaluation continue only while the local
backend is running.

Reproduction:

```bash
.venv/bin/python scripts/acquire_historical_speeds.py
.venv/bin/python scripts/backtest_grand_batam.py
.venv/bin/python scripts/backtest_historical_expansion.py
.venv/bin/python scripts/train_real_traffic.py
```

Verification: 562 frontend tests in 51 files, lint, TypeScript and production build
passed. 101 targeted backend tests passed. Guard tests cover causal feature isolation, temporal
purges, immutable prospective predictions, late/synthetic-target rejection,
residual decoding, source validation and independent-day counting. Reports and
[browser proof](verification/historical-ml-baseline-comparison-2026-10-24.jpg) are local; no deployment or push was made.

## Earlier local runtime — 24 October 2026, 03:18 SGT

The local API now uses **observed traffic only** for its traffic forecast path.
It does not train or fall back to the synthetic cold-start model when
`CROSSFLOW_REAL_TRAFFIC_TRAINING=1` (enabled in this workspace's ignored `.env`).
Three actual Random Forests, 100 trees each, are fitted at 15/30/60 minutes with
43 inputs. A validation-only choice selects the forest or persistence for each
horizon before calibration and the final chronological test. Persistence means
holding the latest measured traffic index constant; at that checkpoint it won
all three validation comparisons. The forests are trained and evaluated, but are not
represented as an improvement when they lose to that baseline.

At 03:18 SGT, the archive contained **1,064 real TomTom records over 9.60 hours**,
with 160 original 15-minute target samples across five monitored locations.
The 30-minute forest used 45 fit-plus-validation forecast pairs. Each horizon
has only five calibration and ten final test pairs. Selected test MAE was
0.24/0.49/0.49 congestion-index points at 15/30/60 minutes; corresponding forest
MAE was 1.29/1.42/3.33. These small, mostly uncongested overnight windows do not
support a near-100 accuracy claim. R² may be negative and is preserved as such.
The [saved evaluation](verification/real-traffic-evaluation-2026-10-24.json)
contains source counts, actual windows, availability policy, hashes, strategies
and errors. Counts and metrics change on subsequent real training runs.

Inputs include measured provider speed/free-flow references, strictly available
lagged traffic, target clock/calendar, nearby mapped OSM road class/segment
length/tags/terminal distance, and archived live weather. The OSM match is nearby
undirected context, **not a verified provider-direction/lane match**. Missing lanes,
speed limits and capacity stay unknown; tagged OSM values are not field sensors.
Open-Meteo supplies one regional Batam model grid, with current conditions and
forecasts, a five-minute cache, strict units/time/location validation and explicit
stale/unavailable states. It is not a roadside weather station. Only weather
captured before each forecast origin can enter training. Initial weather-enriched
training coverage is **zero pairs** because capture started after the older
traffic windows; historical weather is not fabricated or backfilled as prior knowledge.

Training uses original traffic ingestion times, all available prior measurements
for lag inputs, whole 15-minute target groups, four chronological partitions and
60-minute purges. No synthetic augmentation or interpolated target labels is
used. Calibration-only error radii are evaluated on untouched future test pairs.
The five-point mean has its own synchronized-group calibration; marginal point
quantiles are not averaged into a network confidence interval. Sparse point and
mean intervals are withheld in the live response. Missing uncertainty does not
become a fabricated routing risk penalty.

Release checks require at least seven days, seven local dates, 18 hour bins,
weather-enriched training coverage, larger fit/validation/calibration/test
windows, per-corridor test evidence, validation selection of the forest, at least
5% final-test improvement over persistence, and interval coverage. **Current
status is provisional and the release checks fail.** Passing these checks is
still not a guarantee of future accuracy or independently measured road capacity.

The collector continues every 120 seconds and the trainer attempts a refit at
startup and hourly **while the local backend is running**. A failed refit keeps
the previous complete JSON artifact. The data-only artifact is bounded, hashed,
schema checked and atomically replaced; no untrusted pickle is loaded. Inspect
actual trainer state through the protected `/api/analytics/real-model` endpoint.
The protected `/api/analytics/real-forecast` drives Analytics → Trends and Model;
`/api/weather` exposes the fixed regional feed. The old 24-hour synthetic scenario
endpoint returns 409 in observed-only mode. Route contexts beyond 60 minutes use
an explicitly labelled latest-observed reference, not an invented future forecast.

Manual reproduction from the repository root:

```bash
.venv/bin/python scripts/train_real_traffic.py --refresh-weather
bash scripts/dev.sh --backend-only --force-restart
```

The training script never fabricates missing traffic. Keep the local collector
and real-training flags enabled in `.env`; `.env.example` documents them. Local
artifacts and weather archives are ignored by Git. No cloud deployment was made.

Verification: 548 frontend tests in 51 files, ESLint, TypeScript, production build
and 92 targeted backend tests passed. Tests cover holdout-label isolation,
chronological availability, synthetic-row exclusion, safe artifact roundtrips,
missing/stale inputs, causal weather, role checks and the observed-only routing
adapter. Browser proof: [real model and live weather](verification/real-traffic-engine-2026-10-24.jpg).

## Historical state — 23 October 2026

**No measured-data replacement model has been activated.** The running model
remains the explicitly labelled 4,000-row synthetic baseline. The observed-only
candidate pipeline, import validation, promotion gate and restart artifact path
are implemented and tested.

The [read-only archive snapshot](verification/observed-training-readiness.json)
contains **110 genuine TomTom rows spanning 0.6922 hours**, across five
corridors; 20 original records remain after 15-minute sampling. This is a
point-in-time count: collection continues every 120 seconds. It is insufficient
historical coverage, and the report returns `insufficient_observations` with
`model_replaced=false`. It does not train a substitute synthetic candidate.

The owner's key was saved locally without printing it. Official-service probes
returned **Traffic Flow HTTP 200** and **historical Traffic Stats HTTP 403**.
Current flow access therefore works; historical entitlement is not established.
The owner verified the email and completed MyTomTom account setup; its
Freemium dashboard is visible. MOVE still reports **Failed to fetch**, and
neither its 30-day trial nor a historical key is verified. MyTomTom's separate
60-day Batch evaluation lists sample regions outside Batam/Singapore, so it
was not activated for this task.

After the owner approved an alternative provider, **HERE Traffic Analytics
confirmed a 30-day trial registration**, ending 22 November 2026. Its page says
login credentials were emailed. The initial request used the SoC alias; after
its message did not arrive, the owner requested the direct address
`e1525902@u.nus.edu` and a new submission succeeded. Delivery to the corrected
address is not verified yet. Dashboard access, Batam segment coverage, export
and specific ML/storage rights remain unverified. No licensed Batam historical
export has been imported or used to replace the model.
[Provider access probe](verification/tomtom-access-check.json),
[HERE trial confirmation](verification/here-trial-u-nus-success.png),
[alternative-source review](HISTORICAL_TRAFFIC_SOURCES.md).

TomTom distinguishes its [current Flow Segment Data service](https://docs.tomtom.com/traffic-api/documentation/tomtom-maps/v1/traffic-flow/flow-segment-data)
from [historical Traffic Stats](https://docs.tomtom.com/traffic-stats/documentation/product-information/introduction).
The latter supports route/area statistics over selected dates and time slices;
TomTom documents a 30-day MOVE trial and a MOVE account/API key for that access.
A working current-flow key or a public annual city average does not establish
access to compatible historical segment measurements.

Do not transplant another city's data into Batam coordinates/corridor IDs,
substitute traffic volume for speed, or repeat a published period average as
individual observations. A vendor export needs a reviewed mapping that
preserves geography, timestamps, aggregation intervals and the free-flow
reference. The current importer accepts the canonical CSV below, not arbitrary
vendor JSON. Licence and retention permission must cover the intended use.

## Read-only operator evaluation

Sign in with an authorized operator account, open **Analytics → Model**, and
choose **Run observed backtest**. In Supabase mode the backend resolves the
actual protected admin/operator profile; driver or anonymous callers cannot
run this work.

`POST /api/analytics/backtest` accepts an empty object. Callers cannot upload
records, include synthetic data, alter thresholds, choose storage paths or
activate a model through this endpoint. It reads at most 50,000 canonical
local/shared records, caches a completed report for 60 seconds and rejects
concurrent expensive evaluations rather than queuing unbounded work. Responses
and errors use private/no-store headers.

The returned `backtest` identifies a **private observed candidate**, not a new
score of the current serving synthetic model. It reports raw/selected counts,
source counts, excluded rows, time span, local-date/hour coverage, fit/
calibration/test windows, baseline errors and activation reasons. The
`evaluation_snapshot` identifies actual storage and the input cap. Evaluation
never writes observations, saves/loads an artifact, or publishes a model.

## Canonical measured CSV and provenance manifest

Required CSV columns:

```csv
observed_at,corridor_id,latitude,longitude,actual_speed_kph,free_flow_speed_kph,source
```

Optional columns: `upstream_event_id`, `road_class`, `capacity_vph`,
`terminal_distance_km`. `lat`/`lng` are aliases. Unknown/duplicate columns,
malformed rows and contradictory duplicate identities are rejected. There
are no example measurement rows here: obtain actual licensed measurements.

- `observed_at` must carry a timezone. Training excludes timestamps after its
  evaluation time, even if ingestion's clock-skew allowance accepted them.
- Corridor IDs are `corridor-1` through `corridor-5`. Coordinates must be
  within the Batam validation bounds, 0.88–1.215°N and 103.75–104.30°E, and
  must identify the actual measured location.
- Speeds are finite km/h: actual 0–250, free-flow greater than 0 and at most
  250; actual/free-flow cannot exceed 1.5. A stopped observation is valid.
  Missing free-flow speed must not be replaced with an invented value or an
  unexplained posted speed limit.
- Supported importer sources are `loop_sensor`, `probe_gps`, `tomtom_live` and
  `verified_traffic_observation`. Confidence/provenance/observed flags are
  assigned by the server's source policy. The CLI excludes sources requiring
  a separate community-review process.
- Default retention is five years; configured retention accepts 365–7,300
  days. The CSV is bounded to 32 MiB/100,000 input rows and the manifest to
  64 KiB. All rows are staged and validated before the first import write.

Create a manifest containing exactly these five fields. Replace every marked
value with the actual dataset/licensing details; the example is not permission:

```json
{
  "dataset_name": "<actual Batam dataset name and date range>",
  "source_url": "https://<actual-provider-host>/<dataset-reference>",
  "license": "<actual licence or written permission covering this use>",
  "reference": "<export/report ID and aggregation/free-flow definition>",
  "source": "verified_traffic_observation"
}
```

The HTTPS source URL must not contain credentials. Every CSV source must match
the manifest. A manifest is an operator assertion; it is not independent proof
that the dataset or licence is genuine. The importer never fetches its URL.
Store licensed raw files under ignored `backend/data/traffic-imports/`.
Do not label a retrospective Traffic Stats export `tomtom_live`: retain its
provider/report/aggregation definition in the manifest and use a reviewed
canonical source mapping. Existing archived live-flow samples retain their
actual `tomtom_live` source. No historical import has occurred yet.

From the repository root, dry-run validation performs no database writes:

```bash
.venv/bin/python scripts/import_observed_traffic.py \
  backend/data/traffic-imports/batam-observations.csv \
  --manifest backend/data/traffic-imports/batam-observations.manifest.json
```

Apply only after reviewing the dry run and confirming the destination is the
same local SQLite database configured for the app:

```bash
.venv/bin/python scripts/import_observed_traffic.py \
  backend/data/traffic-imports/batam-observations.csv \
  --manifest backend/data/traffic-imports/batam-observations.manifest.json \
  --apply --database backend/data/local_history.db
```

Apply is local-only and rejects serverless/shared-store modes. It records
CSV/manifest hashes and import-to-observation associations. Writes use batches
of at most 2,000 observations. If later storage fails, previously committed
batches remain; the tool reports failure/progress and supports idempotent retry.
It does not claim an all-file rollback. A conflicting existing observation is
rejected rather than overwritten.

## Chronology, baselines and promotion policy

Selection revalidates canonical records, detects conflicting replay identities,
excludes non-observed/unsupported/future rows, and deduplicates. Sampling retains
the **first original measurement per location per UTC 15-minute bucket**; it
never averages measurements and relabels the result as an original observation.
Locations match corridor plus coordinates rounded to five decimals, not OSM
edge map matching. The service hard cap is 100,000 input rows.

Global timestamp groups are partitioned approximately 60% fit, 20% calibration,
20% final holdout. No timestamp group crosses a boundary. Fit ends strictly
before the first calibration forecast origin; calibration ends strictly before
the first holdout forecast origin, using 60-minute purges. Final holdout labels
never fit the forest, choose features, widen intervals or tune thresholds.
This follows the temporal separation described in
[scikit-learn's time-series split guidance](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html).

| Engineering promotion criterion | Default |
| --- | --- |
| Measured span | At least 7 days |
| Calendar coverage | At least 7 distinct WIB dates and 18 distinct hour-of-day bins |
| Partition sizes after sampling/purge | At least 100 fit, 50 calibration and 100 final-holdout rows |
| Geographic coverage | All five supported corridors in every partition |
| Forecast horizons | Fixed 15, 30 and 60 minutes |
| Prior current measurement | Same location; no more than 30 minutes old at the forecast origin |
| Evaluation size | At least 30 matched targets per horizon and 5 per corridor per horizon |
| Aggregate mean error | Candidate MAE at least 5% lower than both persistence and current/history blend at every horizon |
| Corridor safeguard | No corridor MAE more than 5% worse than persistence at any horizon |
| Interval evidence | Final-holdout P10–P90 coverage at least 70%, with an 80% nominal target |
| Resource bound | 100 trees, depth at most 10, minimum leaf size 5, one fitting worker |

Persistence uses the latest measurement available at or before the forecast
origin. The blend uses bounded prior comparable history. Future target speeds,
free-flow values, road classes and capacity fields never enter predictor
features; only the target's known calendar time and prior-origin metadata do.
When persistence already has zero MAE, the candidate cannot claim improvement.

Intervals use calibration-only split-conformal widening of tree quantiles.
Temporal dependence means the nominal 80% interval has **no coverage guarantee**.
Weather is encoded clear because the archive does not persist weather; weather
sensitivity is not learned or validated. Ferry proximity is unknown in the
archive; the separate published-timetable adjustment remains unvalidated by
this backtest. Missing capacity/terminal distance use declared defaults.
Provider ingestion delays are unknown, so observation time is assumed to be
availability time. Passing these release criteria is not proof of field accuracy.

A successful model's top-level metrics describe **matched 30-minute final-test
forecast pairs**. `observed_evaluation_rows`, source counts and holdout timestamps
refer to those matches. Calibration has separate counts/timestamps; the report's
chronology retains the full partition counts. The 15/30/60-minute tables report
their own sample counts, errors and empirical coverage.

## Explicit activation and restart artifacts

The read-only operator action never activates a candidate. The separate
`POST /api/retrain-model` machine operation requires the configured
`X-CrossFlow-Admin-Token`. It uses the same observed-only evidence gate and a
non-queuing mutation lock. Do not put that token into frontend/VITE variables,
URL parameters, documentation, screenshots or logs.

Configure this optional trusted server path in the ignored root `.env`:

```dotenv
CROSSFLOW_OBSERVED_MODEL_PATH=backend/data/model-artifacts/observed-congestion.json
```

Relative paths resolve from the repository root. No HTTP caller can select a
path. An eligible candidate is persisted **before** its model and matching
metrics replace the serving revision. A failed artifact write retains the
previous model. Without a configured artifact path, successful activation is
explicitly process-memory only.

Artifacts contain bounded numeric JSON tree arrays, metrics, gate evidence and
a SHA-256 integrity checksum. Loading never executes pickle/joblib code. The
loader checks feature shape, finite values, tree connectivity, child indexes,
depth, byte/node budgets and eligible observed-only provenance. The checksum
checks integrity, not who authored a file; use only a trusted local path.
[scikit-learn's model-persistence guidance](https://scikit-learn.org/stable/model_persistence.html)
explains the executable-deserialization risk avoided here.

Startup restores a configured valid artifact only; it does not train a new
candidate or activate one during a read request. `/api/model-status` exposes
`observed_artifact_status` as disabled, waiting for an eligible model, restored,
or restore-failed/previous-model-retained. Paths and credentials are not exposed.
The ignored `backend/data/model-artifacts/` directory is single-host storage,
not a shared production model registry. No real observed artifact exists yet
because the available real archive has not passed the gate.

## Verification at this checkpoint

- Full backend/auth suite: **418 tests and 397 subtests passed** in 137.00s,
  `/tmp/crossflow-observed-final-all-backend-verified.log`.
- After the approved holdout-metadata correction: **22 focused tests and 11
  subtests passed**, `/tmp/crossflow-observed-metadata-final.log`. These overlap
  the full run; do not add the totals.
- Standalone runner: **152/153 passed**; its only failure was an obsolete
  all-sources-August review-date assertion. The corrected source-specific case
  passed separately: **1 passed / 152 deselected**,
  `/tmp/crossflow-shared-ferry-source-metadata-regression.log`. This is not a
  newly completed 153/153 standalone command.
- Frontend: **403 tests / 39 files**, lint, TypeScript, production build and diff
  check passed, `/tmp/crossflow-observed-backtest-full-check.log`.
- PostgreSQL: **26 cases passed**, `/tmp/crossflow-auth-sql-final.log`.
- Current-engine stress: **2,000,000 scenarios**, zero invariant failures in
  **40.48s**, [report](verification/observed-task-engine-validation.json).
  This exercised the running **synthetic baseline**, not real-data accuracy or
  two million complete itinerary solves. Its separate real archive snapshot
  correctly reported insufficient observations.

The successful fit→gate→numeric-JSON→activation→restore regression uses explicitly
labelled unit fixtures. It verifies the software path and is not evidence that
real historical traffic trained a replacement model. Fixture-marked reports are
rejected by the frontend as real evaluation results.

After the restart, the real Google operator reissued the same request and
received the stable code **AHMKAQM**. The real NUS driver entered a padded,
lowercase code and loaded 62 geometry points, 12 directions, ROAD → FERRY → ROAD,
the 19:00 WIB → 20:55 SGT published connection/countdown and 25/25 map tiles.
The History UI showed 24 observed samples per corridor, all collected today;
there is no earlier measured daily trend. The public API confirmed direct
TomTom transport for five segments, anonymous backtest 401 and anonymous
retraining 403. [Runtime proof](verification/observed-task-runtime.json),
[driver map](verification/driver-observed-task-assigned-map.png),
[operator backtest](verification/operator-observed-backtest-local.png).

## Independent academic historical benchmark

The free [SUTD Singapore archive](https://github.com/EYLeong/traffic_prediction) is now downloaded locally and benchmarked. It contains published LTA **speed bands**, not exact km/h, and is geographically separate from the Batam production model. The original three files remain ignored; their SHA-256 hashes are pinned in `scripts/backtest_academic_singapore.py`. The source's original API payloads and timestamp timezone/availability delays are unknown. Upstream data attribution and licence are recorded in `docs/HISTORICAL_TRAFFIC_SOURCES.md` and shown in Analytics → History.

Reproduce against the already-downloaded files from the repository root:

```bash
.venv/bin/python scripts/backtest_academic_singapore.py
```

For reacquisition, retrieve only `dataset.npy`, `metadata.json` and `timestamps.json` from `https://raw.githubusercontent.com/EYLeong/traffic_prediction/master/data/processed/` into `backend/data/traffic-imports/singapore-sutd/`. The runner checks all three known checksums before numeric deserialization; changed upstream data requires a fresh review. It never executes external code or enables pickle.

The 53 links have 7,497 snapshots (397,341 speed labels) spanning 27 source-calendar dates. Five prior bands plus road identity/category/length and calendar features predict 15/30/60-minute targets. Missing windows are excluded. Global 60/20/20 chronological boundaries are purged by the maximum target horizon; training uses a fixed stride to cap fit rows, and calibration alone determines interval radii. An 80-tree depth-12 forest with minimum leaf size 12 and seed 42 was fixed before held-out evaluation. No parameter search on the test partition occurred.

Each horizon has 77,539 held-out forecasts; they overlap and are not independent. Forest MAEs are 0.528920 / 0.551534 / 0.562846 band units, compared with persistence 0.549156 / 0.625221 / 0.659475. Previous-day comparisons use the report's common subset, not the full test denominator. Empirical 80% interval coverages are approximately 79.9% / 80.6% / 80.6%; temporal dependence prevents an independence-based coverage guarantee. The [full report](verification/singapore-academic-backtest.json) also includes per-road outcomes and source hashes.

The API serves a compact reviewed copy from `backend/data/research/singapore-academic-backtest.json`, linked to the full report by its SHA-256. This endpoint reads bundled evidence only; it cannot import observations, invoke training, accept file paths or replace the serving model. The separately transcribed 48-row Batam survey retains original 2023 dates and source directions, with no invented coordinates, free-flow measurements or vehicle sample counts. It is too sparse for the existing network-model promotion gates. The serving Batam model therefore remains explicitly synthetic; **this benchmark does not change that status**.

## Exploratory published Batam site benchmark

Run `.venv/bin/python scripts/backtest_batam_survey.py` locally. The runner pins the reviewed 48-row Gajah Mada CSV checksum, holds out entire subsequent days, and uses only knowable time phase/direction inputs. It never uses target travel time, inferred density, random row splitting or synthetic labels. Fixed 80-tree parameters are not tuned on the test days. The report is `docs/verification/batam-site-backtest.json`; the reviewed compact API artifact is `backend/data/research/batam-site-backtest.json`. Neither is a serving model artifact. No app database is written or canonical corridor mapping invented.

Sixteen prior rows train the first fold, 32 prior rows train the second; 32 forecasts are scored in **km/h** against published speed means. Forest MAE 2.637907; previous-day baseline 3.207188; historical-slot mean 2.907969. These are correlated observations from one 100m site over three days, with a retained published speed/time quality flag. They do not establish 15/30/60-minute network accuracy, uncertainty coverage or model-promotion eligibility. The endpoint validates scope, counts, units and chronology before rendering. [Audit and limitations](WEBSITE_AUDIT_2026-10-23.md).
