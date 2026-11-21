import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { preview } from 'vite';

let browser, server, origin;
before(async () => {
  server = await preview({ preview: { host: '127.0.0.1', port: 0 } });
  origin = 'http://127.0.0.1:' + server.httpServer.address().port;
  browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
});
after(async () => { await browser?.close(); await server?.close(); });

test('a stalled health check allows a valid guide submission exactly once', async t => {
  const page = await browser.newPage();
  t.after(() => page.close());
  await page.route('**/api/health', () => {});
  let creations = 0;
  await page.route('**/api/projects', route => {
    creations++;
    return route.fulfill({ status: 202, json: { project_id: 'health-test', status: 'queued' } });
  });
  await page.route('**/events', route => route.fulfill({ contentType: 'text/event-stream', body: '' }));
  await page.goto(origin, { waitUntil: 'domcontentloaded' });
  await page.getByRole('button', { name: 'New guide' }).click();
  await page.getByLabel('Your project').fill('Fold a paper glider');
  await page.getByRole('button', { name: 'Generate guide' }).click();
  await page.waitForURL('**/?project=health-test');
  assert.equal(creations, 1);
});

test('the open form recovers after a temporary server outage without losing the prompt', async t => {
  const page = await browser.newPage();
  t.after(() => page.close());
  let online = false;
  await page.route('**/api/health', route => route.fulfill({ json: {
    gemini_configured: true, youtube_configured: true, generation_ready: online,
    generation_message: online ? 'Ready' : 'Worker is starting.',
  } }));
  await page.goto(origin);
  await page.getByRole('button', { name: 'New guide' }).click();
  await page.getByText('Worker is starting.', { exact: true }).last().waitFor();
  await page.getByLabel('Your project').fill('Fold a paper crane');
  const generate = page.getByRole('button', { name: 'Generate guide' });
  assert.equal(await generate.isDisabled(), true);
  online = true;
  await page.waitForFunction(() => !document.querySelector('button[type=submit]').disabled, null, { timeout: 10000 });
  assert.equal(await page.getByLabel('Your project').inputValue(), 'Fold a paper crane');
});
