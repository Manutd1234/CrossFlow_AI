# Congestion engine and evaluation

Local verification, 23 October 2026. The implementation improves numerical
consistency, source attribution and repeatable evaluation. It does not certify
perfect forecasts or production traffic accuracy.

## Calculation and integration changes

- One bounded batch inference path serves individual predictions, legacy
  current/30/60-minute telemetry and route forecasts. All horizons in a batch
  use one forest snapshot. The maximum batch is 8,192 scenarios.
- Live speeds, history estimates, training labels and backtest targets share
  `speed_ratio_congestion_score`: free flow is 0 points, half speed is 50,
  stopped traffic is 95. The 0.05 speed-ratio floor keeps route costs finite;
  raw zero and near-zero speeds are retained as valid measurements.
- Modelled daily emissions caches include the model revision, so a retrain
  cannot leave the previous forest's integral cached until midnight.
- Shared route history uses independent account/guest associations and an
  atomic server-only save RPC. Identical routes keep one public dispatch code
  without stealing another account's history.
- Route retrieval survives a failed local SQLite read and still tries shared
  storage. A storage outage is a retryable 503, distinct from a healthy 404;
  error responses are not cached.
- Protected integration diagnostics expose storage/configuration and model
  provenance without credentials, database paths or inferred readiness.

## Current traffic from the existing Vercel project

The reviewed project is `ianwangsa2-8990s-projects/cross-flow-ai`:
`prj_uATJ1ZB0gKcU8CANQGGhF60q6TYo`. Its environment metadata lists TomTom,
Google Maps and Supabase variables. A configured variable is not connectivity
proof, and sensitive production values were not retrieved.

Ordinary Codex browser access to
<https://cross-flow-ai.vercel.app/api/live-traffic> returned five `tomtom_live`
segments with numeric current/free-flow speeds and provider confidence. A
protected-fetch tool was rejected because it may create an authentication
bypass link; no such link was created. Normal public access required no bypass.

For local use, set this in the gitignored root `.env`:

```dotenv
CROSSFLOW_TRAFFIC_UPSTREAM_URL=https://cross-flow-ai.vercel.app/api/live-traffic
```

The local adapter permits only that exact HTTPS URL, sends no credentials,
rejects redirects, bounds response size and wall time, and validates all five
known points, source labels, numeric measurements and snapshot age. It discards
upstream scores/zones and recomputes scores with local math. Current segments
are cached in memory only; failure/staleness returns labelled simulation.
`snapshot_age_seconds` describes the upstream response timestamp, not an
independently measured sensor-observation age. Do not configure the relay on
the upstream deployment itself.

The 30 spatial hotspot areas remain modelled estimates even when their
corridor inputs are live. No provider observations are automatically archived
or promoted into training.

## Reproducible stress run

```bash
.venv/bin/python scripts/validate_congestion_engine.py \
  --scenarios 2000000 --seed 20261023 --batch-size 4096 \
  --history-db backend/data/local_history.db --synthetic-replay \
  --output docs/verification/congestion-engine-validation.json
```

The final recorded run evaluated **2,000,000 scenarios**, zero invariant
failures, in **36.841 seconds**. It includes 333,334 risk-monotonicity pairs,
333,333 ferry-surge pairs and 333,333 surge-decay pairs. Inputs vary five
corridors, twelve road classes, seven weekdays, three weather states, time,
capacity, terminal distance and free-flow speed. Checks cover finite values,
quantile order, speed bounds, nonnegative delay, risk monotonicity, surge caps
and downside route-speed bounds. CI repeats a smaller 10,000-scenario run.

These are actual forest inference and cost-invariant scenarios. They are not
two million full road/ferry itinerary solves, measured traffic observations or
proof that all possible states have been tested.

## Chronological backtesting

`POST /api/routing-intelligence/congestion-backtest` uses the existing machine
admin header and no-store policy. It reads at most 2,000 typed observations
from the explicitly selected local/shared store, evaluates observed records
only at fixed 15/30/60-minute horizons, and never changes the live model.
Requests cannot supply records, loosen provenance rules or select horizons.

The standalone evaluator also accepts an audited CSV with `--observations-csv`.
Required columns are `corridor_id,observed_at,latitude,longitude,actual_speed_kph,
free_flow_speed_kph,source`. Timestamps need explicit offsets. Optional columns:
`road_class,capacity_vph,terminal_distance_km,upstream_event_id,reviewed`.
CSV evaluation does not import records into a database. Source labels must
reflect the actual collection method; invented labels do not establish truth.

The candidate uses a frozen training snapshot strictly before the first
forecast origin, with its own internal calibration holdout. Equal split
timestamps stay together. Persistence and current/history baselines use only
measurements available at or before each origin, at the same corridor/location
rounded to five decimals (about one metre, not edge map matching). Future target
speeds and road metadata do not enter prediction inputs. Stale/missing current
samples are explicitly skipped; duplicates do not multiply the sample count.

The evaluator treats `observed_at` as availability time because ingestion-delay
history is unavailable. Weather and ferry proximity are missing from typed
history and are declared defaults. These limits remain visible in its report.

The local measured-history table has **zero** observations. The hosted project
does not yet have `crossflow_spatial_traffic_observations`. A current five-point
live snapshot is not a historical archive. Real backtesting therefore reports
`insufficient_observations` until an authorized measured dataset is available.

The separate **synthetic** replay has 3,360 rows and 675 outer holdout targets
per horizon. It is independent of the bundled generator and exposes limitations:

| Horizon | Forest MAE | Latest-speed MAE | Current/history MAE |
|---|---:|---:|---:|
| 15 min | 6.6282 | 4.6786 | 5.1065 |
| 30 min | 6.6282 | 5.1312 | 5.4543 |
| 60 min | 6.6282 | 6.0766 | 6.1829 |

All errors are congestion points. The frozen forest uses target time/static
context rather than observed current innovations, so its predictions are the
same across lead times for a given target. P10–P90 outer coverage was 37.19%,
below the 80% target. This candidate is not promoted by the evaluation. Real
measurements and stronger validation are needed before claiming improvement in
forecast accuracy.

## Shared database rollout

The new code and migrations are local; no production backend deployment or
shared-storage schema upgrade has been performed in this pass.

1. Apply the tested, rerunnable `backend/data/route_runs.sql` before deploying
   its matching backend. It preserves retained legacy attribution; owners
   already overwritten by older saves cannot be reconstructed.
2. For an existing spatial-history installation, apply
   `backend/data/migrations/003_allow_stopped_traffic_observations.sql`, or rerun
   its canonical schema. This widens speed acceptance without widening grants.
3. SQLite upgrades its own speed constraints atomically and retains existing
   rows, timezone metadata and custom indexes.

Hosted schema existence and configured credentials are not proof of successful
shared writes. Verify a real account-scoped save/retrieve/history round trip
after an authorized rollout.
