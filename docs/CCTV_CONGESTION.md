# Backend CCTV congestion and routing — 24 October 2026

CCTV runs in the backend. The existing frontend map, route planner and time controls remain the user interface; camera panels, camera badges, upload controls and camera diagnostics are not displayed. The old image-upload product endpoints remain retired (HTTP 410).

The implemented path is **public Matanya HLS → sampled frames in RAM → local YOLO26s → road-region tracking and occupancy → numeric congestion index → exact-road route-search cost → the existing route polyline**. Camera estimates do not train or replace the separate physical speed/travel-time forecast.

## Active local pilot and source access

The official location graphic lists **44 camera views**; its printed number 35 appears twice. The current Matanya homepage publishes **28 player links**. The [round 13 inventory](../../research/round13_matanya_inventory/inventory.json) preserves all 44 graphic slots separately from current players, historical aliases, HLS checks and actual browser playback checks. Numeric player IDs have been reassigned historically, so a historical name is never assumed to identify today's view. Archived official directories supplied another 15 historical public view URLs, giving 41 of 44 graphic names with a current or historical published URL. None of those historical-only links yielded an additional confirmed current stream: three had empty HLS values, eleven timed out, and one legacy request failed. Kejaksaan Arah BTN, the extra unqualified Bundaran Madani view, and Sambau 3 still have no recovered published URL. Timeouts remain unverified, not proof of permanent closure; see the [historical discovery receipts](../../research/round13_matanya_inventory/missing_view_search.json).

The initial bounded audit found **six advancing public playlists: 1, 13, 51, 15, 19 and 48**. The remaining current players comprised 21 ended playlists and one stale playlist (camera 10, with a 5 June source date despite advancing playback). All 28 published players were opened in a browser and observed over at least five seconds; six had current advancing road video. These are dated observations, not a promise of continued uptime. Unavailable feeds never become zero-congestion observations.

Six local nonretained numeric pilots are enabled: Atap Kantor Walikota (1), Gerbang Selatan (13), DPRD (51), Simpang BI (15), Bundaran Madani Pancuran (19), and the road view labelled Damkar Tembesi / Arah Batu Aji (48). Each has a reviewed road polygon, a static-background polygon, temporal tracking and a signed image-plane crossing line. Crossing direction is not advertised as a verified compass direction. Camera 1 uses an in-memory crop of the upper road, excluding the plaza and enlarging road vehicles for inference; detections are remapped into full-frame coordinates before road filtering.

Only camera 15 currently has a provisional exact-road routing binding: the graph's directed edge `5253032156 → 5394956319`, road index 619, Jalan Engku Putri Timur, approximately 107.5 metres. Its graph revision is pinned in server configuration. Camera-pole coordinates, lane direction and exact field coverage have not been independently surveyed. The other five views contribute numeric view estimates but remain excluded from route penalties until their directed road coverage is established. The [mapping candidates](../../research/round13_matanya_inventory/road_mapping_candidates.json) preserve source geometry for that work without silently turning candidates into confirmed coverage.

Official pages support public road monitoring. The inspected CCTV SOP restricts operators' release of recorded footage; it did not establish a blanket ban on user-requested, nonretained numeric analysis. Automated reuse terms remain unstated. This pilot follows the user's explicit request for local public monitoring, without asserting an agency-issued reuse license. It saves no CCTV images/video, republishes no camera footage, and sends no emails or access inquiries. Commercial redistribution and source-specific recording/training rights have not been established. The [policy evidence](../../research/round12_cctv_live_sources/published_use_policy.json) preserves that distinction.

## YOLO26 model and actual training

