import { mkdir, readFile, writeFile } from 'node:fs/promises';

const source = new URL('../../src/nyc_gemini4_hack/web/example/', import.meta.url);
const destination = new URL('../public/static/example/', import.meta.url);
const project = JSON.parse(await readFile(new URL('project.json', source), 'utf8'));
const firstStep = project.plan.steps[0].step_number;
const poster = project.media[firstStep]?.keyframe_url;

// Builds and tests share these assets with the open development workbench.
// Rewriting identical files triggers a browser refresh in the middle of a guide.
async function publish(target, bytes) {
  const current = await readFile(target).catch(error => {
    if (error.code !== 'ENOENT') throw error;
    return null;
  });
  if (current?.equals(bytes)) return;
  await mkdir(new URL('.', target), { recursive: true });
  await writeFile(target, bytes);
}

// The frontend publishes one film. Intermediate rendering files belong to the backend.
for (const asset of Object.values(project.media)) {
  delete asset.video_url;
  delete asset.audio_url;
  delete asset.captions_url;
  if (asset.step_number !== firstStep) delete asset.keyframe_url;
}
await publish(new URL('../public/ucp-agent-profile.json', import.meta.url),
  await readFile(new URL('../../ucp-agent-profile.json', import.meta.url)));
for (const url of [project.full_video_url, project.full_captions_url, poster].filter(Boolean)) {
  const prefix = '/static/example/';
  if (!url.startsWith(prefix)) throw new Error('Expected a bundled example asset at ' + url);
  const name = url.slice(prefix.length);
  await publish(new URL(name, destination), await readFile(new URL(name, source)));
}
await publish(new URL('project.json', destination), Buffer.from(JSON.stringify(project, null, 2) + '\n'));
