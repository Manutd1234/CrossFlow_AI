import React, { useEffect, useMemo, useState } from 'react';
import {
  Activity, Brain, Database, ExternalLink, RefreshCw, Sparkles, TriangleAlert, Zap,
} from 'lucide-react';

import type { ModelMetricsWithProvenance } from '../../data/modelManifest';
import { fetchModelStatus } from '../../services/api';
import { ICON_SIZE } from '../../theme/iconSizes';
import { DataSource, ModelMetrics } from '../../types';
import { ModelForecastCheck } from './ModelForecastCheck';

const MODEL_STATUS_POLL_MS = 60_000;

function formatModelTimestamp(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat('en-SG', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'Asia/Singapore',
  }).format(parsed);
}

const VALIDATION_SCOPE_BY_SOURCE: Record<string, readonly string[]> = {
  synthetic_profile_generator: ['synthetic_holdout'],
  history_store_observed: ['history_holdout_observed'],
  history_store_mixed: ['history_holdout_mixed'],
  history_store_non_observed: ['history_holdout_non_observed'],
  validated_spatial_observed: ['chronological_spatial_holdout_observed', 'purged_chronological_observed_holdout'],
  validated_spatial_mixed: ['chronological_spatial_holdout_mixed'],
  validated_spatial_modelled: ['chronological_spatial_holdout_modelled'],
};

