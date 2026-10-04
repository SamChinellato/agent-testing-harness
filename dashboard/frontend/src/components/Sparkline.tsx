// A 60-second trace for one worker. Throttled buckets are ticks on the baseline.
export function Sparkline({
  values,
  throttled,
  colour,
  label,
}: {
  values: number[];
  throttled: number[];
  colour: string;
  label: string;
}) {
  const width = 240;
  const height = 36;
  const top = 4;
  const base = height - 2;
  const max = Math.max(1, ...values);
  const step = values.length > 1 ? width / (values.length - 1) : width;
  const points = values
    .map((v, i) => `${(i * step).toFixed(1)},${(base - ((base - top) * v) / max).toFixed(1)}`)
    .join(' ');

  return (
    <svg className="worker__spark" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label} preserveAspectRatio="none">
      <line x1="0" x2={width} y1={base} y2={base} stroke="var(--rule-strong)" strokeWidth="1" />
      <polyline points={points} fill="none" stroke={colour} strokeWidth="1.5" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
      {throttled.map((n, i) =>
        n > 0 ? (
          <line
            key={i}
            x1={(i * step).toFixed(1)}
            x2={(i * step).toFixed(1)}
            y1={base - 7}
            y2={base}
            stroke="var(--throttle)"
            strokeWidth="2"
            vectorEffect="non-scaling-stroke"
          />
        ) : null,
      )}
    </svg>
  );
}
