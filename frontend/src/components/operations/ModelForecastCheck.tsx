import { useEffect, useId, useRef, useState } from 'react';
import { Activity, Play } from 'lucide-react';
import { requestAnalyticsSimulation, type AnalyticsSimulation } from '../../services/api';
import type { Fetched } from '../../types';
import { forecastClock } from './analyticsChartData';

/** A user-triggered inference run, independent of the model's saved training scores. */
export function ModelForecastCheck({ accessToken }: { accessToken: string | null }) {
  return <ForecastRun key={accessToken ?? 'anonymous'} accessToken={accessToken} />;
}

function ForecastRun({ accessToken }: { accessToken: string | null }) {
  const [weather, setWeather] = useState(0);
  const [run, setRun] = useState<Fetched<AnalyticsSimulation> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestId = useRef(0);
  const gradient = useId();
  useEffect(() => () => { requestId.current += 1; }, []);

  function changeWeather(value: number) {
    requestId.current += 1;
    setWeather(value);
    setRun(null);
    setError(null);
    setLoading(false);
  }

  async function compute() {
    const id = ++requestId.current;
    setLoading(true);
    setError(null);
    setRun(null);
    try {
      const result = await requestAnalyticsSimulation(weather, undefined, accessToken);
      if (id === requestId.current) setRun(result);
    } catch (cause) {
      if (id === requestId.current) setError(cause instanceof Error ? cause.message : 'Forecast could not be computed.');
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  }

  const data = run?.data;
  const points = data?.hourly_forecast ?? [];
  const peak = points.reduce<AnalyticsSimulation['hourly_forecast'][number] | null>((best, point) => !best || point.mean_score > best.mean_score ? point : best, null);
  const line = points.map((point, index) => `${12 + index * 576 / Math.max(1, points.length - 1)},${122 - point.mean_score * 1.1}`).join(' ');
  return <section className="model-forecast-check" aria-label="Live forecast check" aria-busy={loading}>
    <div className="model-forecast-check__header">
      <div><h3><Activity size={20} aria-hidden="true" />Live forecast check</h3><p>Run the serving model with the current time and your weather scenario.</p></div>
      <div className="operations-source-controls operations-forecast-toolbar">
        <label>Weather<select aria-label="Model check weather" value={weather} onChange={event => changeWeather(Number(event.target.value))}>
          <option value={0}>Clear</option><option value={1}>Rain</option><option value={2}>Storm</option>
        </select></label>
        <button type="button" className="ui-button-primary" disabled={loading} onClick={() => void compute()}><Play size={16} aria-hidden="true" />{loading ? 'Running model…' : 'Run live forecast'}</button>
      </div>
    </div>
    {error ? <p role="alert">{error}</p> : null}
    {data && points.length ? <div className="model-forecast-check__result" role="status">
      <div className="model-forecast-check__stats">
        <div><span>Current forecast</span><strong>{points[0].mean_score.toFixed(1)}<small> / 100</small></strong></div>
        <div><span>24-hour peak · {peak ? forecastClock(peak.timestamp) : ''} WIB</span><strong>{peak?.mean_score.toFixed(1)}<small> / 100</small></strong></div>
        <div><span>Predictions computed</span><strong>{data.prediction_count?.toLocaleString() ?? 'Undeclared'}</strong></div>
      </div>
      <svg viewBox="0 0 600 140" role="img" aria-label="Computed 24-hour mean congestion forecast on a zero to one hundred scale">
        <defs><linearGradient id={gradient} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="var(--accent-cyan)" stopOpacity="0.25" /><stop offset="100%" stopColor="var(--accent-cyan)" stopOpacity="0.02" /></linearGradient></defs>
        {[12, 67, 122].map(y => <line key={y} x1="12" x2="588" y1={y} y2={y} stroke="var(--border-color)" strokeDasharray="3 5" />)}
        <polygon points={`12,122 ${line} 588,122`} fill={`url(#${gradient})`} />
        <polyline points={line} fill="none" stroke="var(--accent-cyan)" strokeWidth="2.5" strokeLinejoin="round" />
      </svg>
      <div className="model-forecast-check__caption"><span>{forecastClock(points[0].timestamp)} — {forecastClock(points[points.length - 1].timestamp)} WIB</span>
        {run ? <time dateTime={run.fetchedAt}>Computed {new Date(run.fetchedAt).toLocaleTimeString('en-SG', { timeZone: 'Asia/Singapore' })} SGT</time> : null}
      </div>
      <p>{data.engine ? `${data.engine.algorithm} · ${data.engine.estimator_count} fitted trees · ${data.engine.feature_count} input features` : 'Engine metadata was not supplied.'}
        {' '}· {data.corridors.length} corridors · {['Clear', 'Rain', 'Storm'][data.weather]}</p>
      <p>Modelled output. Training: {data.model.training_data_source?.replace(/_/g, ' ') ?? 'undeclared'}; real-world accuracy still requires observed validation.</p>
    </div> : !loading && !error ? <p className="model-forecast-check__idle">Ready to compute a fresh forecast. Saved scores below change when the trained model changes.</p> : null}
  </section>;
}
