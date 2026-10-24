/** Display-only translations for known report fields; source records stay intact.
 *
 * These helpers are deliberately not a general translator. Do not apply them
 * to place, street, operator, publisher, author or official agency names.
 */
const MONTHS = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
] as const;

const INDONESIAN_MONTHS: Readonly<Record<string, string>> = {
  januari: 'January', februari: 'February', maret: 'March', april: 'April',
  mei: 'May', juni: 'June', juli: 'July', agustus: 'August', september: 'September',
  oktober: 'October', november: 'November', desember: 'December',
  agu: 'Aug', agt: 'Aug', okt: 'Oct', des: 'Dec',
};
const MONTH_PATTERN = /\b(Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember|Agu|Agt|Okt|Des)(?=$|[^A-Za-z])/gi;

function reportingMonth(value?: string): { month: string; year: number; ordinal: number } | null {
  const parsed = value?.match(/^(\d{4})-(0[1-9]|1[0-2])$/);
  if (!parsed) return null;
  const year = Number(parsed[1]);
  const index = Number(parsed[2]) - 1;
  return { month: MONTHS[index], year, ordinal: year * 12 + index };
}

/** An English reporting-period heading; never derives a date from today's clock. */
export function englishReportingPeriod(label: string, start?: string, end?: string): string {
  const translated = label.replace(MONTH_PATTERN, month => INDONESIAN_MONTHS[month.toLowerCase()]);
  const months = '(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)';
  const knownPeriod = new RegExp(`^\\s*${months}(?:\\s*(?:[-–—]|s\\.?d\\.?|to)\\s*${months})?\\s*\\d{4}\\s*$`, 'i').test(translated);
  // Structured period fields are authoritative only for a recognized month
  // heading. Preserve unrelated source captions rather than guessing meaning.
  if (knownPeriod || !label.trim()) {
    const first = reportingMonth(start);
    const last = reportingMonth(end);
    if (first && last && first.ordinal <= last.ordinal) {
      if (first.ordinal === last.ordinal) return `${first.month} ${first.year}`;
      return first.year === last.year
        ? `${first.month}–${last.month} ${last.year}`
        : `${first.month} ${first.year}–${last.month} ${last.year}`;
    }
  }
  if (!knownPeriod && translated === label) return label;
  return translated
    .replace(/\s*[-–—]\s*(?=[A-Za-z])/g, '–')
    .replace(/([A-Za-z])(\d{4})\b/g, '$1 $2');
}

const SOURCE_TEXT: Readonly<Record<string, string>> = {
  'kinerja operasional': 'Operating performance',
  'kapal (call)': 'Passenger-vessel calls',
  'kapal(call)': 'Passenger-vessel calls',
  'penumpang domestik': 'Domestic passenger movements',
  'penumpang internasional': 'International passenger movements',
  'jumlah penumpang': 'Passenger count',
  'jumlah kendaraan': 'Vehicle count',
  'volume lalu lintas': 'Traffic volume',
  'arus lalu lintas': 'Traffic flow',
  'kecepatan rata-rata': 'Average speed',
  'derajat kejenuhan': 'Degree of saturation',
  'panjang antrean': 'Queue length',
  'panjang antrian': 'Queue length',
  'waktu tempuh': 'Travel time',
  'kondisi lalu lintas': 'Traffic conditions',
  'lancar': 'Smooth',
  'sedang': 'Moderate',
  'padat': 'Heavy',
  'sangat padat': 'Very heavy',
  'macet': 'Congested',
  'terjadwal': 'Scheduled',
  'tertunda': 'Delayed',
  'dibatalkan': 'Cancelled',
  'tidak tersedia': 'Unavailable',
  'belum diamati': 'Not observed',
};

/** Exact known generic report/status phrases; unknown names remain untouched. */
export function englishSourceText(value: string): string {
  return SOURCE_TEXT[value.trim().replace(/\s+/g, ' ').toLowerCase()] ?? value;
}

const SOURCE_UNITS: Readonly<Record<string, string>> = {
  orang: 'people', penumpang: 'people', call: 'calls', panggilan: 'calls',
  kapal: 'vessels', kendaraan: 'vehicles', 'kendaraan/jam': 'vehicles/hour',
  'kend/jam': 'vehicles/hour', 'smp/jam': 'PCU/hour', 'km/jam': 'km/h',
  detik: 'seconds', menit: 'minutes', jam: 'hours', hari: 'days',
};

/** Explicit display units only; numerical values and source units are retained. */
export function englishSourceUnit(value: string): string {
  return SOURCE_UNITS[value.trim().toLowerCase()] ?? value;
}
