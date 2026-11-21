import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';
import { preview } from 'vite';

const sample = JSON.parse(await readFile(new URL('../dist/static/example/project.json', import.meta.url), 'utf8'));
const steps = ['Fold the center crease', 'Open the wings'].map((title, index) => ({
  title, step_number: index + 1, instruction_text: 'Line up the corners before folding.',
  narration_script: 'Press the fold gently.', duration_seconds: 9,
  physical_state: { start_state: 'A flat sheet of paper.', hand_kinematics: 'Run your thumb along the crease.', end_state: 'The edges align.' },
}));
const plan = {
  project_title: 'Paper glider', steps, difficulty: 'beginner', estimated_time_minutes: 10,
  source_youtube_urls: [
    'https://youtu.be/T5I6Jez-pNI', 'https://www.youtube.com/watch?v=T5I6Jez-pNI',
    'https://example.test/watch?v=T5I6Jez-pNI', 'https://www.youtube.com/redirect?q=other',
  ],
  materials: [
    { id: 'paper', name: 'Paper', quantity: '1 sheet', status: 'OWNED_EXACT' },
    { id: 'pencil', name: 'Pencil', quantity: '1', status: 'BUY' },
  ],
};
const media = {
  1: { step_number: 1, status: 'ready', duration_seconds: 2.375, chapter_start_seconds: 0 },
  2: { step_number: 2, status: 'ready', duration_seconds: 3, chapter_start_seconds: 2.375 },
};
const completed = {
  project_id: 'glider', goal: 'Fold paper', owned_items: ['Paper'], plan, status: 'completed',
  status_message: 'Your guide is ready.', media, full_video_url: '/media/glider/full.mp4', full_captions_url: '/media/glider/full.vtt',
};
let browser, server, origin;
before(async () => {
  server = await preview({ preview: { host: '127.0.0.1', port: 0 } });
  origin = 'http://127.0.0.1:' + server.httpServer.address().port;
  browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
});
after(async () => { await browser?.close(); await server?.close(); });

async function workbench(t, { width = 1100, state = completed, postStatus = 202, configured = true, health = {} } = {}) {
  const page = await browser.newPage({ viewport: { width, height: 850 } });
  const errors = [];
  t.after(async () => { await page.close(); assert.deepEqual(errors, []); });
  page.on('pageerror', error => errors.push(error.message));
  const submissions = [];
  await page.addInitScript(() => {
    window.EventSource = class Stream extends EventTarget {
      static CLOSED = 2;
      constructor(url) { super(); this.url = url; this.readyState = 1; window.streams = [...(window.streams || []), this]; }
      close() { this.closed = true; this.readyState = 2; }
    };
  });
  await page.route('**/api/**', async route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === '/api/health') return route.fulfill({ json: { gemini_configured: configured, youtube_configured: configured, ...health } });
    if (pathname === '/api/projects' && route.request().method() === 'POST') {
      submissions.push(route.request().postData());
      return route.fulfill({ status: postStatus, json: { project_id: 'glider', status: 'queued' } });
    }
    if (pathname === '/api/projects/glider') return route.fulfill({ json: state });
    return route.fulfill({ status: 404 });
  });
  await page.route('**/media/glider/full.*', route => route.fulfill({
    status: 307, headers: { location: origin + (route.request().url().endsWith('.mp4') ? sample.full_video_url : sample.full_captions_url) },
  }));
  return { page, submissions };
}

async function emit(page, name, data) {
  await page.waitForFunction(() => window.streams?.length);
  await page.evaluate(([eventName, payload]) => {
    window.streams.at(-1).dispatchEvent(new MessageEvent(eventName, { data: JSON.stringify(payload) }));
  }, [name, data]);
}

async function createGuide(page) {
  await page.goto(origin);
  await page.getByText('Services ready').waitFor();
  await page.locator('#new-guide').click();
  await page.getByLabel('Your project').fill('Fold a paper glider');
  await page.getByLabel('Supplies you have').fill('Paper');
  await page.getByRole('button', { name: 'Generate guide' }).click();
  await page.waitForURL('**/?project=glider');
}

