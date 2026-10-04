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

/** The two behaviours the dashboard offers. A worker started with the older
 *  'naive' mode still shows, under its own name, until it is switched. */
export const MODE_TEXT: Record<string, string> = {
  paced: 'Obeys limit',
  runaway: 'Ignores limit',
  naive: 'Naive retry',
};

export const SELECTABLE_MODES = ['paced', 'runaway'] as const;