export const AIModelPanel: React.FC<{ accessToken?: string | null }> = ({ accessToken = null }) => {
  const [metrics, setMetrics] = useState<ModelMetrics | null>(null);
  const [loading, setLoading] = useState(true);
  const [source, setSource] = useState<DataSource>('offline');
  const [error, setError] = useState<string | null>(null);
  const [refreshRevision, setRefreshRevision] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;

    const loadMetrics = async () => {
      if (inFlight) return;
      inFlight = true;
      setLoading(true);
      try {
        const res = await fetchModelStatus();
        if (cancelled) return;
        setMetrics(res.data);
        setSource(res.source);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Model telemetry could not be loaded.');
      } finally {
        inFlight = false;
        if (!cancelled) setLoading(false);
      }
    };

    const refreshWhenVisible = () => {
      if (document.visibilityState !== 'hidden') void loadMetrics();
    };
    void loadMetrics();
    const timer = window.setInterval(refreshWhenVisible, MODEL_STATUS_POLL_MS);
    window.addEventListener('focus', refreshWhenVisible);
    document.addEventListener('visibilitychange', refreshWhenVisible);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      window.removeEventListener('focus', refreshWhenVisible);
      document.removeEventListener('visibilitychange', refreshWhenVisible);
    };
  }, [refreshRevision]);

  const featureEntries = useMemo(
    () => Object.entries(source === 'offline' ? {} : metrics?.feature_importances ?? {}).sort((a, b) => b[1] - a[1]),
    [metrics?.feature_importances, source],
  );

  if (!metrics && loading) {
    return (
      <section
        className="glass-panel operations-model-loading"
        role="status"
        aria-live="polite"
        aria-busy="true"
      >
        <RefreshCw className="operations-spinner" aria-hidden="true" size={ICON_SIZE.large} />
        <div>
          <strong className="operations-model-loading__title">
            Connecting to model telemetry
          </strong>
          <span className="operations-model-loading__description">Loading training metrics and feature weights…</span>
        </div>
      </section>
    );
  }

  const r2Pct = metrics ? Math.round(metrics.r2_score * 100) : 0;
  const modelReady = source !== 'offline' && Boolean(metrics?.is_trained);
  const declaredMetrics = metrics as ModelMetricsWithProvenance | null;
  const trainingDataSource: string | undefined = declaredMetrics?.training_data_source;
  const validationScope = declaredMetrics?.validation_scope;
  const trainingSourceCounts = declaredMetrics?.training_source_counts;
  const observedTrainingRows = declaredMetrics?.observed_training_rows ?? 0;
  const provenanceDeclared = Boolean(trainingDataSource && validationScope
    && VALIDATION_SCOPE_BY_SOURCE[trainingDataSource]?.includes(validationScope));
  const isSyntheticValidation = provenanceDeclared && trainingDataSource === 'synthetic_profile_generator';
  const isHistoryValidation = provenanceDeclared && Boolean(
    trainingDataSource?.startsWith('history_store_') || trainingDataSource?.startsWith('validated_spatial_'),
  );
  const isObservedHistory = provenanceDeclared && (trainingDataSource === 'history_store_observed'
    || trainingDataSource === 'validated_spatial_observed');
  const isPurgedObservedHistory = isObservedHistory && validationScope === 'purged_chronological_observed_holdout';
  const isMixedHistory = provenanceDeclared && (trainingDataSource === 'history_store_mixed'
    || trainingDataSource === 'validated_spatial_mixed');
  const isNonObservedHistory = provenanceDeclared && (trainingDataSource === 'history_store_non_observed'
    || trainingDataSource === 'validated_spatial_modelled');
  const calibration = declaredMetrics?.calibration;
  const coverage = calibration?.interval_coverage_observed;
  const coverageTarget = calibration?.interval_coverage_target;
  const hasCalibration = modelReady && provenanceDeclared
    && typeof coverage === 'number' && Number.isFinite(coverage) && coverage >= 0 && coverage <= 1
    && typeof coverageTarget === 'number' && Number.isFinite(coverageTarget)
    && coverageTarget >= 0 && coverageTarget <= 1;

  const validationLabel = isSyntheticValidation
    ? 'Synthetic holdout'
    : isObservedHistory
      ? isPurgedObservedHistory ? 'Purged observed holdout' : 'Observed-history holdout'
      : isMixedHistory
        ? 'Mixed-history holdout'
        : isNonObservedHistory
          ? 'Non-observed history holdout'
          : 'Provenance-undeclared holdout';

  const readinessLabel = !modelReady
    ? 'MODEL STATUS UNAVAILABLE'
    : isSyntheticValidation
        ? 'BACKEND RF · SYNTHETIC'
        : isObservedHistory
          ? 'BACKEND RF · OBSERVED HISTORY'
          : isMixedHistory
            ? 'BACKEND RF · MIXED HISTORY'
            : isNonObservedHistory
              ? 'BACKEND RF · NON-OBSERVED HISTORY'
              : 'BACKEND RF · PROVENANCE UNDECLARED';

  const trainingSourceSummary = Object.entries(trainingSourceCounts ?? {})
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([name, count]) => `${name.replace(/_/g, ' ')}: ${count.toLocaleString()}`)
    .join(' · ');

  return (
    <section
      className="glass-panel operations-model-panel"
      aria-labelledby="model-panel-title"
      aria-busy={loading}
    >
      <div className="operations-model-panel__header">
        <div className="operations-model-panel__intro">
          <div className="operations-model-panel__title-row">
            <Brain aria-hidden="true" size={ICON_SIZE.large} color="var(--accent-indigo)" />
            <h3 id="model-panel-title" className="operations-model-panel__title">
              Random Forest Engine
            </h3>
          </div>
        </div>

        <div className="operations-model-panel__badges">
          <button
            type="button"
            className="glass-button"
            disabled={loading}
            onClick={() => setRefreshRevision(value => value + 1)}
          >
            <RefreshCw aria-hidden="true" size={ICON_SIZE.small} />
            {loading ? 'Checking model…' : 'Refresh model status'}
          </button>
          <span
            className={`badge operations-model-panel__status ${modelReady && provenanceDeclared ? 'badge-smooth' : 'badge-neutral'}`}
          >
            {readinessLabel}
          </span>
          <span className="badge badge-neutral">
            {source === 'offline' ? 'MODEL API UNAVAILABLE' : 'API MODEL STATUS'}
          </span>
        </div>
      </div>

      {error ? (
        <div
          role="alert"
          className="operations-inline-alert"
        >
          <TriangleAlert aria-hidden="true" size={ICON_SIZE.big} /> {error}
        </div>
      ) : null}

      <ModelForecastCheck key={accessToken ?? 'anonymous-model-check'} accessToken={accessToken} />

      {modelReady ? <div className="operations-model-run" aria-label="Saved training run">
        <strong><Database size={16} aria-hidden="true" />Saved training results</strong>
        {metrics?.last_trained_at ? <span>Trained <time dateTime={metrics.last_trained_at}>{formatModelTimestamp(metrics.last_trained_at)} SGT</time></span> : null}
        <p>MAE and dataset size belong to this model version. Live traffic updates separately; refreshing does not retrain the model.</p>
      </div> : null}

      <div className="operations-model-metrics">
        <article className="operations-model-metric">
          <div className="operations-model-metric__label">
            <Zap aria-hidden="true" size={ICON_SIZE.big} color="var(--accent-emerald)" /> {validationLabel} R²
          </div>
          <div className="operations-model-metric__value operations-model-metric__value--emerald">
            {modelReady ? metrics?.r2_score.toFixed(4) : 'Unavailable'}
          </div>
          <div className="operations-model-metric__detail">
            {modelReady ? `${r2Pct}% variance explained in ${validationLabel.toLowerCase()}` : 'No backend metric loaded'}
          </div>
        </article>

        <article className="operations-model-metric">
          <div className="operations-model-metric__label">
            <Activity aria-hidden="true" size={ICON_SIZE.big} color="var(--accent-cyan)" /> Mean Absolute Error (MAE)
          </div>
          <div className="operations-model-metric__value operations-model-metric__value--cyan">
            {modelReady ? metrics?.mae.toFixed(2) : 'Unavailable'}{' '}
            <span className="operations-model-metric__unit">score pts</span>
          </div>
          <div className="operations-model-metric__detail">
            {modelReady ? `${validationLabel} · ` : ''}RMSE: {modelReady ? metrics?.rmse.toFixed(2) : 'Unavailable'}
          </div>
        </article>

        <article className="operations-model-metric">
          <div className="operations-model-metric__label">
            <Database aria-hidden="true" size={ICON_SIZE.big} color="var(--accent-indigo)" /> Model Dataset Size
          </div>
          <div className="operations-model-metric__value operations-model-metric__value--indigo">
            {modelReady ? metrics?.total_samples.toLocaleString() : 'Unavailable'}{' '}
            <span className="operations-model-metric__unit">rows</span>
          </div>
          <div className="operations-model-metric__detail">
            {!modelReady
              ? 'No backend training-size metric loaded'
              : isSyntheticValidation
                ? 'Synthetic baseline rows generated for this saved model'
                : isPurgedObservedHistory
                  ? `${observedTrainingRows.toLocaleString()} observed rows for fit/calibration; ${declaredMetrics?.observed_evaluation_rows?.toLocaleString() ?? 'unreported'} evaluation forecast pairs`
                  : isHistoryValidation
                  ? `${observedTrainingRows.toLocaleString()} rows tagged observed`
                  : 'Backend row provenance was not declared'}
          </div>
        </article>
      </div>

      {featureEntries.length > 0 ? (
        <div aria-labelledby="feature-importance-title">
          <h4 id="feature-importance-title" className="operations-feature-importance__title">
            <Sparkles aria-hidden="true" size={ICON_SIZE.medium} color="var(--accent-cyan)" /> Model Feature Importance Weights
          </h4>
          <div className="operations-feature-importance__list">
            {featureEntries.map(([feature, value]) => {
              const percent = Math.round(value * 1000) / 10;
              return (
                <div key={feature} className="operations-feature-importance__item">
                  <div className="operations-feature-importance__heading">
                    <span className="operations-feature-importance__name">{feature}</span>
                    <strong className="operations-feature-importance__value">{percent}%</strong>
                  </div>
                  <div
                    role="progressbar"
                    aria-label={`${feature} importance`}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={percent}
                    className="ui-progress-track"
                  >
                    <div className="ui-progress-fill" style={{ width: `${percent}%` }} />
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ) : (
        <div className="operations-feature-importance__empty">
          Model API feature weights are unavailable.
        </div>
      )}

      {hasCalibration ? (
        <p className="operations-model-panel__provenance" role="note">
          P10–P90 ensemble interval coverage: {(coverage * 100).toFixed(2)}% on the declared holdout
          {' '}(target {(coverageTarget * 100).toFixed(0)}%).
          {typeof calibration?.empirical_crps === 'number' && Number.isFinite(calibration.empirical_crps)
            ? ` Empirical ensemble CRPS: ${calibration.empirical_crps.toFixed(3)}.` : ''}
          {typeof calibration?.calibration_rows === 'number' ? ` Calibration: ${calibration.calibration_rows.toLocaleString()} distinct rows.` : ''}
          {typeof calibration?.calibration_forecast_pairs === 'number' ? ` ${calibration.calibration_forecast_pairs.toLocaleString()} calibration forecast pairs.` : ''}
          {typeof calibration?.holdout_samples === 'number' ? ` Holdout: ${calibration.holdout_samples.toLocaleString()} ${isObservedHistory ? 'observed ' : ''}targets.` : ''}
          {calibration?.coverage_guaranteed === false ? ' Temporal dependence prevents a coverage guarantee.' : ''}
        </p>
      ) : null}

      <details className="operations-model-panel__provenance operations-disclosure">
        <summary>Training evidence and accuracy scope</summary>
        <div role="note">
        {!modelReady ? (
          <>Current model telemetry is unavailable. Validation scores and feature weights require a response from the model API.</>
        ) : isSyntheticValidation ? (
          <>These metrics measure fit to the deterministic synthetic profile generator; they are not real-world Batam accuracy claims.</>
        ) : isPurgedObservedHistory ? (
          <>The dataset contains observed speed measurements only. Fit rows: {declaredMetrics?.observed_fit_rows?.toLocaleString() ?? 'unreported'}; calibration rows: {declaredMetrics?.observed_calibration_rows?.toLocaleString() ?? 'unreported'}; final evaluation forecast pairs: {declaredMetrics?.observed_evaluation_rows?.toLocaleString() ?? 'unreported'}.
            {typeof declaredMetrics?.observed_evaluation_horizon_minutes === 'number' ? ` R², MAE and RMSE use ${declaredMetrics.observed_evaluation_horizon_minutes}-minute observed targets.` : ' The top-level score horizon was not declared.'}
            {' '}The holdout follows fitting and calibration with a purge gap; these scores are not an official Batam accuracy certification.</>
        ) : isHistoryValidation ? (
          <>These metrics measure a chronological holdout from the stored history dataset. {observedTrainingRows.toLocaleString()} of {metrics?.total_samples.toLocaleString()} rows are tagged observed; the score is not an official Batam accuracy certification.</>
        ) : (
          <>The backend did not declare training-data provenance, so these metrics must be treated as unverified rather than assumed synthetic or observed.</>
        )}
        {modelReady && metrics?.evaluation_scope === 'raw_forest_holdout_excludes_post_model_adjustment' ? (
          <> Holdout scores evaluate the raw forest before the published-timetable adjustment used when serving route forecasts.</>
        ) : null}
        {modelReady && metrics?.evaluation_scope === 'observed_serving_pipeline_weather_clear_ferry_unknown' ? (
          <> The observed evaluation uses clear-weather encoding and unknown ferry proximity because the measurements do not record those inputs. Weather response and the external timetable adjustment are not validated by these observations.</>
        ) : null}
        {modelReady && metrics?.feature_importance_scope === 'raw_forest_impurity_importance' ? (
          <> Feature weights describe splits in the fitted training forest; they do not measure the separate timetable adjustment.</>
        ) : null}
        {modelReady && trainingSourceSummary ? <> Training source counts: {trainingSourceSummary}.</> : null}
        {modelReady && metrics?.last_trained_at ? (
          <> Trained <time dateTime={metrics.last_trained_at}>{formatModelTimestamp(metrics.last_trained_at)} SGT</time>.</>
        ) : null}
        {' '}
        <a
          href="https://dishub.batam.go.id/wp-content/uploads/sites/3/2025/02/DISHUB_LAKIP_2024.pdf"
          target="_blank"
          rel="noreferrer"
          className="operations-model-panel__source-link"
        >
          Official ATCS inventory <ExternalLink aria-hidden="true" size={ICON_SIZE.small} />
        </a>
        {!provenanceDeclared && source !== 'offline' ? ' · provenance manifest required' : null}
        </div>
      </details>
      {modelReady && isPurgedObservedHistory && declaredMetrics?.observed_training ? (
        <details className="operations-model-panel__provenance">
          <summary>Serving model’s recorded training window</summary>
          <p>Selected observed archive span: {declaredMetrics.observed_training.selection.span_hours.toFixed(2)} hours.
            {' '}The fitting, interval calibration and final evaluation partitions are separate.</p>
          <div className="operations-observed-table"><table><thead><tr><th>Partition</th><th>Rows</th><th>First observation (SGT)</th><th>Last observation (SGT)</th></tr></thead><tbody>
            {(['fit', 'calibration', 'holdout'] as const).map(name => {
              const partition = declaredMetrics.observed_training!.chronology[name];
              return <tr key={name}><th>{name === 'fit' ? 'Fit' : name === 'calibration' ? 'Calibration' : 'Final holdout'}</th><td>{partition.rows}</td>
                <td>{partition.start_at ? <time dateTime={partition.start_at}>{formatModelTimestamp(partition.start_at)}</time> : 'Unavailable'}</td>
                <td>{partition.end_at ? <time dateTime={partition.end_at}>{formatModelTimestamp(partition.end_at)}</time> : 'Unavailable'}</td></tr>;
            })}
          </tbody></table></div>
          <p>Purge gap: {declaredMetrics.observed_training.chronology.purge_minutes} minutes. Equal observation timestamps remain together.</p>
        </details>
      ) : null}
    </section>
  );
};
