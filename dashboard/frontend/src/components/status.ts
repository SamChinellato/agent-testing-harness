import type { WorkerStatus } from '../api/types';

export const STATUS_TEXT: Record<WorkerStatus, string> = {
  running: 'Running',
  stopped: 'Stopped',
  starting: 'Starting',
  draining: 'Stopping',
  pending: 'Applying',
  unknown: 'No heartbeat',
  lost: 'Lost',
  errored: 'Error',
};

/** PatternFly label colour per status. Every label also carries its text. */
export const STATUS_COLOUR: Record<WorkerStatus, 'green' | 'grey' | 'blue' | 'orange' | 'red'> = {
  running: 'green',
  stopped: 'grey',
  starting: 'blue',
  draining: 'blue',
  pending: 'orange',
  unknown: 'grey',
  lost: 'red',
  errored: 'red',
};

export const MODE_TEXT: Record<string, string> = {
  naive: 'Naive retry',
  paced: 'Paced',
  runaway: 'Ignores limits',
};

export const ENFORCEMENT_TEXT: Record<string, string> = {
  client_bucket: 'Enforce',
  shadow: 'Measure only',
  off: 'Off',
};
