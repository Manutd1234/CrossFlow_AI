"""Read-only, source-linked historical evidence, separate from serving telemetry.

These reviewed artifacts are bundled data, never caller-selected paths. Loading
them cannot train a model, mutate observed history, or fetch a remote source.
"""
import csv
from datetime import datetime, timedelta
import hashlib
import io
import json
import math
from pathlib import Path


DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "research"


class ResearchHistoryUnavailable(ValueError):
    pass


def _read(path):
    if path.stat().st_size > 256_000:
        raise ValueError("Research artifact exceeds its size limit")
    return path.read_bytes()


def _number(value, low=0, high=1000):
    number = float(value)
    if isinstance(value, bool) or not math.isfinite(number) or not low <= number <= high:
        raise ValueError("Invalid research measurement")
    return number


def _load_saturation_survey(directory):
    manifest = json.loads(_read(directory / "batam-tengku-sulung-2023.manifest.json"))
    raw = _read(directory / "batam-tengku-sulung-2023.csv")
    if hashlib.sha256(raw).hexdigest() != manifest["csv_sha256"]:
        raise ValueError("Saturation survey checksum differs")
    rows, seen = [], set()
    for row in csv.DictReader(io.StringIO(raw.decode("utf-8"))):
        start, end = (datetime.fromisoformat(row[k]) for k in ("interval_start", "interval_end"))
        if (start.utcoffset() != timedelta(hours=7) or end.utcoffset() != timedelta(hours=7)
                or end - start != timedelta(hours=1) or start in seen
                or row["unit"] != "volume_capacity_ratio"):
            raise ValueError("Invalid saturation interval or units")
        seen.add(start)
        rows.append({"interval_start": start.isoformat(), "interval_end": end.isoformat(),
                     "degree_of_saturation": _number(row["reported_degree_of_saturation"], 0, 5)})
    if not rows or len(rows) != manifest["transcription_rows"] or manifest["eligible_speed_training_rows"] != 0:
        raise ValueError("Unreviewed saturation scope")
    return {**{key: manifest[key] for key in ("dataset_name", "source_url", "source_pdf_url", "license", "license_url",
                                              "attribution", "source_table", "location", "scope_limitations")},
            "unit": "volume_capacity_ratio", "eligible_speed_training_rows": 0,
            "rows": rows, "dates": sorted({r["interval_start"][:10] for r in rows})}


def _load_batam_benchmark(directory, checksum):
    report = json.loads(_read(directory / "batam-site-backtest.json"))
    if (report["scope"] != "exploratory_single_batam_site_next_survey_day"
            or report["csv_sha256"] != checksum or report["unit"] != "km/h"
            or report["source_rows"] != 48 or report["source_calendar_days"] != 3
            or report["synthetic_training_rows"] != 0
            or report["serving_model_replaced"] is not False
            or report["application_history_modified"] is not False
            or report["promotion_eligible"] is not False
            or report["canonical_corridor_mapping"] is not None
            or len(report["folds"]) != 2):
        raise ValueError("Unreviewed Batam benchmark scope")
    for index, fold in enumerate(report["folds"]):
        origin = datetime.fromisoformat(fold["forecast_origin"])
        if (fold["test_date"] != ["2023-02-17", "2023-02-18"][index]
                or origin.date().isoformat() != fold["test_date"]
                or origin.utcoffset() != timedelta(hours=7)
                or datetime.fromisoformat(fold["latest_training_interval_end"]) >= origin
                or fold["training_rows"] != [16, 32][index] or fold["test_rows"] != 16):
            raise ValueError("Invalid Batam benchmark chronology")
    for group, count in ((report, 32), *((fold, 16) for fold in report["folds"])):
        for model in ("random_forest", "previous_survey_day", "historical_slot_mean"):
            if group[model]["samples"] != count:
                raise ValueError("Invalid Batam benchmark counts")
            _number(group[model]["mae_kph"], 0, 200)
            _number(group[model]["rmse_kph"], 0, 200)
    return report


