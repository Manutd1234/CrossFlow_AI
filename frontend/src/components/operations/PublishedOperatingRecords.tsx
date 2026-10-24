import { useCallback } from 'react';
import { ChevronDown, ExternalLink, Landmark, RefreshCw, Ship, Users } from 'lucide-react';
import { fetchAuthorityReports, type AuthorityReport } from '../../services/api';
import { useCheckedSource } from '../../hooks/useCheckedSource';
import { englishReportingPeriod, englishSourceText, englishSourceUnit } from '../../utils/englishDisplay';
import './PublishedOperatingRecords.css';

const passengerColors = ['var(--accent-cyan)', 'var(--accent-indigo)'];

function Report({ report }: { report: AuthorityReport }) {
  const metrics = report.metrics.map(metric => ({
    ...metric, label: englishSourceText(metric.label), unit: englishSourceUnit(metric.unit),
  }));
  const passengers = metrics.filter(metric => metric.unit === 'people');
  const passengerTotal = passengers.reduce((sum, metric) => sum + metric.value, 0);
  return <article className="operations-published-report maritime-report">
    <div className="maritime-report-heading"><h4>{englishReportingPeriod(report.period_label, report.period_start, report.period_end)}</h4>
      <a href={report.source_url} target="_blank" rel="noreferrer">{report.publisher}<ExternalLink size={14} aria-hidden="true" /></a>
    </div>
    <dl className="maritime-report-metrics">{metrics.map(metric => {
      const Icon = metric.unit === 'calls' ? Ship : Users;
      return <div key={metric.id} className="maritime-report-metric">
        <dt><Icon size={18} aria-hidden="true" />{metric.label}</dt>
        <dd>{metric.value.toLocaleString('en-SG')} <span>{metric.unit}</span></dd>
      </div>;
    })}</dl>
    {passengers.length > 1 && passengerTotal > 0 ? <div className="maritime-passenger-mix">
      <div className="maritime-passenger-mix-heading"><strong>Passenger movements</strong><span>Share of this reporting period</span></div>
      <div className="maritime-passenger-bar" role="img" aria-label={passengers.map(metric => `${metric.label}: ${metric.value.toLocaleString('en-SG')}, ${(metric.value / passengerTotal * 100).toFixed(1)}%`).join('; ')}>
        {passengers.map((metric, index) => <span key={metric.id} style={{ width: `${metric.value / passengerTotal * 100}%`, background: passengerColors[index % passengerColors.length] }} />)}
      </div>
      <ul className="maritime-passenger-legend">{passengers.map((metric, index) => <li key={metric.id}>
        <span className="analytics-chart-swatch" style={{ background: passengerColors[index % passengerColors.length] }} aria-hidden="true" />
        {metric.label}<strong>{(metric.value / passengerTotal * 100).toFixed(1)}%</strong>
      </li>)}</ul>
    </div> : null}
  </article>;
}

export function PublishedOperatingRecords() {
  const request = useCallback(() => fetchAuthorityReports(), []);
  const { snapshot, error, loading, refresh } = useCheckedSource(request);
  const report = snapshot?.report;
  const olderReports = snapshot?.stored_reports.filter(item => item.content_sha256 !== report?.content_sha256) ?? [];
  const status = error && report ? 'stale' : snapshot?.status ?? (loading ? 'checking' : 'unavailable');
  return <section className="glass-panel operations-published-records" aria-label="Published operating records" aria-busy={loading}>
    <div className="maritime-records-header"><div><h3><Landmark size={18} aria-hidden="true" />Published operating records</h3>
      <p>Published period totals, not live terminal activity.</p></div>
      <button type="button" className="glass-button" disabled={loading} onClick={refresh}><RefreshCw size={16} aria-hidden="true" />{loading ? 'Checking report…' : 'Check official report'}</button>
    </div>
    {error || snapshot?.error ? <p role="alert">{error ?? snapshot?.error}{report ? ' Last known published report retained; verify its period at the source.' : ''}</p> : null}
    {report ? <Report report={report} /> : !loading ? <p role="status">No validated official report is available. No fixed passenger or vessel totals are substituted.</p> : null}
    <div className="maritime-records-footer"><span className="badge badge-neutral" aria-label={`Report status: ${status}`}>{status === 'fresh' ? 'Verified report' : status === 'stale' ? 'Saved report · stale' : status}</span>
      {snapshot?.checked_at ? <span>Source checked <time dateTime={snapshot.checked_at}>{new Date(snapshot.checked_at).toLocaleString('en-SG')}</time></span> : null}
    </div>
    {olderReports.length ? <details className="operations-disclosure"><summary>Earlier reporting periods ({olderReports.length})<ChevronDown size={18} aria-hidden="true" /></summary>
      {olderReports.map(item => <Report key={item.content_sha256} report={item} />)}
    </details> : null}
  </section>;
}
