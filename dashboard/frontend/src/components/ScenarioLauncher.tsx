import { useEffect, useState } from 'react';
import { api, ApiError } from '../api/client';
import type { ScenarioFile } from '../api/types';

export function ScenarioLauncher() {
  const [files, setFiles] = useState<ScenarioFile[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [launching, setLaunching] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ tone: 'error' | 'ok'; text: string; path?: string } | null>(null);

  async function load() {
    try {
      setFiles(await api.scenarios());
      setLoadError(null);
    } catch (e) {
      setLoadError(`Couldn't load scenario files: ${e instanceof ApiError ? e.message : String(e)}`);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function launch(file: ScenarioFile) {
    setLaunching(file.path);
    setNotice(null);
    try {
      const result = await api.launchRun(file.path);
      setNotice({
        tone: 'ok',
        text: `Launched ${result.spawned.length} worker${result.spawned.length === 1 ? '' : 's'} from ${file.run}.`,
      });
    } catch (e) {
      setNotice({
        tone: 'error',
        path: file.path,
        text: `Couldn't launch ${file.file}: ${e instanceof ApiError ? e.message : String(e)}`,
      });
    } finally {
      setLaunching(null);
    }
  }

  return (
    <section aria-labelledby="launcher-title">
      <h2 className="section__title" id="launcher-title">
        Scenarios
        <button className="button button--quiet" onClick={() => void load()}>
          Refresh
        </button>
      </h2>
      {loadError && <p className="form-error" role="alert">{loadError}</p>}
      {files && files.length === 0 && (
        <p className="log__empty">No scenario files found. Add a .yaml file to the scenarios folder.</p>
      )}
      {files?.map((f) => (
        <div className="launcher-row" key={f.path} data-valid={f.valid ? 'true' : 'false'}>
          <div className="launcher-row__head">
            <span className="launcher-row__name">{f.file}</span>
            <button
              className="button"
              disabled={!f.valid || launching !== null}
              onClick={() => void launch(f)}
              aria-label={`Launch ${f.file}`}
            >
              {launching === f.path ? 'Launching…' : 'Launch'}
            </button>
          </div>
          {f.valid ? (
            <div className="launcher-row__detail">
              {f.scenarios.map((s) => (
                <div key={s.name}>
                  {s.name}: {s.agent}, {s.mode === 'runaway' ? 'ignores limits' : s.mode}, {s.executor.replace(/_/g, ' ')}
                </div>
              ))}
            </div>
          ) : (
            <p className="launcher-row__error">{f.error}</p>
          )}
          {notice && notice.path === f.path && (
            <p className="notice" data-tone={notice.tone} role={notice.tone === 'error' ? 'alert' : 'status'}>
              {notice.text}
            </p>
          )}
        </div>
      ))}
      {notice && !notice.path && (
        <p className="notice" data-tone={notice.tone} role="status">{notice.text}</p>
      )}
    </section>
  );
}
