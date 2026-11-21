import { decisionSubject, label, operationState } from './activity.js';
import { chapterSourceLinks, youtubeLinks } from './project.js';

const urlFromId = id => id && /^[\w-]{11}$/.test(id) ? 'https://www.youtube.com/watch?v=' + id : null;

function sourceClips(source, planned) {
  if (!source.url) return [];
  const analysis = source.analysis?.state === 'completed' ? chapterSourceLinks(source.analysis.source_segments) : [];
  const clips = new Map();
  // One observed interval can appear in the research record and in several chapters.
  for (const segment of [...analysis, ...planned]) {
    if (youtubeLinks([segment.source_url])[0] !== source.url) continue;
    const interval = segment.start_seconds + '-' + segment.end_seconds;
    if (!clips.has(interval)) clips.set(interval, segment);
  }
  return [...clips.values()].sort((a, b) => a.start_seconds - b.start_seconds || a.end_seconds - b.end_seconds);
}

// A relevance gate approves a candidate for analysis. Only a plan citation establishes that
// the candidate actually contributed to the plan; several candidates can pass the same gate.
export function graphSources(project, connection) {
  const decisions = project.decisions_log || [];
  const activity = project.activity || [];
  const planned = (project.plan?.steps || []).flatMap(step => chapterSourceLinks(step.source_segments));
  const planUrls = youtubeLinks([...(project.plan?.source_youtube_urls || []), ...planned.map(segment => segment.source_url)]);
  const analyses = activity.filter(entry => entry.stage === 'research' && entry.id.startsWith('analyze-'));
  const search = activity.find(entry => entry.id === 'source-search' && entry.state === 'completed');
  const searchTitles = search?.detail?.split(' · ') || [];
  const candidates = new Map();
  for (const event of activity) {
    for (const { gate_name, title, url, channel, duration_seconds } of event.sources || []) {
      const [href] = youtubeLinks([url]);
      if (gate_name && title && href) {
        const previous = candidates.get(gate_name);
        const reviewState = event.id.startsWith('source-selection') ? operationState(event, project.status, connection) : undefined;
        candidates.set(gate_name, { key: gate_name, title, hasTitle: true, url: href,
          channel: channel || previous?.channel, duration_seconds: duration_seconds ?? previous?.duration_seconds,
          reviewState: reviewState || previous?.reviewState });
      }
    }
  }
  const sources = decisions.map((decision, index) => {
    const gate = decision.gate_name || '';
    const candidate = candidates.get(gate);
    const ordinal = /^video_(\d+)_relevance$/.exec(gate)?.[1];
    const title = decision.source_title || candidate?.title || (ordinal && searchTitles.length === decisions.length && searchTitles[Number(ordinal) - 1]);
    let url = youtubeLinks([decision.source_url])[0] || candidate?.url || urlFromId(/^tutorial_([\w-]{11})$/.exec(gate)?.[1]);
    // Before source URLs were persisted, analysis events recorded the video ID and exact title.
    // Never pair an anonymous gate with a plan URL or analysis by list position.
    const exactMatches = title ? analyses.filter(entry => entry.detail === title || entry.detail?.startsWith(title + ' · ')) : [];
    const analysis = url ? analyses.find(entry => urlFromId(entry.id.slice(8)) === url)
      : exactMatches.length === 1 && decisions.filter(other => other.source_title === title).length <= 1 ? exactMatches[0] : null;
    url ||= urlFromId(analysis?.id.slice(8));
    return { ...candidate, key: gate || String(index + 1), title: title || decisionSubject(decision) || 'Tutorial ' + (index + 1),
      hasTitle: Boolean(title), url, decision, analysis };
  });
  for (const candidate of candidates.values()) {
    if (sources.some(source => source.key === candidate.key || source.url === candidate.url)) continue;
    const analysis = analyses.find(entry => urlFromId(entry.id.slice(8)) === candidate.url);
    sources.push({ ...candidate, analysis });
  }
  for (const entry of analyses) {
    const id = entry.id.slice(8);
    const url = urlFromId(id);
    if (!url || sources.some(source => source.url === url || source.analysis === entry)) continue;
    const recorded = entry.detail?.split(' · ')[0];
    const name = recorded && !youtubeLinks([recorded]).length ? recorded : null;
    sources.push({ key: id, url, title: name || 'YouTube tutorial', hasTitle: Boolean(name), analysis: entry });
  }
  for (const url of planUrls) {
    if (!sources.some(source => source.url === url)) {
      const title = planned.find(segment => youtubeLinks([segment.source_url])[0] === url)?.source_title;
      sources.push({ key: url.split('=').at(-1), url, title: title || 'YouTube tutorial ' + (sources.length + 1), hasTitle: Boolean(title) });
    }
  }
  return sources.map((source, index) => ({
    ...source, number: index + 1, cited: planUrls.includes(source.url), clips: sourceClips(source, planned),
    analysisState: source.analysis && operationState(source.analysis, project.status, connection),
  }));
}

export function reviewLabel(source) {
  if (source.decision?.choice === 'KEEP_TUTORIAL') return 'Kept';
  if (['SKIP_FILLER', 'REJECT'].includes(source.decision?.choice)) return 'Skipped';
  if (source.decision) return label(source.decision.choice);
  if (source.cited) return 'Cited';
  if (source.reviewState && !source.analysisState) {
    return { started: 'In review', completed: 'Reviewed', failed: 'Failed', interrupted: 'Interrupted',
      offline: 'Last report', unconfirmed: 'Unconfirmed' }[source.reviewState];
  }
  return { started: 'In review', completed: 'Analyzed', failed: 'Failed', interrupted: 'Interrupted',
    offline: 'Last report', unconfirmed: 'Unconfirmed' }[source.analysisState] || 'Found';
}

export function graphEdges(sources, project) {
  const edges = [];
  const add = (from, to, type, pending = false, branch) => edges.push({ from, to, type, pending, branch });
  for (const source of sources) {
    const candidate = 'source-' + source.key;
    const review = 'review-' + source.key;
    add('research', candidate, 'branch', false, source.key);
    add(candidate, review, 'review', !source.decision && !source.cited && source.analysisState !== 'completed', source.key);
    if (source.cited || (!project.plan && source.analysis?.state === 'completed')) {
      add(review, 'planning', 'merge', !source.cited, source.key);
    }
  }
  if (!sources.length) add('research', 'planning', 'stage', true);
  const hasFilmWork = (project.activity || []).some(event => ['motion', 'illustration', 'narration', 'assembly'].includes(event.stage))
    || Object.values(project.media || {}).some(asset => asset.status === 'generating' || asset.video_model || asset.voice_model || asset.video_url)
    || Boolean(project.full_video_url);
  add('planning', 'motion', 'stage', !project.plan || !hasFilmWork);
  return edges;
}
