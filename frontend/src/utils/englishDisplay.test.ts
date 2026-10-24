import { describe, expect, it } from 'vitest';
import { englishReportingPeriod, englishSourceText, englishSourceUnit } from './englishDisplay';

describe('English display for trusted report fields', () => {
  it('formats the actual source reporting bounds in English without changing source records', () => {
    const source = Object.freeze({ label: 'Januari-Agustus2026', start: '2026-01', end: '2026-08' });
    expect(englishReportingPeriod(source.label, source.start, source.end)).toBe('January–August 2026');
    expect(source.label).toBe('Januari-Agustus2026');
    expect(englishReportingPeriod('Januari 2026', '2026-01', '2026-01')).toBe('January 2026');
    expect(englishReportingPeriod('', '2025-12', '2026-01')).toBe('December 2025–January 2026');
  });

  it.each([
    ['Januari', 'January'], ['Februari', 'February'], ['Maret', 'March'], ['April', 'April'],
    ['Mei', 'May'], ['Juni', 'June'], ['Juli', 'July'], ['Agustus', 'August'],
    ['September', 'September'], ['Oktober', 'October'], ['November', 'November'], ['Desember', 'December'],
  ])('displays %s in English even without structured dates', (source, english) => {
    expect(englishReportingPeriod(`${source}2026`)).toBe(`${english} 2026`);
  });

  it('handles lowercase, abbreviated and cross-year headings without inferring missing periods', () => {
    expect(englishReportingPeriod('januari — agustus 2026')).toBe('January–August 2026');
    expect(englishReportingPeriod('Jan-Agu 2026')).toBe('Jan–Aug 2026');
    expect(englishReportingPeriod('Desember 2025-Januari 2026')).toBe('December 2025–January 2026');
    expect(englishReportingPeriod('Source period A', '2026-01', '2026-08')).toBe('Source period A');
    expect(englishReportingPeriod('BP-Batam official report')).toBe('BP-Batam official report');
    expect(englishReportingPeriod('Januari-Agustus 2026', '2026-13', '2026-99')).toBe('January–August 2026');
  });

  it('translates only exact known generic labels, statuses and units', () => {
    expect(englishSourceText('PENUMPANG DOMESTIK')).toBe('Domestic passenger movements');
    expect(englishSourceText('  Penumpang   Internasional  ')).toBe('International passenger movements');
    expect(englishSourceText('KAPAL (CALL)')).toBe('Passenger-vessel calls');
    expect(englishSourceText('Tidak tersedia')).toBe('Unavailable');
    expect(englishSourceUnit('orang')).toBe('people');
    expect(englishSourceUnit('kendaraan/jam')).toBe('vehicles/hour');
    expect(englishSourceUnit('km/jam')).toBe('km/h');
  });

  it('preserves proper place, road, operator and official agency names', () => {
    for (const name of ['Jalan Hang Tuah', 'Simpang Jam', 'Pelabuhan Sekupang',
      'Badan Pengusahaan Batam', 'PT Pelayaran Sindo', 'Juliatin Ferry', 'Unknown source caption']) {
      expect(englishSourceText(name)).toBe(name);
      expect(englishSourceUnit(name)).toBe(name);
    }
  });
});
