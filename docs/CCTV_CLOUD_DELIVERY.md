# CCTV aggregates on Vercel

Vercel hosts the existing React frontend and FastAPI routing/analytics API. The
continuous YOLO26 collector runs on a separate, persistent host with Python,
PyTorch, FFmpeg and the existing model weights. Serverless API requests never
open video streams or start a background inference process.

The collector retains the local numeric SQLite observations for local analytics
and sends only the same timestamped numeric aggregates to
`POST /api/internal/cctv-observations`. The cloud API checks the current bundled
camera registry, detector/crop identity, capture time, permissions and evidence
schema before a service-only database RPC advances the shared camera watermark.
Every subsequent cloud routing/analytics read rechecks freshness and mapping.
Old observations never acquire a new capture timestamp after delivery.

## Deployment configuration

Apply `backend/data/cctv_observations.sql` to the existing Supabase project using
an authorized database administrator. The table and RPC grant access only to
`service_role`; browser `anon` and `authenticated` roles have no access. The
shared store holds at most 128 latest camera records, with no camera URLs,
image bytes, object detections, or video segments.

Configure these **server-only** Vercel environment variables:

- `SUPABASE_URL` and `SUPABASE_SECRET_KEY` (or the existing legacy server key).
- `CROSSFLOW_CCTV_STORE=supabase`.
- `CROSSFLOW_CCTV_INGEST_TOKEN`: a generated credential of at least 32 characters.

Configure the persistent collector host with:

- The exact same `CROSSFLOW_CCTV_INGEST_TOKEN`.
- `CROSSFLOW_CCTV_INGEST_URL=https://cross-flow-ai.vercel.app/api/internal/cctv-observations`.
- Its existing local camera registry, local SQLite store and YOLO26 environment.
- `CROSSFLOW_CCTV_STORE=local`; neither Supabase nor an administrator key belongs
  in a vision subprocess.

The existing local API supervisor starts enabled collectors and passes only the
scoped delivery credential and URL to them. `scripts/cctv_capture.py` can also
run independently on a persistent collector host. Its existing maximum run is
one hour, so independent operation requires a process supervisor to restart it.
The local API supervisor already restarts completed/failed collectors.

The scoped header is `X-Crossflow-Cctv-Token`. The older admin header remains
accepted for compatibility, but a CCTV token cannot authorize retraining,
shortcut approval, spatial ingestion or any other admin endpoint. HTTPS targets
must end in exactly `/api/internal/cctv-observations`; redirects, embedded URL
credentials, query strings and fragments are refused. Transient delivery failures
are retried on later current samples and recorded only by safe diagnostic codes.

## Runtime limits and verification

This architecture requires a running collector host. Closing the local API,
sleeping the laptop or losing its network stops updates. Vercel does not keep
the laptop awake, and a deployment is not proof of a persistent cloud inference
host. Once a view's registered freshness limit expires, its routing effect is
withdrawn and the API uses the explicitly labelled provider/fallback costs.

Vercel never falls back to a per-instance `/tmp` SQLite CCTV store. Missing server
credentials, a missing migration or a shared-store outage leaves camera evidence
unavailable. Operator-to-driver route-code persistence separately requires the
existing `backend/data/route_runs.sql` migration; ferry verification requires
`backend/data/ferry_freshness.sql`. A successful frontend deployment alone does
not establish either persistence path.

The engineering tests in `test_cctv_shared_store.py`,
`test_cctv_cloud_delivery.py` and `test_cctv_api.py` check atomic watermark behavior,
freshness/revocation, restricted database grants, scoped machine authorization,
bounded numeric delivery and refusal to leak credentials or send media. These
tests establish integration behavior, not independently validated vehicle-count
or congestion accuracy.

## Observed forecast deployment

`vercel.json` also pins observed-only serving and the reviewed
`backend/data/observed_runtime_model.json` snapshot. Its adjacent manifest records
the file hash, original training time, graph revision and provenance. The roughly
120 KB file contains numeric tree arrays, a feature schema and aggregate
development evaluation metadata. Raw provider history, weather archives, camera
media and local databases are not included.

Cloud requests obtain fresh genuine TomTom representative-point speeds directly
in memory. They force the latest-observation persistence baseline for 15, 30 and
60 minutes and withhold uncertainty intervals, even if a future packaged forest
candidate passes its local development gate. Without a shared causal lag archive,
the serverless runtime cannot justify promoting that candidate. If provider
samples expire or disappear, forecasts become unavailable; synthetic traffic is
never substituted. The pinned artifact's metrics describe earlier development
windows, rather than current cloud history or independent field validation.

Automatic fitting and collection of a local training archive stay disabled on
Vercel. Current weather remains usable even when optional archival writes fail.
Weather and prospective-evaluation SQLite paths default to `/tmp/crossflow-ai/`
there, so they are explicitly ephemeral per instance. A forecast summary read
does not initialize or write an evaluation ledger. A durable cloud traffic
history and continuous prospective evaluation need a separate shared collector
pipeline; this deployment does not claim that they already exist.
