"""Historical evidence must preserve geographic scope and reject corrupt data."""
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.research_history import DATA_DIR, load_research_history, ResearchHistoryUnavailable


def copy_reviewed_artifacts(destination):
    """Copy the bounded historical inputs, excluding optional vision runs."""
    destination.mkdir()
    for source in DATA_DIR.iterdir():
        if source.is_file() and source.suffix in {'.csv', '.json'}:
            shutil.copyfile(source, destination / source.name)


class ResearchHistoryTests(unittest.TestCase):
    def test_batam_benchmark_is_real_but_cannot_claim_serving_promotion(self):
        report = load_research_history()['batam_benchmark']
        self.assertEqual(report['synthetic_training_rows'], 0)
        self.assertEqual(report['random_forest']['samples'], 32)
        self.assertFalse(report['promotion_eligible'])
        with TemporaryDirectory() as directory:
            copied = Path(directory) / 'research'
            copy_reviewed_artifacts(copied)
            path = copied / 'batam-site-backtest.json'
            data = json.loads(path.read_text())
            data['folds'][0]['latest_training_interval_end'] = data['folds'][0]['forecast_origin']
            path.write_text(json.dumps(data))
            with self.assertRaises(ResearchHistoryUnavailable):
                load_research_history(copied)

    def test_reviewed_artifacts_preserve_dates_units_and_geographic_limits(self):
        evidence = load_research_history()
        survey, benchmark = evidence['survey'], evidence['benchmark']
        self.assertEqual(survey['row_count'], 48)
        self.assertEqual(survey['dates'], ['2023-02-16', '2023-02-17', '2023-02-18'])
        self.assertEqual((survey['min_speed_kph'], survey['max_speed_kph']), (20.35, 34.32))
        self.assertIsNone(survey['canonical_corridor_mapping'])
        self.assertFalse(survey['serving_model_replaced'])
        self.assertEqual(sum('over_0.5' in r['quality_flag'] for r in survey['rows']), 1)
        self.assertEqual(benchmark['speed_band_records'], 397341)
        self.assertEqual(benchmark['synthetic_training_rows'], 0)
        self.assertFalse(benchmark['serving_model_replaced'])
        self.assertLess(benchmark['results'][2]['random_forest']['mae_band_units'], benchmark['results'][2]['persistence']['mae_band_units'])

    def test_checksum_change_fails_closed_without_returning_a_partial_survey(self):
        with TemporaryDirectory() as directory:
            copied = Path(directory) / 'research'
            copy_reviewed_artifacts(copied)
            csv = copied / 'batam-gajah-mada-2023.csv'
            csv.write_bytes(csv.read_bytes().replace(b'21.16', b'99.99', 1))
            with self.assertRaises(ResearchHistoryUnavailable):
                load_research_history(copied)

    def test_seven_day_saturation_records_cannot_become_speed_labels(self):
        survey = load_research_history()['saturation_survey']
        self.assertEqual(len(survey['rows']), 28)
        self.assertEqual(survey['dates'], [f'2023-06-{d:02}' for d in range(12, 19)])
        self.assertEqual(survey['unit'], 'volume_capacity_ratio')
        self.assertEqual(survey['eligible_speed_training_rows'], 0)
        peak = next(r for r in survey['rows'] if r['interval_start'] == '2023-06-17T17:00:00+07:00')
        self.assertEqual(peak['degree_of_saturation'], 1.294)
        self.assertNotIn('speed_kph', peak)

    def test_later_partition_cannot_include_earlier_training_targets(self):
        with TemporaryDirectory() as directory:
            copied = Path(directory) / 'research'
            copy_reviewed_artifacts(copied)
            report = copied / 'singapore-academic-backtest.json'
            data = json.loads(report.read_text())
            data['chronology']['fit']['latest_target_source_clock'] = data['chronology']['test']['latest_target_source_clock']
            report.write_text(json.dumps(data))
            with self.assertRaises(ResearchHistoryUnavailable):
                load_research_history(copied)

    def test_loading_never_modifies_the_reviewed_artifacts(self):
        before = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in DATA_DIR.iterdir() if p.is_file()}
        load_research_history()
        after = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in DATA_DIR.iterdir() if p.is_file()}
        self.assertEqual(before, after)

    def test_missing_artifact_returns_a_sanitized_failure(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ResearchHistoryUnavailable, '^Published historical evidence is unavailable or failed validation.$'):
                load_research_history(Path(directory))


if __name__ == '__main__':
    unittest.main()
