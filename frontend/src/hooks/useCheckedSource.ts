import { useCallback, useEffect, useState } from 'react';

/** Refresh actual sources; preserve a prior snapshot as stale on failure. */
export function useCheckedSource<T>(request: () => Promise<T>) {
  const [snapshot, setSnapshot] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checkedAt, setCheckedAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision(value => value + 1), []);
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      try {
        const response = await request();
        if (cancelled) return;
        setSnapshot(response);
        setError(null);
      } catch (caught) {
        if (!cancelled) setError(caught instanceof Error ? caught.message : 'The source could not be checked.');
      } finally {
        if (!cancelled) { setCheckedAt(new Date().toISOString()); setLoading(false); }
      }
    };
    void load();
    return () => { cancelled = true; };
  }, [request, revision]);
  useEffect(() => {
    const checkWhenVisible = () => { if (document.visibilityState !== 'hidden') refresh(); };
    const timer = window.setInterval(checkWhenVisible, 60_000);
    window.addEventListener('focus', checkWhenVisible);
    document.addEventListener('visibilitychange', checkWhenVisible);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('focus', checkWhenVisible);
      document.removeEventListener('visibilitychange', checkWhenVisible);
    };
  }, [refresh]);
  return { snapshot, error, checkedAt, loading, refresh };
}
