import { describe, expect, it } from 'vitest';
import { formatFerryTime, formatScheduleVerifiedAt, relativeDepartureFromTime } from './format';

describe('ferry terminal clocks', () => {
  it('makes a crossing from Singapore to Batam clear even when the arrival clock is earlier', () => {
    expect(formatFerryTime('2026-10-23T20:30:00+08:00')).toBe('20:30 SGT');
    expect(formatFerryTime('2026-10-23T20:20:00+07:00')).toBe('20:20 WIB');
    expect(formatFerryTime('06:00')).toBe('06:00');
  });
});

describe('source check timestamps in Batam time', () => {
  it('converts a UTC fetch timestamp instead of labelling its UTC clock as WIB', () => {
    expect(formatScheduleVerifiedAt('2026-10-23T09:44:00.000Z')).toBe('23 Oct 2026, 16:44 WIB');
  });

  it('keeps the published WIB date and handles date rollover from UTC', () => {
    expect(formatScheduleVerifiedAt('2026-08-13T00:30:00+07:00')).toBe('13 Aug 2026, 00:30 WIB');
    expect(formatScheduleVerifiedAt('2026-08-12T17:30:00Z')).toBe('13 Aug 2026, 00:30 WIB');
  });

  it('does not invent a timezone for missing or ambiguous timestamps', () => {
    expect(formatScheduleVerifiedAt()).toBe('verification date unavailable');
    expect(formatScheduleVerifiedAt('2026-08-13T00:30:00')).toBe('2026-08-13T00:30:00');
    expect(formatScheduleVerifiedAt('invalid')).toBe('invalid');
  });
});

describe('assigned sailing countdown', () => {
  it('uses the current clock rather than a countdown saved with an old route', () => {
    const departure = '2026-10-23T18:20:00+07:00';
    expect(relativeDepartureFromTime(departure, Date.parse('2026-10-23T16:47:00+07:00'))).toBe('in 1h 33m');
    expect(relativeDepartureFromTime(departure, Date.parse('2026-10-23T17:47:00+07:00'))).toBe('in 33 min');
  });

  it('marks a passed sailing as departed and rejects missing absolute time', () => {
    const departure = '2026-10-23T18:20:00+07:00';
    expect(relativeDepartureFromTime(departure, Date.parse(departure))).toBe('departing now');
    expect(relativeDepartureFromTime(departure, Date.parse(departure) + 1)).toBe('departed');
    expect(relativeDepartureFromTime('invalid')).toBe('');
  });
});