async function capture(page, name) {
  if (process.env.WORKBENCH_SCREENSHOTS) {
    await mkdir(process.env.WORKBENCH_SCREENSHOTS, { recursive: true });
    await page.screenshot({ path: path.join(process.env.WORKBENCH_SCREENSHOTS, name + '.png') });
  }
}

test('streams immediately readable steps and preserves the checklist through a partial result', async t => {
  const { page, submissions } = await workbench(t);
  await createGuide(page);
  assert.match(submissions[0], /Fold a paper glider/);
  await emit(page, 'plan_ready', plan);
  await emit(page, 'status', { status: 'generating', message: 'Preparing the film and narration.' });
  await page.getByRole('heading', { name: steps[0].title }).waitFor();
  assert.equal(await page.getByText('Run your thumb along the crease.').isVisible(), true);
  assert.deepEqual(await page.locator('#source-links a').evaluateAll(links => links.map(link => link.href)),
    ['https://www.youtube.com/watch?v=T5I6Jez-pNI']);
  await capture(page, 'desktop-generating');
  assert.equal(await page.getByRole('button', { name: 'Previous step' }).isDisabled(), true);
  assert.equal(await page.getByRole('checkbox', { name: /Paper/ }).isChecked(), true);
  await page.getByRole('checkbox', { name: /Pencil/ }).check();
  const clip = { ...media[1], video_url: '/media/glider/step1.mp4' };
  await emit(page, 'step_media_ready', clip);
  await emit(page, 'step_media_error', { step_number: 2, status: 'error' });
  assert.equal(await page.locator('video').count(), 0);
  await page.getByRole('button', { name: 'Next step' }).click();
  await page.waitForFunction(() => document.activeElement.id === 'step-title');
  await emit(page, 'snapshot', { ...completed, status: 'generating', full_video_url: null, media: { 1: clip } });
  assert.equal(await page.locator('#supply-count').textContent(), '2 of 2 gathered');
  assert.equal(await page.locator('#step-title').textContent(), steps[1].title);
  await emit(page, 'status', { status: 'partial', message: 'The full video could not be assembled. The written guide is available.' });
  await page.getByText('The full video could not be assembled. The written guide is available.').waitFor();
  assert.equal(await page.evaluate(() => window.streams.at(-1).closed), true);
  assert.equal(await page.locator('video').count(), 0);
  await page.getByRole('button', { name: 'Edit project' }).click();
  assert.equal(await page.getByLabel('Supplies you have').inputValue(), 'Paper');
});

test('restores a finished guide, measured chapter position and checklist at narrow phone width', async t => {
  const { page } = await workbench(t, { width: 320 });
  await page.goto(origin + '/?project=glider');
  await page.getByRole('heading', { name: plan.project_title }).waitFor();
  assert.equal(await page.locator('#step-title').evaluate(node => node.getBoundingClientRect().bottom < innerHeight), true);
  await page.getByRole('button', { name: 'New guide' }).click();
  assert.equal(await page.getByLabel('Your project').evaluate(node => node === document.activeElement), true);
  await page.keyboard.press('Escape');
  assert.equal(await page.getByRole('dialog').isVisible(), false);
  assert.equal(await page.locator('video').getAttribute('src'), completed.full_video_url);
  await page.getByRole('checkbox', { name: /Pencil/ }).check();
  await page.getByLabel('Jump to step').selectOption('1');
  await page.reload();
  await page.getByRole('heading', { name: steps[1].title }).waitFor();
  await page.waitForFunction(time => Math.abs(document.querySelector('video').currentTime - time) < 0.01, media[2].chapter_start_seconds);
  assert.equal(await page.getByRole('checkbox', { name: /Pencil/ }).isChecked(), true);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.equal(await page.evaluate(() => !!window.streams), false);
  await capture(page, 'phone-restored');
});

