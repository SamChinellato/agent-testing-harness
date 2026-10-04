import { describe, expect, it } from 'vitest';
import { buildLimits, buildLines, spreadLabels } from './Trace';
import { makeSnapshot, makeWorker } from '../test/fixtures';

describe('buildLines', () => {
  it('draws one line per slotted worker, an Other line, and an All-workers total', () => {
    const workers = [
      makeWorker({ worker_id: 'a', series: [1, 1] }),
      makeWorker({ worker_id: 'b', series: [2, 2] }),
      makeWorker({ worker_id: 'c', series: [4, 4] }),
    ];
    const snap = makeSnapshot(workers);
    snap.buckets = [0, 5];
    const lines = buildLines(snap, { a: 0, b: 1, c: -1 });
    const keys = lines.map((l) => l.key);
    expect(keys).toEqual(['a', 'b', '__other', '__total']);
    expect(lines.find((l) => l.key === '__other')?.values).toEqual([4, 4]);
    expect(lines.find((l) => l.key === '__total')?.values).toEqual([7, 7]);
  });
});

describe('buildLimits', () => {
  it('draws a shared budget once, labelled by team, and a worker budget per worker', () => {
    const shared = {
      budget: {
        budget_id: 'team:banking-ops', scope: 'team' as const, resolved_via: 'team' as const,
        tokens_per_minute: 3000, effective_tokens_per_minute: 2700, enforcement: 'client_bucket' as const,
        shared_by: 2, shared_tokens_per_minute: 0,
      },
    };
    const a = makeWorker({ worker_id: 'a', budget: shared.budget });
    const b = makeWorker({ worker_id: 'b', budget: shared.budget });
    const own = makeWorker({
      worker_id: 'c',
      budget: { ...shared.budget, budget_id: 'worker:c', scope: 'worker', resolved_via: 'worker_assignment', effective_tokens_per_minute: 500, shared_by: 1 },
    });
    const limits = buildLimits(makeSnapshot([a, b, own]), { a: 0, b: 1, c: 2 });
    expect(limits).toHaveLength(2);
    const teamLimit = limits.find((l) => l.key === 'team:banking-ops')!;
    expect(teamLimit.label).toBe('Team limit');
    expect(teamLimit.value).toBe(2700);
    expect(limits.find((l) => l.key === 'worker:c')!.label).toBe('c limit');
  });
});

describe('spreadLabels', () => {
  it('keeps every label at least the gap apart and never moves a label far from its point', () => {
    const out = spreadLabels([100, 102, 104, 300], 16);
    for (let i = 1; i < 3; i++) {
      expect(out[i] - out[i - 1]).toBeGreaterThanOrEqual(16);
    }
    expect(out[3]).toBe(300);
  });
});
