import React, { useMemo, useRef, useState } from 'react';
import {
  Anchor, ExternalLink, ImageOff,
  PackageCheck, RefreshCw, ShieldCheck, Ship, TriangleAlert,
} from 'lucide-react';

import { TERMINAL_MEDIA, type TerminalMedia } from '../../data/terminalMedia';
import { ICON_SIZE } from '../../theme/iconSizes';
import {
  DataSource, FerryRefreshReport, FerrySchedule,
  FerryTimetableMetadata, PortStatus,
} from '../../types';
import {
  ferryBadgeClass, formatScheduleVerifiedAt, formatFerryTime, prettyStatus,
  relativeDeparture,
} from '../../utils/format';
import { batamParts } from '../../utils/batamTime';
import { WorkspaceSubtabs } from '../shared/WorkspaceSubtabs';
import './FerrySourceCheck.css';


interface FerryPortTrackerProps {
  ferries: FerrySchedule[];
  dataSource: DataSource;
  timetable: FerryTimetableMetadata;
  ports: PortStatus[];
  portSource: DataSource;
  portsLoading: boolean;
  portsError: string | null;
  portsCheckedAt?: string | null;
  isRefreshingOfficialSources: boolean;
  onRefreshOfficialSources: () => Promise<FerryRefreshReport>;
}

const PORT_FILTERS = ['ALL', 'Batam Centre', 'HarbourBay', 'Sekupang', 'Nongsa Pura'] as const;
type FerryWorkspaceTab = 'terminals' | 'departures' | 'cargo';

function wasVerifiedToday(value?: string, now: Date = new Date()): boolean {
  if (!value) return false;
  const verifiedAt = new Date(value);
  if (Number.isNaN(verifiedAt.getTime())) return false;
  const verifiedDate = batamParts(verifiedAt);
  const currentDate = batamParts(now);
  return verifiedDate.year === currentDate.year
    && verifiedDate.month === currentDate.month
    && verifiedDate.day === currentDate.day;
}

interface TerminalPhotoProps {
  media: TerminalMedia;
  terminalName: string;
}

const TerminalPhoto: React.FC<TerminalPhotoProps> = ({ media, terminalName }) => {
  const [failed, setFailed] = useState(false);

  return (
    <figure className={`terminal-card-media${failed ? ' is-unavailable' : ''}`}>
      {failed ? (
        <div
          className="terminal-card-photo-fallback"
          role="img"
          aria-label={`${terminalName} location photo unavailable`}
        >
          <ImageOff aria-hidden="true" size={ICON_SIZE.big} />
          <span>Location photo unavailable</span>
        </div>
      ) : (
        <img
          src={media.imageUrl}
          alt={media.alt}
          loading="lazy"
          decoding="async"
          referrerPolicy="no-referrer"
          onError={() => setFailed(true)}
        />
      )}
    </figure>
  );
};

const TerminalPhotoCredit: React.FC<{ media: TerminalMedia }> = ({ media }) => (
  <p className="terminal-card-photo-credit">
    <span>{media.context}, {media.capturedYear}.</span>
    <span>
      Photo by{' '}
      <a href={media.sourceUrl} target="_blank" rel="noreferrer">{media.author}</a>
      {' · '}
      <a
        className="terminal-card-photo-license"
        href={media.licenseUrl}
        target="_blank"
        rel="noreferrer"
      >
        {media.license}
      </a>
    </span>
  </p>
);


