import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { chromium } from 'playwright';
import { preview } from 'vite';
import { installYoutubePlayer } from './youtube-player.mjs';

const videoId = 'T5I6Jez-pNI';
const watchUrl = 'https://www.youtube.com/watch?v=' + videoId;
const sources = [
  { gate_name: 'video_1_relevance', title: 'Fold a paper dart', url: watchUrl, channel: 'Paper studio', duration_seconds: 85 },
  { gate_name: 'video_2_relevance', title: 'Fold a glider wing', url: 'https://www.youtube.com/watch?v=ccDD11_aaBB' },
];
const clip = { segment_id: 'crease', source_url: watchUrl, source_title: sources[0].title,
  start_seconds: 8, end_seconds: 13, technique: 'Set the crease', hand_action: 'Pinch the aligned corners.' };
const search = { id: 'source-search', stage: 'research', state: 'completed', label: 'Find tutorials',
  agent: 'YouTube', timestamp: '2026-11-21T14:30:00Z', sources };
const analysis = { ...search, sources: [], id: 'analyze-' + videoId, state: 'started', label: 'Analyze tutorial footage' };
const project = { project_id: 'dart', goal: 'Make a gliding paper dart', status: 'researching', media: {} };
const plan = { project_title: project.goal, materials: [], source_youtube_urls: [watchUrl], steps: [
  { step_number: 1, title: 'Pinch the corners', instruction_text: clip.hand_action, source_segments: [clip] },
] };
const frameUrl = 'https://www.youtube-nocookie.com/embed/' + videoId;
let browser, server, origin;

before(async () => {
  server = await preview({ preview: { host: '127.0.0.1', port: 0 } });
  origin = 'http://127.0.0.1:' + server.httpServer.address().port;
  browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || undefined });
});
after(async () => { await browser?.close(); await server?.close(); });

async function openResearch(t, { width = 1200, state = project, example = false, blockScript = false, silentFrame = false } = {}) {
  const context = await browser.newContext({ viewport: { width, height: 850 } });
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  const requests = [], errors = [];
  page.on('pageerror', error => errors.push(error.message));
  t.after(async () => { await context.close(); assert.deepEqual(errors, []); });
  await page.addInitScript(holdPlayerReady => {
    window.holdPlayerReady = holdPlayerReady;
    window.EventSource = class extends EventTarget {
      constructor() { super(); window.researchStream = this; }
      close() { this.closed = true; }
    };
  }, silentFrame);
  await context.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.origin === origin) {
      if (url.pathname === '/api/health') return route.fulfill({ json: { gemini_configured: true, youtube_configured: true } });
      if (url.pathname === '/api/projects/dart') return route.fulfill({ json: state });
      return route.continue();
    }
    requests.push(url.href);
    if (url.hostname === 'i.ytimg.com') return route.fulfill({ contentType: 'image/gif',
      body: Buffer.from('R0lGODlhAQABAPAAAP///wAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==', 'base64') });
    if (url.href === 'https://www.youtube.com/iframe_api') {
      if (blockScript) return route.abort();
      return route.fulfill({ contentType: 'text/javascript', body: '(' + installYoutubePlayer.toString() + ')()' });
    }
    if (url.hostname === 'www.youtube-nocookie.com' || (url.hostname === 'www.youtube.com' && url.pathname === '/watch')) {
      return route.fulfill({ contentType: 'text/html', body: '<title>YouTube fixture</title>' });
    }
    return route.abort();
  });
  await page.goto(example ? origin : origin + '/?project=dart');
  if (!example) await page.waitForFunction(() => Boolean(window.researchStream));
  return { page, requests };
}

async function emit(page, name, data) {
  await page.evaluate(([name, data]) => window.researchStream.dispatchEvent(new MessageEvent(name, {
    data: JSON.stringify(data),
  })), [name, data]);
}

async function checkFitsScreen(locator) {
  assert.ok(await locator.evaluate(el => {
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.left >= 0 && box.right <= innerWidth && document.documentElement.scrollWidth <= innerWidth;
  }));
}

