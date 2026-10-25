import { useState } from 'react';
import { CalendarDays, Clock, Layers, TrendingUp } from 'lucide-react';
import type { DailyTrend, ModelledPatternAnalysis } from '../../types';
import { batamParts } from '../../utils/batamTime';
import './ModelledPatternInsights.css';

type DayType = 'all' | 'weekday' | 'weekend';
const DAY_TYPES: { id: DayType; label: string }[] = [
  { id: 'all', label: 'All days' }, { id: 'weekday', label: 'Weekdays' }, { id: 'weekend', label: 'Weekends' },
];
const hourLabel = (hour: number) => `${hour % 12 || 12}${hour < 12 ? 'am' : 'pm'}`;
const plural = (count: number, noun: string) => `${count.toLocaleString()} ${noun}${count === 1 ? '' : 's'}`;
const dateLabel = (date: string) => new Intl.DateTimeFormat('en-GB', {
  day: 'numeric', month: 'short', timeZone: 'Asia/Jakarta',
}).format(new Date(date));
const validCalendarDay = (date: string): boolean => /^\d{4}-\d{2}-\d{2}$/.test(date)
  && Number.isFinite(Date.parse(`${date}T00:00:00Z`))
  && new Date(`${date}T00:00:00Z`).toISOString().slice(0, 10) === date;

