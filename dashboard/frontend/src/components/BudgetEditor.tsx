import { useEffect, useState } from 'react';
import { api, ApiError } from '../api/client';
import type { BudgetView, Enforcement, Snapshot, WorkerSnapshot } from '../api/types';
import { count } from '../format';

type ScopeChoice = 'worker' | 'team' | 'global';

const ENFORCEMENT_LABEL: Record<Enforcement, string> = {
  client_bucket: 'Pace requests in paced mode',
  shadow: 'Measure only, never delay',
  off: 'No limit',
};

interface Draft {
  scope: ScopeChoice;
  tokens: string;
  headroom: string;
  enforcement: Enforcement;
}

function initialDraft(w: WorkerSnapshot): Draft {
  const scope: ScopeChoice =
    w.budget.resolved_via === 'worker_assignment' ? 'worker' : w.budget.resolved_via === 'team' ? 'team' : 'global';
  return {
    scope,
    tokens: w.budget.tokens_per_minute === null ? '' : String(w.budget.tokens_per_minute),
    headroom: String(Math.round((w.budget.effective_tokens_per_minute !== null && w.budget.tokens_per_minute
      ? (w.budget.effective_tokens_per_minute / w.budget.tokens_per_minute) : 0.9) * 100)),
    enforcement: w.budget.enforcement,
  };
}

const sameDraft = (a: Draft, b: Draft) =>
  a.scope === b.scope && a.tokens === b.tokens && a.headroom === b.headroom && a.enforcement === b.enforcement;

/** Who a change reaches, stated before the user applies it. */
function reach(snapshot: Snapshot, w: WorkerSnapshot, scope: ScopeChoice): number {
  if (scope === 'worker') return 0;
  return snapshot.workers.filter((o) => {
    if (o.worker_id === w.worker_id) return false;
    if (o.desired.budget_id) return false; // has its own assignment, unaffected
    return scope === 'global' || o.team === w.team;
  }).length;
}