def load_research_history(directory=DATA_DIR):
    try:
        manifest = json.loads(_read(directory / "batam-gajah-mada-2023.manifest.json"))
        raw_csv = _read(directory / "batam-gajah-mada-2023.csv")
        if hashlib.sha256(raw_csv).hexdigest() != manifest["csv_sha256"]:
            raise ValueError("Survey checksum differs")
        rows, seen = [], set()
        for row in csv.DictReader(io.StringIO(raw_csv.decode("utf-8"))):
            start, end = (datetime.fromisoformat(row[key]) for key in ("interval_start", "interval_end"))
            direction = row["direction_as_published"]
            key = (start, direction)
            if (start.utcoffset() != timedelta(hours=7) or end.utcoffset() != timedelta(hours=7)
                    or end-start != timedelta(minutes=manifest["aggregation_minutes"])
                    or direction not in ("from_east", "from_west") or key in seen):
                raise ValueError("Invalid survey interval or direction")
            seen.add(key)
            rows.append({
                "interval_start": start.isoformat(), "interval_end": end.isoformat(),
                "direction": direction,
                "speed_kph": _number(row["reported_speed_kph"], .1, 200),
                "travel_seconds": _number(row["reported_travel_time_seconds"], .1),
                "segment_length_m": _number(row["survey_segment_length_m"], 1, 10000),
                "quality_flag": row["quality_flag"],
            })
        dates = sorted({row["interval_start"][:10] for row in rows})
        if (not rows or len(rows) != manifest["transcription_rows"]
                or len(dates) != manifest["distinct_calendar_days"]
                or manifest["crossflow_corridor_mapping"] is not None
                or manifest["serving_model_replaced"] is not False):
            raise ValueError("Survey scope changed; review required")
        benchmark = json.loads(_read(directory / "singapore-academic-backtest.json"))
        if (benchmark["synthetic_training_rows"] != 0 or benchmark["serving_model_replaced"] is not False
                or benchmark["application_history_modified"] is not False
                or not benchmark["chronology"]["latest_fit_label_precedes_first_calibration_origin"]
                or not benchmark["chronology"]["latest_calibration_label_precedes_first_test_origin"]):
            raise ValueError("Unreviewed benchmark scope or chronology")
        for phase, next_phase in (("fit", "calibration"), ("calibration", "test")):
            if datetime.fromisoformat(benchmark["chronology"][phase]["latest_target_source_clock"]) >= datetime.fromisoformat(benchmark["chronology"][next_phase]["first_origin_source_clock"]):
                raise ValueError("Benchmark labels overlap a later partition")
        if [r["horizon_minutes"] for r in benchmark["results"]] != [15, 30, 60]:
            raise ValueError("Unexpected benchmark horizons")
        for result in benchmark["results"]:
            for model in ("random_forest", "persistence"):
                metrics = result[model]
                if metrics["samples"] != benchmark["chronology"]["test"]["rows"]:
                    raise ValueError("Benchmark sample counts differ")
                _number(metrics["mae_band_units"], 0, 7)
                _number(metrics["rmse_band_units"], 0, 7)
            _number(result["interval_empirical_coverage"], 0, 1)
        return {
            "survey": {
                "name": manifest["dataset_name"], "location": manifest["location"],
                "source_url": manifest["source_url"], "source_pdf_url": manifest["source_pdf_url"],
                "attribution": manifest["attribution"], "license": manifest["license"],
                "license_url": manifest["license_url"], "csv_sha256": manifest["csv_sha256"],
                "source_table": "Table 11, printed page 487 (PDF page 9)",
                "dates": dates, "rows": rows, "row_count": len(rows),
                "min_speed_kph": min(r["speed_kph"] for r in rows),
                "max_speed_kph": max(r["speed_kph"] for r in rows),
                "scope_limitations": manifest["scope_limitations"],
                "canonical_corridor_mapping": None, "serving_model_replaced": False,
            },
            "benchmark": benchmark,
            "saturation_survey": _load_saturation_survey(directory),
            "batam_benchmark": _load_batam_benchmark(directory, manifest["csv_sha256"]),
        }
    except (OSError, ValueError, KeyError, TypeError, OverflowError, csv.Error) as exc:
        raise ResearchHistoryUnavailable("Published historical evidence is unavailable or failed validation.") from exc
