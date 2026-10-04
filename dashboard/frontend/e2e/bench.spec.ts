import { expect, test } from '@playwright/test';
import { Fleet } from './fleet';

let fleet: Fleet;

test.beforeEach(async ({ request }) => {
  fleet = new Fleet(request);
  await fleet.add('Dispute triage');
  await fleet.add('Policy research');
  fleet.start();
});

test.afterEach(() => fleet.stop());

test('shows the fleet, the live connection, and the summary readouts', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Agent bench' })).toBeVisible();
  await expect(page.getByRole('status').filter({ hasText: 'Live' })).toBeVisible();
  await expect(page.getByText('Tokens per minute, all workers')).toBeVisible();
  await expect(page.getByRole('article', { name: fleet.display('Dispute triage') })).toBeVisible();
  await expect(page.getByRole('article', { name: fleet.display('Policy research') })).toBeVisible();
  await expect(page.getByRole('img', { name: /^Tokens per minute over the last minute\. / })).toBeVisible();
});

test('Stop reaches the worker and the row reports it as stopped', async ({ page }) => {
  await page.goto('/');
  const row = page.getByRole('article', { name: fleet.display('Dispute triage') });
  await expect(row.getByText('Running', { exact: true })).toBeVisible({ timeout: 10_000 });

  await row.getByRole('button', { name: 'Stop', exact: true }).click();
  await expect(row.getByText('Stopped', { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(row.getByRole('button', { name: 'Start', exact: true })).toBeVisible();
});

test('a worker-level budget applies from the editor and shows in the API', async ({ page, request }) => {
  await page.goto('/');
  const row = page.getByRole('article', { name: fleet.display('Dispute triage') });
  await row.getByRole('button', { name: 'Budget', exact: true }).click();
  await expect(row.getByRole('button', { name: 'Close budget' })).toHaveAttribute('aria-expanded', 'true');

  const editor = row.getByRole('form', { name: /Budget for/ });
  await editor.getByLabel('Tokens per minute').fill('1234');
  await editor.getByLabel('This worker only').check();
  await editor.getByRole('button', { name: 'Apply budget' }).click();
  await expect(row.getByText('Budget applied.')).toBeVisible();

  const resolved = await (await request.get(`/api/workers/${fleet.workers[0].id}/budget`)).json();
  expect(resolved.tokens_per_minute).toBe(1234);
  expect(resolved.resolved_via).toBe('worker_assignment');
});

test('a refused budget explains why and keeps the draft', async ({ page }) => {
  await page.goto('/');
  const row = page.getByRole('article', { name: fleet.display('Policy research') });
  await row.getByRole('button', { name: 'Budget', exact: true }).click();
  const editor = row.getByRole('form', { name: /Budget for/ });
  await editor.getByLabel('Tokens per minute').fill('');
  await editor.getByRole('button', { name: 'Apply budget' }).click();
  await expect(row.getByRole('alert')).toContainText('Tokens per minute must be a whole number');
});

test('controls are keyboard reachable and show a visible focus ring', async ({ page }) => {
  await page.goto('/');
  const row = page.getByRole('article', { name: fleet.display('Dispute triage') });
  const stop = row.getByRole('button', { name: 'Stop', exact: true });
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
