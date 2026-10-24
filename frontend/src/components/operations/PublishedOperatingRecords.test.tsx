/* @vitest-environment jsdom */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchAuthorityReports, type AuthorityReports } from '../../services/api';
import { PublishedOperatingRecords } from './PublishedOperatingRecords';

vi.mock('../../services/api', () => ({ fetchAuthorityReports: vi.fn() }));
let root: Root | undefined;

afterEach(() => {
  act(() => root?.unmount());
  root = undefined;
  document.body.replaceChildren();
  vi.resetAllMocks();
});

describe('published report English display', () => {
  it('shows English current/earlier periods, metrics and accessible shares while retaining source provenance', async () => {
    const report = {
      publisher: 'Badan Pengusahaan Batam',
      source_url: 'https://batamport.bpbatam.go.id/pelabuhan-penumpang/',
      period_label: 'Januari-Agustus2026', period_start: '2026-01', period_end: '2026-08',
      content_sha256: 'actual-source-hash',
      metrics: [
        { id: 'calls', label: 'KAPAL (CALL)', value: 99, unit: 'calls' },
        { id: 'domestic', label: 'PENUMPANG DOMESTIK', value: 100, unit: 'orang' },
        { id: 'international', label: 'PENUMPANG INTERNASIONAL', value: 300, unit: 'orang' },
      ],
    };
    const snapshot: AuthorityReports = {
      status: 'fresh', report, checked_at: '2026-10-24T10:00:00Z', fetched_at: '2026-10-24T10:00:00Z',
      stored_reports: [{ ...report, period_label: 'Januari-Juni 2026', period_end: '2026-06', content_sha256: 'older-hash' }],
      storage: { durable: true, scope: 'single_server' }, error: null,
    };
    const original = JSON.stringify(snapshot);
    vi.mocked(fetchAuthorityReports).mockResolvedValue(snapshot);
    const container = document.createElement('div'); document.body.append(container);
    root = createRoot(container);
    await act(async () => root!.render(<PublishedOperatingRecords />));

    expect([...container.querySelectorAll('h4')].map(heading => heading.textContent))
      .toEqual(['January–August 2026', 'January–June 2026']);
    expect(container.textContent).toContain('Passenger-vessel calls');
    expect(container.textContent).toContain('Domestic passenger movements');
    expect(container.textContent).toContain('International passenger movements');
    expect(container.textContent).toContain('100 people');
    expect(container.textContent).toContain('Badan Pengusahaan Batam');
    expect(container.textContent).not.toMatch(/Januari|Agustus|PENUMPANG|orang/);
    const accessibleShares = container.querySelector('.maritime-passenger-bar')?.getAttribute('aria-label');
    expect(accessibleShares).toContain('Domestic passenger movements: 100, 25.0%');
    expect(accessibleShares).toContain('International passenger movements: 300, 75.0%');
    expect(container.querySelector('a')?.getAttribute('href')).toBe(report.source_url);
    expect(JSON.stringify(snapshot)).toBe(original);
  });
});
