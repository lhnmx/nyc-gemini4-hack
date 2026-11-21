import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';
import { preview } from 'vite';

const sample = JSON.parse(await readFile(new URL('../dist/static/example/project.json', import.meta.url), 'utf8'));
const exampleDir = new URL('../../src/nyc_gemini4_hack/web/example/', import.meta.url);
const sourceExample = JSON.parse(await readFile(new URL('project.json', exampleDir), 'utf8'));
const clipFixture = Object.values(sourceExample.media).find(asset => asset.video_url && asset.captions_url);
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
  await page.route(/\/media\/glider\/(?:chapter-|step)(\d+)\.(mp4|vtt)$/, async route => {
    const type = new URL(route.request().url()).pathname.endsWith('.mp4') ? 'mp4' : 'vtt';
    const url = type === 'mp4' ? clipFixture.video_url : clipFixture.captions_url;
    const fixture = new URL(url.slice('/static/example/'.length), exampleDir);
    const body = await readFile(fixture);
    const contentType = type === 'mp4' ? 'video/mp4' : 'text/vtt';
    // Serve byte ranges just like the media endpoint so Chrome can seek in a chapter.
    const range = /^bytes=(\d+)-(\d*)$/.exec(route.request().headers().range || '');
    if (!range) return route.fulfill({ body, contentType });
    const start = Number(range[1]);
    const end = range[2] ? Math.min(Number(range[2]), body.length - 1) : body.length - 1;
    return route.fulfill({ status: 206, contentType, body: body.subarray(start, end + 1),
      headers: { 'Accept-Ranges': 'bytes', 'Content-Range': `bytes ${start}-${end}/${body.length}` } });
  });
  return { page, submissions };
}

async function emit(page, name, data) {
  await page.waitForFunction(() => window.streams?.length);
  await page.evaluate(([eventName, payload]) => {
    window.streams.at(-1).dispatchEvent(new MessageEvent(eventName, { data: JSON.stringify(payload) }));
  }, [name, data]);
}

async function selectMobileStep(page, index) {
  await page.locator('.mobile-picker > summary').click();
  await page.locator('#mobile-step-list button').nth(index).click();
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
  assert.equal(await page.getByText('Run your thumb along the crease.').isVisible(), false);
  await page.getByText('Technique & tips').click();
  assert.equal(await page.getByText('Run your thumb along the crease.').isVisible(), true);
  await page.getByText('Technique & tips').click();
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
  await page.getByText('Watch this chapter').click();
  assert.equal(await page.getByLabel(steps[0].title + ' · Chapter video').getAttribute('src'), clip.video_url);
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
  await selectMobileStep(page, 1);
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
  await selectMobileStep(page, 0);
  await page.evaluate(() => window.scrollTo(0, 0));
  await capture(page, 'phone-guide');
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.equal(await video.evaluate(node => node.getBoundingClientRect().bottom <= innerHeight), true);
});

test('shows the film surface before submission returns and preserves it as the worker accepts the goal', async t => {
  const { page } = await workbench(t, { width: 375 });
  let accept;
  const request = new Promise(resolve => { accept = resolve; });
  await page.route('**/api/projects', async route => {
    await request;
    return route.fulfill({ status: 202, json: { project_id: 'glider', status: 'queued' } });
  });
  await page.goto(origin);
  await page.getByRole('button', { name: 'New guide' }).click();
  await page.getByLabel('Your project').fill('Fold a paper envelope');
  await page.getByRole('button', { name: 'Generate guide' }).click();
  try {
    await page.getByText('Sending your project').waitFor();
    assert.equal(await page.getByRole('dialog').isVisible(), false);
    const box = await page.locator('.player').boundingBox();
    assert.ok(Math.abs(box.width / box.height - 16 / 9) < .02);
    assert.equal(await page.getByRole('heading', { name: 'Fold a paper envelope' }).isVisible(), true);
  } finally { accept(); }
  await page.getByText('Your guide is queued').waitFor();
  assert.equal(await page.locator('.progress li[data-state=active]').innerText(), 'Queue\nIn progress');
  await capture(page, 'phone-queued');
});

