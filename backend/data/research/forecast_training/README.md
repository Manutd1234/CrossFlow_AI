# Paris hourly-flow transfer benchmark

This run trained a real hourly-flow forecaster on public Paris loop measurements.
It does **not** validate Batam speeds, travel times, congestion, cameras or queues.

- Selected on validation MAE: `hist_gradient_boosting_15_squared_error`.
- Untouched 14-day holdout: 7,454 observed road-hours.
- MAE: 56.331 vehicles/hour; WAPE: 11.780%.
- Persistence MAE: 73.929 vehicles/hour.
- Daily seasonal MAE: 105.714; weekly seasonal MAE: 78.279.
- Original source has 2,328 null flow values and 48 absent arc/hour keys. These remain missing, not zero.

The first seven days supply feature history. August 8–31 trains six fixed CPU
candidates, September 1–16 selects the forecaster and calibrates residuals, and
September 17–30 is evaluated once after refitting on development data. All models
and baselines use the same observed holdout targets. Forecasts roll forward one
hour at a time and may use earlier measured holdout values, never future values.
Zero publication delay is assumed. This is not an open-loop two-week forecast.

`report.json` contains full protocol, split-key hashes, missingness, aggregate
metrics, validation candidate scores, limitations and environment. Per-road
metrics and holdout predictions are retained for inspection. `model.joblib`
contains the best learned candidate plus the selected forecaster identity and
fit-only fallback profile. Selection can prefer a baseline if it wins validation.
The model is intentionally ineligible for production and no app wiring is changed.

Source: Direction de la Voirie et des Déplacements, Ville de Paris,
[permanent road counts](https://opendata.paris.fr/explore/dataset/comptages-routiers-permanents/),
[ODbL](https://opendatacommons.org/licenses/odbl/). Source normalization hash:
`dc3db788c3937713d6d62c71a5f6cb9dcd3bc2b36724af74064f113b7e653583`. Derived predictions retain that attribution and license notice.

Reproduce with the recorded Python/scikit-learn environment:

```sh
.venv/bin/python scripts/research_forecasting.py --output backend/data/research/forecast_training_reproduction
```

The consumed holdout must not be reused to tune a new experiment. A future Batam
deployment requires authorized timestamped Batam observations, latency checks,
local chronological backtesting and target-specific validation.

Execution recovery: the first attempt froze model selection and fitted the final
model, then stopped while reporting an arc with no observed holdout targets.
Only empty-arc reporting was corrected. The identical fixed protocol was rerun;
no features, dates, model hyperparameters or selection rules changed in response
to holdout scores. Both attempts selected the same model with the same validation
scores. `execution_state.json` retains the second-attempt provenance.