test('previews the live research source and loops only the recorded interval as analysis arrives', async t => {
  const { page, requests } = await openResearch(t);
  const research = page.getByRole('region', { name: 'Research preview' });
  await emit(page, 'activity', { ...search, state: 'started', sources: [] });
  await research.getByText('Searching YouTube').waitFor();
  assert.equal(await research.getByRole('button', { name: 'Watch tutorial' }).count(), 0);
  await emit(page, 'activity', search);
  await research.getByRole('heading', { name: sources[0].title }).waitFor();
  await research.getByText('Tutorial discovered').waitFor();
  await research.getByText(sources[0].channel).waitFor();
  const film = page.getByRole('region', { name: 'Guide film' });
  const left = await film.boundingBox();
  const right = await research.boundingBox();
  assert.ok(right.x >= left.x + left.width && Math.abs(right.y - left.y) < 2, 'research shares the film row on desktop');
  await page.waitForFunction(() => document.querySelector('#research-preview img')?.naturalWidth > 0);
  assert.ok(requests.includes('https://i.ytimg.com/vi/' + videoId + '/hqdefault.jpg'));
  assert.equal(await page.locator('iframe').count(), 0);
  assert.ok(requests.every(url => new URL(url).hostname === 'i.ytimg.com'));
  await emit(page, 'activity', { ...search, id: 'source-selection', state: 'started' });
  await research.getByText('Reviewing tutorial').waitFor();
  await emit(page, 'research_decisions', { decisions: sources.map((source, index) => ({
    gate_name: source.gate_name, source_title: source.title, source_url: source.url,
    choice: index === 0 ? 'KEEP_TUTORIAL' : 'SKIP_FILLER',
  })) });
  await research.getByText('Kept in review').waitFor();
  await page.clock.install();
  await research.getByRole('button', { name: 'Watch tutorial' }).press('Enter');
  await page.waitForFunction(() => window.youtubePlayers?.[0]?.commands.includes('play'));
  const frame = research.locator('iframe');
  const full = new URL(await frame.getAttribute('src'));
  assert.equal(full.origin + full.pathname, frameUrl);
  assert.equal(full.searchParams.get('enablejsapi'), '1');
  assert.equal(full.searchParams.get('origin'), origin);
  assert.equal(full.searchParams.has('start'), false);
  assert.equal(full.searchParams.has('end'), false);
  assert.deepEqual(await page.evaluate(() => {
    const player = window.youtubePlayers[0];
    player.state(1);
    player.state(0);
    return player.commands;
  }), ['mute', 'play', { load: { videoId } }]);
  await emit(page, 'activity', analysis);
  await research.getByText('Analyzing footage').waitFor();
  await emit(page, 'activity', { ...analysis, state: 'completed', source_segments: [clip] });
  await research.getByText('Analysis complete').waitFor();
  await research.getByText(clip.hand_action).waitFor();
  assert.ok(await frame.evaluate(el => window.youtubePlayers.length === 1 && window.youtubePlayers[0].iframe === el));
  await research.getByRole('button', { name: 'Loop Set the crease, 0:08–0:13' }).click();
  await page.waitForFunction(() => window.youtubePlayers[1]?.commands.includes('play'));
  const bounded = new URL(await frame.getAttribute('src'));
  assert.equal(bounded.origin + bounded.pathname, frameUrl);
  assert.deepEqual([bounded.searchParams.get('start'), bounded.searchParams.get('end')], ['8', '13']);
  const bounds = { load: { videoId, startSeconds: 8, endSeconds: 13 } };
  assert.deepEqual(await page.evaluate(() => {
    const player = window.youtubePlayers[1];
    player.state(0);
    player.state(0);
    player.state(2);
    return player.commands;
  }), ['mute', 'play', bounds, bounds]);
  await page.clock.fastForward(30000);
  assert.deepEqual(await page.evaluate(() => window.youtubePlayers[1].commands), ['mute', 'play', bounds, bounds]);
  await page.setViewportSize({ width: 320, height: 850 });
  await checkFitsScreen(frame);
  const above = await film.boundingBox();
  assert.ok((await research.boundingBox()).y >= above.y + above.height, 'research stacks below the workflow on mobile');
  await research.getByRole('combobox', { name: 'Research source' }).selectOption({ label: sources[1].title });
  await frame.waitFor({ state: 'detached' });
  await research.getByRole('heading', { name: sources[1].title }).waitFor();
  await research.getByText('Skipped in review').waitFor();
  assert.equal(await research.getByRole('link').getAttribute('href'), sources[1].url);
  assert.equal(await research.getByRole('button', { name: /^Loop / }).count(), 0);
  assert.deepEqual(await page.evaluate(() => {
    const stopped = window.youtubePlayers[1];
    stopped.ready();
    stopped.state(0);
    return stopped.commands;
  }), ['mute', 'play', bounds, bounds]);
  await research.getByRole('button', { name: 'Watch tutorial' }).click();
  await page.waitForFunction(() => window.youtubePlayers[2]?.commands.includes('play'));
  await emit(page, 'plan_ready', plan);
  await research.waitFor({ state: 'detached' });
  await page.getByRole('heading', { name: plan.steps[0].title }).waitFor();
  await page.setViewportSize({ width: 1200, height: 850 });
  const chapter = await page.getByRole('heading', { name: plan.steps[0].title }).boundingBox();
  const filmWithPlan = await film.boundingBox();
  assert.ok(chapter.x >= filmWithPlan.x + filmWithPlan.width, 'the chapter occupies the same right-hand column after planning');
  assert.equal(await page.locator('iframe').count(), 0);
});

