import { useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Pie, PieChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { Activity, ChartColumn, CloudRain, Grid2X2, type LucideIcon } from 'lucide-react';
import type { AnalyticsSimulation } from '../../services/api';
import { corridorForecastData, forecastClock, measuredChartData, trafficBand, trafficBands, type TrafficPoint } from './analyticsChartData';
import './AnalyticsCharts.css';

const axis = { fill: 'var(--text-muted)', fontSize: 12 };
const tooltipStyle = { borderRadius: 12, border: '1px solid var(--border-color)',
  background: 'var(--warm-card)', fontSize: 13, color: 'var(--text-primary)' };
const chartMargin = { top: 12, right: 20, bottom: 4, left: 0 };

function ChartCard({ title, source, description, icon: Icon, children, className = '' }: {
  title: string; source: string; description: string; icon: LucideIcon; children: ReactNode; className?: string;
}) {
  const id = useId();
  return <figure className={`glass-panel analytics-chart-card ${className}`} aria-labelledby={`${id}-title`} aria-describedby={`${id}-description`}>
    <figcaption>
      <div className="analytics-chart-heading"><h3 id={`${id}-title`}><Icon size={18} aria-hidden="true" />{title}</h3><span className="analytics-chart-source">{source}</span></div>
      <p id={`${id}-description`}>{description}</p>
    </figcaption>
    {children}
  </figure>;
}

function ChartLegend({ items }: { items: Array<{ name: string; color: string; value?: string }> }) {
  return <ul className="analytics-chart-legend">{items.map(item => <li key={item.name}>
    <span className="analytics-chart-swatch" style={{ background: item.color }} aria-hidden="true" />
    <span>{item.name}</span>{item.value ? <strong>{item.value}</strong> : null}
  </li>)}</ul>;
}

export function ObservedTrafficCharts({ samples, expectedCount }: { samples: TrafficPoint[]; expectedCount?: number }) {
  const { speeds, conditions, pointCount, classifiedCount } = measuredChartData(samples);
  const coverage = `${pointCount}${expectedCount ? ` of ${expectedCount}` : ''} sampled road points`;
  return <div className="analytics-charts-grid" aria-label="Observed traffic charts">
    <ChartCard title="Road speeds at a glance" icon={ChartColumn} source="Measured speeds"
      description="Current provider speed alongside its free-flow reference, in km/h.">
      {speeds.length ? <>
        <div className="analytics-chart-plot" style={{ height: Math.max(250, speeds.length * 52) }}>
          <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 540, height: 260 }}>
            <BarChart data={speeds} layout="vertical" margin={chartMargin} accessibilityLayer>
              <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 5" horizontal={false} />
              <XAxis type="number" tick={axis} axisLine={false} tickLine={false} unit=" km/h" />
              <YAxis type="category" dataKey="name" width={116} tick={axis} axisLine={false} tickLine={false} />
              <Tooltip contentStyle={tooltipStyle} cursor={{ fill: 'var(--tint-cyan)', opacity: 0.4 }} formatter={(value, name) => [`${value} km/h`, name]} />
              <Bar dataKey="freeFlow" name="Free-flow reference" fill="var(--accent-indigo)" fillOpacity={0.35} radius={[0, 4, 4, 0]} maxBarSize={12} isAnimationActive={false} />
              <Bar dataKey="measured" name="Measured speed" fill="var(--accent-cyan)" radius={[0, 4, 4, 0]} maxBarSize={12} isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        </div>
        <ChartLegend items={[{ name: 'Measured speed', color: 'var(--accent-cyan)' }, { name: 'Free-flow reference', color: 'var(--accent-indigo)' }]} />
        <details className="analytics-chart-data"><summary>View speed values</summary><div className="operations-observed-table"><table>
          <thead><tr><th>Road point</th><th>Measured km/h</th><th>Free-flow km/h</th></tr></thead>
          <tbody>{speeds.map(point => <tr key={point.id}><th>{point.name}</th><td>{point.measured}</td><td>{point.freeFlow}</td></tr>)}</tbody>
        </table></div></details>
      </> : <p className="analytics-chart-empty" role="status">No fresh measured speeds are available.</p>}
    </ChartCard>
    <ChartCard title="Traffic conditions" icon={Activity} source={classifiedCount ? 'Observed pressure' : 'Conditions unavailable'}
      description={`${coverage}. Pressure bands use validated provider observations; unqualified points remain unavailable.`}>
      {pointCount ? <>
        <div className="analytics-chart-plot analytics-chart-donut">
          <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 540, height: 260 }}>
            <PieChart accessibilityLayer>
              <Pie data={conditions.filter(item => item.count > 0)} dataKey="count" nameKey="name" innerRadius={68} outerRadius={98}
                paddingAngle={2} stroke="var(--warm-card)" isAnimationActive={false}>
                {conditions.filter(item => item.count > 0).map(item => <Cell key={item.name} fill={item.color} />)}
              </Pie>
              <text x="50%" y="48%" textAnchor="middle" className="analytics-donut-count">{pointCount}</text>
              <text x="50%" y="59%" textAnchor="middle" className="analytics-donut-caption">road points</text>
              <Tooltip contentStyle={tooltipStyle} formatter={value => [`${value} road points`, 'Count']} />
            </PieChart>
          </ResponsiveContainer>
        </div>
        <ChartLegend items={conditions.map(item => ({ name: item.name, color: item.color, value: `${item.count}` }))} />
        <p className="analytics-chart-footnote">Below 70 · amber 70–79 · critical 80+. Sampled road points only.</p>
      </> : <p className="analytics-chart-empty" role="status">Traffic conditions are unavailable without fresh observations.</p>}
    </ChartCard>
  </div>;
}

