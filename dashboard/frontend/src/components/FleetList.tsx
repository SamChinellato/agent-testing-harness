import type { Snapshot } from '../api/types';
import { useLive } from '../state/store';
import { WorkerRow } from './WorkerRow';

export function FleetList({ snapshot }: { snapshot: Snapshot }) {
  const colorSlots = useLive((s) => s.colorSlots);
  if (snapshot.workers.length === 0) {
    return <p className="empty">No workers yet. Start one with <code>uv run agent</code>.</p>;
  }
  return (
    <div>
      {snapshot.workers.map((w) => (
        <WorkerRow key={w.worker_id} worker={w} snapshot={snapshot} slot={colorSlots[w.worker_id]} />
      ))}
    </div>
  );
}
