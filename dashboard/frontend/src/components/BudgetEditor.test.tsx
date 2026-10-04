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
  it('starts from the worker\'s budget and is clean until edited', () => {
    const worker = makeWorker({ worker_id: 'w1' });
    render(<BudgetEditor worker={worker} snapshot={makeSnapshot([worker])} />);
    expect(screen.getByLabelText('Tokens per minute')).toHaveValue('3000');
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('saves a team limit, then clears this worker\'s own assignment', async () => {
    const calls: { method: string; url: string; body?: string }[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      calls.push({ method: init?.method ?? 'GET', url: String(input), body: init?.body as string | undefined });
      return okJson({});
    });

    const worker = makeWorker({ worker_id: 'w1' });
    const other = makeWorker({ worker_id: 'w2' });
    render(<BudgetEditor worker={worker} snapshot={makeSnapshot([worker, other])} />);

    fireEvent.change(screen.getByLabelText('Tokens per minute'), { target: { value: '5000' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[0]).toMatchObject({ method: 'PUT', url: '/api/budgets/team%3Abanking-ops' });
    expect(JSON.parse(calls[0].body!)).toMatchObject({ scope: 'team', scope_ref: 'banking-ops', tokens_per_minute: 5000 });
    expect(calls[1]).toMatchObject({ method: 'PATCH', url: '/api/workers/w1/desired' });
    expect(JSON.parse(calls[1].body!)).toEqual({ budget_id: '' });
    expect(await screen.findByText('Saved. Also affects 1 other worker.')).toBeInTheDocument();
  });

  it('shows the server\'s reason when a save is refused, and keeps the draft', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'Unknown budget' }), { status: 404, headers: { 'Content-Type': 'application/json' } }),
    );
    const worker = makeWorker({ worker_id: 'w1' });
    render(<BudgetEditor worker={worker} snapshot={makeSnapshot([worker])} />);
    fireEvent.change(screen.getByLabelText('Tokens per minute'), { target: { value: '4000' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Unknown budget');
    expect(screen.getByLabelText('Tokens per minute')).toHaveValue('4000');
  });

  it('refuses an empty rate without calling the server', () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const worker = makeWorker({ worker_id: 'w1' });
    render(<BudgetEditor worker={worker} snapshot={makeSnapshot([worker])} />);
    fireEvent.change(screen.getByLabelText('Tokens per minute'), { target: { value: '' } });
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
