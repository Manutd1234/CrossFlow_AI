# Batam aggregate count experiments — 24 October 2026

Two source-specific retrospective experiments use **288 published Batam count cells** newly introduced into the count experiments: **228 cells in final fitting/history and 60 final holdout targets**. These are previously collected paper observations; **zero new field observations were acquired**. The units and movement scopes are kept separate. Neither model is enabled for production.

| Experiment | Final fitting/history | Final test | Chosen model | MAE | Persistence MAE | WAPE |
|---|---:|---:|---|---:|---:|---:|
| Sekupang, 15-minute count per approach/movement/class | 180 cells; 144 labeled transitions | 36 cells at one timestamp | Ridge residual, alpha 10 | **1.493 vehicles / 15-minute cell** | 1.889 | 8.001% |
| Gajah Mada, hourly U-turn count per direction/class/clock slot | 48 cells over two days | 24 cells on the third day | **Persistence baseline** | 32.000 vehicles / hour cell | 32.000 | 14.904% |

The Sekupang model's final MAE is 20.93% below persistence. Its test is only the 07:45–08:00 interval of one Sunday, with 36 correlated cells from four approaches, three movements and three classes. This does not establish independent vehicle accuracy, citywide accuracy, operational readiness or superiority over Google. WAPE is an aggregate error metric; `100 - WAPE` is not reported as classification accuracy. No route travel-time, speed, severity, queue or image-object model was trained by this script.

The U-turn experiment uses 16 February for candidate fitting/history, 17 February for selection and 18 February for the final diagnostic. With only one initial survey day, same-series means and all shrinkage variants coincide with persistence on validation. They are not independent sources of validation evidence. The declared tie rule keeps persistence. The final test does not trigger retuning or promotion. This article and its dates were previously examined for the separate speed diagnostic.

## Sources and licenses

**BATAM-014:** Syarqiah and Sari (2024), *Optimizing Traffic Management at Batam City Intersections: Analysis of Congestion Causes and Mitigation Strategies*, Journal of Civil Engineering and Planning 5(1), [DOI and publisher page](https://journal.uib.ac.id/index.php/jce/article/view/9076). The publisher article page was rechecked and states **[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)**. Table 4.1 provides 216 class cells at six 15-minute timestamps on 17 December 2023. Later hourly summaries and percentages disagree with component cells; the original individual cells remain unchanged and flagged. Field count sheets and original capture timing were not independently available.

**BATAM-EXT-005:** Kurniawan and Anwar (2023), *Pengaruh U-Turn terhadap Kinerja Lalu Lintas pada Ruas Jalan Gajah Mada Depan Simpang Sekolah Kemilau Bangsa Kota Batam*, Sigma Teknika 6(2), 479–492, [publisher page](https://www.journal.unrika.ac.id/index.php/sigmateknika/article/view/5642/0). The existing hash-verified speed-source manifest records the reviewed publisher **[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)** license. Its 72 hourly U-turn class cells are distinct from the 48 mean-speed cells previously fitted. Printed subtotals disagree with individual counts and the caption names only the first day; cells are retained unchanged, flagged, with the explicit three dates in the table. The derived U-turn count data, model and results remain under CC BY-SA 4.0. Source attribution, URLs, original PDF hashes and changes are retained in `sources.json` and each model/report. No author endorsement is implied.

Other inspected sources were excluded from these models: BATAM-EXT-010/016/018 have header/unit or peak-only issues; BATAM-013 provides only four all-approach timestamps, including an inconsistent angkot total; BATAM-022 provides one timestamp; BATAM-EXT-001 has no established explicit open training/reuse license. Counts from incompatible studies were not rescaled into a false cross-site holdout.

## Frozen selection and causal inputs

`protocol.json` was saved before running either experiment. It fixes source hashes, candidate parameters, group boundaries, metric definitions and the refusal to overwrite results. The Sekupang fit uses targets at 06:45, 07:00 and 07:15, validation at 07:30 and final test at 07:45; complete approach/movement/class timestamp groups stay together. The frozen winner is refitted on fit and validation targets, then evaluated on the final group. `selection.json` is written before final test metric evaluation and hashes the final numeric model. The data were previously collected and inspected; the split is a retrospective diagnostic, not a newly untouched prospective holdout.

Inputs are the completed previous count for the same series and known approach/movement/class or clock-slot identity. No target interval's count, other-class count, PCU, density, saturation, speed, queue or travel time enters a feature. Constant calendar fields add no information to this tiny session and are omitted. Missing source intervals remain gaps. Published counts are assumed available at their interval end for this retrospective simulation; original real-time delivery latency is unknown.

Models are numeric JSON. `predict_scoped` in the training script checks source hash, site, known series identity, explicit timezone offsets, expected earlier interval and measurement availability. Inference is restricted to the frozen historical test intervals. It rejects current dates and other roads. This is a research contract and is not wired into the app or five canonical corridors.

## Reproduce and inspect

From `CrossFlow_AI/`, use a new output directory, since the default frozen results refuse overwrite:

```sh
.venv/bin/python scripts/train_batam_counts.py --output-dir /tmp/batam-count-reproduction --prepare-only
.venv/bin/python scripts/train_batam_counts.py --output-dir /tmp/batam-count-reproduction
.venv/bin/python -m pytest backend/tests/test_batam_count_training.py -q
```

Repetition only checks reproducibility and cannot create fresh validation evidence. The script uses the existing Python/NumPy runtime, reads only reviewed files and performs no network, provider, database, app activation or camera-recording operations. One reporting-only correction clarified that 288 experiment cells comprise 228 fitting/history cells and 60 holdout targets; model selection, predictions and evaluation were unchanged and no retraining occurred.

Seven focused checks passed, including final-target mutation tests that leave both frozen models and validation selections unchanged, unavailable-count and foreign-site rejection, gap preservation, whole-day grouping, and zero-target metric behavior. Independent JSON formula replay matched every saved final prediction and MAE/WAPE; all 34 hash, source, model, scope and reporting checks passed in `qa.json`. These engineering checks support the artifact's integrity, not a claim that the data are sufficient for deployment.
