import { legacyMediaStage, mediaActivity } from './project.js';

export function label(value) {
  const words = value?.replaceAll('_', ' ').toLowerCase() || '';
  return words.charAt(0).toUpperCase() + words.slice(1);
}

const stages = [
  ['queued', 'Queue'], ['research', 'Research'], ['planning', 'Planning'],
  ['motion', 'Motion'], ['narration', 'Narration'], ['assembly', 'Assembly'],
];

export const stateNames = {
  waiting: 'Waiting', active: 'In progress', recorded: 'Recorded', failed: 'Failed', unknown: 'No record',
  interrupted: 'Interrupted', unconfirmed: 'Finish unconfirmed', offline: 'Last reported active',
};

export function operationState(entry, status, connection) {
  if (entry.state !== 'started') return entry.state;
  if (status === 'failed' || status === 'partial') return 'interrupted';
  if (status === 'completed' || (entry.stage === 'research' && ['planning', 'generating'].includes(status))
    || (entry.stage === 'planning' && status === 'generating')) return 'unconfirmed';
  return connection ? 'offline' : 'started';
}

export const operationLabels = {
  started: 'In progress', completed: 'Done', failed: 'Failed', interrupted: 'Interrupted',
  unconfirmed: 'Finish unconfirmed', offline: 'Last reported active',
};

export function stageProgress(project, connection) {
  const history = project?.activity || [];
  const assets = Object.values(project?.media || {});
  const status = project?.status;
  const afterQueue = status && !['submitting', 'queued'].includes(status);
  const production = mediaActivity(project);
  const legacyStage = legacyMediaStage(project, production)?.stage;
  const startedClip = !production.length && !legacyStage && status === 'generating'
    && assets.some(asset => asset.status === 'generating');
  return stages.map(([key, baseName]) => {
    const entries = history.filter(item => item.stage === key || (key === 'motion' && item.stage === 'illustration'));
    let name = baseName;
    if (key === 'motion') {
      if (entries.some(item => item.stage === 'illustration' && item.state === 'started')
        || assets.some(asset => asset.media_mode === 'illustrated_frames')) name = 'Illustration';
      else if (!assets.some(asset => asset.media_mode === 'motion') && !entries.length) name = 'Footage';
    }
    let state = 'waiting';
    if (key === 'motion' && assets.some(asset => asset.video_model)) state = 'recorded';
    if (key === 'narration' && assets.some(asset => asset.voice_model)) state = 'recorded';
    const outcomes = entries.filter(entry => !(entry.stage === 'motion' && entry.state === 'failed'
      && entries.some(other => other.stage === 'illustration' && other.step_number === entry.step_number
        && other.state === 'completed'))).map(entry => operationState(entry, status, connection));
    if (outcomes.includes('started')) state = 'active';
    else if (outcomes.includes('failed')) state = 'failed';
    else if (outcomes.includes('interrupted')) state = 'interrupted';
    else if (outcomes.includes('offline')) state = 'offline';
    else if (outcomes.includes('unconfirmed')) state = 'unconfirmed';
    else if (outcomes.includes('completed')) state = 'recorded';
    if (key === 'queued' && afterQueue) state = 'recorded';
    if (key === 'queued' && status === 'queued') state = 'active';
    if (key === 'research' && status === 'researching' && !entries.length) state = 'active';
    if (key === 'planning' && status === 'planning' && !entries.length) state = 'active';
    if (key === 'planning' && project?.plan) state = 'recorded';
    if (key === 'motion' && (legacyStage === 'footage' || startedClip)) state = 'active';
    if (key === 'assembly' && ['assembly', 'stitching'].includes(legacyStage)) state = 'active';
    if (key === 'assembly' && project?.full_video_url) state = 'recorded';
    if (['partial', 'failed', 'completed'].includes(status)) {
      if (state === 'waiting') state = 'unknown';
    }
    if (connection && state === 'active') state = 'offline';
    return { key, name, state };
  });
}

export function decisionSubject(decision) {
  const candidate = /^video_(\d+)_relevance$/.exec(decision.gate_name);
  return decision.source_title || (candidate ? 'Tutorial ' + candidate[1] : label(decision.gate_name));
}

export function decisionByline(decision) {
  const via = decision.via_decisions_api === true ? 'Decisions API' : decision.via_decisions_api === false ? 'Structured fallback' : null;
  return [via, decision.model_used].filter(Boolean).join(' · ');
}
