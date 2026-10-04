import { create } from 'zustand';
import type { LogEvent, Snapshot } from '../api/types';

export type Connection = 'connecting' | 'live' | 'reconnecting' | 'offline';

export const MAX_LOG_EVENTS = 500;
// Chart series colours: the first four workers, by first appearance. A worker
// keeps its colour for as long as it's in the store, so filtering or
// reordering never repaints the survivors. Anything past four folds into a
// neutral "Other" line.
export const SERIES_SLOTS = 4;

export interface LiveState {
  snapshot: Snapshot | null;
  events: LogEvent[];
  connection: Connection;
  lastSnapshotAt: number | null;
  colorSlots: Record<string, number>;
  applySnapshot: (next: Snapshot, at?: number) => void;
  appendEvents: (incoming: LogEvent[]) => void;
  setConnection: (c: Connection) => void;
}

/** Pure: returns the next state for an incoming snapshot, or null to ignore it.
 *  Snapshots are replaced whole, never merged. An older revision is discarded,
 *  so a late frame can't overwrite a newer view. */
export function nextSnapshotState(
  current: { snapshot: Snapshot | null; colorSlots: Record<string, number> },
  next: Snapshot,
): { snapshot: Snapshot; colorSlots: Record<string, number> } | null {
  if (current.snapshot && next.revision <= current.snapshot.revision) return null;
  const colorSlots = { ...current.colorSlots };
  let taken = new Set(Object.values(colorSlots));
  for (const w of next.workers) {
    if (colorSlots[w.worker_id] !== undefined) continue;
    let slot = 0;
    while (taken.has(slot)) slot += 1;
    if (slot < SERIES_SLOTS) {
      colorSlots[w.worker_id] = slot;
      taken = new Set(taken).add(slot);
    } else {
      colorSlots[w.worker_id] = -1; // folded into "Other"
    }
  }
  return { snapshot: next, colorSlots };
}

/** Pure: merges new log events, dedupes by seq, keeps the newest MAX_LOG_EVENTS. */
export function mergeEvents(existing: LogEvent[], incoming: LogEvent[]): LogEvent[] {
  const bySeq = new Map<number, LogEvent>();
  for (const e of existing) bySeq.set(e.seq, e);
  for (const e of incoming) bySeq.set(e.seq, e);
  const merged = [...bySeq.values()].sort((a, b) => a.seq - b.seq);
  return merged.length > MAX_LOG_EVENTS ? merged.slice(-MAX_LOG_EVENTS) : merged;
}

export const useLive = create<LiveState>((set, get) => ({
  snapshot: null,
  events: [],
  connection: 'connecting',
  lastSnapshotAt: null,
  colorSlots: {},
  applySnapshot: (next, at = Date.now()) => {
    const result = nextSnapshotState(get(), next);
    if (result) set({ ...result, lastSnapshotAt: at, connection: 'live' });
  },
  appendEvents: (incoming) => set({ events: mergeEvents(get().events, incoming) }),
  setConnection: (connection) => set({ connection }),
}));
