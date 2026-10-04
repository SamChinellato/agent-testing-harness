import { useEffect, useState } from 'react';
import { Button, Form, FormGroup, FormSelect, FormSelectOption, Radio, TextInput } from '@patternfly/react-core';
import { api, ApiError } from '../api/client';
import type { Enforcement, Snapshot, WorkerSnapshot } from '../api/types';
import { ENFORCEMENT_TEXT } from './status';

type ScopeChoice = 'worker' | 'team' | 'global';

interface Draft {
  scope: ScopeChoice;
  tokens: string;
  enforcement: Enforcement;
}

function fromWorker(w: WorkerSnapshot): Draft {
  const scope: ScopeChoice =
    w.budget.resolved_via === 'worker_assignment' ? 'worker' : w.budget.resolved_via === 'team' ? 'team' : 'global';
  return {
    scope,
    tokens: w.budget.tokens_per_minute === null ? '' : String(w.budget.tokens_per_minute),
    enforcement: w.budget.enforcement,
  };
}

const same = (a: Draft, b: Draft) => a.scope === b.scope && a.tokens === b.tokens && a.enforcement === b.enforcement;

/** Workers a team or global change reaches (those without their own assignment). */
function reach(snapshot: Snapshot, w: WorkerSnapshot, scope: ScopeChoice): number {
  if (scope === 'worker') return 0;
  return snapshot.workers.filter(
    (o) => o.worker_id !== w.worker_id && !o.desired.budget_id && (scope === 'global' || o.team === w.team),
  ).length;
}

function globalId(snapshot: Snapshot): string {
  return snapshot.budgets.find((b) => b.scope === 'global')?.budget_id ?? 'global-default';
}

export function BudgetEditor({ worker, snapshot }: { worker: WorkerSnapshot; snapshot: Snapshot }) {
  const next = fromWorker(worker);
  const nextKey = JSON.stringify(next);
  const [draft, setDraft] = useState<Draft>(next);
  const [saved, setSaved] = useState<Draft>(next);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Adopt the server's values only when the form is clean, so a live update never overwrites an edit.
  useEffect(() => {
    if (same(draft, saved)) {
      const fresh = JSON.parse(nextKey) as Draft;
      setDraft(fresh);
      setSaved(fresh);
    }
  }, [nextKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const dirty = !same(draft, saved);
  const tokens = Number(draft.tokens);
  const valid = Number.isInteger(tokens) && tokens >= 1;
  const affected = reach(snapshot, worker, draft.scope);
  const headroom = worker.budget.tokens_per_minute
    ? (worker.budget.effective_tokens_per_minute ?? 0) / worker.budget.tokens_per_minute
    : 0.9;

  async function apply() {
    setError(null);
    setNotice(null);
    if (!valid) {
      setError('Enter a whole number, 1 or more.');
      return;
    }
    const { scope } = draft;
    const budgetId = scope === 'worker' ? `worker:${worker.worker_id}` : scope === 'team' ? `team:${worker.team}` : globalId(snapshot);
    setBusy(true);
    try {
      await api.putBudget(budgetId, {
        scope,
        scope_ref: scope === 'worker' ? worker.worker_id : scope === 'team' ? worker.team : null,
        tokens_per_minute: tokens,
        requests_per_minute: null,
        max_concurrency: null,
        headroom,
        enforcement: draft.enforcement,
      });
      await api.assignBudget(worker.worker_id, scope === 'worker' ? budgetId : '');
      setSaved(draft);
      setNotice(affected > 0 ? `Saved. Also affects ${affected} other worker${affected === 1 ? '' : 's'}.` : 'Saved.');
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const scopes: [ScopeChoice, string][] = [
    ['worker', 'This worker'],
    ['team', `Team ${worker.team}`],
    ['global', 'Everyone'],
  ];

  return (
    <Form
      className="budget-form"
      onSubmit={(e) => {
        e.preventDefault();
        void apply();
      }}
      aria-label={`Limit for ${worker.display_name}`}
    >
      <FormGroup label="Tokens per minute" fieldId={`tpm-${worker.worker_id}`}>
        <TextInput
          id={`tpm-${worker.worker_id}`}
          inputMode="numeric"
          value={draft.tokens}
          validated={valid || draft.tokens === '' ? 'default' : 'error'}
          onChange={(_e, value) => setDraft({ ...draft, tokens: value.replace(/[^0-9]/g, '') })}
        />
      </FormGroup>

      <FormGroup label="Applies to" role="radiogroup" isInline={false}>
        <div className="budget-form__radios">
          {scopes.map(([value, text]) => (
            <Radio
              key={value}
              id={`scope-${worker.worker_id}-${value}`}
              name={`scope-${worker.worker_id}`}
              label={text}
              isChecked={draft.scope === value}
              onChange={() => setDraft({ ...draft, scope: value })}
            />
          ))}
        </div>
      </FormGroup>

      <FormGroup label="When reached" fieldId={`enf-${worker.worker_id}`}>
        <FormSelect
          id={`enf-${worker.worker_id}`}
          value={draft.enforcement}
          onChange={(_e, value) => setDraft({ ...draft, enforcement: value as Enforcement })}
        >
          {(Object.keys(ENFORCEMENT_TEXT) as Enforcement[]).map((k) => (
            <FormSelectOption key={k} value={k} label={ENFORCEMENT_TEXT[k]} />
          ))}
        </FormSelect>
      </FormGroup>

      <div className="budget-form__actions">
        <Button variant="secondary" isDisabled={!dirty || busy} onClick={() => { setDraft(saved); setError(null); setNotice(null); }}>
          Revert
        </Button>
        <Button type="submit" variant="primary" isDisabled={!dirty || busy || !valid}>
          {busy ? 'Saving…' : 'Save'}
        </Button>
      </div>

      {(error || notice) && (
        <p className="worker__message" role={error ? 'alert' : 'status'} style={{ color: error ? 'var(--throttle)' : 'var(--ink-2)', gridColumn: '1 / -1', margin: 0 }}>
          {error ?? notice}
        </p>
      )}
    </Form>
  );
}
