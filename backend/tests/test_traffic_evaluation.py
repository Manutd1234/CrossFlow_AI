"""Prospective immutability, residual decoding and temporal benchmark boundaries."""
from datetime import datetime,timedelta,timezone
import importlib.util,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'backend'))
from services import forecast_evaluation as ledger
from services.traffic_observations import SpatialTrafficObservation
from services.traffic_candidate_selection import encode_target,decode_prediction

NOW=datetime(2026,10,24,tzinfo=timezone.utc)
spec=importlib.util.spec_from_file_location('expansion_benchmark',ROOT/'scripts/backtest_historical_expansion.py')
benchmark=importlib.util.module_from_spec(spec);spec.loader.exec_module(benchmark)


def forecast():
 return {'generated_at':NOW.isoformat(),'model':{'report':{'trained_at':(NOW-timedelta(hours=1)).isoformat(),
  'synthetic_rows':0,'training_source':'observed_traffic_archive','dataset_sha256':'unit-fixture',
  'horizons':[{'horizon_minutes':15,'selected_strategy':'random_forest','forest_candidate':'unit-fixture'}]}},
  'points':[{'corridor_id':'corridor-1','latitude':1.1,'longitude':104.02,'observed_at':NOW.isoformat(),
   'current_index':50.,'forecasts':[{'horizon_minutes':15,'forest_candidate_score':40.,'mean_score':40.}]}]}


class ProspectiveEvaluationTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'ledger.db'
 def observation(self,minutes,speed=30,source='loop_sensor'):
  stamp=NOW+timedelta(minutes=minutes)
  return SpatialTrafficObservation.create(corridor_id='corridor-1',latitude=1.1,longitude=104.02,observed_at=stamp,
   actual_speed_kph=speed,free_flow_speed_kph=50,source=source,validation_now=stamp)
 def test_only_predictions_issued_before_future_labels_are_scored_and_never_rewritten(self):
  value=forecast();self.assertEqual(ledger.record(value,NOW+timedelta(seconds=1),self.path),1)
  value['points'][0]['forecasts'][0]['forest_candidate_score']=99
  self.assertEqual(ledger.record(value,NOW+timedelta(seconds=2),self.path),0)
  self.assertEqual(ledger.score([self.observation(14)],NOW+timedelta(minutes=14),self.path),0)
  self.assertEqual(ledger.score([self.observation(16)],NOW+timedelta(minutes=16),self.path),1)
  self.assertEqual(ledger.score([self.observation(16,speed=5)],NOW+timedelta(minutes=17),self.path),0)
  result=ledger.summary(self.path);self.assertEqual(result['matched'],1)
  self.assertEqual(result['horizons'][0]['candidate_mae_points'],0)
  self.assertEqual(result['horizons'][0]['persistence_mae_points'],10)
  self.assertFalse(result['field_accuracy_validated'])
 def test_hindsight_predictions_and_future_trained_models_are_rejected(self):
  with self.assertRaises(ValueError):ledger.record(forecast(),NOW+timedelta(minutes=16),self.path)
  value=forecast();value['model']['report']['trained_at']=(NOW+timedelta(seconds=1)).isoformat()
  with self.assertRaises(ValueError):ledger.record(value,NOW,self.path)
 def test_stale_late_or_synthetic_targets_do_not_score(self):
  ledger.record(forecast(),NOW,self.path)
  self.assertEqual(ledger.score([self.observation(19),self.observation(16,source='synthetic')],NOW+timedelta(minutes=20),self.path),0)
  self.assertEqual(ledger.summary(self.path)['matched'],0)
 def test_unavailable_evaluation_database_does_not_destroy_forecasts(self):
  self.path.write_text('invalid database')
  self.assertEqual(ledger.summary(self.path)['status'],'evaluation_unavailable')
 def test_environment_path_is_resolved_after_import_for_record_score_and_summary(self):
  other=Path(self.temp.name)/'other.db';default=Path(self.temp.name)/'default-must-not-be-used.db'
  with patch.object(ledger,'PATH',default),patch.dict(os.environ,{'CROSSFLOW_FORECAST_EVALUATION_DB':str(self.path)}):
   self.assertEqual(ledger.record(forecast(),NOW),1)
   self.assertEqual(ledger.score([self.observation(16)],NOW+timedelta(minutes=16)),1)
   self.assertEqual(ledger.summary()['matched'],1)
   with patch.dict(os.environ,{'CROSSFLOW_FORECAST_EVALUATION_DB':str(other)}):
    self.assertEqual(ledger.summary()['status'],'awaiting_predictions')
    self.assertFalse(other.exists())
    self.assertEqual(ledger.record(forecast(),NOW),1)
    self.assertEqual(ledger.summary()['matched'],0)
   self.assertEqual(ledger.summary()['matched'],1)
  self.assertFalse(default.exists())
 def test_explicit_fixture_path_overrides_environment_for_every_operation(self):
  configured=Path(self.temp.name)/'configured.db'
  with patch.dict(os.environ,{'CROSSFLOW_FORECAST_EVALUATION_DB':str(configured)}):
   self.assertEqual(ledger.record(forecast(),NOW),1)
   self.assertEqual(ledger.record(forecast(),NOW,self.path),1)
   self.assertEqual(ledger.score([self.observation(16)],NOW+timedelta(minutes=16),self.path),1)
   self.assertEqual(ledger.summary(self.path)['matched'],1)
   self.assertEqual(ledger.summary()['matched'],0)
 def test_unset_or_blank_environment_uses_default_without_creating_on_read(self):
  with patch.object(ledger,'PATH',self.path):
   for settings in ({},{'CROSSFLOW_FORECAST_EVALUATION_DB':'   '}):
    with patch.dict(os.environ,settings,clear=True):
     self.assertEqual(ledger.database_path(),self.path)
     self.assertEqual(ledger.summary()['status'],'awaiting_predictions')
     self.assertFalse(self.path.exists())


