import type { ReactNode } from 'react';

export function PublishedTrafficHistory({ modelledContent }: { modelledContent?: ReactNode }) {
  return <section className="operations-history-workspace" aria-label="Modelled traffic-flow analysis">
    <div className="operations-history-intro">
      <h3>Modelled traffic-flow analysis</h3>
      <p>Explore possible traffic patterns by corridor and time window. These planning scenarios are separate from live traffic and measured history.</p>
    </div>
    {modelledContent ?? <p role="status">Traffic-flow analysis is unavailable without corridor definitions.</p>}
  </section>;
}
