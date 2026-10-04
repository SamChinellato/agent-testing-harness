import { useLayoutEffect, useRef, useState } from 'react';
import type { Snapshot } from '../api/types';
import { SERIES_SLOTS } from '../state/store';
import { clock, count } from '../format';

interface Line {
  key: string;
  name: string;
  colour: string;
  values: number[];
  budget: number | null;
}

export function colourFor(slot: number | undefined): string {
  if (slot === undefined || slot < 0 || slot >= SERIES_SLOTS) return 'var(--series-other)';
  return `var(--series-${slot + 1})`;
}

/** Plotted lines: the first four workers in fixed slot order. Anything past
 *  that folds into one neutral "Other" line, so no fifth colour is ever invented. */
export interface Limit {
  key: string;
  label: string;
  value: number;
  colour: string;
}

/** Limit lines: one per distinct budget in use. A shared budget is drawn once, labelled,
 *  and compared against the group total. A worker's own budget keeps its worker's colour. */
export function buildLimits(snapshot: Snapshot, colorSlots: Record<string, number>): Limit[] {
  const seen = new Map<string, Limit>();
  for (const w of snapshot.workers) {
    const b = w.budget;
    if (b.effective_tokens_per_minute === null || seen.has(b.budget_id)) continue;
    const shared = b.shared_by > 1;
    seen.set(b.budget_id, {
      key: b.budget_id,
      label: shared ? (b.scope === 'global' ? 'Global limit' : 'Team limit') : `${w.display_name} limit`,
      value: b.effective_tokens_per_minute,
      colour: shared ? 'var(--ink-2)' : colourFor(colorSlots[w.worker_id]),
    });
  }
  return [...seen.values()];
}

/** Moves labels apart vertically so none overlap. Input and output are y coordinates. */
export function spreadLabels(ys: number[], gap: number): number[] {
  const order = ys.map((y, i) => ({ y, i })).sort((a, b) => a.y - b.y);
  const out = new Array(ys.length).fill(0);
  let prev = -Infinity;
  for (const { y, i } of order) {
    const placed = Math.max(y, prev + gap);
    out[i] = placed;
    prev = placed;
  }
  return out;
}

export function buildLines(snapshot: Snapshot, colorSlots: Record<string, number>): Line[] {
  const lines: Line[] = [];
  const total = new Array(snapshot.buckets.length).fill(0);
  const other = new Array(snapshot.buckets.length).fill(0);
  let otherCount = 0;
  for (const w of snapshot.workers) {
    w.series.forEach((v, i) => (total[i] += v));
    const slot = colorSlots[w.worker_id];
    if (slot === undefined || slot < 0) {
      w.series.forEach((v, i) => (other[i] += v));
      otherCount += 1;
      continue;
    }
    lines.push({
      key: w.worker_id,
      name: w.display_name,
      colour: colourFor(slot),
      values: w.series,
      budget: w.budget.effective_tokens_per_minute,
    });
  }
  if (otherCount > 0) {
    lines.push({ key: '__other', name: `Other (${otherCount})`, colour: colourFor(-1), values: other, budget: null });
  }
  if (snapshot.workers.length > 1) {
    lines.push({ key: '__total', name: 'All workers', colour: 'var(--ink)', values: total, budget: null });
  }
  return lines;
}

function niceMax(v: number): number {
  if (v <= 0) return 100;
  const pow = Math.pow(10, Math.floor(Math.log10(v)));
  const n = v / pow;
  const nice = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
  return nice * pow;
}