export function ForecastCharts({ simulation }: { simulation: AnalyticsSimulation }) {
  const id = useId().replace(/:/g, '');
  const heatmapRef = useRef<HTMLTableElement>(null);
  const [focusedCell, setFocusedCell] = useState(0);
  const [inspectedCell, setInspectedCell] = useState<{ corridorId: string; timestamp: string } | null>(null);
  const hourly = useMemo(() => simulation.hourly_forecast.map(point => ({ ...point, clock: forecastClock(point.timestamp),
    range: [point.p10_score, point.p90_score] })), [simulation.hourly_forecast]);
  const corridors = useMemo(() => corridorForecastData(simulation), [simulation]);
  const peaks = corridors.filter(row => row.peak !== null);
  const inspectedRoad = corridors.find(row => row.id === inspectedCell?.corridorId);
  const inspectedHour = hourly.findIndex(row => row.timestamp === inspectedCell?.timestamp);
  const inspectedScore = inspectedRoad?.scores[inspectedHour];
  const inspection = inspectedRoad && inspectedHour >= 0
    ? `${inspectedRoad.name} · ${hourly[inspectedHour].clock} WIB · ${inspectedScore == null ? 'Unavailable' : `${inspectedScore.toFixed(1)} / 100`}` : null;
  const activeCell = Math.min(focusedCell, Math.max(0, corridors.length * hourly.length - 1));
  const moveCell = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const offset = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: hourly.length, ArrowUp: -hourly.length }[event.key];
    if (offset === undefined) return;
    event.preventDefault();
    const next = Math.max(0, Math.min(corridors.length * hourly.length - 1, index + offset));
    heatmapRef.current?.querySelectorAll<HTMLButtonElement>('button')[next]?.focus();
  };
  return <div className="analytics-charts-grid" aria-label="Model forecast charts">
    <ChartCard title="The next 24 hours" icon={Activity} source="Modelled forecast"
      description="Mean congestion index with average corridor P10–P90 tree dispersion. Times use WIB.">
      <div className="analytics-chart-plot">
        <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 540, height: 260 }}>
          <AreaChart data={hourly} margin={chartMargin} accessibilityLayer>
            <defs><linearGradient id={`${id}-forecast`} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="var(--accent-cyan)" stopOpacity={0.3} /><stop offset="100%" stopColor="var(--accent-cyan)" stopOpacity={0.02} /></linearGradient></defs>
            <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 5" vertical={false} />
            <XAxis dataKey="clock" tick={axis} minTickGap={34} interval="preserveStartEnd" axisLine={false} tickLine={false} />
            <YAxis domain={[0, 100]} tick={axis} axisLine={false} tickLine={false} width={32} />
            <Tooltip contentStyle={tooltipStyle} labelFormatter={label => `${label} WIB`} formatter={(value, name) => [Array.isArray(value) ? value.map(v => Number(v).toFixed(1)).join('–') : Number(value).toFixed(1), name]} />
            <Area type="linear" dataKey="range" name="Mean corridor P10–P90" stroke="none" fill="var(--accent-cyan)" fillOpacity={0.12} isAnimationActive={false} />
            <Area type="linear" dataKey="mean_score" name="Mean congestion index" stroke="var(--accent-cyan)" strokeWidth={2.5} fill={`url(#${id}-forecast)`} isAnimationActive={false} />
          </AreaChart>
        </ResponsiveContainer>
      </div>
      <ChartLegend items={[{ name: 'Mean index', color: 'var(--accent-cyan)' }, { name: 'P10–P90 dispersion', color: 'var(--tint-cyan)' }]} />
      <p className="analytics-chart-footnote">Tree dispersion; not a calibrated joint interval or an 80% guarantee.</p>
    </ChartCard>
    <ChartCard title="When the weather changes" icon={CloudRain} source="Modelled comparison"
      description="Current-time congestion under clear, rain and storm assumptions. This is not a weather report.">
      {simulation.weather_comparison.length ? <>
        <div className="analytics-chart-plot">
          <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 540, height: 260 }}>
            <BarChart data={simulation.weather_comparison} margin={chartMargin} accessibilityLayer>
              <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 5" vertical={false} />
              <XAxis dataKey="label" tick={axis} axisLine={false} tickLine={false} />
              <YAxis domain={[0, 100]} tick={axis} axisLine={false} tickLine={false} width={32} />
              <Tooltip contentStyle={tooltipStyle} cursor={{ fill: 'var(--tint-cyan)', opacity: 0.4 }} formatter={value => [`${Number(value).toFixed(1)} / 100`, 'Mean index']} />
              <Bar dataKey="mean_score" name="Mean index" radius={[6, 6, 0, 0]} maxBarSize={56} isAnimationActive={false}>
                {simulation.weather_comparison.map((row, index) => <Cell key={row.weather} fill={['var(--accent-cyan)', 'var(--accent-indigo)', 'var(--accent-amber)'][index % 3]} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
        <ChartLegend items={simulation.weather_comparison.map((row, index) => ({ name: row.label,
          color: ['var(--accent-cyan)', 'var(--accent-indigo)', 'var(--accent-amber)'][index % 3], value: `${row.mean_score.toFixed(1)}` }))} />
      </> : <p className="analytics-chart-empty" role="status">No weather comparison was returned.</p>}
    </ChartCard>
    <ChartCard title="Corridor pressure" icon={ChartColumn} source="Modelled forecast"
      description="First forecast hour compared with each corridor’s highest hourly index in this forecast window.">
      {peaks.length ? <>
        <div className="analytics-chart-plot" style={{ height: Math.max(260, peaks.length * 52) }}>
          <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 540, height: 260 }}>
            <BarChart data={peaks} layout="vertical" margin={chartMargin} accessibilityLayer>
              <CartesianGrid stroke="var(--border-color)" strokeDasharray="3 5" horizontal={false} />
              <XAxis type="number" domain={[0, 100]} tick={axis} axisLine={false} tickLine={false} />
              <YAxis type="category" dataKey="name" width={116} tick={axis} axisLine={false} tickLine={false} />
              <Tooltip contentStyle={tooltipStyle} formatter={(value, name) => [`${Number(value).toFixed(1)} / 100`, name]} />
              <Bar dataKey="current" name="First hour" fill="var(--accent-cyan)" radius={[0, 4, 4, 0]} maxBarSize={12} isAnimationActive={false} />
              <Bar dataKey="peak" name="Peak hour" fill="var(--accent-amber)" radius={[0, 4, 4, 0]} maxBarSize={12} isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        </div>
        <ChartLegend items={[{ name: 'First hour', color: 'var(--accent-cyan)' }, { name: 'Peak hour', color: 'var(--accent-amber)' }]} />
      </> : <p className="analytics-chart-empty" role="status">No corridor forecasts were returned.</p>}
    </ChartCard>
    <ChartCard title="Corridors through the day" icon={Grid2X2} source="Modelled heatmap"
      description="Hourly corridor forecasts. Inspect a cell with the pointer, or use arrow keys within the heatmap.">
      {corridors.length ? <div className="analytics-heatmap-scroll"><table ref={heatmapRef} className="analytics-heatmap">
        <caption className="visually-hidden">Hourly corridor congestion indices, WIB. Missing forecasts are shown as a dash.</caption>
        <thead><tr><th scope="col">Road corridor</th>{hourly.map((point, index) => <th key={point.timestamp} scope="col"><span>{index % 4 === 0 ? point.clock : ''}</span></th>)}</tr></thead>
        <tbody>{corridors.map((row, rowIndex) => <tr key={row.id}><th scope="row">{row.name}</th>{row.scores.map((score, index) => <td key={hourly[index].timestamp}>
          <button type="button" className="analytics-heatmap-cell" style={score === null ? undefined : { background: trafficBands[trafficBand(score)].color, opacity: 0.25 + score / 100 * 0.65 }}
            tabIndex={activeCell === rowIndex * hourly.length + index ? 0 : -1}
            onKeyDown={event => moveCell(event, rowIndex * hourly.length + index)}
            onFocus={() => { setFocusedCell(rowIndex * hourly.length + index); setInspectedCell({ corridorId: row.id, timestamp: hourly[index].timestamp }); }}
            onMouseEnter={() => setInspectedCell({ corridorId: row.id, timestamp: hourly[index].timestamp })}
            onClick={() => setInspectedCell({ corridorId: row.id, timestamp: hourly[index].timestamp })}
            aria-label={`${row.name}, ${hourly[index].clock} WIB: ${score === null ? 'unavailable' : `${score.toFixed(1)} out of 100`}`}
            title={`${row.name} · ${hourly[index].clock} WIB · ${score === null ? 'Unavailable' : score.toFixed(1)}`}>
            <span className="visually-hidden">{score === null ? '—' : score.toFixed(1)}</span>
          </button>
        </td>)}</tr>)}</tbody>
      </table></div> : <p className="analytics-chart-empty" role="status">No corridor heatmap is available.</p>}
      {corridors.length ? <p className="analytics-chart-footnote analytics-heatmap-inspection">{inspection ?? 'Select an hour to inspect its forecast.'}</p> : null}
      <ChartLegend items={trafficBands.map(band => ({ name: `${band.name} ${band.range}`, color: band.color }))} />
    </ChartCard>
  </div>;
}
