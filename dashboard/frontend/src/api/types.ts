// Mirrors maasagentharness/dashboard/snapshot.py. Keep the two in step.

export type WorkerStatus =
  | 'running'
  | 'stopped'
  | 'starting'
  | 'draining'
  | 'pending'
  | 'unknown'
  | 'lost'
  | 'errored';

export type Mode = 'naive' | 'paced' | 'runaway';
export type Enforcement = 'off' | 'client_bucket' | 'shadow';
export type Scope = 'global' | 'team' | 'worker';
export type ResolvedVia = 'worker_assignment' | 'team' | 'global' | 'default';

export interface WorkerSnapshot {
  worker_id: string;
  team: string;
  display_name: string;
  task_description: string;
  managed: boolean;
  pid: number | null;
  desired: {
    state: 'running' | 'stopped';
    agent: string;
    mode: Mode;
    budget_id: string | null;
    generation: number;
  };
  observed: {
    status: WorkerStatus;
    phase: string;
    applied_generation: number;
    current_node: string | null;
    iteration: number;
    last_error: string | null;
    heartbeat_at: number | null;
    lease_expires_at: number | null;
  };
  budget: {
    budget_id: string;
    scope: Scope;
    resolved_via: ResolvedVia;
    tokens_per_minute: number | null;
    effective_tokens_per_minute: number | null;
    enforcement: Enforcement;
    shared_by: number;
    shared_tokens_per_minute: number;
  };
  live: {
    tokens_per_minute: number;
    calls_per_minute: number;
    throttled_last_minute: number;
    latest_node: string | null;
    latest_status: number | null;
    latency_ms: number | null;
  };
  dropped_total: number;
  series: number[];
  series_throttled: number[];
}

export interface BudgetView {
  budget_id: string;
  scope: Scope;
  scope_ref: string | null;
  tokens_per_minute: number | null;
  requests_per_minute: number | null;
  max_concurrency: number | null;
  headroom: number;
  enforcement: Enforcement;
  effective_tokens_per_minute: number | null;
  effective_requests_per_minute: number | null;
  updated_at: number;
}

export interface Snapshot {
  revision: number;
  timestamp: number;
  buckets: number[];
  workers: WorkerSnapshot[];
  budgets: BudgetView[];
  teams: string[];
  totals: {
    workers: number;
    running: number;
    tokens_per_minute: number;
    throttled_last_minute: number;
    dropped_total: number;
  };
}

export interface LogEvent {
  seq: number;
  ts: string;
  ts_ingest_ms: number;
  user: string;
  team: string;
  agent: string;
  mode: string;
  run_id: string;
  node: string;
  status: number;
  prompt_tokens: number;
  completion_tokens: number;
  latency_ms: number;
  attempt: number;
}

export interface ScenarioFile {
  path: string;
  file: string;
  run: string | null;
  valid: boolean;
  error: string | null;
  scenarios: { name: string; agent: string; mode: string; executor: string; budget: string | null }[];
}
