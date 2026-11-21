import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { chromium } from 'playwright';
import { preview } from 'vite';
import { installYoutubePlayer } from './youtube-player.mjs';

const sample = JSON.parse(await readFile(new URL('../dist/static/example/project.json', import.meta.url)));
const voiceFixture = await readFile(new URL('../../src/nyc_gemini4_hack/web/example/step-2.wav', import.meta.url));
const tutorial = 'https://www.youtube.com/watch?v=DrSupGG7Nvo';
const checkout = 'https://clay-shop.example/cart/123:1?key=signed%2Fvalue';
const product = 'https://clay-shop.example/products/green-clay';
const instruction = 'Press the white disc onto the eye dome and center the pupil on top.';
const segment = { segment_id: 'eyes', source_title: 'Clay frog tutorial', source_url: tutorial,
  start_seconds: 3, end_seconds: 4, hand_action: 'Press the white and black discs onto each eye.' };
const plan = {
  project_title: 'Clay frog keychain', materials: [
    { id: 'mat', name: 'Work mat', status: 'OWNED_EXACT' },
    { id: 'clay', name: 'Green clay', status: 'BUY' },
  ], source_youtube_urls: [tutorial], steps: [
    { step_number: 1, title: 'Shape the head', instruction_text: 'Roll the green clay into a smooth oval.' },
    { step_number: 2, title: 'Layer the eyes', instruction_text: instruction, source_segments: [
      { ...segment, segment_id: 'invalid', start_seconds: 11, end_seconds: 10 }, segment,
      { ...segment, segment_id: 'unknown', source_url: 'https://other.example/watch?v=DrSupGG7Nvo' },
    ] },
    { step_number: 3, title: 'Add the smile', instruction_text: 'Press a curved strip of clay onto the face.' },
    { step_number: 4, title: 'Attach the key ring', instruction_text: 'Thread the ring through the eyelet.', source_segments: [segment] },
  ],
};
const queued = { project_id: 'frog', goal: 'Make a clay frog keychain', status: 'queued', media: {} };
let browser, server, origin;
before(async () => {
  server = await preview({ preview: { host: '127.0.0.1', port: 0 } });
  origin = 'http://127.0.0.1:' + server.httpServer.address().port;
  browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
});
after(async () => { await browser?.close(); await server?.close(); });

async function emit(page, name, data) {
  await page.evaluate(([name, data]) => window.stream.dispatchEvent(new MessageEvent(name, {
    data: JSON.stringify(data),
  })), [name, data]);
}

