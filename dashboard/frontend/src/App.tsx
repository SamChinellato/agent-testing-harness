import { useLive } from './state/store';
import { useLiveStream } from './state/useLiveStream';
import { TopBar } from './components/TopBar';
import { Readouts } from './components/Readouts';
import { Trace } from './components/Trace';
import { FleetList } from './components/FleetList';
import { EventLog } from './components/EventLog';
import { ScenarioLauncher } from './components/ScenarioLauncher';

export function App() {
  useLiveStream();
  const snapshot = useLive((s) => s.snapshot);
  const colorSlots = useLive((s) => s.colorSlots);

  return (
    <div className="page">
      <TopBar snapshot={snapshot} />

      {snapshot ? (
        <>
          <Readouts snapshot={snapshot} />
          <section className="trace" aria-label="Throughput">
            <Trace snapshot={snapshot} colorSlots={colorSlots} />
          </section>
          <div className="workspace">
            <section aria-label="Workers">
              <FleetList snapshot={snapshot} />
            </section>
            <aside className="side">
              <ScenarioLauncher />
              <section aria-label="Recent calls">
                <h2 className="side__title">Calls</h2>
                <EventLog />
              </section>
            </aside>
          </div>
        </>
      ) : (
        <p className="trace__empty" role="status">Connecting…</p>
      )}
    </div>
  );
}

export default App;
