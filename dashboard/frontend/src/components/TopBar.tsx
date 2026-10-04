import { useState } from 'react';
import { Button, Label } from '@patternfly/react-core';
import { api, ApiError } from '../api/client';
import type { Snapshot } from '../api/types';
import { useLive, type Connection } from '../state/store';

const CONNECTION: Record<Connection, { text: string; colour: 'green' | 'orange' | 'red' | 'grey' }> = {
  connecting: { text: 'Connecting', colour: 'grey' },
  live: { text: 'Live', colour: 'green' },
  reconnecting: { text: 'Reconnecting', colour: 'orange' },
  offline: { text: 'Offline', colour: 'red' },
};

export function TopBar({ snapshot }: { snapshot: Snapshot | null }) {
  const connection = useLive((s) => s.connection);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const running = snapshot?.workers.filter((w) => w.desired.state === 'running') ?? [];
  const link = CONNECTION[connection];

  async function stopAll() {
    setBusy(true);
    setError(null);
    const results = await Promise.allSettled(running.map((w) => api.setState(w.worker_id, 'stopped')));
    const failed = results.filter((r): r is PromiseRejectedResult => r.status === 'rejected');
    if (failed.length > 0) {
      const reason = failed[0].reason;
      setError(`Couldn't stop ${failed.length}: ${reason instanceof ApiError ? reason.message : String(reason)}`);
    }
    setBusy(false);
  }

  return (
    <header className="topbar">
      <h1 className="wordmark">Agent bench</h1>
      <div className="topbar__right">
        <Label color={link.colour} isCompact role="status" aria-live="polite">
          {link.text}
        </Label>
        <Button variant="secondary" isDisabled={busy || running.length === 0} onClick={() => void stopAll()}>
          Stop all
        </Button>
      </div>
      {error && (
        <p role="alert" className="form-error" style={{ flexBasis: '100%', margin: 0, color: 'var(--throttle)' }}>
          {error}
        </p>
      )}
    </header>
  );
}