const completedResearch = { ...project, activity: [search, { ...analysis, state: 'completed', source_segments: [clip] }] };
for (const code of [101, 150]) {
  test('opens the timestamped watch page when the owner restricts the embed (' + code + ')', async t => {
    const { page } = await openResearch(t, { state: completedResearch, width: 320 });
    const research = page.getByRole('region', { name: 'Research preview' });
    await research.getByRole('button', { name: /^Loop Set the crease/ }).click();
    await page.waitForFunction(() => window.youtubePlayers?.length);
    await page.evaluate(code => window.youtubePlayers[0].error(code), code);
    await research.getByText(/only be watched on YouTube/i).waitFor();
    assert.equal(await research.locator('iframe').count(), 0);
    const fallback = research.getByRole('link', { name: /Open on YouTube/ });
    await checkFitsScreen(fallback);
    const [watch] = await Promise.all([page.waitForEvent('popup'), fallback.click()]);
    await watch.waitForLoadState();
    assert.equal(watch.url(), watchUrl + '&t=8s');
    await watch.close();
    await research.getByRole('button', { name: 'Stop preview' }).click();
    await research.getByRole('button', { name: 'Watch tutorial' }).waitFor();
    assert.equal(await research.getByRole('link', { name: /Open on YouTube/ }).count(), 0);
  });
}

test('offers a direct link when the API script is blocked or a frame never responds', async t => {
  for (const failure of ['script', 'frame']) {
    await t.test(failure, async t => {
      const { page } = await openResearch(t, { state: completedResearch, blockScript: failure === 'script', silentFrame: failure === 'frame' });
      const research = page.getByRole('region', { name: 'Research preview' });
      await page.clock.install();
      await research.getByRole('button', { name: 'Watch tutorial' }).click();
      if (failure === 'frame') {
        await page.waitForFunction(() => window.youtubePlayers?.length);
        await page.clock.fastForward(30000);
      }
      const fallback = research.getByRole('link', { name: /Open on YouTube/ });
      await fallback.waitFor();
      assert.equal(await fallback.getAttribute('href'), watchUrl);
      assert.equal(await research.locator('iframe').count(), 0);
    });
  }
});

test('opens recorded research from Workflow and replays a verified interval without replacing the guide film', async t => {
  const { page } = await openResearch(t, { width: 320, example: true });
  await page.waitForFunction(() => document.querySelector('#video')?.readyState >= 1);
  await page.locator('#video').evaluate(async video => { window.guideFilm = video; video.muted = true; await video.play(); });
  const workflow = page.getByRole('region', { name: 'Workflow', exact: true });
  const summary = workflow.locator('.graph-disclosure > summary');
  assert.equal(await page.getByRole('button', { name: 'Replay research' }).count(), 0);
  const replay = page.getByRole('region', { name: 'Research replay' });
  assert.equal(await replay.count(), 0);
  await summary.press('Enter');
  await replay.getByText('Analysis complete').waitFor();
  await workflow.locator('.graph-board').waitFor();
  assert.equal(await page.locator('#video').evaluate(el => el === window.guideFilm && !el.paused), true);
  assert.equal(await replay.locator('iframe').count(), 0);
  await replay.getByRole('button', { name: 'Loop Centerline valley fold and unfold, 0:06–0:14' }).click();
  await page.waitForFunction(() => window.youtubePlayers?.[0]?.commands.includes('play'));
  assert.equal(await page.locator('#video').evaluate(el => el === window.guideFilm && el.paused), true);
  const url = new URL(await replay.locator('iframe').getAttribute('src'));
  assert.equal(url.pathname, '/embed/gZQI1Z_viNg');
  assert.deepEqual([url.searchParams.get('start'), url.searchParams.get('end')], ['6', '14']);
  assert.deepEqual(await page.evaluate(() => {
    const player = window.youtubePlayers[0];
    player.state(0);
    return player.commands.at(-1);
  }), { load: { videoId: 'gZQI1Z_viNg', startSeconds: 6, endSeconds: 14 } });
  await checkFitsScreen(replay.locator('iframe'));
  await summary.click();
  await workflow.locator('iframe').waitFor({ state: 'detached' });
  assert.equal(await replay.isVisible(), false);
  assert.ok(await page.locator('#video').evaluate(el => el === window.guideFilm));
});
