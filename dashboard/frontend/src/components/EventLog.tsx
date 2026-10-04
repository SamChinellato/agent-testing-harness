import { Label } from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { useLive } from '../state/store';
import { clock, count } from '../format';

const VISIBLE = 100;

export function EventLog() {
  const events = useLive((s) => s.events);
  const rows = events.slice(-VISIBLE).reverse();

  if (rows.length === 0) {
    return <p className="empty">No calls yet.</p>;
  }

  return (
    <div className="calls">
      <Table aria-label="Recent calls" variant="compact" borders={false}>
        <Thead>
          <Tr>
            <Th>Time</Th>
            <Th>Worker</Th>
            <Th>Status</Th>
            <Th textCenter={false} modifier="fitContent">
              ms
            </Th>
          </Tr>
        </Thead>
        <Tbody>
          {rows.map((e) => {
            const throttled = e.status === 429;
            return (
              <Tr key={e.seq} data-throttled={throttled ? 'true' : 'false'}>
                <Td>{clock(e.ts_ingest_ms)}</Td>
                <Td>{e.user}</Td>
                <Td>
                  {throttled ? <Label color="red" isCompact>Throttled</Label> : <span className="num">{e.status}</span>}
                </Td>
                <Td className="num">{count(e.latency_ms)}</Td>
              </Tr>
            );
          })}
        </Tbody>
      </Table>
    </div>
  );
}