test('shows observed contributors, decisions, measured media progress and concurrent stages through reconnects', async t => {
  const { page } = await workbench(t, { width: 1440 });
  await createGuide(page);
  const stage = (id, label, stage, state, agent, model, offset, step_number) => ({
    id, stage, state, label, agent, model, step_number, timestamp: '2026-11-21T14:30:' + offset + 'Z',
  });
  const screening = stage('screen', 'Choose source tutorials', 'research', 'completed', 'Research agent', 'decision-model', '02');
  const planning = stage('plan', 'Write the build plan', 'planning', 'started', 'Planner', 'planner-model', '06');
  const motion = stage('motion1', 'Create footage', 'motion', 'started', 'Motion generator', 'motion-model', '17', 1);
  const voice = stage('voice1', 'Record narration', 'narration', 'started', 'Narrator', null, '18', 1);
  await emit(page, 'status', { status: 'researching', message: 'Searching sources.' });
  await page.getByText('Researching tutorials').waitFor();
  await emit(page, 'activity', screening);
  const decisions = [
    { gate_name: 'video_1_relevance', choice: 'KEEP_TUTORIAL', model_used: 'decision-model', via_decisions_api: true },
    { gate_name: 'video_2_relevance', choice: 'REJECT', model_used: 'fallback-model', via_decisions_api: false },
  ];
  await emit(page, 'research_decisions', { decisions });
  await page.locator('#generation-activity > summary').filter({ hasText: '2 decisions' }).waitFor();
  await emit(page, 'status', { status: 'planning', message: 'Planning.' });
  await emit(page, 'activity', planning);
  await page.getByText('Planning your guide').waitFor();
  await page.locator('#generation-activity > summary').click();
  await page.getByText('Started · Planner · planner-model', { exact: true }).waitFor();
  const finishedPlan = { ...planning, state: 'completed', timestamp: '2026-11-21T14:30:16Z' };
  await emit(page, 'activity', finishedPlan);
  await page.getByText('Completed · Planner · planner-model', { exact: true }).waitFor();
  assert.equal(await page.locator('.activity-list li').count(), 2);
  assert.equal(await page.locator('.activity-list time').last().getAttribute('datetime'), '2026-11-21T14:30:16.000Z');
  await page.getByText('Research decisions (2)').click();
  const researchHistory = page.locator('.decision-detail').filter({ hasText: 'Research decisions' });
  await researchHistory.getByText('Decisions API · decision-model', { exact: true }).waitFor();
  await researchHistory.getByText('Model decision · fallback-model', { exact: true }).waitFor();
  await page.getByText('Research decisions (2)').click();
  await emit(page, 'plan_ready', plan);
  const media_progress = { stage: 'footage', step_number: 1, step_count: 2, ready_count: 0, elapsed_seconds: 12 };
  await emit(page, 'status', { status: 'generating', message: 'Preparing the first chapter.', media_progress });
  await page.getByText('Creating chapter footage').waitFor();
  assert.equal(await page.locator('#step-list button').first().getAttribute('data-chapter-state'), 'rendering');
  await emit(page, 'activity', motion);
  await emit(page, 'activity', voice);
  await page.getByText('Creating motion and narration').waitFor();
  await page.getByText('Media time 0:12').waitFor();
  assert.equal(await page.locator('.progress.compact [data-state=active]').count(), 2);
  await capture(page, 'desktop-activity');
  await emit(page, 'snapshot', { ...completed, full_video_url: null, status: 'generating', media: {},
    activity: [screening, finishedPlan, motion, voice], decisions_log: decisions, media_progress });
  assert.equal(await page.locator('.activity-list li').count(), 4);
  const secondVoice = { ...voice, id: 'voice2', step_number: 2 };
  await emit(page, 'activity', secondVoice);
  assert.equal(await page.locator('#step-list button').nth(1).getAttribute('data-chapter-state'), 'queued');
  await emit(page, 'activity', { ...secondVoice, state: 'completed' });
  // The last footage status is still present while narration and encoding continue.
  await emit(page, 'activity', { ...motion, state: 'completed' });
  await page.getByText('Recording narration', { exact: true }).waitFor();
  assert.equal(await page.locator('.progress.compact [data-state=active]').count(), 1);
  assert.equal(await page.locator('#step-list button').first().getAttribute('data-chapter-state'), 'waiting');
  await emit(page, 'activity', { ...voice, state: 'completed' });
  const assembly = stage('assembly1', 'Encode chapter', 'assembly', 'started', 'FFmpeg', null, '40', 1);
  await emit(page, 'activity', assembly);
  await page.getByText('Assembling chapter 1', { exact: true }).waitFor();
  await emit(page, 'activity', { ...assembly, state: 'completed' });
  await emit(page, 'status', { status: 'generating', message: 'Joining the chapters.', media_progress: {
    stage: 'stitching', step_number: null, step_count: 2, ready_count: 2, elapsed_seconds: 42,
  } });
  await emit(page, 'activity', stage('assembly-film', 'Full film assembly', 'assembly', 'started', 'FFmpeg', null, '42'));
  await page.getByText('Assembling the full film').waitFor();
  await page.getByText('2 of 2 chapters ready').waitFor();
  await emit(page, 'step_media_ready', { ...media[1], video_model: 'recorded-video-model', voice_model: 'Local speech' });
  await page.getByText('Chapter production (1)').click();
  await page.getByText('Footage · recorded-video-model', { exact: true }).waitFor();
  await page.getByText('Narration · Local speech', { exact: true }).waitFor();
});