export function ModelledPatternInsights({ analysis, daily, corridorName }: {
  analysis: ModelledPatternAnalysis; daily: DailyTrend[]; corridorName: string;
}) {
  const [dayType, setDayType] = useState<DayType>('all');
  const pattern = analysis.day_profiles[dayType];
  const populated = pattern.hourly_profile.filter(bucket => bucket.avg_score !== null && bucket.sample_count > 0);
  const peak = populated.reduce<(typeof populated)[number] | null>((best, bucket) => (
    !best || bucket.avg_score! > best.avg_score! ? bucket : best
  ), null);
  const quiet = populated.reduce<(typeof populated)[number] | null>((best, bucket) => (
    !best || bucket.avg_score! < best.avg_score! ? bucket : best
  ), null);
  const currentHour = batamParts(new Date()).hour;
  const selectedDays = daily.filter(day => {
    if (!validCalendarDay(day.date) || !Number.isFinite(day.avg_score)
      || day.avg_score < 0 || day.avg_score > 100 || !Number.isSafeInteger(day.sample_count) || day.sample_count <= 0) return false;
    const weekday = new Date(`${day.date}T12:00:00+07:00`).getUTCDay();
    return dayType === 'all' || (dayType === 'weekend' ? weekday === 0 || weekday === 6 : weekday > 0 && weekday < 6);
  });
  const weekdayMean = analysis.day_profiles.weekday.weighted_mean_score;
  const weekendMean = analysis.day_profiles.weekend.weighted_mean_score;
  const stats = [
    { label: 'Modelled peak hour', value: peak ? hourLabel(peak.hour) : 'Unavailable', detail: peak ? `${peak.avg_score!.toFixed(1)} / 100 average` : 'No matching scenarios', Icon: TrendingUp },
    { label: 'Quieter modelled hour', value: quiet ? hourLabel(quiet.hour) : 'Unavailable', detail: quiet ? `${quiet.avg_score!.toFixed(1)} / 100 average` : 'No matching scenarios', Icon: Clock },
    { label: 'Average scenario index', value: pattern.weighted_mean_score?.toFixed(1) ?? '—', detail: 'Across available scenarios · / 100', Icon: Layers },
    { label: 'Scenario coverage', value: plural(pattern.day_count, 'day'), detail: plural(pattern.sample_count, 'modelled sample'), Icon: CalendarDays },
  ];

  return <section className="modelled-patterns" aria-label="Modelled pattern comparison">
    <div className="modelled-patterns__toolbar">
      <div role="group" aria-label="Modelled day type" className="modelled-patterns__day-types">
        {DAY_TYPES.map(option => <button key={option.id} type="button"
          className="ui-sand-interactive" aria-pressed={dayType === option.id}
          onClick={() => setDayType(option.id)}>{option.label}</button>)}
      </div>
      <p>{plural(analysis.coverage.day_count, 'modelled day')} in the {analysis.coverage.requested_days === 1 ? '24-hour' : `${analysis.coverage.requested_days}-day`} window · WIB</p>
    </div>

    <div className="modelled-patterns__metrics">
      {stats.map(({ label, value, detail, Icon }) => <article key={label}>
        <span><Icon size={16} aria-hidden="true" />{label}</span>
        <strong>{value}</strong><small>{detail}</small>
      </article>)}
    </div>

    <div className="modelled-patterns__charts">
      <section className="modelled-patterns__chart" aria-label="Hourly scenario variation">
        <div className="modelled-patterns__chart-heading"><h4>Hourly traffic pattern</h4><span>Scenario index / 100</span></div>
        <p>Hourly average with the middle 80% of modelled values.</p>
        {populated.length ? <>
          <div className="modelled-patterns__plot-wrap">
            <div className="modelled-patterns__y-axis" aria-hidden="true"><span>100</span><span>50</span><span>0</span></div>
            <div className="modelled-patterns__hourly" role="group" aria-label={`Hourly modelled values for ${corridorName}`}>
              {pattern.hourly_profile.map(bucket => <button type="button" key={bucket.hour}
                className={`modelled-patterns__hour${bucket.hour === currentHour ? ' is-current' : ''}${bucket.sample_count === 0 ? ' is-empty' : ''}`}
                aria-label={bucket.avg_score === null ? `${hourLabel(bucket.hour)}: no modelled samples`
                  : `${hourLabel(bucket.hour)}: average ${bucket.avg_score.toFixed(1)}, middle 80% ${bucket.p10_score!.toFixed(1)} to ${bucket.p90_score!.toFixed(1)}, ${plural(bucket.sample_count, 'modelled sample')}`}>
                {bucket.avg_score !== null ? <>
                  <span className="modelled-patterns__range" style={{ bottom: `${bucket.p10_score}%`, height: `${bucket.p90_score! - bucket.p10_score!}%` }} />
                  <span className="modelled-patterns__mean" style={{ bottom: `${bucket.avg_score}%` }} />
                </> : null}
                <span className="modelled-patterns__hour-tooltip">{hourLabel(bucket.hour)}<br />
                  {bucket.avg_score === null ? 'No scenarios' : `${bucket.avg_score.toFixed(1)} average · ${plural(bucket.sample_count, 'sample')}`}</span>
              </button>)}
            </div>
          </div>
          <div className="modelled-patterns__x-axis" aria-hidden="true">{Array.from({ length: 24 }, (_, hour) => <span key={hour}>{hour % 4 === 0 ? hourLabel(hour) : ''}</span>)}</div>
          <div className="modelled-patterns__legend"><span><i className="is-mean" />Average</span><span><i className="is-range" />Middle 80% of scenarios</span><span><i className="is-current" />Current hour</span></div>
        </> : <p role="status" className="modelled-patterns__empty">No {dayType === 'weekend' ? 'weekend' : 'weekday'} scenarios in this window. Choose another day type or a longer window.</p>}
      </section>

      <section className="modelled-patterns__chart" aria-label="Daily scenario comparison">
        <div className="modelled-patterns__chart-heading"><h4>Day-to-day pattern</h4><span>Same 0–100 scale</span></div>
        <p>{weekdayMean !== null && weekendMean !== null
          ? `Weekdays average ${weekdayMean.toFixed(1)} · weekends ${weekendMean.toFixed(1)}.`
          : 'Compare the modelled days available in this window.'}</p>
        {selectedDays.length ? <>
          <div className="modelled-patterns__plot-wrap">
            <div className="modelled-patterns__y-axis" aria-hidden="true"><span>100</span><span>50</span><span>0</span></div>
            <div className="modelled-patterns__daily" role="group" aria-label={`Daily modelled values for ${corridorName}`}>
              {selectedDays.map(day => <button type="button" key={day.date}
                aria-label={`${dateLabel(`${day.date}T12:00:00+07:00`)}: average ${day.avg_score.toFixed(1)}, ${plural(day.sample_count, 'modelled sample')}`}>
                <span className="modelled-patterns__day-bar" style={{ height: `${day.avg_score}%` }} />
                <span className="modelled-patterns__hour-tooltip">{dateLabel(`${day.date}T12:00:00+07:00`)}<br />{day.avg_score.toFixed(1)} average</span>
              </button>)}
            </div>
          </div>
          <div className="modelled-patterns__daily-labels"><span>{dateLabel(`${selectedDays[0].date}T12:00:00+07:00`)}</span><span>{plural(selectedDays.length, 'modelled day')}</span><span>{dateLabel(`${selectedDays[selectedDays.length - 1].date}T12:00:00+07:00`)}</span></div>
          <p className="modelled-patterns__coverage-note">{analysis.coverage.first_sample_at && analysis.coverage.last_sample_at
            ? `Available ${dateLabel(analysis.coverage.first_sample_at)}–${dateLabel(analysis.coverage.last_sample_at)}. Missing days are not filled in.`
            : 'No modelled dates are available.'}</p>
        </> : <p role="status" className="modelled-patterns__empty">No matching modelled days.</p>}
      </section>
    </div>
    <p className="modelled-patterns__note">Engineering scenarios, not measured history or a live forecast. The spread describes modelled variation; it is not a prediction confidence interval.</p>
  </section>;
}
