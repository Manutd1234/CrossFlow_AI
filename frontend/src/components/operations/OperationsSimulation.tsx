import { useCallback, useState } from 'react';
import { requestAnalyticsSimulation } from '../../services/api';
import { useCheckedSource } from '../../hooks/useCheckedSource';
import type { Corridor } from '../../types';
import { ChevronDown } from 'lucide-react';
import { ForecastCharts } from './AnalyticsCharts';
import { forecastClock } from './analyticsChartData';

export function OperationsSimulation({ corridors, accessToken }: { corridors: Corridor[]; accessToken: string | null }) {
  const [weather, setWeather] = useState(0);
  const [corridorId, setCorridorId] = useState('ALL');
  const key = `${weather}:${corridorId}`;
  const request = useCallback(async () => ({ key,
    result: await requestAnalyticsSimulation(weather, corridorId === 'ALL' ? undefined : [corridorId], accessToken),
  }), [weather, corridorId, accessToken, key]);
  const { snapshot, error, loading, refresh } = useCheckedSource(request);
  const result = snapshot?.key === key ? snapshot.result : null;
  const simulation = result?.data;
  const rows = simulation?.hourly_forecast ?? [];
  return <section className="glass-panel operations-simulation" aria-label="Computed congestion simulation" aria-busy={loading}>
    <div className="glass-panel operations-simulation-controls">
    <h3>24-hour congestion simulation</h3>
    <p>Explore the next 24 hours. Weather is a planning assumption.</p>
    <div className="operations-source-controls operations-forecast-toolbar">
      <label>Weather <select aria-label="Simulation weather" value={weather} onChange={event => setWeather(Number(event.target.value))}>
        <option value={0}>Clear</option><option value={1}>Rain</option><option value={2}>Storm</option>
      </select></label>
      <label>Corridor <select aria-label="Simulation corridor" value={corridorId} onChange={event => setCorridorId(event.target.value)}>
        <option value="ALL">All corridors</option>{corridors.map(corridor => <option key={corridor.id} value={corridor.id}>{corridor.name}</option>)}
      </select></label>
      <button type="button" className="glass-button" disabled={loading} onClick={refresh}>{loading ? 'Computing forecast…' : 'Recompute forecast'}</button>
    </div>
    {error ? <p role="alert">{error}{result ? ' Showing the previous computed snapshot; it is stale.' : ''}</p> : null}
    {simulation ?
      <p className="operations-forecast-caption">Updated <time dateTime={result?.fetchedAt}>{new Date(result!.fetchedAt).toLocaleTimeString('en-SG', { timeZone: 'Asia/Jakarta', hour: '2-digit', minute: '2-digit' })}</time> WIB · Random Forest simulation</p>
    : null}
    </div>
    {simulation ? <>
      <ForecastCharts simulation={simulation} />
      <details className="glass-panel operations-simulation-controls operations-disclosure"><summary>Forecast values and model details<ChevronDown size={18} aria-hidden="true" /></summary><div className="operations-observed-table"><table><thead><tr><th>Time (WIB)</th><th>Mean index</th><th>Mean P10</th><th>Mean P90</th></tr></thead><tbody>
        {rows.map(row => <tr key={row.timestamp}><td>{forecastClock(row.timestamp)}</td><td>{row.mean_score.toFixed(1)}</td><td>{row.p10_score.toFixed(1)}</td><td>{row.p90_score.toFixed(1)}</td></tr>)}
      </tbody></table></div></details>
      <div className="glass-panel operations-simulation-controls">
        <details className="operations-disclosure"><summary>Forecast scope and limitations<ChevronDown size={18} aria-hidden="true" /></summary>
          <p>Training source: {simulation.model.training_data_source ?? 'Undeclared'}. Observed training rows: {simulation.model.observed_training_rows ?? 'Undeclared'}.</p>
          {simulation.limitations.map(limit => <p key={limit}>{limit}</p>)}
        </details>
      </div>
    </> : !loading ? <p role="status">No computed forecast is available. Reconnect the backend model and retry; no fixed curve is substituted.</p> : null}
  </section>;
}