test('keeps source choices and live agent work in the same focused film panel across phases on a phone', async t => {
  const state = { project_id: 'glider', goal: 'Fold a long-distance paper glider', status: 'researching', media: {} };
  const { page } = await workbench(t, { state, width: 320 });
  await page.goto(origin + '/?project=glider');
  const panel = page.locator('#generation-panel');
  const feed = panel.getByRole('list', { name: 'Recent agent operations' });
  const choices = panel.getByRole('list', { name: 'Tutorial choices' });
  const source = 'A stable paper glider with symmetric wings';
  const skipped = 'Paper airplane flight showcase';
  const search = { id: 'source-search', stage: 'research', label: 'Find tutorials', agent: 'YouTube Data API v3',
    state: 'started', detail: state.goal, timestamp: '2026-11-21T14:30:01Z' };
  await emit(page, 'activity', search);
  await feed.getByText(state.goal).waitFor();
  await feed.focus();
  await feed.evaluate(node => {
    window.firstFeed = node;
    window.firstPanel = node.closest('#generation-panel');
    window.firstStream = window.streams.at(-1);
  });
  await emit(page, 'activity', { ...search, state: 'completed', detail: source + ' · ' + skipped });
  const decisions = [
    { gate_name: 'video_1_relevance', source_title: source, choice: 'KEEP_TUTORIAL', model_used: 'observed-selector',
      via_decisions_api: true, rationale: 'Shows the center crease and both wing folds.' },
    { gate_name: 'video_2_relevance', source_title: skipped, choice: 'SKIP_FILLER', model_used: 'observed-fallback',
      via_decisions_api: false, rationale: 'Shows the flight without demonstrating the folds.' },
  ];
  await emit(page, 'research_decisions', { decisions });
  const kept = choices.getByRole('listitem').filter({ hasText: source });
  const rejected = choices.getByRole('listitem').filter({ hasText: skipped });
  await kept.getByText('Keep tutorial').waitFor();
  await kept.getByText(decisions[0].rationale).waitFor();
  await kept.getByText('Decisions API · observed-selector').waitFor();
  await rejected.getByText('Skip tutorial').waitFor();
  await rejected.getByText('Model decision · observed-fallback').waitFor();
  assert.equal(await page.locator('#generation-activity').getAttribute('open'), null);
  const analysis = { ...search, id: 'analyze-source', label: 'Analyze tutorial footage', agent: 'Research agent',
    model: 'observed-video-analyst', detail: source, timestamp: '2026-11-21T14:30:05Z' };
  await emit(page, 'activity', analysis);
  const analyzeRow = feed.locator('[data-operation-id="analyze-source"]');
  await analyzeRow.getByText('In progress').waitFor();
  await analyzeRow.getByText(source).waitFor();
  await analyzeRow.getByText('Research agent · observed-video-analyst').waitFor();
  await capture(page, 'phone-agent-research');
  const observed = { ...analysis, state: 'completed', detail: source + ' · 0:21 Align the paper corners.' };
  await emit(page, 'activity', observed);
  await emit(page, 'status', { status: 'planning', message: 'Organizing the materials and working out each step.' });
  const planning = { id: 'physical-planning', stage: 'planning', label: 'Check physical continuity', state: 'started',
    agent: 'Physical planner', model: 'observed-planner', detail: steps.map(step => step.title).join(' · '),
    timestamp: '2026-11-21T14:30:14Z' };
  await emit(page, 'activity', planning);
  const planner = feed.locator('[data-operation-id="physical-planning"]');
  await planner.getByText(planning.detail).waitFor();
  await planner.getByText('Physical planner · observed-planner').waitFor();
  await analyzeRow.getByText(observed.detail).waitFor();
  await emit(page, 'activity', { ...planning, state: 'completed' });
  await emit(page, 'plan_ready', plan);
  await emit(page, 'status', { status: 'generating', message: 'Preparing the chapter videos.' });
  const motion = { id: 'motion-1', stage: 'motion', label: 'Generate motion', agent: 'Motion generator', state: 'started',
    model: 'observed-film-model', step_number: 1, timestamp: '2026-11-21T14:30:30Z' };
  await emit(page, 'activity', motion);
  const render = feed.locator('[data-operation-id="motion-1"]');
  await render.getByText('Step 1 · ' + steps[0].title).waitFor();
  await render.getByText('Motion generator · observed-film-model').waitFor();
  await page.evaluate(() => window.streams.at(-1).onerror(new Event('error')));
  await render.getByText('Last reported active').waitFor();
  await panel.getByText('Last report · Creating motion').waitFor();
  await emit(page, 'snapshot', { ...state, status: 'generating', plan,
    activity: [observed, { ...planning, state: 'completed' }, motion], decisions_log: decisions });
  await render.getByText('In progress').waitFor();
  assert.equal(await choices.getByRole('listitem').count(), 2);
  assert.equal(await feed.evaluate(node => node === window.firstFeed && node.closest('#generation-panel') === window.firstPanel
    && node === document.activeElement && window.streams.length === 1 && window.streams[0] === window.firstStream), true);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.equal(await page.locator('.progress.compact li > span:nth-child(2)').evaluateAll(labels =>
    labels.every(label => label.offsetHeight <= parseFloat(getComputedStyle(label).lineHeight) + 1)), true);
  await capture(page, 'phone-agent-generating');
});

