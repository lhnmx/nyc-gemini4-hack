import { copyFile, mkdir, readFile, rm, writeFile } from 'node:fs/promises';

const source = new URL('../../src/nyc_gemini4_hack/web/example/', import.meta.url);
const destination = new URL('../public/static/example/', import.meta.url);
const project = JSON.parse(await readFile(new URL('project.json', source), 'utf8'));
const firstStep = project.plan.steps[0].step_number;
const poster = project.media[firstStep]?.keyframe_url;

// The frontend publishes one film. Intermediate rendering files belong to the backend.
for (const asset of Object.values(project.media)) {
  delete asset.video_url;
  delete asset.audio_url;
  delete asset.captions_url;
  if (asset.step_number !== firstStep) delete asset.keyframe_url;
}
await rm(destination, { recursive: true, force: true });
await mkdir(destination, { recursive: true });
await copyFile(new URL('../../ucp-agent-profile.json', import.meta.url),
  new URL('../public/ucp-agent-profile.json', import.meta.url));
await writeFile(new URL('project.json', destination), JSON.stringify(project, null, 2) + '\n');
for (const url of [project.full_video_url, project.full_captions_url, poster].filter(Boolean)) {
  const prefix = '/static/example/';
  if (!url.startsWith(prefix)) throw new Error('Expected a bundled example asset at ' + url);
  const name = url.slice(prefix.length);
  const target = new URL(name, destination);
  await mkdir(new URL('.', target), { recursive: true });
  await copyFile(new URL(name, source), target);
}