test('handles whitespace, submission failures and stale guide links', async t => {
  const { page, submissions } = await workbench(t, { postStatus: 503 });
  await page.goto(origin + '/?project=expired');
  await page.locator('#status').filter({ hasText: 'could not be found' }).waitFor();
  await page.locator('#new-guide').click();
  await page.getByLabel('Your project').fill('   ');
  await page.getByRole('button', { name: 'Generate guide' }).click();
  assert.equal(submissions.length, 0);
  await page.getByLabel('Your project').fill('Make a paper crane');
  await page.getByRole('button', { name: 'Generate guide' }).click();
  await page.locator('#form-status').filter({ hasText: 'Could not start' }).waitFor();
  assert.equal(await page.getByRole('button', { name: 'Generate guide' }).isDisabled(), false);
});

test('honors deployment readiness and keeps the bundled guide playable', async t => {
  const reason = 'Guide generation needs a media worker on this deployment.';
  const { page, submissions } = await workbench(t, { health: { generation_ready: false, generation_message: reason } });
  await page.goto(origin);
  await page.waitForFunction(() => document.querySelector('video')?.readyState >= 1);
  await page.locator('#new-guide').click();
  await page.locator('#form-status').filter({ hasText: reason }).waitFor();
  assert.equal(await page.getByRole('button', { name: 'Generate guide' }).isDisabled(), true);
  assert.deepEqual(submissions, []);
});

test('recovers a stream and keeps the same film and focus as new media updates arrive', async t => {
  const { page } = await workbench(t);
  await createGuide(page);
  await page.waitForFunction(() => window.streams?.length);
  await page.evaluate(() => window.streams.at(-1).onerror(new Event('error')));
  await page.getByText(/Reconnecting to your guide/).waitFor();
  await emit(page, 'snapshot', { ...completed, status: 'generating', full_video_url: null, media: {} });
  await page.getByRole('heading', { name: steps[0].title }).waitFor();
  await page.getByRole('button', { name: 'Next step' }).click();
  await emit(page, 'full_video_ready', { video_url: completed.full_video_url, captions_url: completed.full_captions_url, media });
  const video = page.locator('video');
  await page.waitForFunction(time => Math.abs(document.querySelector('video')?.currentTime - time) < 0.01, media[2].chapter_start_seconds);
  assert.equal(await video.locator('track').getAttribute('src'), completed.full_captions_url);
  await video.evaluate(node => { window.originalFilm = node; });
  await page.getByRole('checkbox', { name: /Pencil/ }).check();
  await emit(page, 'snapshot', completed);
  assert.equal(await video.evaluate(node => node === window.originalFilm && node.currentTime === 2.375), true);
  assert.equal(await page.getByRole('checkbox', { name: /Pencil/ }).evaluate(node => node === document.activeElement), true);
  assert.equal(await page.evaluate(() => window.streams.at(-1).closed), true);
});

test('distinguishes a permanent disconnect and a generation failure, keeping written steps available', async t => {
  const state = { ...completed, status: 'generating', full_video_url: null, media: { 1: { step_number: 1, status: 'skipped' } } };
  const { page } = await workbench(t, { state });
  await page.goto(origin + '/?project=glider');
  await page.getByRole('heading', { name: steps[0].title }).waitFor();
  await page.evaluate(() => {
    const source = window.streams.at(-1);
    source.readyState = EventSource.CLOSED;
    source.onerror(new Event('error'));
  });
  await page.getByText(/Live updates stopped/).waitFor();
  assert.equal(await page.locator('video').count(), 0);
  await page.reload();
  await emit(page, 'project_error', { status: 'failed', message: 'Generation failed.' });
  await page.getByText('Generation failed.').waitFor();
  assert.equal(await page.locator('#status').getAttribute('data-kind'), 'error');
  assert.equal(await page.evaluate(() => window.streams.at(-1).closed), true);
  assert.equal(await page.getByRole('heading', { name: steps[0].title }).isVisible(), true);
});

