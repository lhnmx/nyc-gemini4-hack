import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { setTimeout as delay } from 'node:timers/promises';
import { chromium } from 'playwright';
import { preview } from 'vite';

const sample = JSON.parse(await readFile(new URL('../dist/static/example/project.json', import.meta.url), 'utf8'));
const materials = [
  { id: 'paper', name: 'Letter paper', quantity: '1 sheet', search_query: 'paper & card', status: 'BUY' },
  { id: 'ruler', name: 'Ruler', quantity: '1', status: 'OWNED_SUBSTITUTE', substitute_note: 'A straight edge works.' },
  { id: 'pencil', name: 'Pencil', category: 'tool', required: false },
];
let server, browser, origin;
before(async () => {
  server = await preview({ preview: { host: '127.0.0.1', port: 0 } });
  origin = 'http://127.0.0.1:' + server.httpServer.address().port;
  browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
});
after(async () => { await browser?.close(); await server?.close(); });

async function openGuide(t, enabled, respond, supplies = materials) {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const queries = [], externalCalls = [], errors = [];
  page.on('pageerror', error => errors.push(error.message));
  t.after(async () => {
    await page.close();
    assert.deepEqual(externalCalls, []);
    assert.deepEqual(errors, []);
  });
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) {
      externalCalls.push(url.href);
      return route.abort();
    }
    if (url.pathname === '/api/health') return route.fulfill({ json: { capabilities: { material_catalog: enabled } } });
    if (url.pathname === '/static/example/project.json') {
      return route.fulfill({ json: { ...sample, plan: { ...sample.plan, materials: supplies } } });
    }
    if (url.pathname === '/api/shopping/search') {
      assert.equal(route.request().method(), 'GET');
      const query = url.searchParams.get('q');
      queries.push(query);
      const response = typeof respond === 'function' ? await respond(query) : respond;
      return route.fulfill(response);
    }
    if (url.pathname.startsWith('/api/')) throw Error('Unexpected API call ' + url.pathname);
    return route.continue();
  });
  await page.goto(origin);
  await page.getByRole('checkbox').first().waitFor();
  return { page, queries };
}

test('shows independent carousels automatically and preserves merchant prices, links and keyboard focus', async t => {
  const url = 'https://supplies.example/products/paper?variant=004&shclid=a%2Fb';
  const checkout = 'https://checkout.example/cart/004:1?shclid=a%2Fb';
  const offers = [
    { title: 'Ivory stationery', seller: 'Acre Paper Co.', variant: 'A4', price: { amount: 1234, currency: 'USD' },
      url, checkout_url: checkout, image_url: origin + '/static/example/' + sample.media['1'].keyframe_url.split('/').pop() },
    { title: 'Loose sheets', price: { amount: 1200, currency: 'JPY' }, url: 'https://supplies.example/products/sheets' },
    { title: 'Blank cards', url: 'https://supplies.example/products/cards' },
  ];
  const alsoNeeded = { id: 'card', name: 'Card stock', search_query: 'paper & card' };
  const { page, queries } = await openGuide(t, true, { json: { products: offers } }, [...materials, alsoNeeded]);
  const paper = page.getByRole('group', { name: 'Shopify products for Letter paper', exact: true });
  const card = page.getByRole('group', { name: 'Shopify products for Card stock', exact: true });
  await paper.waitFor();
  await card.waitFor();
  assert.equal(await paper.getByRole('link', { name: 'Ivory stationery', exact: true }).getAttribute('href'), url);
  assert.equal(await paper.getByRole('link', { name: /Checkout at the store/ }).getAttribute('href'), checkout);
  assert.match(await paper.innerText(), /Acre Paper Co\./);
  assert.match(await paper.innerText(), /USD\s*12\.34/);
  assert.equal(await paper.locator('img').getAttribute('loading'), 'lazy');

  await paper.getByRole('button', { name: 'Next product for Letter paper' }).click();
  assert.match(await paper.innerText(), /JPY\s*1,200/);
  assert.match(await card.innerText(), /Ivory stationery/);
  assert.match(await paper.innerText(), /supplies\.example/);
  const last = paper.getByRole('button', { name: 'Show product 3 of 3 for Letter paper' });
  await last.click();
  assert.equal(await last.getAttribute('aria-current'), 'true');
  assert.match(await paper.innerText(), /See store for price/);
  await last.press('ArrowRight');
  assert.equal(await paper.getByRole('button', { name: 'Show product 1 of 3 for Letter paper' }).evaluate(el => el === document.activeElement), true);
  assert.match(await paper.innerText(), /Ivory stationery/);
  await paper.getByRole('button', { name: 'Previous product for Letter paper' }).click();
  assert.match(await paper.innerText(), /Blank cards/);

  const gather = page.getByRole('checkbox', { name: /Letter paper/ });
  await gather.check();
  assert.equal(await paper.count(), 0);
  assert.equal(await gather.evaluate(el => el === document.activeElement), true);
  await gather.uncheck();
  await paper.waitFor();
  assert.equal(await page.getByRole('checkbox', { name: /Ruler/ }).isChecked(), true);
  assert.equal(await page.getByRole('checkbox', { name: /Pencil/ }).isChecked(), false);
  assert.deepEqual(queries, ['paper & card']);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
});

test('limits simultaneous searches while showing single offers without navigation', async t => {
  let active = 0, peak = 0;
  const supplies = ['Paper', 'Glue', 'Ruler', 'Scissors', 'Tape'].map(name => ({ id: name, name }));
  const { page, queries } = await openGuide(t, true, async name => {
    active += 1;
    peak = Math.max(peak, active);
    await delay(100);
    active -= 1;
    return { json: { products: [{ title: 'Choice of ' + name, url: 'https://shop.example/' + name }] } };
  }, supplies);
  for (const item of supplies) await page.getByRole('link', { name: 'Choice of ' + item.name, exact: true }).waitFor();
  assert.equal(peak, 2);
  assert.deepEqual(queries.sort(), supplies.map(item => item.name).sort());
  assert.equal(await page.getByRole('button', { name: /Next product|Previous product|Show product/ }).count(), 0);
});

test('offers an encoded Shop search when the catalog is unavailable or returns no matches', async t => {
  for (const [enabled, response, message] of [
    [false, {}, null],
    [true, { status: 502 }, 'Shopify’s catalog is unavailable.'],
    [true, { json: { products: [] } }, 'No matching products found.'],
  ]) {
    const { page, queries } = await openGuide(t, enabled, response);
    if (message) await page.getByText(message, { exact: true }).waitFor();
    const link = page.getByRole('link', { name: 'Search Shop by Shopify for Letter paper' });
    await link.waitFor();
    const destination = new URL(await link.getAttribute('href'));
    assert.equal(destination.origin + destination.pathname, 'https://shop.app/search/results');
    assert.equal(destination.searchParams.get('query'), 'paper & card');
    assert.deepEqual(queries, enabled ? ['paper & card'] : []);
    assert.equal(await page.getByRole('button', { name: /Show product/ }).count(), 0);
  }
});
