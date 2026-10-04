import { useState } from 'react';
import { api, ApiError } from '../api/client';
import type { Snapshot, WorkerSnapshot } from '../api/types';
import { count, ago } from '../format';
import { BudgetEditor } from './BudgetEditor';
import { Sparkline } from './Sparkline';
import { StatusMark, ThrottleTag } from './StatusMark';
import { colourFor } from './Trace';

const MODE_LABEL: Record<string, string> = {
  naive: 'Naive retry',
  paced: 'Paced',
  runaway: 'Ignores limits',
};

export function WorkerRow({
  worker,
  snapshot,
  slot,
}: {
  worker: WorkerSnapshot;
  snapshot: Snapshot;
  slot: number | undefined;
}) {
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: 'error' | 'ok'; text: string } | null>(null);

  const colour = colourFor(slot);
  const running = worker.desired.state === 'running';
  const throttled = worker.live.throttled_last_minute;
  const status = worker.observed.status;
  const budget = worker.budget.effective_tokens_per_minute;
  // A shared budget is one limit for several workers, so the gauge measures the group.
  const shared = worker.budget.shared_by > 1;
  const current = worker.live.tokens_per_minute;
  const ratio = budget ? current / budget : 0;
  const over = budget !== null && current > budget;
  const managed = worker.managed;

  async function run(label: string, action: () => Promise<unknown>, okText?: string) {
    setBusy(true);
    setMessage(null);
    try {
      await action();
      if (okText) setMessage({ tone: 'ok', text: okText });
    } catch (e) {
      setMessage({ tone: 'error', text: `${label} failed: ${e instanceof ApiError ? e.message : String(e)}` });
    } finally {
      setBusy(false);
    }
  }

  const stateButton = running ? (
    <button className="button" disabled={busy} onClick={() => run('Stop', () => api.setState(worker.worker_id, 'stopped'))}>
      Stop
    </button>
  ) : (
    <button
      className="button button--action"
      disabled={busy}
      onClick={() => run('Start', () => api.setState(worker.worker_id, 'running'))}
    >
      Start
    </button>
  );

  return (
    <article
      className="worker"
      style={{ ['--worker-colour' as string]: slot === undefined || slot < 0 ? 'transparent' : colour }}
      aria-label={worker.display_name}
    >
      <div className="worker__main">
        <div className="worker__identity">
          <div className="worker__name">{worker.display_name}</div>
          <div className="worker__meta">
            {worker.team} · {worker.desired.agent} · {MODE_LABEL[worker.desired.mode] ?? worker.desired.mode}
          </div>
          <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 4 }}>
            <StatusMark status={status} />
            {throttled > 0 && running && <ThrottleTag count={throttled} />}
          </div>
        </div>

        {shared ? (
          <div className="gauge">
            <div className="gauge__figures">
              <span>
                <strong style={{ fontWeight: 500 }}>{count(worker.live.tokens_per_minute)}</strong> per min
              </span>
              <span style={{ color: 'var(--ink-2)' }}>own share</span>
            </div>
            <div className="gauge__note">Shares the team limit</div>
          </div>
        ) : (
        <div className="gauge" aria-label={`Tokens per minute against the limit for ${worker.display_name}`}>
          <div className="gauge__figures">
            <span>
              <strong style={{ fontWeight: 500 }}>{count(current)}</strong> per min
            </span>
            <span style={{ color: 'var(--ink-2)' }}>
              of {budget === null ? 'no limit' : count(budget)}
            </span>
          </div>
          <div className="gauge__track" role="presentation">
            <div
              className="gauge__fill"
              data-over={over ? 'true' : 'false'}
              style={{ width: `${Math.min(100, ratio * 100).toFixed(1)}%` }}
            />
            {budget !== null && <div className="gauge__ceiling" style={{ left: '100%' }} />}
          </div>
          <div className="gauge__note" data-over={over ? 'true' : 'false'}>
            {budget === null
              ? 'No limit applied'
              : over
                ? `Over the limit by ${count(current - budget)} per minute`
                : `${Math.round(ratio * 100)}% of the limit`}
          </div>
        </div>

        )}

        <div className="worker__latest">
          {worker.live.latest_node ? (
            <>
              Latest step <strong>{worker.live.latest_node}</strong>
              <br />
              {worker.live.latest_status ?? '—'} · {count(worker.live.latency_ms)} ms
            </>
          ) : (
            'No calls yet'
          )}
          {worker.dropped_total > 0 && (
            <>
              <br />
              {count(worker.dropped_total)} dropped
            </>
          )}
        </div>

        <Sparkline
          values={worker.series}
          throttled={worker.series_throttled}
          colour={slot === undefined || slot < 0 ? 'var(--series-other)' : colour}
          label={`Tokens per minute over the last minute for ${worker.display_name}`}
        />
      </div>

      <div className="worker__controls">
        {stateButton}
        {running && (
          <button className="button" disabled={busy} onClick={() => run('Interrupt', () => api.interrupt(worker.worker_id), 'Interrupt sent. The current call will be cut short.')}>
            Interrupt
          </button>
        )}
        <label className="field" style={{ flexDirection: 'row', alignItems: 'center', gap: 6 }}>
          <span className="field__label">Mode</span>
          <select
            value={worker.desired.mode}
            disabled={busy}
            onChange={(e) => run('Mode change', () => api.setMode(worker.worker_id, e.target.value))}
          >
            <option value="naive">Naive retry</option>
            <option value="paced">Paced</option>
            <option value="runaway">Ignores limits</option>
          </select>
        </label>
        <button
          className="button button--quiet"
          aria-expanded={editing}
          aria-controls={`editor-${worker.worker_id}`}
          onClick={() => setEditing((v) => !v)}
        >
          {editing ? 'Close budget' : 'Budget'}
        </button>
        {managed && (
          <button className="button button--quiet" disabled={busy} onClick={() => run('Kill', () => api.kill(worker.worker_id), 'Process stopped.')}>
            Kill process
          </button>
        )}
        {(worker.observed.status === 'stopped' || worker.observed.status === 'lost') && (
          <button className="button button--quiet" disabled={busy} onClick={() => run('Forget', () => api.forget(worker.worker_id))}>
            Forget
          </button>
        )}
      </div>

      {worker.observed.last_error && (status === 'errored' || status === 'lost') && (
        <p className="worker__error">{worker.observed.last_error}</p>
      )}
      {message && (
        <p className="notice" data-tone={message.tone} role={message.tone === 'error' ? 'alert' : 'status'}>
          {message.text}
        </p>
      )}
      {!running && status !== 'pending' && worker.observed.heartbeat_at && (
        <p className="notice" data-tone="ok">
          Last seen {ago(Date.now() / 1000 - worker.observed.heartbeat_at)}
        </p>
      )}

      <div id={`editor-${worker.worker_id}`}>
        <BudgetEditor worker={worker} snapshot={snapshot} open={editing} />
      </div>
    </article>
  );
}
