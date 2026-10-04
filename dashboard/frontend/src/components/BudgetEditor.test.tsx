import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { BudgetEditor } from './BudgetEditor';
import { makeSnapshot, makeWorker } from '../test/fixtures';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function okJson(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

describe('BudgetEditor', () => {
  it('starts from the worker\'s resolved budget and stays clean until edited', () => {
    const worker = makeWorker({ worker_id: 'w1' });
    render(<BudgetEditor worker={worker} snapshot={makeSnapshot([worker])} open />);
    expect(screen.getByLabelText('Tokens per minute')).toHaveValue('3000');
    expect(screen.getByText('Up to date')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Apply budget' })).toBeDisabled();
  });

  it('applies a team budget: saves it, then clears this worker\'s own assignment', async () => {
    const calls: { method: string; url: string; body?: string }[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      calls.push({ method: init?.method ?? 'GET', url: String(input), body: init?.body as string | undefined });
      return okJson({});
    });

    const worker = makeWorker({ worker_id: 'w1' });
    const other = makeWorker({ worker_id: 'w2' });
    render(<BudgetEditor worker={worker} snapshot={makeSnapshot([worker, other])} open />);

    fireEvent.change(screen.getByLabelText('Tokens per minute'), { target: { value: '5000' } });
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Apply budget' }));

    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[0]).toMatchObject({ method: 'PUT', url: '/api/budgets/team%3Abanking-ops' });
    expect(JSON.parse(calls[0].body!)).toMatchObject({ scope: 'team', scope_ref: 'banking-ops', tokens_per_minute: 5000 });
    expect(calls[1]).toMatchObject({ method: 'PATCH', url: '/api/workers/w1/desired' });
    expect(JSON.parse(calls[1].body!)).toEqual({ budget_id: '' });
    expect(await screen.findByText(/also changes 1 other worker in team banking-ops/)).toBeInTheDocument();
  });

  it('keeps the draft and says why when the server refuses it', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'Unknown budget' }), { status: 404, headers: { 'Content-Type': 'application/json' } }),
    );
    const worker = makeWorker({ worker_id: 'w1' });
    render(<BudgetEditor worker={worker} snapshot={makeSnapshot([worker])} open />);
    fireEvent.change(screen.getByLabelText('Tokens per minute'), { target: { value: '4000' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply budget' }));
    expect(await screen.findByRole('alert')).toHaveTextContent("Couldn't apply the budget: Unknown budget. Your changes are still here.");
    expect(screen.getByLabelText('Tokens per minute')).toHaveValue('4000');
  });

  it('refuses a rate that is not a whole number, without calling the server', () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const worker = makeWorker({ worker_id: 'w1' });
    render(<BudgetEditor worker={worker} snapshot={makeSnapshot([worker])} open />);
    fireEvent.change(screen.getByLabelText('Tokens per minute'), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply budget' }));
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
