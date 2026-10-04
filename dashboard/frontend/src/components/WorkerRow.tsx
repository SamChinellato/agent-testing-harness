import { useState, type Ref } from 'react';
import { Button, Dropdown, DropdownItem, DropdownList, Label, MenuToggle, type MenuToggleElement } from '@patternfly/react-core';
import EllipsisVIcon from '@patternfly/react-icons/dist/esm/icons/ellipsis-v-icon';
import { api, ApiError } from '../api/client';
import type { Snapshot, WorkerSnapshot } from '../api/types';
import { count } from '../format';
import { BudgetEditor } from './BudgetEditor';
import { Sparkline } from './Sparkline';
import { colourFor } from './Trace';
import { MODE_TEXT, SELECTABLE_MODES, STATUS_COLOUR, STATUS_TEXT } from './status';

type Message = { tone: 'danger' | 'success'; text: string } | null;

export function WorkerRow({ worker, snapshot, slot }: { worker: WorkerSnapshot; snapshot: Snapshot; slot: number | undefined }) {
  const [panelOpen, setPanelOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Message>(null);

  const colour = colourFor(slot);
  const running = worker.desired.state === 'running';
  const status = worker.observed.status;
  const rate = worker.live.tokens_per_minute;
  const shared = worker.budget.shared_by > 1;
  const limit = worker.budget.effective_tokens_per_minute;
  const over = !shared && limit !== null && rate > limit;

  async function run(action: () => Promise<unknown>, okText?: string) {
    setBusy(true);
    setMessage(null);
    try {
      await action();
      if (okText) setMessage({ tone: 'success', text: okText });
    } catch (e) {
      setMessage({ tone: 'danger', text: e instanceof ApiError ? e.message : String(e) });
    } finally {
      setBusy(false);
      setMenuOpen(false);
    }
  }

  const id = worker.worker_id;
  const canForget = status === 'stopped' || status === 'lost';

  return (
    <div className="worker-block" style={{ ['--worker-colour' as string]: slot === undefined || slot < 0 ? 'transparent' : colour }}>
      <div className="worker" aria-label={worker.display_name} role="article">
        <div style={{ minWidth: 0 }}>
          <div className="worker__name">{worker.display_name}</div>
          <div className="worker__meta">
            {worker.team} · {MODE_TEXT[worker.desired.mode] ?? worker.desired.mode}
          </div>
        </div>

        <Label color={STATUS_COLOUR[status]} isCompact>
          {STATUS_TEXT[status]}
        </Label>

        <div className="worker__rate" data-over={over ? 'true' : 'false'}>
          <strong>{count(rate)}</strong>
          <div className="worker__rate-note">
            {shared ? 'shared' : limit === null ? 'no limit' : `of ${count(limit)}`}
          </div>
        </div>

        <Sparkline
          values={worker.series}
          throttled={worker.series_throttled}
          colour={slot === undefined || slot < 0 ? 'var(--series-other)' : colour}
          label={`Tokens per minute for ${worker.display_name}`}
        />

        <div className="worker__actions">
          {running ? (
            <Button variant="secondary" isDisabled={busy} onClick={() => run(() => api.setState(id, 'stopped'))}>
              Stop
            </Button>
          ) : (
            <Button variant="primary" isDisabled={busy} onClick={() => run(() => api.setState(id, 'running'))}>
              Start
            </Button>
          )}
          <Dropdown
            isOpen={menuOpen}
            onOpenChange={setMenuOpen}
            onSelect={() => setMenuOpen(false)}
            popperProps={{ position: 'end' }}
            toggle={(ref: Ref<MenuToggleElement>) => (
              <MenuToggle
                ref={ref}
                variant="plain"
                aria-label={`More actions for ${worker.display_name}`}
                isExpanded={menuOpen}
                onClick={() => setMenuOpen(!menuOpen)}
              >
                <EllipsisVIcon />
              </MenuToggle>
            )}
          >
            <DropdownList>
              <DropdownItem onClick={() => { setPanelOpen(!panelOpen); setMenuOpen(false); }}>
                {panelOpen ? 'Hide limit' : 'Limit'}
              </DropdownItem>
              {SELECTABLE_MODES.map((mode) => (
                <DropdownItem
                  key={mode}
                  isDisabled={busy || worker.desired.mode === mode}
                  onClick={() => run(() => api.setMode(id, mode))}
                >
                  {MODE_TEXT[mode]}
                </DropdownItem>
              ))}
              {running && (
                <DropdownItem isDisabled={busy} onClick={() => run(() => api.interrupt(id), 'Interrupt sent.')}>
                  Interrupt
                </DropdownItem>
              )}
              {worker.managed && (
                <DropdownItem isDisabled={busy} onClick={() => run(() => api.kill(id), 'Process stopped.')}>
                  Kill process
                </DropdownItem>
              )}
              {canForget && (
                <DropdownItem isDisabled={busy} onClick={() => run(() => api.forget(id))}>
                  Forget
                </DropdownItem>
              )}
            </DropdownList>
          </Dropdown>
        </div>
      </div>

      {message && (
        <p className="worker__message" role={message.tone === 'danger' ? 'alert' : 'status'} style={{ padding: '0 var(--space-4)', margin: '0 0 var(--space-2)', color: message.tone === 'danger' ? 'var(--throttle)' : 'var(--ink-2)' }}>
          {message.text}
        </p>
      )}

      {panelOpen && (
        <div className="worker__panel">
          <BudgetEditor worker={worker} snapshot={snapshot} />
        </div>
      )}
    </div>
  );
}
