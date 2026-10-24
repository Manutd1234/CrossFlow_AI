"""Keep the real, sparse Batam site benchmark chronological and isolated."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("batam_survey_benchmark", ROOT / "scripts/backtest_batam_survey.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def test_whole_days_and_both_directions_are_held_out_after_all_training_targets():
    rows, _ = benchmark.load_source(ROOT / "backend/data/research")
    folds = list(benchmark.day_folds(rows))
    assert [(len(train), len(test)) for _, train, test in folds] == [(16, 16), (32, 16)]
    for origin, train, test in folds:
        assert max(r["end"] for r in train) < origin < min(r["start"] for r in test)
        assert not {r["start"].date() for r in train} & {r["start"].date() for r in test}
        assert {r["direction"] for r in test} == {"from_east", "from_west"}


def test_future_labels_cannot_change_features_or_an_earlier_day_forecast():
    rows, manifest = benchmark.load_source(ROOT / "backend/data/research")
    before = benchmark.evaluate(rows, manifest)
    changed = copy.deepcopy(rows)
    for row in changed:
        if row["start"].day == 18:
            row["speed"] = 100
    np.testing.assert_array_equal(benchmark.features(rows), benchmark.features(changed))
    after = benchmark.evaluate(changed, manifest)
    assert before["predictions"][:16] == after["predictions"][:16]
    assert [r["random_forest_kph"] for r in before["predictions"]] == [r["random_forest_kph"] for r in after["predictions"]]
    assert before["random_forest"]["mae_kph"] != after["random_forest"]["mae_kph"]


def test_changed_csv_cannot_silently_relabel_the_real_source():
    with TemporaryDirectory() as directory:
        path = Path(directory)
        copied = path / "research"
        copied.mkdir()
        # The checksum boundary reads this source pair only. Optional local
        # vision checkpoints and photo exports are unrelated to this test.
        for name in ("batam-gajah-mada-2023.csv", "batam-gajah-mada-2023.manifest.json"):
            shutil.copyfile(ROOT / "backend/data/research" / name, copied / name)
        source = path / "research/batam-gajah-mada-2023.csv"
        source.write_bytes(source.read_bytes().replace(b"21.16", b"99.99", 1))
        with pytest.raises(ValueError, match="checksum"):
            benchmark.load_source(path / "research")


def test_recomputed_results_match_reviewed_report_without_model_activation():
    rows, manifest = benchmark.load_source(ROOT / "backend/data/research")
    result = benchmark.evaluate(rows, manifest)
    stored = json.loads((ROOT / "backend/data/research/batam-site-backtest.json").read_text())
    for model in ("random_forest", "previous_survey_day", "historical_slot_mean"):
        assert result[model] == pytest.approx(stored[model], rel=1e-10, abs=1e-10)
    assert result["synthetic_training_rows"] == 0
    assert result["application_history_modified"] is False
    assert result["serving_model_replaced"] is False
    assert result["promotion_eligible"] is False