export const FerryPortTracker: React.FC<FerryPortTrackerProps> = ({
  ferries,
  dataSource,
  timetable,
  ports,
  portsLoading,
  portsError,
  isRefreshingOfficialSources,
  onRefreshOfficialSources,
}) => {
  const [selectedPort, setSelectedPort] = useState<string>('ALL');
  const [activeWorkspaceTab, setActiveWorkspaceTab] = useState<FerryWorkspaceTab>('terminals');
  const [refreshPending, setRefreshPending] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [refreshConfirmation, setRefreshConfirmation] = useState<string | null>(null);
  const refreshInFlight = useRef(false);
  const refreshBusy = refreshPending || isRefreshingOfficialSources;
  const verifiedToday = wasVerifiedToday(timetable.last_verified_at);

  const handleOfficialRefresh = async () => {
    if (refreshBusy || refreshInFlight.current) return;
    refreshInFlight.current = true;
    setRefreshPending(true);
    setRefreshError(null);
    setRefreshConfirmation(null);
    try {
      const report = await onRefreshOfficialSources();
      // The cooldown marks prior reports "cached", including failed checks.
      // Their retained outcome must not become a successful confirmation.
      if (report.status === 'failed_using_last_known_good'
        || (report.summary.failed > 0 && report.summary.verified === 0)) {
        setRefreshError('Schedules could not be refreshed. The last verified timetable is still shown.');
      } else if (report.status === 'partial' || report.summary.failed > 0) {
        setRefreshError('Some schedules could not be checked. The last verified timetable is still shown.');
      } else {
        setRefreshConfirmation(`${report.summary.verified} official sources checked.`);
      }
    } catch {
      setRefreshError('Schedules could not be refreshed. Please try again.');
    } finally {
      refreshInFlight.current = false;
      setRefreshPending(false);
    }
  };

  const filteredFerries = useMemo(
    () => ferries.filter(ferry => (
      selectedPort === 'ALL'
      || ferry.departure_port.toLowerCase().includes(selectedPort.toLowerCase())
    )),
    [ferries, selectedPort],
  );

  const filteredPorts = useMemo(
    () => ports.filter(port => (
      selectedPort === 'ALL'
      || port.port_name.toLowerCase().includes(selectedPort.toLowerCase())
    )),
    [ports, selectedPort],
  );

  return (
    <div className="app-screen-layout ferry-port-layout">
      <section className="glass-panel ui-white-card-hover ferry-port-overview" aria-labelledby="ferry-port-title">
        <div className="ferry-port-overview__header">
          <div className="ferry-port-overview__intro">
            <div className="ferry-port-overview__title-row">
              <Anchor aria-hidden="true" size={ICON_SIZE.big} />
              <h2 id="ferry-port-title" className="ferry-port-overview__title">
                Cross-Border Ferry &amp; Port Intelligence
              </h2>
            </div>
            <p className="ferry-port-overview__description">
              Terminal references and published departures across the Singapore–Batam strait.
            </p>
          </div>

          <div className="ferry-port-overview__controls" aria-label="Ferry data controls">
            <span className={`ferry-port-overview__verified-badge ${verifiedToday ? 'is-current' : 'is-stale'}`}>
              <ShieldCheck aria-hidden="true" size={ICON_SIZE.medium} /> Last verified{' '}
              <time dateTime={timetable.last_verified_at}>
                {formatScheduleVerifiedAt(timetable.last_verified_at)}
              </time>
            </span>

          </div>
        </div>

        {refreshError ? <p className="ferry-port-refresh-error" role="alert">{refreshError}</p> : null}
        {refreshConfirmation ? <p className="ferry-port-refresh-confirmation" role="status">{refreshConfirmation}</p> : null}

        <div className="ferry-port-overview__footer">
          <div className="ferry-port-filter-list" role="group" aria-label="Filter by departure port">
            {PORT_FILTERS.map(port => (
              <button
                key={port}
                type="button"
                aria-pressed={selectedPort === port}
                onClick={() => setSelectedPort(port)}
                className="ui-button-choice ui-sand-interactive"
              >
                {port === 'ALL' ? 'All' : port}
              </button>
            ))}
          </div>
          <button
            type="button"
            className="ui-button-primary ferry-port-overview__refresh-button"
            onClick={() => void handleOfficialRefresh()}
            disabled={refreshBusy}
            aria-busy={refreshBusy}
          >
            <RefreshCw
              aria-hidden="true"
              size={ICON_SIZE.large}
              className={refreshBusy ? 'ferry-port-spinner' : undefined}
            />
            {refreshBusy ? 'Checking Schedules…' : 'Check Schedules'}
          </button>
        </div>
      </section>

      {portsError ? (
        <div className="ferry-port-error" role="alert">
          <TriangleAlert aria-hidden="true" size={ICON_SIZE.medium} /> {portsError}
        </div>
      ) : null}

      <WorkspaceSubtabs<FerryWorkspaceTab>
        className="ferry-port-tabs"
        activeTab={activeWorkspaceTab}
        onActiveTabChange={setActiveWorkspaceTab}
        ariaLabel="Ferry and port workspace sections"
        idPrefix="ferry-workspace"
        tabs={[
          {
            id: 'terminals',
            label: 'Terminals',
            content: (
      <section className="terminal-status" aria-label="Terminal information and published departures" aria-busy={portsLoading}>
        <p className="terminal-availability-note">Published sailings. Live queues and berth occupancy are unavailable.</p>

        {portsLoading && ports.length === 0 ? (
          <div className="ferry-port-empty-state ferry-port-empty-state--terminals" role="status">
            Loading terminal references…
          </div>
        ) : filteredPorts.length > 0 ? (
          <div className="terminal-card-grid">
            {filteredPorts.map(port => {
              const media = TERMINAL_MEDIA[port.port_name];
              return (
                <article key={port.port_name} className="ui-white-card-hover terminal-intelligence-card terminal-card-content">
                  <header className="terminal-card-heading">
                    {media ? <TerminalPhoto media={media} terminalName={port.port_name} /> : null}
                    <div className="terminal-card-heading-copy">
                      <div className="terminal-card-heading-row">
                        <div>
                          <h4 className="terminal-card-title">{port.port_name}</h4>
                          <span className="terminal-card-code">Terminal {port.terminal_code}</span>
                        </div>
                        <span className="badge badge-neutral terminal-card-status">Timetable</span>
                      </div>
                    </div>
                  </header>

                  <div className="terminal-next-sailing">
                    <span className="terminal-next-sailing__label">Next departure</span>
                    {port.next_sailing_in_mins != null ? (
                      <>
                        <strong className="terminal-next-sailing__time">{relativeDeparture(port.next_sailing_in_mins)}</strong>
                        {port.next_operator || port.next_vessel ? <span>{port.next_operator ?? port.next_vessel}</span> : null}
                      </>
                    ) : <span>No published sailing in this window.</span>}
                  </div>
                  <div className="terminal-card-links">
                    <a
                      href={port.official_reference_url ?? media?.officialUrl}
                      target="_blank"
                      rel="noreferrer"
                    >
                      BP Batam Terminal Reference <ExternalLink aria-hidden="true" size={ICON_SIZE.small} />
                    </a>
                  </div>
                  {media ? <TerminalPhotoCredit media={media} /> : null}
                </article>
              );
            })}
          </div>
        ) : (
          <div className="ferry-port-empty-state" role="status">
            No terminal intelligence matches this port filter.
          </div>
        )}
      </section>
            ),
          },
          {
            id: 'departures',
            label: 'Departures',
            content: (
        <section className="glass-panel ferry-departures" aria-labelledby="sailings-title">
          <div className="ferry-departures__header">
            <h3 id="sailings-title" className="ferry-departures__title">
              <Ship aria-hidden="true" size={ICON_SIZE.large} color="var(--accent-cyan)" /> Upcoming Departures
            </h3>
            <span className="badge badge-neutral">{dataSource === 'offline' ? 'BUNDLED TIMETABLE' : 'PUBLISHED TIMETABLE'}</span>
            <span className="badge badge-neutral">{filteredFerries.length} shown</span>
          </div>

          {filteredFerries.length > 0 ? (
            <ul className="ferry-departures__list">
              {filteredFerries.map((ferry, index) => (
                <li key={ferry.sailing_id ?? `${ferry.ferry_name}-${ferry.departure_time}-${index}`}>
                  <article className="ferry-departure-card">
                    <div className="ferry-departure-card__details">
                      <div className="ferry-departure-card__operator-row">
                        <strong className="ferry-departure-card__operator">{ferry.operator ?? ferry.ferry_name}</strong>
                        <span className={`badge ${ferryBadgeClass(ferry.status)}`}>{prettyStatus(ferry.status)}</span>
                      </div>
                      <div className="ferry-departure-card__route">
                        <span>{ferry.departure_port} ➔ {ferry.arrival_port}</span>
                      </div>
                      <div className="ferry-departure-card__evidence">
                        {ferry.schedule_source_url ? (
                          <a
                            href={ferry.schedule_source_url}
                            target="_blank"
                            rel="noreferrer"
                            className="ferry-departure-card__source-link"
                          >
                            Official operator timetable
                          </a>
                        ) : null}
                      </div>
                    </div>

                    <div className="ferry-departure-card__timing">
                      <time className="ferry-departure-card__departure-time" dateTime={ferry.departure_time}>
                        {formatFerryTime(ferry.departure_time)}
                      </time>
                      <div className="ferry-departure-card__relative-time">{relativeDeparture(ferry.minutes_until_departure)}</div>
                      {ferry.arrival_time ? (
                        <div className="ferry-departure-card__arrival-time">
                          Est. arrival {formatFerryTime(ferry.arrival_time)}
                        </div>
                      ) : null}
                      {ferry.available_seats != null ? (
                        <div className="ferry-departure-card__seat-count">
                          {ferry.available_seats} seats reported
                        </div>
                      ) : null}
                    </div>
                  </article>
                </li>
              ))}
            </ul>
          ) : (
            <div className="ferry-port-empty-state ferry-port-empty-state--departures" role="status">
              No upcoming sailings match this departure-port filter.
            </div>
          )}
        </section>
            ),
          },
          {
            id: 'cargo',
            label: 'Cargo',
            content: (
        <section className="glass-panel cargo-monitor" aria-labelledby="cargo-title">
          <div className="cargo-monitor__header">
            <h3 id="cargo-title" className="cargo-monitor__title">
              <PackageCheck aria-hidden="true" size={ICON_SIZE.large} color="var(--accent-emerald)" /> Batam-SG Cargo Monitor
            </h3>
            <span className="badge badge-neutral cargo-monitor__dataset-badge">NO LIVE CARGO FEED</span>
          </div>
          <p className="cargo-monitor__description" role="status">
            Cargo tracking is unavailable because no shipment or customs feed is connected.
            {' '}There are no verified shipment positions, progress percentages or clearance events to display.
          </p>
        </section>
            ),
          },
        ]}
      />
    </div>
  );
};
