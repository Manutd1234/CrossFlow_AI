# ML training and engine changes — 24 October 2026

Real-data training has begun following the user's explicit instruction. The experiments below are saved locally with source hashes, licenses, chronological splits, model artifacts, predictions and baseline comparisons. They do not replace the serving synthetic demo model. No provider API, cloud training job, account, database mutation or camera recording was used.

| Experiment | Fit / evaluation evidence | Model MAE | Comparison MAE |
|---|---|---:|---:|
| Batam Gajah Mada road speed | 32 published means for final fit; 16 means on the third survey day for retrospective evaluation | 3.234 km/h | Previous-day same-slot: 4.135 km/h |
| Paris hourly road counts | 21,658 measured road-hours for final fit; 7,454 later road-hours across 23 scored arcs | 56.331 vehicles/hour | Persistence: 73.929 vehicles/hour |
| Foreign image vehicle counts | 48 Visaitech training frames; 32 source-disjoint TRANSSET aerial images | 3.614 vehicles/frame | Constant training median: **2.469 vehicles/frame** |

The Batam result is 21.8% better than the previous-day baseline, but only about 1.5% better than the stronger directional-mean baseline (3.284 km/h). It covers one site, two directions and three days in February 2023, using published means rather than individual trajectories. The data were previously examined in the repository, so this is a retrospective diagnostic, not a new prospective accuracy trial. The flagged source-row sensitivity changes MAE to 3.284 km/h without retuning. No free-flow reference, severity class or route-travel-time label was invented.

Nine fixed Batam presets, including three baselines, are compared on day two. Only the frozen winner, ExtraTrees with depth two, is refit on days one and two and evaluated on day three. The data-only numeric JSON artifact restricts inference to the reviewed site, source, directions and diagnostic time windows; it cannot silently become a present-day citywide model. [Speed report](../backend/data/research/speed_training/report.json), [artifact](../backend/data/research/speed_training/model.json), [source attribution and license](../backend/data/research/batam-gajah-mada-2023.manifest.json).

The Paris gradient-boosting model reduces persistence MAE by 23.8%. WAPE is 11.78%, and RMSE is 87.832 vehicles/hour. Selection uses August training and early-September validation; the final test covers 17–30 September 2026. Exact timestamp lags preserve gaps and missing targets. An arc with no observed test targets remains explicitly unscored. One reporting-only rerun fixed empty-arc reporting after the holdout had already been consumed; features, splits, hyperparameters and selection stayed fixed. These are foreign detector counts, not Batam speeds or camera labels. [Forecast report](../backend/data/research/forecast_training/report.json), [QA and execution detail](../backend/data/research/forecast_training/README.md).

The visual count model fails its transfer check: its error is 46.4% higher than the constant baseline. Grouped validation already preferred the constant strategy. The learned candidate stays disabled; no test-driven retuning was performed. The experiment uses pixels and supplied vehicle boxes, excludes pedestrians, groups whole source videos and audits duplicate captures across partitions. It is a static image-count diagnostic, not object detection, tracking, hourly flow, queue estimation or a Batam camera model. [Visual report and source/view-shift limits](../backend/data/research/vision_training/report.json).

## Forecasting engine

The new experimental causal residual engine learns changes from persistence using current congestion, lagged measurements, age, recent trend/variation, forecast horizon and calendar features. It handles 15-, 30- and 60-minute forecasts separately. A previous time/spatial-profile forest had no current-speed input; its results must not be described as a learned response to current traffic.

Inputs must have been both observed and available by the forecast origin. Training, validation and calibration labels must also have arrived before their respective model or interval freeze. Whole timestamp groups stay together, with 60-minute purges. Validation chooses between two bounded forest configurations; calibration sets an empirical interval radius; the final test checks error, coverage and corridor safeguards. Stable fingerprints include complete measurement payloads and availability times. Unit-test histories are explicitly artificial fixtures and are never presented as observed model performance.

Seven-day coverage, required corridor coverage, baseline improvement and uncertainty checks remain enforced. The local provider archive is read through SQLite `mode=ro` and `query_only`, with no store initialization. Its source-policy fields and deterministic identities are revalidated, and ingestion timestamps are retained. Provider rows are excluded from fitting because training rights have not been independently established. Its few hours of coverage also fall short of the release requirements. Raw provider observations are not exported in the training bundle.

Derived severity and km/h errors remain distinct. Score definitions include a finite stopped-speed floor; they are not camera queue labels or a calibrated route-travel-time model. Locations are matched by rounded coordinates, without validated directed-edge mapping. No causal candidate is activated merely because its engineering checks pass.

The chronological lag design follows the [scikit-learn lagged-feature example](https://scikit-learn.org/stable/auto_examples/applications/plot_time_series_lagged_features.html); actual sensor availability and source-specific label checks are additional constraints in this implementation.

## Run and inspect

From `CrossFlow_AI/`, aggregate the saved, hash-verified experiments and refresh read-only archive diagnostics:

```sh
.venv/bin/python scripts/train_ml_engine.py --reuse-results
```

On a fresh checkout with the acquired inputs available, omit `--reuse-results` to train. For the other fixed protocols, use `--skip-vision --replace-results` only when deliberately repeating training; the consumed visual holdout stays frozen. The visual feature script uses a Python with Pillow/NumPy/scikit-learn (`--vision-python`, default system `python3`); no package installation is performed by the runner. Repeated use of the same holdout does not create independent validation evidence. `scripts/train_local_mac.py` now enters this real-data runner instead of silently generating demo data or fitting mutable routing costs. The older Modal cost-fitting scripts were not used: `road_network.cost_s` may already contain a heuristic or model prediction.

[Aggregate status](../backend/data/research/ml_training/status.json), [complete run](../backend/data/research/ml_training/report.json), [artifact receipts](../backend/data/research/ml_training/manifest.json). Authorized operators may read the checksum-verified summary at `GET /api/analytics/training-results`. It has private/no-store headers and cannot fit, load executable models, change thresholds or activate a model. Serving-model status remains separate.

The 100,000-image/annotation target is still unmet. No licensed annotated Batam camera sessions are present, and no Batam camera queue model is claimed. Additional independent roads, dates, camera sessions and checked labels are needed before a credible citywide evaluation.

## Verification

91 focused checks passed across the appropriate local runtimes: 15 survey-speed tests, seven hourly-forecast tests, six visual tests, 17 causal-forecast tests, four snapshot/integrity tests, four results-API tests and 38 existing observed-training tests. An independent review reproduced and verified the delayed-target-label fix. Artifact reload parity, per-file hashes and saved prediction metrics were checked. The results API was also exercised in process through ASGI without opening a server or making provider calls.

The visual tests execute with the documented Pillow environment and skip cleanly in the API-only environment. Its optional pins are in [requirements-vision-training.txt](../scripts/requirements-vision-training.txt); no vision dependency was added to the API runtime. The source-specific reports retain the exact environments used for training.
