import { type ReactNode, useCallback, useState } from 'react';
import { fetchHistoricalResearch, type HistoricalResearch } from '../../services/api';
import { useCheckedSource } from '../../hooks/useCheckedSource';
import { WorkspaceSubtabs } from '../shared/WorkspaceSubtabs';

function Survey({ survey, benchmark }: { survey: HistoricalResearch['survey']; benchmark?: HistoricalResearch['batam_benchmark'] }) {
  const [dateSelection, setDateSelection] = useState('');
  const [direction, setDirection] = useState('from_east');
  const date = survey.dates.includes(dateSelection) ? dateSelection : survey.dates[0];
  const rows = survey.rows.filter(row => row.interval_start.startsWith(date) && row.direction === direction);
  const mean = rows.length ? rows.reduce((total, row) => total + row.speed_kph, 0) / rows.length : null;
  return <article className="operations-published-report operations-research-report">
    <h4>Batam · Published road-speed survey</h4>
    <p>{survey.location}. {survey.row_count} published interval observations; {survey.dates.length} survey days.
      {' '}{survey.min_speed_kph.toFixed(2)}–{survey.max_speed_kph.toFixed(2)} km/h.</p>
    <div className="operations-source-controls operations-survey-filters">
      <label>Survey date<select value={date} onChange={event => setDateSelection(event.target.value)}>
        {survey.dates.map(day => <option key={day} value={day}>{day}</option>)}
      </select></label>
      <label>Published direction<select value={direction} onChange={event => setDirection(event.target.value)}>
        <option value="from_east">From east</option><option value="from_west">From west</option>
      </select></label>
      <p>{rows.length} intervals selected · Mean of published speeds: {mean === null ? 'Unavailable' : `${mean.toFixed(2)} km/h`}.</p>
    </div>
    <div className="operations-observed-table"><table>
      <caption>{survey.source_table} · Survey clock WIB (UTC+7)</caption>
      <thead><tr><th scope="col">Interval</th><th scope="col">Speed (km/h)</th><th scope="col">Travel time (s)</th><th scope="col">Quality note</th></tr></thead>
      <tbody>{rows.map(row => <tr key={`${row.interval_start}-${row.direction}`}>
        <th scope="row">{row.interval_start.slice(11, 16)}–{row.interval_end.slice(11, 16)}</th>
        <td>{row.speed_kph.toFixed(2)}</td><td>{row.travel_seconds.toFixed(2)}</td>
        <td>{row.quality_flag.includes('over_0.5') ? 'Reported speed/time cross-check differs by >0.5 km/h; retained as published' : 'Published aggregate; raw samples unavailable'}</td>
      </tr>)}</tbody>
    </table></div>
    {benchmark && <section className="operations-site-backtest" aria-label="Exploratory Batam site backtest">
      <h5>Exploratory Batam site backtest · real published speeds</h5>
      <p>Expanding training windows of 16 and 32 observations; 32 next-survey-day forecasts across two held-out days.
        {' '}{benchmark.synthetic_training_rows} synthetic training rows. Errors below are in km/h.</p>
      <div className="operations-observed-table"><table>
        <caption>Single-site diagnostic · forecast each survey day from midnight WIB</caption>
        <thead><tr><th scope="col">Model / baseline</th><th scope="col">MAE (km/h)</th><th scope="col">RMSE (km/h)</th></tr></thead>
        <tbody>{([
          ['Random forest', benchmark.random_forest], ['Previous survey day', benchmark.previous_survey_day],
          ['Historical time-slot mean', benchmark.historical_slot_mean],
        ] as const).map(([label, metric]) => <tr key={label}><th scope="row">{label}</th>
          <td>{metric.mae_kph.toFixed(3)}</td><td>{metric.rmse_kph.toFixed(3)}</td></tr>)}</tbody>
      </table></div>
      <p>Evaluated <time dateTime={benchmark.evaluated_at}>{new Date(benchmark.evaluated_at).toLocaleString('en-SG')}</time>.
        {' '}Too few days and locations for operational model promotion; these are not 15/30/60-minute network accuracy scores.</p>
    </section>}
    <footer className="operations-research-footer">
    <p>No canonical corridor mapping verified. This survey has not replaced the serving model.</p>
      <p><a href={survey.source_url} title={survey.attribution} target="_blank" rel="noreferrer">Journal source</a>{' · '}
        <a href={survey.source_pdf_url} target="_blank" rel="noreferrer">Original PDF</a>{' · '}
        <a href={survey.license_url} target="_blank" rel="noreferrer">{survey.license}</a></p>
    </footer>
  </article>;
}

