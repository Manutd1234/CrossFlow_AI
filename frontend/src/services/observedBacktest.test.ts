import { afterEach, describe, expect, it, vi } from 'vitest';
import { observedResult } from '../test/fixtures/observedBacktest';
import { fetchModelStatus, requestObservedBacktest } from './api';
import { BUNDLED_RF_VALIDATION } from '../data/modelManifest';

function payload(evaluated = false) {
  const result = observedResult(evaluated);
  return { ...result.data, generated_at: result.fetchedAt, data_source: 'observed_chronological_backtest' };
}
function respond(value: unknown, status = 200, headers: Record<string, string> = {}) {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json', ...headers } }));
  vi.stubGlobal('fetch', fetcher); return fetcher;
}
afterEach(() => vi.unstubAllGlobals());

describe('observed-only backtest API boundary', () => {
  it('sends only the operator bearer and an empty policy-free request', async () => {
    const body = payload(); const fetcher = respond(body);
    expect((await requestObservedBacktest('operator-session')).data).toEqual(observedResult().data);
    const [url, init] = fetcher.mock.calls[0];
    expect(url).toContain('/api/analytics/backtest'); expect(init.method).toBe('POST'); expect(init.body).toBe('{}');
    expect(init.headers).toEqual({ 'Content-Type': 'application/json', Authorization: 'Bearer operator-session' });
    expect(init.cache).toBe('no-store');
  });
  it('preserves actual candidate/baseline errors and negative bias', async () => {
    const body = payload(true); respond(body);
    expect((await requestObservedBacktest(null)).data.backtest).toEqual(body.backtest);
  });
  it.each([401, 403, 409])('propagates authoritative HTTP %s without a synthetic fallback', async status => {
    respond({ detail: 'Observed evaluation rejected' }, status);
    await expect(requestObservedBacktest('operator-session')).rejects.toMatchObject({ status, message: 'Observed evaluation rejected' });
  });
  it('shows the retry delay for a busy evaluation and never disguises it as insufficient data', async () => {
    respond({ detail: 'Evaluation already running' }, 429, { 'Retry-After': '5' });
    await expect(requestObservedBacktest(null)).rejects.toMatchObject({ status: 429, message: 'Evaluation already running Try again in 5 seconds.' });
  });
  it('distinguishes network/non-JSON outages from a legitimate empty observed archive', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));
    await expect(requestObservedBacktest(null)).rejects.toMatchObject({ status: 503 });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('upstream unavailable', { status: 503 })));
    await expect(requestObservedBacktest(null)).rejects.toThrow('temporarily unavailable');
    respond(payload()); expect((await requestObservedBacktest(null)).data.backtest.status).toBe('insufficient_observations');
  });
  it.each(['validation_scope', 'evaluation_subject', 'running_model_evaluated'])('rejects an incorrect evaluation identity in %s', async field => {
    const body = payload(); const broken = { ...body.backtest, [field]: field === 'running_model_evaluated' ? true : 'synthetic_holdout' };
    respond({ ...body, backtest: broken });
    await expect(requestObservedBacktest(null)).rejects.toThrow('No synthetic evaluation was substituted');
  });
  it.each(['fixture_only', 'not_real_accuracy_evidence'])('rejects explicitly non-field evidence marked %s', async flag => {
    const body = payload(true); respond({ ...body, backtest: { ...body.backtest, [flag]: true } });
    await expect(requestObservedBacktest(null)).rejects.toThrow('No synthetic evaluation was substituted');
  });
  it('rejects incomplete/null report structures and invalid candidate errors', async () => {
    const body = payload(true); body.backtest.horizons[0].serving_model.mae_points = -1; respond(body);
    await expect(requestObservedBacktest(null)).rejects.toMatchObject({ status: 503 });
    respond({ backtest: null, evaluation_snapshot: {} });
    await expect(requestObservedBacktest(null)).rejects.toMatchObject({ status: 503 });
  });
  it('preserves an activated model’s recorded observed chronology and rejects partial row metadata', async () => {
    const report = payload(true).backtest;
    report.status = 'activated'; report.model_replaced = true; report.activation.eligible = true;
    respond({ metrics: { ...BUNDLED_RF_VALIDATION, observed_training: report }, generated_at: report.generated_at });
    expect((await fetchModelStatus()).data).toMatchObject({ observed_training: report });
    respond({ metrics: { ...BUNDLED_RF_VALIDATION, observed_fit_rows: 'not-a-count' } });
    expect((await fetchModelStatus()).source).toBe('offline');
    respond({ metrics: { ...BUNDLED_RF_VALIDATION, observed_training: { status: 'activated' } } });
    expect((await fetchModelStatus()).source).toBe('offline');
  });
});
