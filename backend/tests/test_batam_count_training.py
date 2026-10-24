"""Temporal-leakage and scope checks; artificial fixtures are not accuracy evidence."""
from copy import deepcopy
from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/train_batam_counts.py"
SPEC = importlib.util.spec_from_file_location("batam_count_training", SCRIPT)
training = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(training)


def artificial_rows(name="sekupang_15min"):
    # Explicitly artificial series; fixtures test causality, not observed performance.
    rows = []
    if name == "sekupang_15min":
        starts = [datetime.fromisoformat("2023-12-17T06:30:00+07:00") + timedelta(minutes=15 * i) for i in range(6)]
        for i, start in enumerate(starts):
            for c in ("HV", "LV", "MC"):
                rows.append({"id": f"fixture-{i}-{c}", "source_id": "artificial", "start": start,
                             "end": start + timedelta(minutes=15), "value": float(i + (0 if c == "HV" else 10)),
                             "key": ("fixture approach", "fixture movement", c), "class": c,
                             "group": start.isoformat(), "quality_flags": "artificial_fixture",
                             "source_url": "fixture", "pdf_page": "0", "table": "fixture"})
    else:
        for day in range(3):
            for hour in (7, 8, 16, 17):
                start = datetime.fromisoformat(f"2023-02-{16 + day}T{hour:02}:00:00+07:00")
                for c in ("SM (motorcycle)", "KR (light vehicle)", "KB (heavy vehicle)"):
                    rows.append({"id": f"fixture-{day}-{hour}-{c}", "source_id": "artificial", "start": start,
                                 "end": start + timedelta(hours=1), "value": float(day + hour),
                                 "key": ("fixture direction", f"{hour:02}:00", c), "class": c,
                                 "group": start.date().isoformat(), "quality_flags": "artificial_fixture",
                                 "source_url": "fixture", "pdf_page": "0", "table": "fixture"})
    return rows


def test_final_target_values_cannot_change_frozen_model_or_selection():
    original = artificial_rows()
    changed = deepcopy(original)
    for row in changed:
        if row["group"] in training.SOURCE_SPEC["sekupang_15min"]["test_groups"]:
            row["value"] += 10000
    report, model, _, selection = training.run_experiment("sekupang_15min", original, "fixture-protocol")
    report2, model2, _, selection2 = training.run_experiment("sekupang_15min", changed, "fixture-protocol")
    assert model == model2
    assert selection == selection2
    assert report["test_metrics"] != report2["test_metrics"]
    assert report["splits"]["whole_source_timestamp_groups"] is True
    assert report["production_eligible"] is False


def test_selection_receipt_callback_occurs_before_test_metric_evaluation(monkeypatch):
    events = []
    metrics = training.metrics

    def observed_metrics(actual, predicted):
        events.append("metric")
        return metrics(actual, predicted)

    monkeypatch.setattr(training, "metrics", observed_metrics)
    training.run_experiment("sekupang_15min", artificial_rows(), "fixture", freeze_selection=lambda _: events.append("freeze"))
    assert events.index("freeze") == len(training.SOURCE_SPEC["sekupang_15min"]["presets"])
    assert events[events.index("freeze") + 1] == "metric"


def test_transition_builder_preserves_gaps_instead_of_inventing_counts():
    rows = artificial_rows()
    rows = [r for r in rows if r["start"].strftime("%H:%M") != "07:00"]
    pairs = training.transitions("sekupang_15min", rows)
    assert pairs
    assert all(p["target"]["start"] - p["previous"]["start"] == timedelta(minutes=15) for p in pairs)
    assert not any(p["target"]["start"].strftime("%H:%M") == "07:15" for p in pairs)


def test_scoped_inference_rejects_unavailable_or_foreign_counts():
    _, payload, predictions, _ = training.run_experiment("sekupang_15min", artificial_rows(), "fixture")
    p = predictions[0]
    request = {"site_id": payload["site_id"], "source_sha256": payload["source_pdf_sha256"],
               "interval_start": p["interval_start"], "key": json.loads(p["series_key_json"]),
               "previous_count": p["previous_count"], "previous_interval_end": p["prior_interval_end"],
               "previous_available_at": p["prior_interval_end"], "forecast_origin": p["interval_start"]}
    assert training.predict_scoped(payload, **request)["predicted_count"] == pytest.approx(p["selected_prediction"])
    for changes in ({"site_id": "another Batam road"}, {"source_sha256": "foreign"}, {"previous_count": -1},
                    {"previous_count": float("nan")}, {"previous_count": True},
                    {"previous_available_at": "2023-12-17T08:00:00+07:00"},
                    {"interval_start": "2026-10-24T07:45:00+07:00"},
                    {"key": ["unknown", "unknown", "MC"]}):
        with pytest.raises(ValueError):
            training.predict_scoped(payload, **{**request, **changes})


def test_uturn_source_has_a_separate_day_grouped_protocol():
    rows = artificial_rows("gajah_mada_uturn_hourly")
    report, model, _, _ = training.run_experiment("gajah_mada_uturn_hourly", rows, "fixture")
    assert report["splits"]["candidate_fit"]["groups"] == ["2023-02-16"]
    assert report["splits"]["validation"]["groups"] == ["2023-02-17"]
    assert report["splits"]["test"]["groups"] == ["2023-02-18"]
    assert model["source_license"] == "CC BY-SA 4.0"
    assert model["unit"] == "vehicles_in_1_hour_interval"
    assert not set(model["allowed_test_intervals"]) & {r["start"].isoformat() for r in rows if r["group"] < "2023-02-18"}


def test_final_uturn_day_cannot_change_model_or_winner():
    rows = artificial_rows("gajah_mada_uturn_hourly")
    changed = deepcopy(rows)
    for row in changed:
        if row["group"] == "2023-02-18":
            row["value"] *= 50
    _, first, _, first_selection = training.run_experiment("gajah_mada_uturn_hourly", rows, "fixture")
    _, second, _, second_selection = training.run_experiment("gajah_mada_uturn_hourly", changed, "fixture")
    assert first == second
    assert first_selection == second_selection


def test_zero_count_metric_does_not_claim_undefined_percentage_accuracy():
    result = training.metrics([0, 0], [0, 1])
    assert result["mae_vehicles_per_source_interval"] == 0.5
    assert result["wape_percent"] is None
    assert "accuracy" not in result