function SaturationSurvey({ survey }: { survey: NonNullable<HistoricalResearch['saturation_survey']> }) {
  const [selection, setSelection] = useState('');
  const date = survey.dates.includes(selection) ? selection : survey.dates[0];
  const rows = survey.rows.filter(row => row.interval_start.startsWith(date));
  return <article className="operations-published-report operations-research-report">
    <h4>Batam · Published intersection saturation</h4>
    <p>{survey.location}. {survey.rows.length} published hourly ratios over {survey.dates.length} survey days.</p>
    <p>Derived volume/capacity ratios (DS), not vehicle speeds. These records contribute {survey.eligible_speed_training_rows} speed-training labels.</p>
    <div className="operations-source-controls operations-survey-filters"><label>Intersection survey date
      <select value={date} onChange={event => setSelection(event.target.value)}>{survey.dates.map(day => <option key={day} value={day}>{day}</option>)}</select>
    </label></div>
    <div className="operations-observed-table"><table><caption>{survey.source_table} · WIB (UTC+7)</caption>
      <thead><tr><th scope="col">Interval</th><th scope="col">Published saturation ratio (DS)</th></tr></thead>
      <tbody>{rows.map(row => <tr key={row.interval_start}><th scope="row">{row.interval_start.slice(11, 16)}–{row.interval_end.slice(11, 16)}</th>
        <td>{row.degree_of_saturation.toFixed(3)}</td></tr>)}</tbody>
    </table></div>
    <footer className="operations-research-footer"><p>Kept separate from measured speed history and the serving speed model.</p>
      <details><summary>Intersection source, licence and limitations</summary><p>{survey.attribution}</p>
        <p><a href={survey.source_url} target="_blank" rel="noreferrer">Journal article</a>{' · '}
          <a href={survey.source_pdf_url} target="_blank" rel="noreferrer">Original PDF</a>{' · '}
          <a href={survey.license_url} target="_blank" rel="noreferrer">{survey.license}</a></p>
        <ul>{survey.scope_limitations.map(limit => <li key={limit}>{limit}</li>)}</ul>
      </details>
    </footer>
  </article>;
}

