import { useState } from 'react';
import { api, ApiError } from '../api/client';
import type { Snapshot } from '../api/types';
import { useLive, type Connection } from '../state/store';

const CONNECTION_TEXT: Record<Connection, string> = {
  connecting: 'Connecting',
  live: 'Live',
  reconnecting: 'Reconnecting…',
  offline: 'Offline. Last update was over 5 seconds ago.',
};

export function TopBar({ snapshot }: { snapshot: Snapshot | null }) {
  const connection = useLive((s) => s.connection);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const running = snapshot?.workers.filter((w) => w.desired.state === 'running') ?? [];

  async function stopAll() {
    setBusy(true);
    setError(null);
    const results = await Promise.allSettled(running.map((w) => api.setState(w.worker_id, 'stopped')));
    const failed = results.filter((r) => r.status === 'rejected');
    if (failed.length > 0) {
      const first = failed[0] as PromiseRejectedResult;
      setError(`Couldn't stop ${failed.length} worker${failed.length === 1 ? '' : 's'}: ${first.reason instanceof ApiError ? first.reason.message : String(first.reason)}`);
    }
    setBusy(false);
  }

  return (
    <header className="topbar">
      <div>
        <h1 className="wordmark">Agent bench</h1>
      </div>
      <div className="topbar__right">
        <span className="connection" data-state={connection} role="status" aria-live="polite">
          <span className="connection__dot" aria-hidden="true" />
          {CONNECTION_TEXT[connection]}
        </span>
        <button className="button" disabled={busy || running.length === 0} onClick={() => void stopAll()}>
          Stop all{running.length > 0 ? ` (${running.length})` : ''}
        </button>
      </div>
      {error && (
        <p className="form-error" role="alert" style={{ flexBasis: '100%' }}>
          {error}
        </p>
      )}
    </header>
  );
}
