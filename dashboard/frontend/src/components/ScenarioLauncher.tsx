import { useEffect, useState } from 'react';
import { Button, Label } from '@patternfly/react-core';
import { api, ApiError } from '../api/client';
import type { ScenarioFile } from '../api/types';

export function ScenarioLauncher() {
  const [files, setFiles] = useState<ScenarioFile[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [launching, setLaunching] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ path: string; tone: 'danger' | 'success'; text: string } | null>(null);

  async function load() {
    try {
      setFiles(await api.scenarios());
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
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
      setNotice({ path: file.path, tone: 'success', text: `Started ${result.spawned.length}.` });
    } catch (e) {
      setNotice({ path: file.path, tone: 'danger', text: e instanceof ApiError ? e.message : String(e) });
    } finally {
      setLaunching(null);
    }
  }

  return (
    <section aria-label="Scenarios">
      <h2 className="side__title">Scenarios</h2>
      {error && <p role="alert" style={{ color: 'var(--throttle)' }}>{error}</p>}
      {files?.length === 0 && <p className="empty">No scenario files.</p>}
      {files?.map((f) => (
        <div key={f.path} style={{ padding: 'var(--space-3) 0', borderBottom: '1px solid var(--rule)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 'var(--space-3)' }}>
            <div>
              <div style={{ fontWeight: 600 }}>{f.file}</div>
              {!f.valid && <div style={{ color: 'var(--throttle)', fontSize: 'var(--type-sm)' }}>{f.error}</div>}
            </div>
            {f.valid ? (
              <Button variant="secondary" isDisabled={launching !== null} aria-label={`Launch ${f.file}`} onClick={() => void launch(f)}>
                {launching === f.path ? 'Starting…' : 'Launch'}
              </Button>
            ) : (
              <Label color="red" isCompact>Invalid</Label>
            )}
          </div>
          {notice?.path === f.path && (
            <p role={notice.tone === 'danger' ? 'alert' : 'status'} style={{ color: notice.tone === 'danger' ? 'var(--throttle)' : 'var(--ink-2)', margin: 'var(--space-2) 0 0', fontSize: 'var(--type-sm)' }}>
              {notice.text}
            </p>
          )}
        </div>
      ))}
    </section>
  );
}