test('chapter menus track concurrent renders and play an out-of-order result until the full film takes over', async t => {
  const activePlan = { ...plan, steps: [
    'Fold the first sheet', 'Fold the second sheet', 'Make a crease', 'Open the fold', 'Join the sheets',
  ].map((title, i) => ({ ...steps[0], step_number: i + 1, title })) };
  const { page } = await workbench(t, {
    width: 1440, state: { ...completed, plan: activePlan, status: 'generating', status_message: 'Preparing the chapter videos.', full_video_url: null, media: {} },
  });
  await page.goto(origin + '/?project=glider');
  const outline = page.locator('#step-list button');
  await outline.first().waitFor();
  const asset = (n, status) => ({
    step_number: n, status, ...(status === 'ready' && {
      duration_seconds: 2, video_url: '/media/glider/chapter-' + n + '.mp4', captions_url: '/media/glider/chapter-' + n + '.vtt',
    }),
  });
  await emit(page, 'step_media_started', asset(1, 'generating'));
  await emit(page, 'step_media_started', asset(2, 'generating'));
  await emit(page, 'step_media_error', asset(4, 'error'));
  assert.equal(await outline.filter({ hasText: 'Rendering' }).count(), 2);
  assert.equal(await page.getByRole('button', { name: 'Fold the second sheet Rendering' }).count(), 1);
  assert.equal(await outline.nth(0).locator('.rendering').evaluate(el => getComputedStyle(el).animationName), 'chapter-rendering');
  await page.emulateMedia({ reducedMotion: 'reduce' });
  assert.equal(await outline.nth(0).locator('.rendering').evaluate(el => getComputedStyle(el).animationName), 'none');
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  const later = asset(2, 'ready');
  await emit(page, 'step_media_ready', later);
  await emit(page, 'step_media_started', asset(5, 'generating'));
  await page.getByText('Creating chapter footage', { exact: true }).waitFor();
  assert.equal(await outline.nth(0).getAttribute('data-chapter-state'), 'rendering');
  assert.equal(await outline.nth(1).getAttribute('data-chapter-state'), 'ready');
  assert.equal(await outline.nth(2).getAttribute('data-chapter-state'), 'queued');
  assert.equal(await outline.nth(3).getAttribute('data-chapter-state'), 'error');
  assert.equal(await outline.nth(4).getAttribute('data-chapter-state'), 'rendering');
  await capture(page, 'desktop-chapter-status');
  await outline.nth(1).click();
  await page.waitForFunction(() => document.querySelector('#chapter-video')?.currentTime > .05);
  assert.equal(await page.locator('#chapter-video').getAttribute('src'), later.video_url);
  assert.equal(await page.locator('#chapter-video track').getAttribute('src'), later.captions_url);
  assert.equal(await outline.nth(0).getAttribute('data-chapter-state'), 'rendering');
  await page.getByRole('button', { name: 'Return to progress' }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('.mobile-picker > summary').click();
  const mobile = page.locator('#mobile-step-list button');
  assert.deepEqual(await mobile.evaluateAll(rows => rows.map(r => r.dataset.chapterState)), ['rendering', 'ready', 'queued', 'error', 'rendering']);
  await page.locator('.mobile-picker').scrollIntoViewIfNeeded();
  await capture(page, 'phone-chapter-status');
  await mobile.nth(2).click();
  assert.equal(await page.locator('#step-title').innerText(), activePlan.steps[2].title);
  assert.equal(await page.locator('#chapter-video').count(), 0);
  await selectMobileStep(page, 1);
  await page.waitForFunction(() => document.querySelector('#chapter-video')?.currentTime > .05);
  await page.locator('#chapter-video').evaluate(node => { node.pause(); node.currentTime = 1; });
  await page.waitForFunction(() => !document.querySelector('#chapter-video').seeking && document.querySelector('#chapter-video').currentTime === 1);
  const ordered = {};
  for (const n of [5, 3, 1, 4, 2]) {
    const ready = asset(n, 'ready');
    await emit(page, 'step_media_ready', ready);
    ordered[n] = { ...ready, chapter_start_seconds: (n - 1) * ready.duration_seconds };
  }
  await emit(page, 'full_video_ready', { video_url: completed.full_video_url, captions_url: completed.full_captions_url, media: ordered });
  const start = ordered[2].chapter_start_seconds;
  await page.waitForFunction(target => {
    const video = document.querySelector('#video');
    return video && !video.seeking && Math.abs(video.currentTime - target) < .05;
  }, start + 1);
  assert.equal(await page.locator('#chapter-video').count(), 0);
  assert.equal(await page.locator('#step-title').innerText(), activePlan.steps[1].title);
  assert.equal(await page.locator('#video').evaluate(node => node.paused), true);
  await page.locator('.mobile-picker > summary').click();
  assert.equal(await mobile.nth(1).getAttribute('data-chapter-state'), 'timed');
  assert.equal(await mobile.nth(1).getAttribute('aria-current'), 'step');
  assert.equal(await mobile.nth(1).locator('time').getAttribute('datetime'), 'PT' + start + 'S');
  assert.deepEqual(await mobile.locator('time').evaluateAll(times => times.map(time => time.dateTime)),
    Object.values(ordered).map(asset => 'PT' + asset.chapter_start_seconds + 'S'));
});

test('keeps other chapters playable after a full-film or chapter playback failure', async t => {
  const clips = Object.fromEntries(steps.map(step => {
    const n = step.step_number;
    return [n, { ...media[n], video_url: '/media/glider/chapter-' + n + '.mp4' }];
  }));
  const { page } = await workbench(t, { state: { ...completed, media: clips }, width: 320 });
  await page.route('**/media/glider/full.mp4', route => route.fulfill({ status: 404 }));
  await page.route('**/media/glider/chapter-1.mp4', route => route.fulfill({ status: 404 }));
  await page.goto(origin + '/?project=glider');
  await page.getByText('Playback unavailable').waitFor();
  await selectMobileStep(page, 0);
  await page.getByText('This chapter could not play. You can still follow its written steps.').waitFor();
  assert.equal(await page.locator('#step-title').innerText(), steps[0].title);
  await selectMobileStep(page, 1);
  await page.waitForFunction(() => document.querySelector('#chapter-video')?.currentTime > .05);
  assert.equal(await page.locator('#step-title').innerText(), steps[1].title);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
});

test('continues a playing preview at its chapter in the finished film', async t => {
  const later = { step_number: 2, status: 'ready', video_url: '/media/glider/chapter-2.mp4' };
  const { page } = await workbench(t, { state: { ...completed, status: 'generating', full_video_url: null, media: { 2: later } } });
  await page.goto(origin + '/?project=glider');
  await page.getByRole('button', { name: /Open the wings Play chapter/ }).click();
  await page.waitForFunction(() => document.querySelector('#chapter-video')?.currentTime > .5);
  await emit(page, 'full_video_ready', { video_url: completed.full_video_url, captions_url: completed.full_captions_url, media });
  await page.waitForFunction(start => {
    const film = document.querySelector('#video');
    return film && !film.paused && !film.seeking && film.currentTime > start + .2;
  }, media[2].chapter_start_seconds);
  assert.equal(await page.locator('#step-title').innerText(), steps[1].title);
});

test('plays a saved chapter when the older full film has no seek position for it', async t => {
  const { page } = await workbench(t, { state: { ...completed, media: {
    1: { step_number: 1, status: 'ready' },
    2: { step_number: 2, status: 'ready', video_url: '/media/glider/chapter-2.mp4' },
  } } });
  await page.goto(origin + '/?project=glider');
  await page.getByRole('button', { name: /Open the wings Play chapter/ }).click();
  await page.waitForFunction(() => document.querySelector('#chapter-video')?.currentTime > .05);
  await page.getByRole('button', { name: 'Return to full film' }).click();
  await page.waitForFunction(() => document.querySelector('#video')?.readyState >= 2);
});
