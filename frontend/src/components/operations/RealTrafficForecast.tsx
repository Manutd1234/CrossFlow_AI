import { useCallback, useId } from 'react';
import { Activity, Brain, CloudSun, Database, MapPinned, RefreshCw } from 'lucide-react';
import { fetchRealTrafficForecast, type RealTrafficForecast as TrafficForecast } from '../../services/api';
import { useCheckedSource } from '../../hooks/useCheckedSource';
import { shortCorridorName } from '../../utils/format';
import './RealTrafficForecast.css';

function localTime(value: string) {
  return new Date(value).toLocaleString('en-SG', { timeZone: 'Asia/Singapore', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}
function weatherName(code: number) {
  if (code >= 95) return 'Thunderstorm';
  if (code >= 51 && code <= 82) return 'Rain';
  return ['Clear', 'Mostly clear', 'Partly cloudy', 'Overcast'][code] ?? 'Cloudy';
}

function servingStrategy(data: TrafficForecast, index: number) {
  const declared = data.network[index].strategy;
  if (declared) return declared;
  const strategies = new Set(data.points.map(point => point.forecasts[index].strategy));
  return strategies.size === 1 ? [...strategies][0] : 'mixed';
}
function forecastLabel(strategy: string) {
  return strategy === 'persistence' ? 'Latest-observation baseline' : strategy === 'random_forest' ? 'ML forecast' : 'Mixed forecast';
}

export function RealTrafficForecast({ accessToken, showTraining = false }: { accessToken: string | null; showTraining?: boolean }) {
  return <RealForecastWorkspace key={accessToken ?? 'anonymous'} accessToken={accessToken} showTraining={showTraining} />;
}

function RealForecastWorkspace({ accessToken, showTraining }: { accessToken: string | null; showTraining: boolean }) {
  const request = useCallback(() => fetchRealTrafficForecast(accessToken), [accessToken]);
  const { snapshot: data, loading, error, refresh } = useCheckedSource(request);
  const gradient = useId();
  const report = data?.model.report;
  const stale = Boolean(error && data);
  const strategies = data?.network.map((_, index) => servingStrategy(data, index)) ?? [];
  const baseline = strategies.length > 0 && strategies.every(strategy => strategy === 'persistence');
  const latestObservedAt = data?.points.reduce((latest, point) => Date.parse(point.observed_at) > Date.parse(latest) ? point.observed_at : latest, data.points[0].observed_at);
  const current = data?.points.reduce((sum, p) => sum + p.current_index, 0) ?? 0;
  const currentMean = data ? current / data.points.length : 0;
  const plot = data ? [{ minutes: 0, value: currentMean }, ...data.network.map(p => ({ minutes: p.horizon_minutes, value: p.mean_score }))] : [];
  const coordinates = plot.map(p => `${38 + p.minutes * 8.4},${150 - p.value * 1.2}`).join(' ');

  return <section className="real-traffic-workspace" aria-label={showTraining ? 'Observed traffic engine' : 'Real traffic forecasts'} aria-busy={loading}>
    <div className="glass-panel real-traffic-heading">
      <div><h3>{showTraining ? <Brain size={22} aria-hidden="true" /> : <Activity size={22} aria-hidden="true" />}{showTraining ? 'Observed traffic engine' : 'Live traffic forecasts'}</h3>
        <p>Real traffic inputs · 15, 30 and 60 minutes ahead</p></div>
      <div className="real-traffic-actions"><span className="badge badge-neutral">{stale ? 'Saved forecast · stale' : report ? baseline ? 'Baseline · provisional' : report.quality_gate.eligible ? 'Observed data' : 'Real data · provisional' : loading ? 'Connecting' : 'Unavailable'}</span>
        <button type="button" className="glass-button" disabled={loading} onClick={refresh}><RefreshCw size={16} aria-hidden="true" />{loading ? 'Checking forecasts…' : 'Refresh forecast'}</button></div>
    </div>
    {data ? <p className="real-traffic-timestamp">{stale ? 'Previous snapshot' : 'Forecast snapshot'} <time dateTime={data.generated_at}>{localTime(data.generated_at)} SGT</time> · {data.coverage.monitored_points} of {data.coverage.expected_points} configured sampling points available{latestObservedAt ? <> · Latest measurement <time dateTime={latestObservedAt}>{localTime(latestObservedAt)} SGT</time></> : null}</p> : null}
    {error ? <div className="glass-panel real-traffic-error" role="alert">{error}{data ? ' The previous snapshot is stale.' : ''}</div> : null}
    {!data ? <p role="status">{loading ? 'Loading measured traffic, weather and model evidence…' : 'Forecasts are unavailable. Check your account access or refresh to retry.'}</p> : <>
      <div className="real-traffic-kpis">
        {(showTraining ? [
          ['Training archive', report!.raw_observed_rows.toLocaleString(), 'No synthetic training rows'],
          ['Recorded coverage', `${report!.span_hours.toFixed(1)} hours`, `${report!.distinct_local_days} calendar days`],
          ['Monitored roads', `${data.coverage.monitored_points} of ${data.coverage.expected_points} points`, 'Measured representative road points'],
          ['Forecast horizons', '15 · 30 · 60 min', baseline ? 'Latest-observation baseline' : 'Traffic outlook'],
        ] : [
          ['Current mean', `${currentMean.toFixed(1)} / 100`, `${data.points.length} measured road points`],
          ['In 15 minutes', `${data.network[0].mean_score.toFixed(1)} / 100`, forecastLabel(strategies[0])],
          ['In 30 minutes', `${data.network[1].mean_score.toFixed(1)} / 100`, forecastLabel(strategies[1])],
          ['In 60 minutes', `${data.network[2].mean_score.toFixed(1)} / 100`, forecastLabel(strategies[2])],
        ]).map(([label, value, detail]) => <article key={label} className="glass-panel real-traffic-kpi"><span>{label}</span><strong>{value}</strong><small>{detail}</small></article>)}
      </div>
      <div className="glass-panel real-weather-card">
        <CloudSun size={26} aria-hidden="true" /><div><h3>Batam weather <span className="badge badge-neutral">{data.weather.status}</span></h3>
          {data.weather.current ? <p><strong>{weatherName(data.weather.current.weather_code)} · {data.weather.current.temperature_c.toFixed(1)}°C</strong> · {data.weather.current.precipitation_mm} mm rain · {data.weather.current.wind_kph} km/h wind</p> : <p>Weather unavailable.</p>}
          <small><a href={data.weather.source_url} target="_blank" rel="noreferrer">Open-Meteo</a> · regional model grid{data.weather.current ? <> · valid <time dateTime={data.weather.current.valid_at}>{localTime(data.weather.current.valid_at)} SGT</time></> : null}</small></div>
      </div>
      <div className="real-traffic-chart-grid">
        <figure className="glass-panel real-traffic-chart"><figcaption><h3>The next hour</h3><span>{data.points.length} monitored points · mean index / 100</span></figcaption>
          <svg viewBox="0 0 580 190" role="img" aria-label="Observed current mean and computed forecasts at fifteen, thirty and sixty minutes">
            <defs><linearGradient id={gradient} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="var(--lagoon)" stopOpacity="0.25" /><stop offset="100%" stopColor="var(--lagoon)" stopOpacity="0.03" /></linearGradient></defs>
            {[0, 50, 100].map(value => <g key={value}><line x1="38" x2="542" y1={150 - value * 1.2} y2={150 - value * 1.2} stroke="var(--border-color)" strokeDasharray="3 5" /><text x="4" y={154 - value * 1.2}>{value}</text></g>)}
            <polygon points={`38,150 ${coordinates} 542,150`} fill={`url(#${gradient})`} /><polyline points={coordinates} fill="none" stroke="var(--lagoon-text)" strokeWidth="3" />
            {plot.map(p => <g key={p.minutes}><circle cx={38 + p.minutes * 8.4} cy={150 - p.value * 1.2} r="4" fill="var(--lagoon-text)" /><text x={38 + p.minutes * 8.4} y="178" textAnchor="middle">{p.minutes === 0 ? 'Now' : `+${p.minutes}m`}</text></g>)}
          </svg>
          <p>{baseline ? 'Latest-observation baseline · provisional' : report!.quality_gate.eligible ? 'Forecasts available for measured road points.' : 'Forecasts are provisional while measured coverage accumulates.'}</p>
        </figure>
        <figure className="glass-panel real-traffic-chart"><figcaption><h3>Road points · now and +60m</h3><span>Latest observed versus forecast</span></figcaption>
          <div className="real-road-bars">{data.points.map(point => <div key={point.corridor_id}><strong>{point.road.road_name ?? shortCorridorName(point.corridor_id)}</strong>
            <div className="real-road-bars__track"><span style={{ width: `${point.current_index}%`, visibility: point.current_index === 0 ? 'hidden' : 'visible' }} /><i style={{ width: `${point.forecasts[2].mean_score}%`, visibility: point.forecasts[2].mean_score === 0 ? 'hidden' : 'visible' }} /></div>
            <span>{point.current_index.toFixed(1)} → {point.forecasts[2].mean_score.toFixed(1)}</span></div>)}</div>
          <p>Indices cover {data.points.length} monitored points, rather than every road in Batam.</p>
        </figure>
      </div>
      <section className="glass-panel real-road-features"><h3><MapPinned size={20} aria-hidden="true" />Road coverage</h3>
        <div className="operations-observed-table"><table><thead><tr><th>Road</th><th>Class</th><th>Match</th><th>Measured free flow</th><th>Mapped lanes</th><th>Posted limit</th></tr></thead><tbody>
          {data.points.map(point => <tr key={point.corridor_id}><th>{point.road.road_name ?? point.corridor_id}</th><td>{point.road.road_class ?? 'Unmatched'}</td><td>{point.road.match_distance_m.toFixed(1)} m</td><td>{point.free_flow_speed_kph} km/h</td><td>{point.road.lanes ?? 'Not tagged'}</td><td>{point.road.posted_maxspeed_kph ? `${point.road.posted_maxspeed_kph} km/h` : 'Not tagged'}</td></tr>)}
        </tbody></table></div><p>Nearby OpenStreetMap road context · TomTom speed references. The provider’s exact direction is unverified; missing tags and capacity stay unknown.</p>
      </section>
      {showTraining ? <section className="glass-panel real-training-evidence"><h3><Database size={20} aria-hidden="true" />Chronological development results</h3>
        <p>Updated <time dateTime={report!.trained_at}>{localTime(report!.trained_at)} SGT</time></p>
        <div className="operations-observed-table"><table><thead><tr><th>Horizon</th><th>Mean error</th><th>Test measurements</th></tr></thead><tbody>
          {report!.horizons.map(h => <tr key={h.horizon_minutes}><th>{h.horizon_minutes} min</th><td>{h.test.mae_points.toFixed(2)} points</td><td>{h.test.samples}</td></tr>)}
        </tbody></table></div>
        <p>MAE is in congestion-index points. These previously reviewed windows are development evidence; future observations provide the confirmation test.</p>
      </section> : null}
      {showTraining && data.prospective_evaluation ? <section className="glass-panel real-training-evidence" aria-label="Prospective Batam evaluation">
        <h3>Testing against future traffic</h3>
        <p><strong>{data.prospective_evaluation.issued.toLocaleString()} forecasts saved</strong> · {data.prospective_evaluation.matched.toLocaleString()} matched to later measurements · {data.prospective_evaluation.matched_days} test days</p>
        {data.prospective_evaluation.horizons?.length ? <div className="operations-observed-table"><table><thead><tr><th>Horizon</th><th>ML MAE</th><th>Persistence MAE</th><th>Within ±5 index points</th><th>Matched samples</th></tr></thead><tbody>
          {data.prospective_evaluation.horizons.map(h => <tr key={h.horizon_minutes}><th>{h.horizon_minutes} min</th><td>{h.candidate_mae_points.toFixed(2)} points</td><td>{h.persistence_mae_points.toFixed(2)} points</td><td>{(h.within_5_points_fraction * 100).toFixed(1)}%</td><td>{h.samples}</td></tr>)}
        </tbody></table></div> : <p>Waiting for forecast targets to arrive. Accuracy is not available yet.</p>}
        <p>Predictions and their model versions are saved before the target traffic arrives. Results remain provisional until enough days and traffic conditions have been measured.</p>
      </section> : null}
      {showTraining && data.historical_benchmarks?.map(benchmark => <section key={benchmark.id} className="glass-panel real-training-evidence" aria-label={benchmark.name}>
        <h3>{benchmark.name}<span className="badge badge-neutral">{benchmark.geography}</span></h3>
        <p>{benchmark.source_rows.toLocaleString()} source measurements · {benchmark.source_days} source days · <a href={benchmark.source_url} target="_blank" rel="noreferrer">Original source</a></p>
        <div className="operations-observed-table"><table><thead><tr><th>Horizon</th><th>ML error</th><th>Persistence error</th><th>MAE improvement</th><th>Within ±5 km/h</th><th>Test samples</th><th>Test days</th></tr></thead><tbody>
          {benchmark.horizons.map(h => <tr key={h.horizon_minutes}><th>{h.horizon_minutes} min</th><td>{h.candidate.mae_kph.toFixed(2)} km/h</td><td>{h.persistence.mae_kph.toFixed(2)} km/h</td><td>{h.mae_improvement_percent.toFixed(1)}%</td><td>{(h.candidate.within_5_kph * 100).toFixed(1)}%</td><td>{h.candidate.samples.toLocaleString()}</td><td>{h.test_days}</td></tr>)}
        </tbody></table></div><p>{benchmark.scope}</p>
        <p>“Within ±5 km/h” counts predictions inside that tolerance. MAE improvement is error reduction against persistence, not percentage accuracy.</p>
      </section>)}
      <div className="glass-panel real-model-scope">
        <strong>{report!.quality_gate.eligible ? 'Observed-data release checks passed' : 'Provisional: more measured coverage is needed'}</strong>
        <p>{report!.quality_gate.reasons[0] ?? 'Keep checking held-out performance as data changes.'}</p>
        {!data.network.every(p => p.lower !== null) ? <p>Interval estimates are withheld while independent calibration evidence is sparse.</p> : null}
      </div>
    </>}
  </section>;
}
