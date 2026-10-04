import { useEffect } from 'react';
import type { LogEvent, Snapshot } from '../api/types';
import { useLive } from './store';

/** Opens the one live stream. The browser's EventSource reconnects by itself and
 *  sends Last-Event-ID, so the server resumes the event log where it left off. */
export function useLiveStream(url = '/api/stream'): void {
  useEffect(() => {
    const { applySnapshot, appendEvents, setConnection } = useLive.getState();
    const source = new EventSource(url);

    const onSnapshot = (e: MessageEvent) => applySnapshot(JSON.parse(e.data) as Snapshot);
    const onEvents = (e: MessageEvent) => appendEvents(JSON.parse(e.data) as LogEvent[]);
    source.addEventListener('snapshot', onSnapshot);
    source.addEventListener('events', onEvents);
    source.onopen = () => setConnection('live');
    source.onerror = () => {
      // EventSource retries on its own. Say so, and say when we've been silent too long.
      setConnection(useLive.getState().lastSnapshotAt ? 'reconnecting' : 'connecting');
    };

    // If the stream stays silent, stop claiming to be live.
    const watchdog = window.setInterval(() => {
      const last = useLive.getState().lastSnapshotAt;
      if (last && Date.now() - last > 5000) setConnection('offline');
    }, 1000);

    return () => {
      window.clearInterval(watchdog);
      source.removeEventListener('snapshot', onSnapshot);
      source.removeEventListener('events', onEvents);
      source.close();
    };
  }, [url]);
}
