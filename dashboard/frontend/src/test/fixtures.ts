import type { Snapshot, WorkerSnapshot } from '../api/types';

export function makeWorker(overrides: Partial<WorkerSnapshot> & { worker_id: string }): WorkerSnapshot {
  const { worker_id, ...rest } = overrides;
  const base: WorkerSnapshot = {
    worker_id,
    team: 'banking-ops',
    display_name: worker_id,
    task_description: '',
    managed: false,
    pid: null,
    desired: { state: 'running', agent: 'triage', mode: 'paced', budget_id: null, generation: 2 },
    observed: {
      status: 'running', phase: 'running', applied_generation: 2, current_node: 'classify',
      iteration: 4, last_error: null, heartbeat_at: 0, lease_expires_at: 0,
    },
    budget: {
      budget_id: 'team:banking-ops', scope: 'team', resolved_via: 'team', tokens_per_minute: 3000,
      effective_tokens_per_minute: 2700, enforcement: 'client_bucket', shared_by: 1, shared_tokens_per_minute: 0,
    },
    live: {
      tokens_per_minute: 900, calls_per_minute: 3, throttled_last_minute: 0,
      latest_node: 'classify', latest_status: 200, latency_ms: 800,
    },
    dropped_total: 0,
    series: Array.from({ length: 12 }, () => 900),
    series_throttled: Array.from({ length: 12 }, () => 0),
  };
  return { ...base, ...rest } as WorkerSnapshot;
}

export function makeSnapshot(workers: WorkerSnapshot[], revision = 1): Snapshot {
  return {
    revision,
    timestamp: 1_700_000_000,
    buckets: Array.from({ length: 12 }, (_, i) => 1_700_000_000 - (11 - i) * 5),
    workers,
    budgets: [],
    teams: ['banking-ops'],
    totals: {
      workers: workers.length,
      running: workers.filter((w) => w.desired.state === 'running').length,
      tokens_per_minute: workers.reduce((n, w) => n + w.live.tokens_per_minute, 0),
      throttled_last_minute: workers.reduce((n, w) => n + w.live.throttled_last_minute, 0),
      dropped_total: 0,
    },
  };
}
