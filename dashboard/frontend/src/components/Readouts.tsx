import type { Snapshot } from '../api/types';
import { count } from '../format';

export function Readouts({ snapshot }: { snapshot: Snapshot }) {
  const t = snapshot.totals;
  return (
    <section className="readouts" aria-label="Summary">
      <div className="readout">
        <div className="readout__value">{count(t.tokens_per_minute)}</div>
        <div className="readout__label">Tokens / min</div>
      </div>
      <div className="readout">
        <div className="readout__value" data-tone={t.throttled_last_minute > 0 ? 'throttle' : undefined}>
          {count(t.throttled_last_minute)}
        </div>
        <div className="readout__label">Throttled</div>
      </div>
      <div className="readout">
        <div className="readout__value">
          {t.running}
          <span style={{ fontSize: 28, color: 'var(--ink-3)' }}> / {t.workers}</span>
        </div>
        <div className="readout__label">Running</div>
      </div>
      <div className="readout">
        <div className="readout__value">{count(t.dropped_total)}</div>
        <div className="readout__label">Dropped</div>
      </div>
    </section>
  );
}
