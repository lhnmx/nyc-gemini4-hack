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

async function openGuide(t, enabled, respond, supplies = materials, width = 390) {
  const page = await browser.newPage({ viewport: { width, height: 844 } });
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

test('keeps the checklist compact and opens one Shopify carousel at a time with prices, links and keyboard navigation', async t => {
  const url = 'https://supplies.example/products/paper?variant=004&shclid=a%2Fb';
  const checkout = 'https://checkout.example/cart/004:1?shclid=a%2Fb';
  const offers = [
    { title: 'Ivory stationery', seller: 'Acre Paper Co.', variant: 'A4', price: { amount: 1234, currency: 'USD' },
      url, checkout_url: checkout, image_url: origin + '/static/example/' + sample.media['1'].keyframe_url.split('/').pop() },
    { title: 'Loose sheets', price: { amount: 1200, currency: 'JPY' }, url: 'https://supplies.example/products/sheets' },
    { title: 'Blank cards', url: 'https://supplies.example/products/cards' },
  ];
  const alsoNeeded = { id: 'card', name: 'Card stock', search_query: 'paper & card' };
  const { page, queries } = await openGuide(t, true, query => ({ json: { products: query === 'paper & card'
    ? offers : [{ title: query + ' from the catalog', url: 'https://supplies.example/products/' + query }] } }), [...materials, alsoNeeded]);
  const paper = page.getByRole('group', { name: 'Shopify products for Letter paper', exact: true });
  const card = page.getByRole('group', { name: 'Shopify products for Card stock', exact: true });
  const showPaper = page.getByRole('button', { name: 'Shopify 3 offers for Letter paper', exact: true });
  const showCard = page.getByRole('button', { name: 'Shopify 3 offers for Card stock', exact: true });
  await showPaper.waitFor();
  await showCard.waitFor();
  await page.getByRole('button', { name: 'Shopify 1 offer for Pencil', exact: true }).waitFor();
  assert.equal(await page.locator('#materials a, #materials img').count(), 0);
  assert.equal(await showPaper.getAttribute('aria-expanded'), 'false');
  assert.equal(await showPaper.evaluate(el => document.getElementById(el.getAttribute('aria-controls')).hidden), true);
  await showPaper.press('Enter');
  await paper.waitFor();
  assert.equal(await showPaper.getAttribute('aria-expanded'), 'true');
  assert.equal(await card.count(), 0);
  assert.equal(await paper.getByRole('link', { name: 'Ivory stationery', exact: true }).getAttribute('href'), url);
  assert.equal(await paper.getByRole('link', { name: /Checkout at the store/ }).getAttribute('href'), checkout);
  assert.match(await paper.innerText(), /Acre Paper Co\./);
  assert.match(await paper.innerText(), /USD\s*12\.34/);
  assert.equal(await paper.locator('img').getAttribute('loading'), 'lazy');

  await paper.getByRole('button', { name: 'Next product for Letter paper' }).click();
  assert.match(await paper.innerText(), /JPY\s*1,200/);
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

  await showCard.press('Space');
  await card.waitFor();
  assert.equal(await paper.count(), 0);
  assert.equal(await showPaper.getAttribute('aria-expanded'), 'false');
  assert.match(await card.innerText(), /Ivory stationery/);
  await showPaper.click();
  assert.equal(await card.count(), 0);
  const gather = page.getByRole('checkbox', { name: /Letter paper/ });
  await gather.check();
  assert.equal(await paper.count(), 0);
  assert.equal(await gather.evaluate(el => el === document.activeElement), true);
  await gather.uncheck();
  assert.equal(await showPaper.getAttribute('aria-expanded'), 'false');
  assert.equal(await page.getByRole('checkbox', { name: /Ruler/ }).isChecked(), true);
  assert.equal(await page.getByRole('checkbox', { name: /Pencil/ }).isChecked(), false);
  assert.deepEqual(queries.toSorted(), ['Pencil', 'paper & card']);
  const ruler = page.getByRole('button', { name: /^Shopify .*for Ruler$/ });
  assert.equal(await ruler.getAttribute('aria-expanded'), 'false');
  await ruler.click();
  await page.getByRole('link', { name: 'Ruler from the catalog', exact: true }).waitFor();
  await ruler.click();
  await ruler.click();
  await page.getByRole('link', { name: 'Ruler from the catalog', exact: true }).waitFor();
  assert.deepEqual(queries.toSorted(), ['Pencil', 'Ruler', 'paper & card']);
  assert.equal(await page.locator('a[href*="shop.app/search"]').count(), 0);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
});

test('limits simultaneous searches across required and optional materials, including when a pending item changes groups', { timeout: 15000 }, async t => {
  let active = 0, peak = 0;
  let firstStarted, releaseFirst;
  const firstIsPending = new Promise(resolve => { firstStarted = resolve; });
  const firstMayFinish = new Promise(resolve => { releaseFirst = resolve; });
  t.after(releaseFirst);
  const supplies = ['Paper', 'Glue', 'Ruler', 'Scissors', 'Tape'].map((name, i) => ({ id: name, name, required: i < 3 }));
  const { page, queries } = await openGuide(t, true, async name => {
    active += 1;
    peak = Math.max(peak, active);
    if (name === 'Paper') {
      firstStarted();
      await firstMayFinish;
    }
    else await delay(60);
    active -= 1;
    return { json: { products: [{ title: 'Choice of ' + name, url: 'https://shop.example/' + name }] } };
  }, supplies);
  const toggle = page.getByRole('checkbox', { name: 'Paper', exact: true });
  await firstIsPending;
  await toggle.check();
  await toggle.uncheck();
  await toggle.check();
  await page.getByRole('button', { name: 'Shopify offers for Paper', exact: true }).click();
  await page.getByText('Searching Shopify…', { exact: true }).waitFor();
  releaseFirst();
  await page.getByRole('link', { name: 'Choice of Paper', exact: true }).waitFor();
  for (const item of supplies) await page.getByRole('button', { name: 'Shopify 1 offer for ' + item.name, exact: true }).waitFor();
  assert.equal(peak, 2);
  assert.deepEqual(queries.sort(), supplies.map(item => item.name).sort());
  assert.equal(await page.getByRole('button', { name: /Next product|Previous product|Show product/ }).count(), 0);
});

test('shows the catalog failure or empty result and retries the same material through Shopify', async t => {
  for (const [enabled, response, message] of [
    [false, {}, 'Shopify’s catalog is unavailable.'],
    [true, { status: 502 }, 'Shopify’s catalog is unavailable.'],
    [true, { json: { products: [] } }, 'No matching Shopify products found.'],
  ]) {
    let recovering = false;
    const { page, queries } = await openGuide(t, enabled, () => recovering
      ? { json: { products: [{ title: 'White card stock', url: 'https://supplies.example/products/white' }] } } : response, [materials[0]]);
    assert.equal(await page.getByRole('heading', { name: 'Still needed' }).count(), 0);
    await page.getByRole('button', { name: /^Shopify .*for Letter paper$/ }).click();
    await page.getByText(message, { exact: true }).waitFor();
    assert.equal(await page.locator('#materials a').count(), 0);
    assert.deepEqual(queries, enabled ? ['paper & card'] : []);
    recovering = true;
    await page.getByRole('button', { name: 'Retry Shopify search for Letter paper' }).click();
    await page.getByRole('link', { name: 'White card stock', exact: true }).waitFor();
    assert.deepEqual(queries, enabled ? ['paper & card', 'paper & card'] : ['paper & card']);
    assert.equal(await page.getByRole('button', { name: /Show product/ }).count(), 0);
  }
});

test('fits short checklist rows and long product names at desktop and 320px phone widths', async t => {
  const title = 'Premium bright-white paper for home printing and everyday projects, ' + 'ExtraLongPaperSize'.repeat(5);
  const products = [title, 'Loose sheets', 'Blank cards'].map((title, i) => ({ title, url: 'https://supplies.example/products/' + i }));
  for (const width of [1280, 320]) {
    const { page } = await openGuide(t, true, { json: { products } }, [materials[0]], width);
    const row = page.locator('.supply-row');
    const check = row.locator('label');
    const toggle = row.getByRole('button', { name: 'Shopify 3 offers for Letter paper', exact: true });
    await toggle.waitFor();
    const [rowBox, checkBox, toggleBox] = await Promise.all([row.boundingBox(), check.boundingBox(), toggle.boundingBox()]);
    assert.ok(rowBox.height <= 46, 'closed row stays on a single compact line');
    assert.ok(checkBox.x + checkBox.width <= toggleBox.x);
    if (width === 320) assert.ok(checkBox.height >= 44 && toggleBox.height >= 44);

    await toggle.click();
    const link = row.getByRole('link', { name: title, exact: true });
    await link.waitFor();
    assert.ok(await link.locator('.catalog-title').evaluate(el => el.clientHeight > parseFloat(getComputedStyle(el).lineHeight)));
    if (width === 320) {
      for (const target of await row.locator('a, button').all()) {
        const box = await target.boundingBox();
        assert.ok(box.width >= 44 && box.height >= 44, 'touch targets are at least 44px square');
      }
    }
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await toggle.click();
    assert.equal(await row.getByRole('link').count(), 0);
  }
});
