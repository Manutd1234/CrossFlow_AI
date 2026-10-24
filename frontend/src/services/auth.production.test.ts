/* @vitest-environment jsdom */
/* @vitest-environment-options { "url": "http://localhost:3000" } */

import { afterEach, describe, expect, it, vi } from 'vitest';

vi.stubEnv('DEV', false);
vi.stubEnv('VITE_TEST_ADMIN_EMAIL', 'production-test-admin@example.invalid');
vi.stubEnv('VITE_TEST_ADMIN_PASSWORD', 'non-secret-production-test-sentinel');

const { signInAsTestAdmin, testAdminConfigured, sessionGeneration } = await import('./auth');

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe('production test-admin boundary', () => {
  it('disables the shortcut and rejects before any sign-in request even when values are configured', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const generation = sessionGeneration();

    expect(import.meta.env.DEV).toBe(false);
    expect(testAdminConfigured()).toBe(false);
    await expect(signInAsTestAdmin()).rejects.toThrow('Test admin access is not configured');
    expect(fetchMock).not.toHaveBeenCalled();
    expect(sessionGeneration()).toBe(generation);
  });
});