for (const width of [1280, 320]) {
  test('shops while generating and recovers a withheld chapter at ' + width + 'px', { timeout: 25000 }, async t => {
    const page = await browser.newPage({ viewport: { width, height: 850 } });
    const errors = [], external = [];
    let state = queued;
    page.on('pageerror', error => errors.push(error.message));
    t.after(async () => { await page.close(); assert.deepEqual(errors, []); });
    await page.addInitScript(installYoutubePlayer);
    await page.addInitScript(() => {
      window.EventSource = class extends EventTarget {
        constructor() { super(); window.stream = this; }
        close() { this.closed = true; }
      };
    });
    await page.route('**/*', route => {
      const url = new URL(route.request().url());
      if (url.origin !== origin) {
        external.push(url.href);
        return route.fulfill({ contentType: 'text/html', body: '<html lang="en"><title>Source clip</title></html>' });
      }
      if (url.pathname === '/api/health') return route.fulfill({ json: {
        gemini_configured: true, youtube_configured: true, capabilities: { material_catalog: true },
      } });
      if (url.pathname === '/api/projects/frog') return route.fulfill({ json: state });
      if (url.pathname === '/api/shopping/search') return route.fulfill({ json: {
        products: [{ title: 'Green modeling clay', url: product }],
      } });
      if (['/media/frog/smile.mp4', '/media/frog/eyes.mp4'].includes(url.pathname)) return route.fulfill({
        status: 307, headers: { Location: origin + sample.full_video_url },
      });
      if (url.pathname === '/media/frog/eyes.wav') return route.fulfill({ contentType: 'audio/wav', body: voiceFixture });
      return route.continue();
    });
    await page.goto(origin + '/?project=frog');
    await page.waitForFunction(() => window.stream);
    const shop = page.getByRole('link', { name: 'Shop materials' });
    assert.equal(await shop.count(), 0);
    await emit(page, 'plan_ready', plan);
    await emit(page, 'status', { status: 'generating', message: 'Rendering chapters' });
    await shop.waitFor();
    assert.ok(await shop.evaluate(el => el.getBoundingClientRect().bottom < innerHeight));
    assert.equal(await page.locator('video, iframe').count(), 0);
    await shop.press('Enter');
    const offer = page.getByRole('link', { name: 'Green modeling clay' });
    await offer.waitFor();
    assert.equal(await offer.getAttribute('href'), product);
    assert.equal(await page.getByRole('checkbox', { name: 'Work mat' }).isChecked(), true);
    await page.waitForFunction(() => document.activeElement.id === 'supplies-title');
    await emit(page, 'cart_ready', { checkout_url: 'https://trusted.example@other.example/cart' });
    assert.equal(await page.getByRole('link', { name: /Review cart/ }).count(), 0);
    await emit(page, 'cart_ready', { checkout_url: checkout });
    assert.equal(await page.getByRole('link', { name: /Review cart at clay-shop.example/ }).getAttribute('href'), checkout);

    async function chapter(index) {
      if (width < 851) await page.locator('#step-picker > summary').click();
      await page.locator(width < 851 ? '#mobile-step-list button' : '#step-list button').nth(index).click();
    }
    const reviewNote = 'The smile lands a little too low on the face.';
    const third = { step_number: 3, status: 'ready', video_url: '/media/frog/smile.mp4', media_mode: 'illustrated_frames',
      illustration_review: { status: 'rejected', result: { verdict: 'contradicts', observation: reviewNote } } };
    await emit(page, 'step_media_started', { step_number: 3, status: 'generating' });
    await emit(page, 'activity', { id: 'motion-3', step_number: 3, stage: 'motion', state: 'failed',
      label: 'Chapter footage', agent: 'Gemini', detail: 'The first video file could not be decoded.' });
    const outline = page.locator(width < 851 ? '#mobile-step-list button' : '#step-list button');
    assert.equal(await outline.nth(2).getAttribute('data-chapter-state'), 'rendering');
    await emit(page, 'step_media_ready', third);
    await chapter(2);
    await page.waitForFunction(() => document.querySelector('#chapter-video')?.currentTime > 0);
    await page.getByText('Footage review note').click();
    await page.getByText(reviewNote).waitFor();
    await page.locator('#chapter-video').evaluate(el => { window.chapterPlayer = el; });
    await shop.click();
    assert.equal(await offer.isVisible(), true);
    assert.ok(await page.locator('#chapter-video').evaluate(el => el === window.chapterPlayer && !el.paused));

    const observation = 'The rendered hand pressed a disc between the two eye domes.';
    const reason = 'Step 2 footage was withheld after visual review.';
    const detail = 'The second take also showed the disc in the wrong position. The illustration review timed out before it could verify the position of both eye mounds.';
    const second = { step_number: 2, status: 'error', error: reason + ' ' + detail,
      audio_url: '/media/frog/eyes.wav',
      shot_reviews: [{ status: 'rejected', contact: { verdict: 'contradicts', observation } }, { status: 'unavailable' }] };
    const first = { step_number: 1, status: 'error', error: 'Step 1 footage was withheld after visual review.',
      audio_url: '/media/frog/head.wav' };
    await emit(page, 'step_media_error', second);
    await emit(page, 'step_media_error', first);
    await emit(page, 'activity', { id: 'motion-4', stage: 'motion', state: 'started', step_number: 4,
      label: 'Generate motion', timestamp: '2026-11-21T19:00:00Z' });
    const chapterList = page.locator(width < 851 ? '#mobile-step-list' : '#step-list');
    await chapterList.locator('button[data-chapter-state="error"]').nth(1).waitFor({ state: 'attached' });
    assert.equal(await chapterList.locator('small').nth(0).textContent(), 'Narration available');
    assert.equal(await chapterList.locator('small').nth(1).textContent(), 'Narration available');
    await chapter(1);
    await page.getByText(instruction, { exact: true }).waitFor();
    const panel = page.locator('.player > #chapter-panel');
    await panel.getByText('Step 2 · Video unavailable').waitFor();
    await panel.getByText('Layer the eyes', { exact: true }).waitFor();
    assert.equal(await panel.getByText(/Step 4|Creating motion/).count(), 0);
    const progress = page.locator('#workflow .workflow-progress');
    await progress.getByText('Creating motion', { exact: true }).waitFor();
    await progress.getByText('Step 4 · Attach the key ring').waitFor();
    const narration = panel.getByLabel('Narration for Layer the eyes');
    assert.equal(await narration.getAttribute('src'), second.audio_url);
    await page.waitForFunction(() => document.querySelector('#chapter-panel audio')?.duration > 0);
    await narration.evaluate(el => el.play());
    await page.waitForFunction(() => document.querySelector('#chapter-panel audio')?.currentTime > 0);
    await panel.locator('.chapter-issue').getByText(reason, { exact: true }).waitFor();
    assert.equal(await panel.getByText(detail).isVisible(), false);
    await panel.getByText('What the footage review found').click();
    await panel.getByText(detail).waitFor();
    await panel.getByText(observation).waitFor();
    const source = panel.getByRole('button', { name: /Preview source clip for Layer the eyes/ });
    assert.equal(await source.count(), 1);
    assert.deepEqual(external, []);
    assert.equal(await page.locator('iframe').count(), 0);
    assert.equal(await panel.getByRole('link', { name: 'Open Clay frog tutorial on YouTube at 0:03' }).getAttribute('href'), tutorial + '&t=3s');
    await source.press('Enter');
    const frame = page.getByTitle('Layer the eyes · Source clip 0:03–0:04');
    await frame.waitFor();
    assert.equal(await page.locator('audio').count(), 0);
    const url = new URL(await frame.getAttribute('src'));
    assert.equal(url.origin + url.pathname, 'https://www.youtube-nocookie.com/embed/DrSupGG7Nvo');
    assert.equal(url.searchParams.get('start'), '3');
    assert.equal(url.searchParams.get('end'), '4');
    await page.waitForFunction(() => document.activeElement.id === 'chapter-source-video');
    assert.ok(await frame.evaluate(el => el.closest('.player') && el.clientWidth <= innerWidth));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.evaluate(() => window.youtubePlayers.at(-1).error(101));
    const restricted = page.locator('.player > .source-player .source-player-error');
    await restricted.getByText('This tutorial can only be watched on YouTube.').waitFor();
    assert.equal(await restricted.getByRole('link', { name: 'Open on YouTube' }).getAttribute('href'), tutorial + '&t=3s');
    await page.getByRole('button', { name: 'Back to guide' }).click();
    await page.waitForFunction(() => document.activeElement.textContent.includes('Preview source clip'));
    assert.equal(await page.locator('iframe').count(), 0);
    assert.deepEqual(external, [url.href]);
    const recovered = { step_number: 2, status: 'ready', media_mode: 'motion', video_url: '/media/frog/eyes.mp4' };
    await emit(page, 'step_media_ready', recovered);
    await page.getByRole('button', { name: 'Watch this chapter' }).click();
    await page.waitForFunction(() => document.querySelector('#chapter-video')?.currentTime > .05);
    assert.equal(await page.locator('#chapter-video').getAttribute('src'), recovered.video_url);
    assert.equal(await panel.count(), 0);
    state = { ...queued, plan, status: 'partial', cart: { checkout_url: checkout }, media: { 1: first, 2: second, 3: third } };
    await page.reload();
    await page.getByRole('heading', { name: 'Layer the eyes' }).waitFor();
    await source.waitFor();
    assert.equal(await page.evaluate(() => Boolean(window.stream)), false);
    assert.equal(await chapterList.locator('small').nth(3).textContent(), 'YouTube reference');
    await chapter(0);
    assert.equal(await page.getByLabel('Narration for Shape the head').getAttribute('src'), first.audio_url);
    assert.equal(await page.getByRole('button', { name: /Preview source clip/ }).count(), 0);
    assert.equal(await page.getByRole('link', { name: /Full source tutorial on YouTube/ }).getAttribute('href'), tutorial);
    assert.equal(await page.locator('iframe').count(), 0);
    await page.getByText(plan.steps[0].instruction_text, { exact: true }).waitFor();
    assert.deepEqual(external, [url.href]);
  });
}
