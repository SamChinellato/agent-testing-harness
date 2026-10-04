import type { WorkerStatus } from '../api/types';

// Each status has its own shape and a text label. Colour is never the only signal.
export const STATUS_LABEL: Record<WorkerStatus, string> = {
  running: 'Running',
  stopped: 'Stopped',
  starting: 'Starting',
  draining: 'Stopping',
  pending: 'Applying',
  unknown: 'No heartbeat',
  lost: 'Lost',
  errored: 'Error',
};

function Shape({ status }: { status: WorkerStatus }) {
  const common = { width: 12, height: 12, viewBox: '0 0 12 12', 'aria-hidden': true } as const;
  switch (status) {
    case 'running':
      return (
        <svg {...common}>
          <circle cx="6" cy="6" r="5" fill="currentColor" />
        </svg>
      );
    case 'stopped':
      return (
        <svg {...common}>
          <circle cx="6" cy="6" r="4.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
        </svg>
      );
    case 'starting':
    case 'draining':
      return (
        <svg {...common}>
          <circle cx="6" cy="6" r="4.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
          <path d="M6 1.5 A4.5 4.5 0 0 1 6 10.5 Z" fill="currentColor" />
        </svg>
      );
    case 'pending':
      return (
        <svg {...common}>
          <circle cx="6" cy="6" r="4.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeDasharray="2.2 2" />
        </svg>
      );
    case 'unknown':
      return (
        <svg {...common}>
          <circle cx="6" cy="6" r="1.4" fill="currentColor" />
          <circle cx="6" cy="6" r="4.5" fill="none" stroke="currentColor" strokeWidth="1" strokeDasharray="1 2" />
        </svg>
      );
    case 'lost':
      return (
        <svg {...common}>
          <path d="M2.5 2.5 L9.5 9.5 M9.5 2.5 L2.5 9.5" stroke="currentColor" strokeWidth="1.6" />
        </svg>
      );
    case 'errored':
      return (
        <svg {...common}>
          <rect x="2" y="2" width="8" height="8" fill="currentColor" />
        </svg>
      );
  }
}

export function StatusMark({ status }: { status: WorkerStatus }) {
  return (
    <span className="status" data-tone={status === 'pending' ? 'pending' : undefined}>
      <span className="status__mark">
        <Shape status={status} />
      </span>
      {STATUS_LABEL[status]}
    </span>
  );
}

export function ThrottleTag({ count }: { count: number }) {
  return (
    <span className="status" data-tone="throttle">
      <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true" className="status__mark">
        <path d="M6 1.5 L11 10.5 L1 10.5 Z" fill="currentColor" />
      </svg>
      Throttled · {count} in the last minute
    </span>
  );
}