[Ultralytics YOLO26](https://docs.ultralytics.com/models/yolo26/) is available with pretrained vehicle classes. The selected counter is **YOLO26s at 1280 pixels and confidence 0.25**, using the official pretrained checkpoint (SHA-256 `646f8bc3fe0a656803d95c294f7852321748cb29d13466a1af8862e2db384a1b`). Vehicle classes are bicycle, car, motorcycle, bus and truck. The service binds model identity to the checkpoint and the full inference policy, including resolution, confidence, class mapping, native box-size filtering and generic overlap suppression. Optional crop and decode policies add a view-specific identity; old full-frame evidence cannot inherit validation after a crop change. It verifies the local hash before loading; inference disables downloads, auto-installation, telemetry and image saving.

Real nano-model fine-tuning completed 20 epochs and **60 instrumented optimizer updates** on nine licensed Batam training photographs with 56 assistant-authored boxes; three separate validation photographs contain 22 boxes. Nano validation mean count error improved from 5.33 to 2.00 vehicles/frame. Larger pretrained YOLO26s at 1280 achieved 1.33 on that same small validation set, so validation selected it over the fine-tuned nano candidate. Selected serving weights are therefore pretrained, not the trained nano checkpoint.

The four previously consumed Batam test photographs were used only after freezing this selection, as a reused diagnostic: mean error 1.00 vehicle/image, one of four exact counts, three of four within one vehicle, and 88.9% localization precision/recall at IoU 0.5. These are tiny, adaptive, assistant-labelled photographic results, not independent live-camera accuracy. They do not establish 99–100% accuracy or superiority to Google. Actual serving outputs matched the frozen offline detections on all four diagnostic photographs.

The [frozen model report](../backend/data/research/yolo26_batam_refinement/README.md), [training receipt](../backend/data/research/yolo26_batam_refinement/training_execution_receipt.json), [serving policy](../backend/data/research/yolo26_batam_refinement/serving_policy.json) and [parity check](../backend/data/research/yolo26_batam_refinement/serving_parity.json) preserve provenance and limitations. No Matanya footage entered training. Ultralytics offers AGPL-3.0 and Enterprise licensing; the local implementation uses the free AGPL route. Source-availability obligations need to be addressed for distribution or a network deployment; no commercial license was purchased.

## Timing, tracking and congestion percentage

Source-clocked HLS uses program timestamps. The explicit Matanya pilot instead observes media-sequence progress: it waits for a new segment after an initial baseline, anchors that segment to its first observed delivery time, and preserves relative decoded frame times. Repeated playlists cannot renew their timestamp; backwards sequences, ended playlists and stopped progress are rejected. A newly appearing source timestamp cannot silently promote receiver-side timing. This measures freshness of observed delivery, with unknown camera-to-server delay; it is labelled `stream_observation`, not a verified physical capture time.

Frames are decoded in RAM with local FFmpeg and passed to a persistent local detector. A static-background check, exposure checks, detection bounds, tracking history, frame gaps and observation expiry determine whether numeric evidence is usable. The backend associates vehicles across frames and clips the union of detection boxes to the road polygon. Counts are vehicles visible in the sampled view. Signed crossing events are distinct tracked line crossings within the rolling window, not independently validated vehicles/hour. Motion association tolerates short occlusions and avoids repeated jitter crossings; unknown motion is kept separate from observed slow movement. Displacement and stopped duration are image-space proxies, not measured km/h or queue metres.

The provisional 0–100 index is:

```text
occupancy_factor = min(image_box_occupancy / configured_high_occupancy, 1)
slowdown = clamp(1 - image_motion / (5 * stationary_motion_threshold), 0, 1)
index = 100 * occupancy_factor * (0.5 + 0.3 * stationary_fraction + 0.2 * slowdown)
```

The UI's ordinary congestion values may describe a planning index; this camera index is not a probability, percentage of the city congested, or measured lane capacity. The pilot's configured thresholds and camera accuracy remain independently unvalidated. Usable estimates require explicit local policy, a road polygon and temporal evidence. Five seconds verifies playback and initial movement; the collector samples every second and needs at least 15 seconds of qualified rolling evidence for a provisional estimate, retaining up to 60 seconds. Stationary classification still requires its configured stop duration. Current unknown classifications cannot bypass the stricter independently validated operational gate. They retain `operational_eligible=false`, `validation_status=estimated`; independent-validation flags are not forged to enable the demo. Missing, blocked or stale evidence is null and never converted to zero vehicles/free traffic. A real empty-view detection may produce index zero, which is a model output, not a correctness guarantee.

## Route calculation

The route solver reads fresh numeric camera estimates server-side and verifies the graph revision, exact directed edge, age and current policy. A camera cannot affect unrelated nearby roads or an entire corridor by guessed radius. The additive route-search penalty is bounded:

```text
penalty_seconds = min(2 * edge_free_flow_seconds,
                      2 * edge_free_flow_seconds * vehicle_sensitivity * (index / 100)^2)
```

Route-preference weighting then applies. Balanced/fastest/easy/local objectives may avoid a busy mapped edge; Shortest keeps its distance objective. Vehicle access restrictions remain enforced. The penalty changes search preference, not physical road speed, ETA or calculated emissions for a fixed path. Search results retain backend provenance for audit, while the frontend shows its existing route result and map.

Live camera snapshots bypass incompatible route-cache entries. Revoked, stale, unmapped, wrong-direction and wrong-graph observations have no influence. Current observations apply only to departures within 120 seconds of now, not future traffic forecasts. The existing departure picker treats the current minute as now; a short transport grace prevents a freshly sent timestamp being rejected milliseconds later. Explicit future and arrival planning remain supported.

## Local operation

Install the optional isolated runtime using `scripts/requirements-yolo26.txt`; the local `.venv-yolo26` is already installed. The normal API lifespan supervises enabled registered cameras on a persistent local host. Start the app with:

```sh
CROSSFLOW_CCTV_ENABLED=1 ./scripts/dev.sh --backend-only
```

`CROSSFLOW_CCTV_ENABLED=0` disables collection. `CROSSFLOW_CCTV_PYTHON` can select a server-owned compatible Python executable. No external scheduler or recurring notification was created. Serverless execution does not start background capture; deployment requires a separate persistent worker and shared numeric storage.

The supervisor schedules every eligible source fairly rather than truncating the registry to its first two entries. `CROSSFLOW_CCTV_MAX_CONCURRENT` is a strict worker limit (default two; bounded by CPU capacity and eight). The current local six-view configuration uses six concurrent workers, keeping them continuous with hourly renewal. If eligible sources exceed capacity, workers rotate through all of them using `CROSSFLOW_CCTV_SLICE_SECONDS` (at least 60 seconds, extended for temporal requirements). Rotated-out observations still expire after 30 seconds; a 44-view catalog is not 44 simultaneously fresh measurements on this host. Temporary timeouts, selected server failures, expired segments and stalled playlists retry in the same process, preserving the loaded detector and camera state. Freshness and real frame-gap checks still apply. Bounded allowlisted JSON events provide internal diagnostics without storing media, URLs, raw exception text or credentials. Detector children receive a limited runtime environment rather than the API's server secrets.

The trusted registry is `backend/data/cctv_cameras.json` (`CROSSFLOW_CCTV_REGISTRY` override), and numeric SQLite storage defaults to `backend/data/cctv_observations.sqlite` (`CROSSFLOW_CCTV_DB` override). The API and worker must share these paths. Permissions, provisional routing policy and timing mode are rechecked while collecting and reading. The supervisor refreshes eligible registry state every five seconds, independently retries failures with backoff, and terminates revoked or materially reconfigured workers. Metadata-only label changes do not interrupt healthy collection. The store has bounded aggregate retention and immutable camera timestamp watermarks; exact replay is idempotent, conflicting/out-of-order evidence is rejected. No camera media is stored.

A bounded manual reproduction is available when the supervisor is disabled, to avoid two collectors racing on one camera:

```sh
.venv-yolo26/bin/python scripts/cctv_capture.py --camera-id 15 --max-seconds 120
```

The collector supports registered, same-origin HTTPS unencrypted transport-stream HLS. Redirected, encrypted, ended, cross-origin, master and fragmented-MP4 sources are refused. RTSP is not implemented. Raw-frame JSONL is also available only as a trusted local worker protocol; no browser upload feature exists.

`GET /api/analytics/cctv-status` remains an operator diagnostic endpoint with private/no-store responses; it is not mounted as a frontend feature. Configured Supabase mode uses the existing role policy; explicit disabled/demo auth retains guest policy. `POST /api/internal/cctv-observations` accepts bounded numeric evidence using the server-only administrator credential before reading the body. Neither endpoint accepts stream URLs, media, registration, calibration changes or model-promotion commands. Private source details never appear in status responses.

## Verification and practical limits

The first actual public-stream check processed 12 segments / 55 frames / 55 accepted numeric observations without retaining media. The stronger selected YOLO26s then ran under the actual local API supervisor. Browser verification exercised real stored camera estimates and an actual route request using the mapped approach. A separate fixture demonstrated a busy edge changing path selection from 240 to 320 metres while preserving vehicle restrictions; the live night-time pilot was quiet and did not prove real congestion avoidance under busy traffic.

Tests cover geometry, tracking, source progress and clock policy, provisional-versus-validated separation, expiry/revocation, transport/storage bounds, detector hash and serving parity, exact-edge path selection, cache behavior and existing frontend requests. [Routing test evidence](verification/cctv-routing-2026-10-24.json) and current live receipts are separate from model accuracy. Earlier screenshots containing camera cards record an intermediate implementation; those frontend additions were removed at the user's request. The backend pipeline and existing route map remain.

The [current live test receipt](verification/backend-cctv-live-checks.json) records 126 focused backend tests and an overlapping 112-test optional-runtime suite, plus 92 frontend tests, TypeScript, focused lint and the production build. The [final browser receipt](verification/backend-cctv-existing-ui.json) verifies a real current route with fresh hidden camera evidence while the normal UI displays no CCTV controls or copy. Authentication used an isolated loopback operator fixture; production sign-in was not verified. The actual local API and supervised camera collector were left running; temporary QA servers and tabs were removed.

## Expanded live verification, 24 October 2026

Round 13 checks compare HLS sequence movement with decoded browser playback and source dates, and run the actual local YOLO26s against each reviewed view in memory. The camera-1 upper-road crop produced vehicle detections; the revised camera-48 static median resolved an untextured-background quality rejection without weakening the stability gate. Numeric-only receipts are retained under `docs/verification/`; no stream recordings or stills were saved. Runtime scheduling, crop containment, view identities, temporal motion coverage, store rejection, expiry, routing and revocation have focused regression coverage. These engineering checks establish execution and safety gates; live-camera counting accuracy and congestion thresholds still require an independent labelled Batam evaluation.

The actual authenticated local API was restarted with the exact reviewed registry and six worker slots. Cameras 1, 13, 19, 48 and 51 subsequently supplied fresh usable numeric observations together. Fresh qualified counts fluctuate when quality gates block a frame; the API reports the current count and expiry rather than a fixed five or six. Camera 15 changed to an `EXT-X-ENDLIST` playlist during activation and produced no new observations. Its stale estimate is excluded, so the live camera routing-edge count is currently zero. The scheduler continues bounded retries; no ended-playlist or freshness gate was relaxed to manufacture live routing evidence.

[Activation evidence](verification/cctv-six-camera-activation-2026-10-24.json) records the actual five collectors and numeric API response. [Actual current route verification](verification/cctv-reviewed-six-route-live-2026-10-24.json) confirms the ended view contributes zero penalty; it is a successful stale-evidence rejection, not proof of an active camera-driven detour. A browser journey request through the unchanged app returned a route and an interactive map with no video or camera iframe. A further directional mapping review is required before the other fresh views can influence road search.

The independent follow-up mapping review did not approve an additional directed binding: camera 48's isolated approaching carriageway still lacks a georeferenced pole/optical bearing, and camera 13's current road polygon spans opposing carriageways. An official directional label or nearest bus stop is insufficient to select a directed graph edge. These are explicit remaining evidence gaps; the code does not guess them.

Focused post-change checks passed 90 tests in the actual optional YOLO environment. A separate 60-test analyzer/routing run (with 31 subtests) also passed; these suites overlap and are not additive unique-test counts. The browser receipt and [existing route-map screenshot](verification/cctv-existing-route-ui-2026-10-24.png) document a successful ordinary journey request, zero camera media elements, and no visible errors. They do not claim that an unavailable or unmapped camera selected that route.

## Website data-flow audit follow-up

The backend now checks playlist availability for all 28 registered public sources, including inference-disabled views. Two bounded metadata workers check advancing sources normally every 15 seconds and retry ended/unreachable sources after 60 seconds. Checks retain only safe scalar metadata in RAM, reject redirects and known media response bodies, and request no video segments. Source-clock age is rechecked when status is read, not just when a playlist is fetched. Worker lifecycle guards permit restart after a completed delayed shutdown without exceeding two workers. Availability can never grant inference permission, create a road polygon, certify accuracy, or enable routing. The existing protected camera-status API exposes availability separately from numeric traffic evidence; no CCTV frontend was added.

The website's numeric operations summary now refreshes independently of road/ferry loading. Its normal cadence is 10 seconds, with an earlier check two seconds before a camera aggregate expires, a one-second minimum delay and bounded retries after expiry. Automatic hidden-tab checks pause and foreground checks resume immediately; the displayed aggregate still withdraws at its original expiry. Road/ferry polling remains 30 seconds and existing upstream caches are reused. This fixes the former gap in which a 30-second page poll could outlast a frame-based 30-second TTL.

The dated [28-source smoke receipt](verification/cctv-availability-28-2026-10-24.json) found four advancing sources (1, 13, 19 and 48), 23 ended sources and one stalled source (10). Camera 51 changed to ended after the earlier six-view review. These are dated observations; availability is now checked continuously while the local backend runs. Four fresh views contributed provisional numeric estimates after activation, with zero currently eligible directed camera edges.
