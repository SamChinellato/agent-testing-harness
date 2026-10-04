import type { Snapshot } from '../api/types';
import { count, percent } from '../format';

export function Readouts({ snapshot }: { snapshot: Snapshot }) {
  const t = snapshot.totals;
  const calls = snapshot.workers.reduce((n, w) => n + w.live.calls_per_minute, 0);
  const share = calls > 0 ? t.throttled_last_minute / calls : 0;

  return (
    <section className="readouts" aria-label="Summary">
      <div className="readout">
        <div className="readout__value">{count(t.tokens_per_minute)}</div>
        <div className="readout__label">Tokens per minute, all workers</div>
      </div>
      <div className="readout">
        <div className="readout__value" data-tone={t.throttled_last_minute > 0 ? 'throttle' : undefined}>
          {count(t.throttled_last_minute)}
        </div>
        <div className="readout__label">Throttled in the last minute</div>
        <div className="readout__note">{calls > 0 ? `${percent(share)} of ${count(calls)} calls` : 'No calls yet'}</div>
      </div>
      <div className="readout">
        <div className="readout__value">
          {t.running}
          <span style={{ fontSize: 28, color: 'var(--ink-3)' }}> / {t.workers}</span>
        </div>
        <div className="readout__label">Workers running</div>
      </div>
      <div className="readout">
        <div className="readout__value">{count(t.dropped_total)}</div>
        <div className="readout__label">Dropped iterations</div>
        <div className="readout__note">Offered load the system couldn’t take</div>
      </div>
    </section>
  );
}
