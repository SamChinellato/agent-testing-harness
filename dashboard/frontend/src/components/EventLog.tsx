import { useLive } from '../state/store';
import { clock, count } from '../format';

const VISIBLE = 200;

export function EventLog() {
  const events = useLive((s) => s.events);
  const rows = events.slice(-VISIBLE).reverse();

  if (rows.length === 0) {
    return <p className="log__empty">No model calls yet. Each call appears here as it completes.</p>;
  }

  return (
    <div className="log" role="region" aria-label="Model calls, newest first" tabIndex={0}>
      <table>
        <thead>
          <tr>
            <th scope="col">Time</th>
            <th scope="col">Worker</th>
            <th scope="col">Step</th>
            <th scope="col" className="num">Status</th>
            <th scope="col" className="num">In / out</th>
            <th scope="col" className="num">Latency</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((e) => {
            const throttled = e.status === 429;
            return (
              <tr key={e.seq} data-throttled={throttled ? 'true' : 'false'}>
                <td>{clock(e.ts_ingest_ms)}</td>
                <td>{e.user}</td>
                <td>{e.node}</td>
                <td className="num log__status" data-throttled={throttled ? 'true' : 'false'}>
                  {throttled ? '429 throttled' : e.status}
                </td>
                <td className="num">
                  {count(e.prompt_tokens)} / {count(e.completion_tokens)}
                </td>
                <td className="num">{count(e.latency_ms)} ms</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