export function BudgetEditor({ worker, snapshot, open }: { worker: WorkerSnapshot; snapshot: Snapshot; open: boolean }) {
  // Keyed on the values, not the object, so a new snapshot with the same
  // numbers doesn't reset the form (that would briefly show stale values
  // right after an apply, before the server catches up).
  const baseline = initialDraft(worker);
  const baselineKey = JSON.stringify(baseline);
  const [draft, setDraft] = useState<Draft>(baseline);
  const [saved, setSaved] = useState<Draft>(baseline);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Live updates arrive every tick. They replace the form only when the user
  // hasn't edited it, so a draft is never overwritten.
  useEffect(() => {
    if (sameDraft(draft, saved)) {
      setDraft(JSON.parse(baselineKey) as Draft);
      setSaved(JSON.parse(baselineKey) as Draft);
    }
  }, [baselineKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const dirty = !sameDraft(draft, saved);
  const tokens = Number(draft.tokens);
  const headroom = Number(draft.headroom);
  const validTokens = Number.isInteger(tokens) && tokens >= 1;
  const validHeadroom = headroom >= 10 && headroom <= 100;
  const effective = validTokens && validHeadroom ? tokens * (headroom / 100) : null;
  const affected = reach(snapshot, worker, draft.scope);
  const teamLabel = worker.team;

  async function apply() {
    setError(null);
    setNotice(null);
    if (!validTokens) return setError('Tokens per minute must be a whole number, 1 or more.');
    if (!validHeadroom) return setError('Headroom must be between 10% and 100%.');

    const scope = draft.scope;
    const budgetId =
      scope === 'worker' ? `worker:${worker.worker_id}` : scope === 'team' ? `team:${teamLabel}` : globalId(snapshot);
    setBusy(true);
    try {
      await api.putBudget(budgetId, {
        scope: scope === 'global' ? 'global' : scope,
        scope_ref: scope === 'worker' ? worker.worker_id : scope === 'team' ? teamLabel : null,
        tokens_per_minute: tokens,
        requests_per_minute: null,
        max_concurrency: null,
        headroom: headroom / 100,
        enforcement: draft.enforcement,
      });
      // A worker-scoped budget is assigned to this worker. Team and global
      // budgets are reached by clearing the assignment.
      await api.assignBudget(worker.worker_id, scope === 'worker' ? budgetId : '');
      setSaved(draft);
      setNotice(
        scope === 'team' && affected > 0
          ? `Applied. This also changes ${affected} other worker${affected === 1 ? '' : 's'} in team ${teamLabel}.`
          : scope === 'global' && affected > 0
            ? `Applied. This also changes ${affected} other worker${affected === 1 ? '' : 's'}.`
            : 'Budget applied.',
      );
    } catch (e) {
      setError(`Couldn't apply the budget: ${e instanceof ApiError ? e.message : String(e)}. Your changes are still here.`);
    } finally {
      setBusy(false);
    }
  }

  function revert() {
    setDraft(saved);
    setError(null);
    setNotice(null);
  }

  return (
    // A collapsed editor is removed from the accessibility tree and the tab order.
    <div className="editor-wrap" data-open={open ? 'true' : 'false'} inert={!open}>
      <div className="editor-wrap__inner" style={{ minHeight: 0, overflow: 'hidden' }}>
        <form
          className="editor"
          aria-label={`Budget for ${worker.display_name}`}
          onSubmit={(e) => {
            e.preventDefault();
            void apply();
          }}
        >
          <div className="editor__grid">
            <fieldset className="scope" style={{ border: 0, padding: 0, margin: 0 }}>
              <legend className="field__label" style={{ marginBottom: 6 }}>
                Applies to
              </legend>
              {(
                [
                  ['worker', 'This worker only'],
                  ['team', `Everyone in team ${teamLabel}`],
                  ['global', 'Everyone (global)'],
                ] as const
              ).map(([value, text]) => (
                <label key={value}>
                  <input
                    type="radio"
                    name={`scope-${worker.worker_id}`}
                    value={value}
                    checked={draft.scope === value}
                    onChange={() => setDraft({ ...draft, scope: value })}
                  />
                  {text}
                </label>
              ))}
            </fieldset>

            <div className="field">
              <label className="field__label" htmlFor={`tpm-${worker.worker_id}`}>
                Tokens per minute
              </label>
              <input
                id={`tpm-${worker.worker_id}`}
                inputMode="numeric"
                value={draft.tokens}
                onChange={(e) => setDraft({ ...draft, tokens: e.target.value.replace(/[^0-9]/g, '') })}
                aria-invalid={!validTokens}
              />
              <label className="field__label" htmlFor={`headroom-${worker.worker_id}`}>
                Headroom, % of the limit to use
              </label>
              <input
                id={`headroom-${worker.worker_id}`}
                inputMode="numeric"
                value={draft.headroom}
                onChange={(e) => setDraft({ ...draft, headroom: e.target.value.replace(/[^0-9]/g, '') })}
                aria-invalid={!validHeadroom}
              />
              <div className="editor__effective">
                {effective === null
                  ? 'Enter a rate to see the effective limit.'
                  : `Effective limit: ${count(effective)} per minute.`}
              </div>
            </div>

            <div className="field">
              <label className="field__label" htmlFor={`enf-${worker.worker_id}`}>
                When the limit is reached
              </label>
              <select
                id={`enf-${worker.worker_id}`}
                value={draft.enforcement}
                onChange={(e) => setDraft({ ...draft, enforcement: e.target.value as Enforcement })}
              >
                {(Object.keys(ENFORCEMENT_LABEL) as Enforcement[]).map((k) => (
                  <option key={k} value={k}>
                    {ENFORCEMENT_LABEL[k]}
                  </option>
                ))}
              </select>
              <span className="field__hint">
                {draft.scope === 'worker'
                  ? 'Changes only this worker.'
                  : affected > 0
                    ? `Changes ${affected} other worker${affected === 1 ? '' : 's'} that use this budget.`
                    : 'No other workers use this budget yet.'}
              </span>
            </div>
          </div>

          <div className="editor__actions">
            <button type="submit" className="button button--action" disabled={!dirty || busy}>
              {busy ? 'Applying…' : 'Apply budget'}
            </button>
            <button type="button" className="button" onClick={revert} disabled={!dirty || busy}>
              Revert
            </button>
            <span className="editor__dirty">{dirty ? 'Unsaved changes' : 'Up to date'}</span>
          </div>
          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
          {notice && !error && (
            <p className="notice" data-tone="ok" role="status">
              {notice}
            </p>
          )}
        </form>
      </div>
    </div>
  );
}

function globalId(snapshot: Snapshot): string {
  const g = snapshot.budgets.find((b: BudgetView) => b.scope === 'global');
  return g ? g.budget_id : 'global-default';
}
