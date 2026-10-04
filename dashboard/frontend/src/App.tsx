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

          <section className="trace" aria-labelledby="trace-title">
            <div className="trace__head">
              <h2 className="trace__title" id="trace-title">
                Throughput against each limit
              </h2>
              <span className="trace__subtitle">Last 60 seconds · dashed lines are limits · red ticks are throttled calls</span>
            </div>
            <Trace snapshot={snapshot} colorSlots={colorSlots} />
          </section>

          <div className="workspace">
            <section aria-labelledby="fleet-title">
              <h2 className="section__title" id="fleet-title">
                Workers <span className="section__count">{snapshot.workers.length}</span>
              </h2>
              <FleetList snapshot={snapshot} />
            </section>

            <aside className="side" aria-label="Scenarios and model calls">
              <ScenarioLauncher />
              <section aria-labelledby="log-title">
                <h2 className="section__title" id="log-title">
                  Model calls
                </h2>
                <EventLog />
              </section>
            </aside>
          </div>
        </>
      ) : (
        <p className="trace__empty" role="status">
          Connecting to the dashboard…
        </p>
      )}
    </div>
  );
}

export default App;