test('plays the real bundled film and captions, seeks chapters, follows playback and fits on phones', async t => {
  const { page, submissions } = await workbench(t, { width: 1440, configured: false });
  await page.goto(origin);
  await page.getByRole('heading', { name: sample.plan.project_title, exact: true }).waitFor();
  assert.equal(await page.locator('#guide-kind').textContent(), 'Example guide');
  assert.equal(await page.locator('video').count(), 1);
  const video = page.locator('video');
  await page.waitForFunction(() => document.querySelector('video')?.readyState >= 1);
  assert.equal(await video.getAttribute('src'), sample.full_video_url);
  assert.equal(await video.getAttribute('poster'), sample.media[sample.plan.steps[0].step_number].keyframe_url);
  assert.equal(await video.evaluate(node => node.paused), true);
  await video.evaluate(node => { window.originalFilm = node; node.textTracks[0].mode = 'showing'; });
  await page.waitForFunction(() => document.querySelector('video').textTracks[0].cues?.length > 0);
  assert.ok(await video.evaluate(node => node.duration > 1 && node.videoWidth > 0));
  await capture(page, 'desktop-guide');
  await page.locator('#new-guide').click();
  await page.locator('#form-status').filter({ hasText: 'configured on the server' }).waitFor();
  assert.equal(await page.getByRole('button', { name: 'Generate guide' }).isDisabled(), true);
  await page.keyboard.press('Escape');
  const secondStart = sample.media[sample.plan.steps[1].step_number].chapter_start_seconds;
  const outline = page.locator('#step-list button');
  await outline.nth(1).click();
  await page.waitForFunction(time => Math.abs(document.querySelector('video').currentTime - time) < 0.01, secondStart);
  assert.equal(await outline.nth(1).getAttribute('aria-current'), 'step');
  assert.equal(await video.evaluate(node => node === window.originalFilm && node.paused && node.textTracks[0].mode === 'showing'), true);
  await page.locator('#transcript summary').click();
  await page.getByText(sample.plan.steps[1].narration_script, { exact: true }).waitFor();
  await video.evaluate((node, time) => { node.currentTime = time - 0.6; }, secondStart);
  await page.getByRole('heading', { name: sample.plan.steps[0].title }).waitFor();
  await video.evaluate(node => node.play());
  await page.getByRole('heading', { name: sample.plan.steps[1].title }).waitFor();
  const thirdStart = sample.media[sample.plan.steps[2].step_number].chapter_start_seconds;
  await outline.nth(2).click();
  await page.waitForFunction(time => document.querySelector('video').currentTime >= time, thirdStart);
  assert.equal(await video.evaluate(node => node === window.originalFilm && !node.paused), true);
  await video.evaluate(node => node.pause());
  assert.deepEqual(submissions, []);
  await page.setViewportSize({ width: 375, height: 760 });
  await page.getByLabel('Jump to step').selectOption('0');
  await page.evaluate(() => window.scrollTo(0, 0));
  await capture(page, 'phone-guide');
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.equal(await video.evaluate(node => node.getBoundingClientRect().bottom <= innerHeight), true);
});

test('opens the dedicated Live Build Coach screen from the guide and returns cleanly', async t => {
  const { page } = await workbench(t);
  await page.goto(origin + '/?project=glider');
  await page.getByRole('heading', { name: plan.project_title }).waitFor();
  await page.getByRole('button', { name: "Let's build now" }).click();
  await page.getByRole('heading', { name: plan.project_title }).waitFor();
  assert.equal(await page.getByRole('button', { name: 'Start live coach' }).isEnabled(), true);
  assert.equal(await page.getByText(steps[0].title).first().isVisible(), true);
  await page.getByRole('button', { name: 'Next step' }).click();
  assert.equal(await page.getByRole('heading', { name: steps[1].title }).isVisible(), true);
  await page.getByRole('button', { name: '← Back to guide' }).click();
  await page.getByRole('heading', { name: steps[1].title }).waitFor();
});