function Benchmark({ benchmark }: { benchmark: HistoricalResearch['benchmark'] }) {
  return <article className="operations-published-report operations-research-report">
    <h4>Singapore · Historical ML backtest</h4>
    <p>{benchmark.roads} road links · {benchmark.source_calendar_days} calendar days · {benchmark.speed_band_records.toLocaleString('en-SG')} speed-band observations.
      {' '}{benchmark.source_first_timestamp.slice(0, 10)} to {benchmark.source_last_timestamp.slice(0, 10)}.</p>
    <p>{benchmark.source_signal}. Chronological training, calibration and held-out test; {benchmark.synthetic_training_rows} synthetic training rows.
      {' '}This independent Singapore benchmark has not replaced the Batam serving model.</p>
    <div className="operations-observed-table"><table>
      <caption>Held-out error in speed-band units (lower is better); no km/h accuracy claim</caption>
      <thead><tr><th scope="col">Forecast</th><th scope="col">Test forecasts</th><th scope="col">Random forest MAE</th><th scope="col">Persistence MAE</th><th scope="col">80% interval coverage</th></tr></thead>
      <tbody>{benchmark.results.map(row => <tr key={row.horizon_minutes}>
        <th scope="row">{row.horizon_minutes} minutes</th><td>{row.random_forest.samples.toLocaleString('en-SG')}</td>
        <td>{row.random_forest.mae_band_units.toFixed(3)}</td><td>{row.persistence.mae_band_units.toFixed(3)}</td>
        <td>{(row.interval_empirical_coverage * 100).toFixed(1)}%</td>
      </tr>)}</tbody>
    </table></div>
    <footer className="operations-research-footer">
    <p>Evaluated: <time dateTime={benchmark.evaluated_at}>{new Date(benchmark.evaluated_at).toLocaleString('en-SG')}</time>. Overlapping forecasts are not independent samples.</p>
    <details><summary>Benchmark source, licence and limitations</summary>
      <p><a href={benchmark.source_url} target="_blank" rel="noreferrer">Published SUTD research archive</a>{' · '}
        <a href={benchmark.license_url} target="_blank" rel="noreferrer">Singapore Open Data Licence</a></p>
      <p>{benchmark.source_license}. Contains LTA Traffic Speed Bands accessed 23 October 2026 through the cited research archive. No LTA or publisher endorsement.</p>
      <p>{benchmark.source_provenance}</p><p>{benchmark.timestamp_basis}</p>
      <ul>{benchmark.limitations.map(limit => <li key={limit}>{limit}</li>)}</ul>
    </details>
    </footer>
  </article>;
}

type HistoryTab = 'observed' | 'survey' | 'intersection' | 'benchmark';

export function PublishedTrafficHistory({ observedContent }: { observedContent?: ReactNode }) {
  const [activeTab, setActiveTab] = useState<HistoryTab>(observedContent ? 'observed' : 'survey');
  const request = useCallback(() => fetchHistoricalResearch(), []);
  const { snapshot, error, loading, checkedAt, refresh } = useCheckedSource(request);
  return <section className="operations-history-workspace" aria-label="Published historical traffic evidence">
    <div className="operations-history-intro">
    <div className="operations-source-controls"><h3>Historical traffic evidence</h3>
      {activeTab !== 'observed' ? <button type="button" className="glass-button" onClick={refresh} disabled={loading}>{loading ? 'Checking evidence…' : 'Check evidence'}</button> : null}</div>
    <p>Explore corridor observations and dated research, one dataset at a time.</p>
    </div>
    {error ? <p role="alert">{error}{snapshot ? ' Previously checked evidence retained; the current check failed.' : ''}</p> : null}
    <WorkspaceSubtabs<HistoryTab> ariaLabel="History datasets" idPrefix="history-dataset" className="history-dataset-tabs"
      activeTab={activeTab} onActiveTabChange={setActiveTab} tabs={[
        ...(observedContent ? [{ id: 'observed' as const, label: 'Observed traffic', content: observedContent }] : []),
        { id: 'survey', label: 'Batam speed survey', content: snapshot ? <Survey survey={snapshot.survey} benchmark={snapshot.batam_benchmark} /> : null },
        { id: 'intersection', label: 'Intersection study', content: snapshot?.saturation_survey ? <SaturationSurvey survey={snapshot.saturation_survey} /> : <p role="status">No verified intersection study is available.</p> },
        { id: 'benchmark', label: 'Singapore benchmark', content: snapshot ? <Benchmark benchmark={snapshot.benchmark} /> : null },
      ]} />
    {activeTab !== 'observed' && !snapshot ? <p role="status">{loading ? 'Checking historical evidence…' : 'No validated research artifact is available.'}</p> : null}
    {activeTab !== 'observed' && checkedAt ? <p className="operations-history-check">Research last checked <time dateTime={checkedAt}>{new Date(checkedAt).toLocaleString('en-SG')}</time>.</p> : null}
  </section>;
}
