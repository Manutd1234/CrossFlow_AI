# Live CCTV routing release — 24 October 2026

## Verified road bindings

| Camera | Directed road view | Scope |
| --- | --- | --- |
| 13, Gerbang Selatan Engku Putri | Jalan Engku Putri eastbound | One 38 m source-graph edge; narrowed northern-carriageway ROI |
| 51, Depan Kantor DPRD | Jalan Engku Putri westbound | One 45.8 m source-graph edge; narrowed southern-carriageway ROI |
| 48, Depan Stadion Temenggung Arah Batu Aji | Jalan Jenderal Ahmad Yani south-southwest | One 59.6 m source-graph edge; right carriageway toward Batu Aji |

The road-view review is recorded in `verification/cctv-road-mapping-2026-10-24/mapping-review.json`. Bindings identify the committed graph SHA, directed edge, carriageway, normalized image ROI and source landmarks. Source availability and image quality are reevaluated continuously; a registered binding does not guarantee a usable current observation.

All 28 registered public playlists received two bounded metadata checks. Five advanced in that check; 23 were ended. Camera 1 is a civic-plaza view and is disabled for analysis/road estimation. Camera 19 remains aggregate-only because an exact visible road edge is unresolved. The ended camera 15 retains only a draft binding; recovery cannot automatically enable routing. No nearest-landmark/radius assignment, opposite-direction propagation or citywide camera coverage is claimed.

## Data flow

A persistent external worker decodes authorized registered HLS segments in memory, runs the selected pretrained YOLO26s detector and temporal analysis, and retains numeric aggregates. It submits those aggregates using the dedicated CCTV-only credential to `/api/internal/cctv-observations`. Vercel validates the source configuration, timestamps, detector/image-policy identity and gates, then saves the latest immutable capture watermark in service-only Supabase storage. Every route request takes a fresh camera snapshot and applies bounded search preference only to its exact eligible directed edge. Physical speed and ETA are not inferred from image occupancy.

An operator-issued route persists to shared Supabase route storage. Driver lookup returns that saved geometry, navigation and routing audit; it does not silently replan an already issued dispatch. Expired, unavailable, misconfigured or revoked camera observations are excluded from new searches. The cloud receives no raw camera media and the frontend has no camera panel or upload flow.

## Deployment preparation and validation

The user explicitly approved the three reviewed production migrations and a dedicated CCTV-only collector credential. The current Supabase project was upgraded through its existing authenticated SQL Editor. Auth health reports schema 2 and unchanged restricted profile access. CCTV and saved-route/account tables have RLS, no anonymous reads, and service-role backend access. Existing records and account roles are preserved.

The production project is `ianwangsa2-8990s-projects/cross-flow-ai`, linked to `Manutd1234/CrossFlow_AI` main. Vercel is configured for shared camera storage and pinned observed-only forecasting. A small numeric model artifact supplies provenance; cloud forecasts use fresh genuine traffic-provider inputs and the persistence baseline, with no synthetic fallback, raw history export or cloud retraining. Local credentials, runtime stores, optional detector checkpoints, training images and private account setup evidence are excluded from the release.

Validation before release: frontend 600 tests across 55 files, lint, TypeScript and build; full backend/auth suite 835 passed, 32 optional skips and 714 subtests; focused mapping/cloud/HTTP tests also passed. These runs overlap and must not be added. The real HTTP fixture proves a fresh camera cost changes a selected route from 240 m to a 320 m detour, persists it and serves the exact driver geometry/directions. Opposing/unrelated edges stay unchanged; expiry and revocation restore normal route choice without cache clearing. The fixture is deterministic test evidence, not a real-world accuracy score.

## Remaining operational limits

- The selected detector, occupancy thresholds and road-view mappings are provisional, without independent field-accuracy certification or surveyed camera calibration.
- Three reviewed road views are eligible, with dynamic freshness/quality; this is limited coverage, not every Batam road. Camera 19 still requires unambiguous visible-edge correspondence.
- The persistent collector currently runs on this computer. Sleep, shutdown or loss of network stops cloud observations; stale evidence then withdraws automatically. Vercel itself does not run a permanent background YOLO process.
- Shared historical traffic archiving and continuous prospective cloud evaluation are separate infrastructure. The bundled artifact is dated development evidence, not newly collected cloud history.
- Published ferry schedules remain distinct from unavailable passenger queues, berth/cargo operations and emissions savings.

See `CCTV_CLOUD_DELIVERY.md` for collector configuration and serverless forecasting behavior. Post-deployment evidence is recorded separately after the deployed commit is verified.

## Production story verified

The GitHub main release deployed successfully to `https://cross-flow-ai.vercel.app/`. The existing operator account restored normally. It issued dispatch **253J6NV** from Batam Centre Ferry Terminal to Batamindo Industrial Park; an isolated guest-driver workspace then loaded the lowercase code, normalized it, and rendered the same 9.64 km journey, map and 12 driving instructions. The code survived a subsequent production deployment and is stored in shared Supabase route storage.

Camera 48 was on the selected path and its fresh observation was included in the immutable route audit. Its clear/light-traffic observation produces a small positive search preference cost; the minute-rounded API display is zero. This demonstrates the live integration, not that the real-world selected path would differ without that specific observation. A separate deterministic HTTP fixture demonstrates a changed route under stronger camera congestion.

Three production camera snapshots showed 3, 4 and 4 fresh estimates, advancing capture timestamps, and 2–3 eligible mapped directed edges. Actual quality/freshness may subsequently withdraw a camera. Production forecasts loaded five fresh genuine provider points, the explicitly provisional persistence baseline, and fresh weather. Both application consoles had zero application errors; a prior Vercel-login Google/FedCM attempt logged an unrelated error before existing GitHub sign-in succeeded. Deployment protection was retained.

Evidence: `verification/production-browser-story-2026-10-24.json`, `verification/cctv-cloud-delivery-live-2026-10-24.json`, and `verification/production-dispatch-253J6NV-cctv-2026-10-24.json`. Screenshots remain local to avoid publishing account/browser evidence.
