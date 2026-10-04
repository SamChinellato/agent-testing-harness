const nf = new Intl.NumberFormat('en-GB');

export function count(n: number | null | undefined): string {
  return n === null || n === undefined ? '—' : nf.format(Math.round(n));
}

export function percent(share: number): string {
  return `${Math.round(share * 100)}%`;
}

export function clock(ms: number): string {
  const d = new Date(ms);
  return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, '0')).join(':');
}

export function ago(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))} s ago`;
  return `${Math.round(seconds / 60)} min ago`;
}
