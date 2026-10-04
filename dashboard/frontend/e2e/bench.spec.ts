import { expect, test, type Page } from '@playwright/test';
import { Fleet } from './fleet';

let fleet: Fleet;

test.beforeEach(async ({ request }) => {
  fleet = new Fleet(request);
  await fleet.add('Dispute triage');
  await fleet.add('Policy research');
  fleet.start();
});

test.afterEach(() => fleet.stop());

function row(page: Page, name: string) {
  return page.getByRole('article', { name: fleet.display(name) });
}

test('shows the fleet, the live connection, and the readouts', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Agent bench' })).toBeVisible();
  await expect(page.getByText('Live', { exact: true })).toBeVisible();
  await expect(page.getByText('Tokens / min')).toBeVisible();
  await expect(row(page, 'Dispute triage')).toBeVisible();
  await expect(row(page, 'Policy research')).toBeVisible();
  await expect(page.getByRole('img', { name: /^Tokens per minute over the last minute\. / })).toBeVisible();
});

test('Stop reaches the worker and the row reports it as stopped', async ({ page }) => {
  await page.goto('/');
  const r = row(page, 'Dispute triage');
  await expect(r.getByText('Running', { exact: true })).toBeVisible({ timeout: 10_000 });
  await r.getByRole('button', { name: 'Stop', exact: true }).click();
  await expect(r.getByText('Stopped', { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(r.getByRole('button', { name: 'Start', exact: true })).toBeVisible();
});

test('a worker-level limit saves from the row menu and shows in the API', async ({ page, request }) => {
  await page.goto('/');
  const r = row(page, 'Dispute triage');
  await r.getByRole('button', { name: /More actions for/ }).click();
  await page.getByRole('menuitem', { name: 'Limit', exact: true }).click();

  const form = page.getByRole('form', { name: /Limit for/ });
  await form.getByLabel('Tokens per minute').fill('1234');
  await form.getByLabel('This worker', { exact: true }).check();
  await form.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(form.getByText('Saved.')).toBeVisible();

  const resolved = await (await request.get(`/api/workers/${fleet.workers[0].id}/budget`)).json();
  expect(resolved.tokens_per_minute).toBe(1234);
  expect(resolved.resolved_via).toBe('worker_assignment');
});

test('an empty limit cannot be saved', async ({ page }) => {
  await page.goto('/');
  const r = row(page, 'Policy research');
  await r.getByRole('button', { name: /More actions for/ }).click();
  await page.getByRole('menuitem', { name: 'Limit', exact: true }).click();
  const form = page.getByRole('form', { name: /Limit for/ });
  await form.getByLabel('Tokens per minute').fill('');
  await expect(form.getByRole('button', { name: 'Save', exact: true })).toBeDisabled();
});

test('controls are keyboard reachable and show a visible focus ring', async ({ page }) => {
  await page.goto('/');
  const stop = row(page, 'Dispute triage').getByRole('button', { name: 'Stop', exact: true });
  await stop.focus();
  await page.keyboard.press('Shift+Tab');
  await page.keyboard.press('Tab');
  await expect(stop).toBeFocused();
  const outline = await stop.evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(outline).not.toBe('none');
});

test('the scenario launcher lists valid scenario files', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('steady-vs-runaway.yaml')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Launch steady-vs-runaway.yaml' })).toBeEnabled();
});

test('Clear asks first, and Cancel changes nothing', async ({ page, request }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Clear', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Clear the bench' });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: 'Cancel' }).click();
  await expect(dialog).toBeHidden();
  const workers = await (await request.get('/api/workers')).json();
  expect(workers.some((w: { worker_id: string }) => w.worker_id === fleet.workers[0].id)).toBe(true);
});

test('Clear keeps running workers and reports what it kept', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Clear', exact: true }).click();
  await page.getByRole('dialog', { name: 'Clear the bench' }).getByRole('button', { name: 'Clear', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: /Kept \d+ running/ })).toBeVisible();
  await expect(row(page, 'Dispute triage')).toBeVisible();
});
