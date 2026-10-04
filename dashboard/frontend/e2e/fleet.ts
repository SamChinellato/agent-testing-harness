// Test helpers: a fleet that behaves like real workers (heartbeats every
// second), so rows show Running or Stopped instead of Lost.
import type { APIRequestContext } from '@playwright/test';

export class Fleet {
  readonly team = `e2e-${Math.random().toString(36).slice(2, 8)}`;
  private timer: ReturnType<typeof setInterval> | null = null;
  readonly workers: { id: string; display: string }[] = [];

  constructor(private api: APIRequestContext) {}

  /** Display names carry this fleet's suffix, so the page's rows are unambiguous
   *  even though earlier tests' workers are still in the same database. */
  display(name: string): string {
    return `${name} ${this.team}`;
  }

  async add(name: string): Promise<string> {
    const id = `${this.team}-${this.workers.length + 1}`;
    const display = this.display(name);
    await this.api.post('/api/workers', {
      data: { worker_id: id, team: this.team, display_name: display, agent: 'triage', mode: 'paced' },
    });
    await this.api.patch(`/api/workers/${id}/desired`, { data: { state: 'running' } });
    this.workers.push({ id, display });
    return id;
  }

  start(): void {
    this.timer = setInterval(() => void this.beat(), 1000);
    void this.beat();
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  private async beat(): Promise<void> {
    for (const w of this.workers) {
      const desired = await (await this.api.get(`/api/workers/${w.id}/desired`)).json();
      await this.api.post(`/api/workers/${w.id}/heartbeat`, {
        data: {
          applied_generation: desired.generation,
          phase: desired.state === 'running' ? 'running' : 'stopped',
          iteration: 0,
          applied_interrupt_token: desired.interrupt_token,
        },
      });
    }
  }
}