export function Trace({ snapshot, colorSlots }: { snapshot: Snapshot; colorSlots: Record<string, number> }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(900);
  const [hover, setHover] = useState<number | null>(null);

  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const measure = () => setWidth(Math.max(320, el.clientWidth));
    measure();
    const obs = new ResizeObserver(measure);
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  const lines = buildLines(snapshot, colorSlots);
  const buckets = snapshot.buckets;
  const height = 264;
  // Narrow screens get a smaller label gutter and shorter names, so the plot keeps its width.
  const narrow = width < 560;
  const pad = {
    left: narrow ? 54 : 60,
    right: narrow ? Math.min(136, Math.round(width * 0.34)) : 170,
    top: 14,
    bottom: 30,
  };
  const labelName = (n: string) => (narrow && n.length > 12 ? `${n.slice(0, 11)}…` : n);
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const timeEvery = Math.max(1, Math.ceil(72 / (plotW / Math.max(1, buckets.length - 1))));

  const limits = buildLimits(snapshot, colorSlots);
  const dataMax = Math.max(0, ...lines.flatMap((l) => l.values));
  const budgetMax = Math.max(0, ...limits.map((l) => l.value));
  const yMax = niceMax(Math.max(dataMax, budgetMax) * 1.12);

  const x = (i: number) => pad.left + (buckets.length > 1 ? (i * plotW) / (buckets.length - 1) : 0);
  const y = (v: number) => pad.top + plotH - (v / yMax) * plotH;

  const throttledAt = buckets.map((_, i) => snapshot.workers.some((w) => (w.series_throttled[i] ?? 0) > 0));

  if (snapshot.workers.length === 0) {
    return (
      <div className="trace__empty">
        No workers yet. Start one with <code>uv run agent</code>, or launch a scenario.
      </div>
    );
  }

  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * yMax);
  const lastIdx = buckets.length - 1;
  const endY = lines.map((l) => y(l.values[lastIdx] ?? 0));
  const labelYs = spreadLabels(endY, 18);
  const limitLabelYs = spreadLabels(limits.map((l) => y(l.value) - 6), 14);
  const endLabels = lines.map((l, i) => ({
    key: l.key,
    name: l.name,
    colour: l.colour,
    y: endY[i],
    labelY: labelYs[i],
  }));

  function onMove(e: React.PointerEvent<SVGRectElement>) {
    const rect = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - rect.left - pad.left;
    const idx = Math.round((px / plotW) * lastIdx);
    setHover(Math.min(lastIdx, Math.max(0, idx)));
  }

  const summary = lines
    .map((l) => `${l.name}: ${count(l.values[lastIdx])} tokens per minute now`)
    .join('. ');

  return (
    <div className="trace__plot" ref={wrapRef} style={{ position: 'relative' }}>
      <svg
        className="trace__svg"
        width={width}
        height={height}
        role="img"
        aria-label={`Tokens per minute over the last minute. ${summary}.`}
      >
        {ticks.map((t) => (
          <g key={t}>
            <line x1={pad.left} x2={width - pad.right} y1={y(t)} y2={y(t)} stroke="var(--rule)" strokeWidth="1" />
            <text x={pad.left - 10} y={y(t) + 4} textAnchor="end" fontSize="12" fill="var(--ink-3)" className="num">
              {count(t)}
            </text>
          </g>
        ))}

        {buckets.map((ts, i) =>
          i % timeEvery === 0 ? (
            <text key={ts} x={x(i)} y={height - 8} textAnchor="middle" fontSize="12" fill="var(--ink-3)">
              {clock(ts * 1000)}
            </text>
          ) : null,
        )}

        {/* Budget ceilings: dashed, in the worker's own colour, so each line reads against its own limit. */}
        {limits.map((l, i) => (
          <g key={`limit-${l.key}`}>
            <line x1={pad.left} x2={width - pad.right} y1={y(l.value)} y2={y(l.value)} stroke={l.colour} strokeWidth="1.25" strokeDasharray="5 4" />
            <text x={pad.left + 6 + i * 0} y={limitLabelYs[i]} fontSize="12" fill="var(--ink-2)">
              {l.label} {count(l.value)}
            </text>
          </g>
        ))}

        {lines.map((l) => {
          const pts = l.values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
          return (
            <g key={l.key}>
              <polyline
                points={pts}
                fill="none"
                stroke={l.colour}
                strokeWidth={l.key === '__total' ? 1.5 : 2}
                strokeLinejoin="round"
                strokeLinecap="round"
                opacity={l.key === '__total' ? 0.8 : 1}
              />
            </g>
          );
        })}

        {endLabels.map((e) => (
          <g key={`end-${e.key}`}>
            <circle cx={x(lastIdx)} cy={e.y} r="3.5" fill={e.colour} />
            <text x={x(lastIdx) + 10} y={e.labelY + 4} fontSize="13" fill="var(--ink)" fontWeight="500">
              {labelName(e.name)}
            </text>
          </g>
        ))}

        {/* Throttle ticks along the baseline. */}
        {throttledAt.map((on, i) =>
          on ? (
            <line
              key={`t${i}`}
              x1={x(i)}
              x2={x(i)}
              y1={pad.top + plotH}
              y2={pad.top + plotH - 8}
              stroke="var(--throttle)"
              strokeWidth="2"
            />
          ) : null,
        )}

        <line x1={pad.left} x2={width - pad.right} y1={pad.top + plotH} y2={pad.top + plotH} stroke="var(--rule-strong)" />

        {hover !== null && (
          <line x1={x(hover)} x2={x(hover)} y1={pad.top} y2={pad.top + plotH} stroke="var(--ink-2)" strokeWidth="1" />
        )}

        <rect
          x={pad.left}
          y={pad.top}
          width={plotW}
          height={plotH}
          fill="transparent"
          onPointerMove={onMove}
          onPointerLeave={() => setHover(null)}
        />
      </svg>

      {hover !== null && (
        <div
          className="trace__tip"
          style={{
            position: 'absolute',
            left: Math.min(x(hover) + 12, width - 220),
            top: pad.top,
            background: 'var(--surface-panel)',
            border: '1px solid var(--rule-strong)',
            padding: '8px 10px',
            fontSize: 13,
            minWidth: 190,
            pointerEvents: 'none',
          }}
        >
          <div style={{ color: 'var(--ink-2)', marginBottom: 4 }}>{clock(buckets[hover] * 1000)}</div>
          {lines.map((l) => (
            <div key={l.key} style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
              <span>
                <span style={{ color: l.colour }}>■</span> {l.name}
              </span>
              <span className="num">{count(l.values[hover])}</span>
            </div>
          ))}
          {throttledAt[hover] && <div style={{ color: 'var(--throttle)', marginTop: 4 }}>Throttled in this window</div>}
        </div>
      )}
    </div>
  );
}

