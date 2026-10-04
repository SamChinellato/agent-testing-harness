// REST commands. Every call returns the parsed body or throws ApiError with
// the server's own explanation, so the UI can say what went wrong.

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      if (typeof data?.detail === 'string') message = data.detail;
    } catch {
      // Non-JSON error bodies keep the status line.
    }
    throw new ApiError(res.status, message);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  setState: (workerId: string, state: 'running' | 'stopped') =>
    request('PATCH', `/api/workers/${encodeURIComponent(workerId)}/desired`, { state }),
  setMode: (workerId: string, mode: string) =>
    request('PATCH', `/api/workers/${encodeURIComponent(workerId)}/desired`, { mode }),
  assignBudget: (workerId: string, budgetId: string) =>
    // An empty string clears the worker's own assignment, so it falls back to team or global.
    request('PATCH', `/api/workers/${encodeURIComponent(workerId)}/desired`, { budget_id: budgetId }),
  interrupt: (workerId: string) => request('POST', `/api/workers/${encodeURIComponent(workerId)}/interrupt`),
  kill: (workerId: string) => request('POST', `/api/workers/${encodeURIComponent(workerId)}/kill`),
  forget: (workerId: string) => request('DELETE', `/api/workers/${encodeURIComponent(workerId)}`),
  putBudget: (budgetId: string, body: Record<string, unknown>) =>
    request('PUT', `/api/budgets/${encodeURIComponent(budgetId)}`, body),
  launchRun: (scenarioPath: string) =>
    request<{ run: string; spawned: string[] }>('POST', '/api/runs', { scenario_path: scenarioPath }),
  scenarios: () => request<import('./types').ScenarioFile[]>('GET', '/api/scenarios'),
};
