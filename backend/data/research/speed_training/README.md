# Batam survey speed experiment

This directory contains an actually fitted model for the 48 published Gajah Mada interval speed means. Day 1 fits nine fixed candidates, day 2 chooses the smallest validation MAE (then RMSE, then model name), and the chosen family is refit on days 1 and 2 before scoring day 3. Both directions remain together within each survey day. These data were already benchmarked in this repository, so this is a retrospective diagnostic and does not supply fresh prospective holdout evidence.

`report.json` includes the selection scores, final errors by direction, source attribution and license, source quality flags, and a sensitivity fit excluding the one flagged training row without choosing a new model family. `predictions.csv` contains all 16 final test predictions. `validation.csv` contains the fixed candidate comparison. `model.json` is bounded, checksummed numeric JSON with no pickle or executable deserialization. `manifest.json` hashes these four outputs.

The artifact predicts historical 30-minute mean speed at this one reported site on 18 February 2023, in the eight published peak clock windows. Exact geometry and a canonical corridor mapping are unverified. It rejects other roads, dates, directions and clock windows. It does not replace the application's serving forecaster or write observations. It uses clock time and direction, or strictly earlier same-slot speeds for baselines. Target-interval travel times, volume, density and calculated free-flow references are excluded.

Reproduce from the repository parent:

```sh
CrossFlow_AI/.venv/bin/python CrossFlow_AI/scripts/train_batam_speed.py
```

Run from the backend directory to use portable inference:

```python
from pathlib import Path
from services.survey_speed_model import (
    SITE_ID, SOURCE_PDF_SHA256, load_survey_speed_model, predict_survey_speed,
)
model = load_survey_speed_model(Path("data/research/speed_training/model.json"))
prediction = predict_survey_speed(
    model, site_id=SITE_ID, source_sha256=SOURCE_PDF_SHA256,
    interval_start="2023-02-18T07:00:00+07:00", direction="from_east",
)
```

Kurniawan and Anwar (2023), Sigma Teknika 6(2), 479–492, Table 11 p487. The reviewed source manifest records CC BY-SA 4.0. The transcription retains published means and flags their limitations; no author endorsement is implied. Source and license links are retained in every JSON output. No severity, vehicle annotation, current-traffic or citywide forecast accuracy is established here.