class CandidateAndChronologyTests(unittest.TestCase):
 def test_residual_numeric_artifact_matches_fitted_estimator_after_decoding(self):
  from sklearn.ensemble import RandomForestRegressor
  from services.observed_model_artifact import NumericRandomForest
  from services.real_traffic_model import _trees
  x=np.arange(30,dtype=float).reshape(10,3)
  baseline=np.linspace(5,50,10);actual=baseline*.8
  model=RandomForestRegressor(n_estimators=10,max_depth=3,random_state=42)
  model.fit(x,encode_target(actual,baseline,'shifted_residual'))
  numeric=NumericRandomForest(_trees(model),model.feature_importances_.tolist(),feature_count=3)
  decoder={'target_transform':'shifted_residual','residual_weight':.25}
  np.testing.assert_allclose(decode_prediction(numeric.predict(x),baseline,decoder),decode_prediction(model.predict(x),baseline,decoder),atol=1e-10)
 def test_shifted_residual_roundtrip_retains_negative_changes_and_bounds(self):
  actual=np.array([0.,100.,30.]);baseline=np.array([100.,0.,50.])
  encoded=encode_target(actual,baseline,'shifted_residual')
  self.assertTrue(np.all((encoded>=0)&(encoded<=100)))
  np.testing.assert_allclose(decode_prediction(encoded,baseline,{'target_transform':'shifted_residual','residual_weight':1.}),actual)
 def test_new_benchmark_has_purged_chronological_windows(self):
  grid=np.full((61*144,2),40.,dtype=np.float32);features,_=benchmark.temporal_features(grid)
  parts=[benchmark.examples(grid,features,60,p) for p in ('fit','validation','calibration','test')]
  for previous,later in zip(parts,parts[1:]):self.assertLess(previous['target'].max(),later['origin'].min())
 def test_unavailable_targets_are_excluded_and_future_changes_do_not_change_features(self):
  grid=np.full((200,2),40.,dtype=np.float32);before,_=benchmark.temporal_features(grid)
  grid[150:]=99.;after,_=benchmark.temporal_features(grid)
  np.testing.assert_array_equal(before[:150],after[:150])
 def test_bootstrap_does_not_treat_road_rows_as_independent_days(self):
  a=np.arange(100,dtype=float)
  result=benchmark.day_block_interval(a,a,a+1,np.zeros(100))
  self.assertEqual(result['status'],'insufficient_independent_days');self.assertIsNone(result['ci95'])

if __name__=='__main__':unittest.main()
