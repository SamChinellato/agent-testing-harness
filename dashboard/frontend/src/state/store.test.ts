import { describe, expect, it } from 'vitest';
import type { LogEvent } from '../api/types';
import { SERIES_SLOTS, mergeEvents, MAX_LOG_EVENTS, nextSnapshotState } from './store';
import { makeSnapshot, makeWorker } from '../test/fixtures';

describe('nextSnapshotState', () => {
  it('replaces the snapshot and ignores an older or repeated revision', () => {
    const first = makeSnapshot([makeWorker({ worker_id: 'a' })], 5);
    const start = nextSnapshotState({ snapshot: null, colorSlots: {} }, first)!;

    expect(nextSnapshotState(start, makeSnapshot([], 4))).toBeNull();
    expect(nextSnapshotState(start, makeSnapshot([], 5))).toBeNull();
    const newer = nextSnapshotState(start, makeSnapshot([], 6));
    expect(newer?.snapshot.revision).toBe(6);
  });

  it('gives each worker a colour slot in order of first appearance', () => {
    const snap = makeSnapshot([makeWorker({ worker_id: 'a' }), makeWorker({ worker_id: 'b' })]);
    const next = nextSnapshotState({ snapshot: null, colorSlots: {} }, snap)!;
    expect(next.colorSlots).toEqual({ a: 0, b: 1 });
  });

  it('never repaints a surviving worker when another leaves', () => {
    const before = nextSnapshotState(
      { snapshot: null, colorSlots: {} },
      makeSnapshot([makeWorker({ worker_id: 'a' }), makeWorker({ worker_id: 'b' }), makeWorker({ worker_id: 'c' })], 1),
    )!;
    const after = nextSnapshotState(before, makeSnapshot([makeWorker({ worker_id: 'c' })], 2))!;
    expect(after.colorSlots.c).toBe(before.colorSlots.c);
  });

  it('folds workers past the fourth into "Other" (-1) instead of inventing a colour', () => {
    const workers = Array.from({ length: SERIES_SLOTS + 2 }, (_, i) => makeWorker({ worker_id: `w${i}` }));
    const next = nextSnapshotState({ snapshot: null, colorSlots: {} }, makeSnapshot(workers))!;
    const slots = Object.values(next.colorSlots);
    expect(slots.filter((s) => s === -1)).toHaveLength(2);
    expect(Math.max(...slots)).toBeLessThan(SERIES_SLOTS);
  });
});

function ev(seq: number, node = 'n'): LogEvent {
  return {
    seq, ts: '', ts_ingest_ms: seq, user: 'u', team: 't', agent: 'triage', mode: 'naive', run_id: 'r',
    node, status: 200, prompt_tokens: 1, completion_tokens: 1, latency_ms: 1, attempt: 1,
  };
}

describe('mergeEvents', () => {
  it('dedupes by seq and keeps order', () => {
    const merged = mergeEvents([ev(1), ev(2)], [ev(2), ev(3)]);
    expect(merged.map((e) => e.seq)).toEqual([1, 2, 3]);
  });

  it('keeps only the newest MAX_LOG_EVENTS', () => {
    const many = Array.from({ length: MAX_LOG_EVENTS + 25 }, (_, i) => ev(i + 1));
    const merged = mergeEvents([], many);
    expect(merged).toHaveLength(MAX_LOG_EVENTS);
    expect(merged[merged.length - 1].seq).toBe(MAX_LOG_EVENTS + 25);
  });
});
