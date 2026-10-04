import type { Snapshot, WorkerSnapshot } from '../api/types';
import { count } from '../format';
import { useLive } from '../state/store';
import { WorkerRow } from './WorkerRow';

/** One gauge per shared budget, above the rows. The group is measured once here,
 *  not repeated on every worker it applies to. */
function SharedBudgets({ workers }: { workers: WorkerSnapshot[] }) {
  const seen = new Map<string, WorkerSnapshot>();
  for (const w of workers) {
    if (w.budget.shared_by > 1 && w.budget.effective_tokens_per_minute !== null && !seen.has(w.budget.budget_id)) {
      seen.set(w.budget.budget_id, w);
    }
  }
  if (seen.size === 0) return null;
  return (
    <div className="shared" role="region" aria-label="Shared limits">
      {[...seen.values()].map((w) => {
        const limit = w.budget.effective_tokens_per_minute as number;
        const total = w.budget.shared_tokens_per_minute;
        const over = total > limit;
        const ratio = limit > 0 ? total / limit : 0;
        const title = w.budget.scope === 'global' ? 'Global limit' : `Team ${w.team} limit`;
        return (
          <div className="shared__item" key={w.budget.budget_id}>
            <div className="gauge__figures">
              <span>
                <strong style={{ fontWeight: 500 }}>{title}</strong>
              </span>
              <span style={{ color: 'var(--ink-2)' }}>{count(limit)} per min</span>
            </div>
            <div className="gauge__track" role="presentation">
              <div className="gauge__fill" data-over={over ? 'true' : 'false'} style={{ width: `${Math.min(100, ratio * 100).toFixed(1)}%` }} />
            </div>
            <div className="gauge__note" data-over={over ? 'true' : 'false'}>
              {count(total)} per min across {w.budget.shared_by} workers
              {over ? ` · over by ${count(total - limit)}` : ` · ${Math.round(ratio * 100)}% of the limit`}
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function FleetList({ snapshot }: { snapshot: Snapshot }) {
  const colorSlots = useLive((s) => s.colorSlots);
  if (snapshot.workers.length === 0) {
    return <p className="log__empty">No workers registered. Start one with uv run agent, or launch a scenario.</p>;
  }
  return (
    <>
      <SharedBudgets workers={snapshot.workers} />
      <div className="fleet">
        {snapshot.workers.map((w) => (
          <WorkerRow key={w.worker_id} worker={w} snapshot={snapshot} slot={colorSlots[w.worker_id]} />
        ))}
      </div>
    </>
  );
}
