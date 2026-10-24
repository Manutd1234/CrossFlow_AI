import { useEffect, useRef, useState } from 'react';
import { requestObservedBacktest } from '../../services/api';
import type { Fetched, ObservedBacktestMetrics, ObservedBacktestResult } from '../../types';

type View = { token: string | null; loading: boolean; result: Fetched<ObservedBacktestResult> | null; error: string | null };
const label = (value: string) => value.replace(/_/g, ' ');
function time(value: string | null) {
  return value ? new Intl.DateTimeFormat('en-SG', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Singapore' }).format(new Date(value)) + ' SGT' : 'Not available';
}
function metric(metrics: ObservedBacktestMetrics, field: 'mae_points' | 'rmse_points' | 'bias_points') {
  return metrics.samples > 0 && metrics[field] !== null ? metrics[field].toFixed(3) : 'Insufficient samples';
}

export function ObservedBacktest({ accessToken }: { accessToken: string | null }) {
  const [view, setView] = useState<View | null>(null);
  const requestRef = useRef(0);
  const pendingRef = useRef<{ id: number; token: string | null } | null>(null);
  useEffect(() => () => { requestRef.current += 1; pendingRef.current = null; }, [accessToken]);
  const current = view?.token === accessToken ? view : null;
  const result = current?.result;
  const report = result?.data.backtest;
  const snapshot = result?.data.evaluation_snapshot;
  const run = async () => {
    if (pendingRef.current?.token === accessToken) return;
    const id = ++requestRef.current;
    pendingRef.current = { id, token: accessToken };
    setView({ token: accessToken, loading: true, result: current?.result ?? null, error: null });
    try {
      const response = await requestObservedBacktest(accessToken);
      if (id === requestRef.current) setView({ token: accessToken, loading: false, result: response, error: null });
    } catch (error) {
      if (id === requestRef.current) setView({ token: accessToken, loading: false, result: current?.result ?? null,
        error: error instanceof Error ? error.message : 'The observed-data backtest could not be completed.' });
    } finally {
      if (pendingRef.current?.id === id) pendingRef.current = null;
    }
  };
  return <section className="operations-model-backtest" aria-label="Observed-data chronological backtest" aria-busy={current?.loading ?? false}>
    <div className="operations-source-controls"><h4>Observed-data chronological backtest</h4>
      <button type="button" className="glass-button" disabled={current?.loading} onClick={() => void run()}>
        {current?.loading ? 'Evaluating observed history…' : 'Run observed backtest'}
      </button></div>
    <p>Evaluate a private candidate using real speed observations, chronological partitions and prior-only baselines. Synthetic rows are excluded. This evaluates the candidate, not the currently running forest.</p>
    {current?.error ? <p role="alert">{current.error}{report ? ' The previous report is retained and may be stale.' : ''}</p> : null}
    {!report ? <p role="status">{current?.loading ? 'Preparing a bounded observed-data evaluation…' : 'No observed backtest has been requested in this workspace. Accuracy is not inferred from the synthetic model’s holdout.'}</p> : <>
      <p role="status"><strong>{report.status === 'insufficient_observations' ? 'Insufficient observed history'
        : report.status === 'activated' ? 'Observed candidate activated'
          : report.status === 'eligible' ? 'Observed candidate eligible for activation' : 'Observed candidate evaluated'}</strong>.
        {' '}Generated <time dateTime={report.generated_at}>{time(report.generated_at)}</time>.
        {' '}{report.model_replaced ? 'The server reports that this candidate replaced the running model; refresh model status to inspect its provenance.' : 'The running model was not replaced.'}</p>
      <dl className="operations-backtest-coverage">
        <div><dt>Eligible observed rows</dt><dd>{report.selection.eligible_rows.toLocaleString()}</dd></div>
        <div><dt>Selected original rows</dt><dd>{report.selection.selected_rows.toLocaleString()}</dd></div>
        <div><dt>Observed archive span</dt><dd>{report.selection.span_hours.toFixed(2)} hours</dd></div>
      </dl>
      <p>Archive window: <time dateTime={report.selection.first_at ?? undefined}>{time(report.selection.first_at)}</time> to <time dateTime={report.selection.last_at ?? undefined}>{time(report.selection.last_at)}</time>.
        {' '}{report.selection.distinct_timestamps.toLocaleString()} distinct timestamps; {report.selection.excluded_non_observed_rows.toLocaleString()} non-observed rows excluded.</p>
      {typeof report.selection.distinct_local_dates === 'number' ? <p>Temporal coverage: {report.selection.distinct_local_dates} distinct local dates.
        {report.selection.observed_hour_bins ? ` Measurements cover ${new Set(report.selection.observed_hour_bins).size} of 24 local hour bins.` : ''}</p> : null}
      {report.activation.reasons.length ? <div role="note"><strong>Activation requirements</strong><ul>{report.activation.reasons.map(reason => <li key={reason}>{label(reason)}</li>)}</ul></div> : null}
      <div className="operations-observed-table"><table><caption>Chronological data partitions</caption><thead><tr><th>Partition</th><th>Rows</th><th>First observation</th><th>Last observation</th></tr></thead><tbody>
        {(['fit', 'calibration', 'holdout'] as const).map(name => <tr key={name}><th>{name === 'fit' ? 'Fit' : name === 'calibration' ? 'Calibration' : 'Holdout'}</th><td>{report.chronology[name].rows.toLocaleString()}</td><td>{time(report.chronology[name].start_at)}</td><td>{time(report.chronology[name].end_at)}</td></tr>)}
      </tbody></table></div>
      <p>Equal timestamps stay in the same partition. Purge gap: {report.chronology.purge_minutes} minutes. Future target speeds are excluded from prediction inputs.</p>
      {report.interval_calibration ? <p>Interval calibration: {report.interval_calibration.calibration_rows.toLocaleString()} distinct rows and {report.interval_calibration.calibration_forecast_pairs.toLocaleString()} prior-origin forecast pairs.
        {' '}Target coverage: {(report.interval_calibration.target_coverage * 100).toFixed(0)}%; interval widening: {report.interval_calibration.padding_points.toFixed(3)} points. Coverage is not guaranteed.</p> : null}
      {report.horizons.length ? <>
        <div className="operations-observed-table"><table><caption>Forward evaluation on observed targets — score points</caption><thead><tr><th>Horizon</th><th>Candidate samples</th><th>Observed candidate MAE</th><th>Persistence MAE</th><th>Current/history blend MAE</th><th>P10–P90 coverage</th></tr></thead><tbody>
          {report.horizons.map(row => <tr key={row.horizon_minutes}><th>{row.horizon_minutes} min</th><td>{row.serving_model.samples}</td><td>{metric(row.serving_model, 'mae_points')}</td><td>{metric(row.persistence, 'mae_points')}</td><td>{metric(row.current_history_blend, 'mae_points')}</td><td>{row.p10_p90_coverage === null ? 'Insufficient samples' : `${(row.p10_p90_coverage * 100).toFixed(1)}%`}</td></tr>)}
        </tbody></table></div>
        <details><summary>RMSE, bias and evaluation sample counts</summary><div className="operations-observed-table"><table><thead><tr><th>Horizon / method</th><th>Samples</th><th>RMSE</th><th>Bias</th></tr></thead><tbody>
          {report.horizons.flatMap(row => ([['Observed candidate', row.serving_model], ['Persistence', row.persistence], ['Current/history blend', row.current_history_blend]] as const).map(([name, values]) => <tr key={`${row.horizon_minutes}-${name}`}><th>{row.horizon_minutes} min / {name}</th><td>{values.samples}</td><td>{metric(values, 'rmse_points')}</td><td>{metric(values, 'bias_points')}</td></tr>))}
        </tbody></table></div></details>
        {report.horizons.some(row => row.per_corridor) ? <details><summary>Observed corridor evaluation coverage</summary><div className="operations-observed-table"><table><thead><tr><th>Horizon / corridor</th><th>Candidate samples</th><th>Candidate MAE</th><th>Persistence MAE</th></tr></thead><tbody>
          {report.horizons.flatMap(row => Object.entries(row.per_corridor ?? {}).map(([corridor, values]) => <tr key={`${row.horizon_minutes}-${corridor}`}><th>{row.horizon_minutes} min / {label(corridor)}</th><td>{values.serving_model.samples}</td><td>{metric(values.serving_model, 'mae_points')}</td><td>{metric(values.persistence, 'mae_points')}</td></tr>))}
        </tbody></table></div></details> : null}
        <p>Coverage is empirical on these observed targets. It is not an accuracy guarantee for other roads, seasons or weather.</p>
      </> : <p>No forecast-error or interval-coverage scores are available for this archive. Collect enough distinct observations across the required time span and retry.</p>}
      <details><summary>Data selection and activation policy</summary>
        <p>Read {report.selection.input_rows.toLocaleString()} archive rows, {report.selection.unique_rows.toLocaleString()} unique; {report.selection.duplicate_rows.toLocaleString()} duplicates and {report.selection.excluded_unsupported_corridor_rows.toLocaleString()} unsupported-corridor rows excluded.</p>
        <p>Sampling: {label(report.selection.sampling_policy)}. Original observation times and speeds are retained.</p>
        <p>Observed sources: {Object.entries(report.selection.source_counts).map(([source, count]) => `${label(source)}: ${count.toLocaleString()}`).join(' · ') || 'None'}.</p>
        {snapshot && typeof snapshot.durability === 'string' ? <p>Evaluation storage: {label(snapshot.durability)}.
          {typeof snapshot.bounded_max_rows === 'number' ? ` Snapshot reads at most ${snapshot.bounded_max_rows.toLocaleString()} archive rows; the reported window describes the loaded observations.` : ''}
          {typeof snapshot.cache_ttl_seconds === 'number' ? ` Results may be reused for ${snapshot.cache_ttl_seconds} seconds.` : ''}</p> : null}
        <dl>{Object.entries(report.activation.policy).map(([key, value]) => <div key={key}><dt>{label(key)}</dt><dd>{Array.isArray(value) ? value.join(', ') : typeof value === 'string' ? label(value)
          : ['min_mae_improvement_fraction', 'min_interval_coverage', 'interval_target'].includes(key) ? `${(value * 100).toFixed(0)}%` : value.toLocaleString()}</dd></div>)}</dl>
      </details>
      <div role="note"><strong>Evaluation assumptions and limits</strong><ul>{report.assumptions.map(assumption => <li key={assumption}>{assumption}</li>)}</ul></div>
    </>}
  </section>;
}
