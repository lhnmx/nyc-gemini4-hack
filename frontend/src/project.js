import { useEffect, useState } from 'react';

const apiBase = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/+$/, '');
export const apiUrl = path => apiBase + path;
export const isFinished = status => ['completed', 'partial', 'failed'].includes(status);

export function mediaActivity(project) {
  return (project?.activity || []).filter(entry => ['motion', 'narration', 'assembly'].includes(entry.stage));
}

export function legacyMediaStage(project, activity) {
  if (activity.length || project?.status !== 'generating') return null;
  const progress = project.media_progress;
  const asset = project.media?.[progress?.step_number];
  return ['ready', 'skipped', 'error'].includes(asset?.status) ? null : progress;
}

export function chapterRenderState(number, asset, activity, legacyStage) {
  // A chapter can have narration while its footage is still queued behind a physical dependency.
  const video = activity.filter(entry => entry.step_number === number && entry.stage !== 'narration');
  if (video.some(entry => entry.state === 'failed')) return 'error';
  if (video.some(entry => entry.state === 'started')) return 'rendering';
  if (video.length) return 'waiting';
  if (asset?.status === 'generating' || legacyStage?.step_number === number) return 'rendering';
  return 'queued';
}

export function mediaUrl(value, projectId, example) {
  const prefix = example ? '/static/example/' : '/media/' + encodeURIComponent(projectId) + '/';
  if (typeof value !== 'string' || !value.startsWith(prefix)) return undefined;
  const resolved = new URL(value, window.location.origin);
  if (resolved.origin !== window.location.origin || !resolved.pathname.startsWith(prefix)) return undefined;
  return example ? resolved.pathname : apiUrl(resolved.pathname);
}

export function youtubeLinks(sources = []) {
  const links = new Set();
  for (const source of sources) {
    let url;
    try { url = new URL(source); } catch { continue; }
    if (!['https:', 'http:'].includes(url.protocol)) continue;
    let id;
    if (url.hostname === 'youtu.be') {
      id = url.pathname.slice(1);
    } else if (['youtube.com', 'www.youtube.com', 'm.youtube.com'].includes(url.hostname)) {
      id = url.pathname === '/watch' ? url.searchParams.get('v') : url.pathname.match(/^\/(?:shorts|live|embed)\/([^/]+)$/)?.[1];
    }
    if (id && /^[\w-]{11}$/.test(id)) links.add('https://www.youtube.com/watch?v=' + id);
  }
  return [...links];
}

export function chapterSourceLinks(segments) {
  if (!Array.isArray(segments)) return [];
  const links = [];
  for (const segment of segments) {
    const start = segment?.start_seconds;
    const end = segment?.end_seconds;
    if (!Number.isSafeInteger(start) || start < 0 || !Number.isSafeInteger(end) || end <= start) continue;
    const [source] = youtubeLinks([segment.source_url]);
    if (source) links.push({ ...segment, href: source + '&t=' + start + 's' });
  }
  return links;
}

export function useHealth(refresh) {
  const [health, setHealth] = useState({ label: 'Checking services', detail: 'Checking guide generation services…' });
  useEffect(() => {
    let closed = false;
    let request;
    let retry;
    async function check() {
      request = new AbortController();
      const deadline = setTimeout(() => request.abort(), 5000);
      try {
        const response = await fetch(apiUrl('/api/health'), { signal: request.signal, cache: 'no-store' });
        if (!response.ok) throw new Error();
        const data = await response.json();
        if (closed) return;
        const missing = [];
        if (!data.gemini_configured) missing.push('Gemini');
        if (!data.youtube_configured) missing.push('YouTube');
        const blocked = data.generation_ready === false || missing.length > 0;
        const keyStatus = missing.length
          ? 'Guide generation needs the ' + missing.join(' and ') + ' API ' + (missing.length === 1 ? 'key' : 'keys') + ' configured on the server.'
          : 'The server has API keys for Gemini and YouTube.';
        const detail = data.generation_message || (blocked && !missing.length ? 'Guide generation is unavailable on this server.' : keyStatus);
        setHealth({ label: blocked ? 'Setup needed' : 'Services ready', detail, blocked, catalogEnabled: data.capabilities?.material_catalog === true });
        if (blocked && refresh) retry = setTimeout(check, 5000);
      } catch {
        if (closed) return;
        setHealth(current => ({ label: 'Reconnecting', detail: 'Checking the connection again shortly.',
          reconnecting: true, catalogEnabled: current.catalogEnabled }));
        retry = setTimeout(check, 5000);
      } finally {
        clearTimeout(deadline);
      }
    }
    check();
    return () => {
      closed = true;
      clearTimeout(retry);
      request.abort();
    };
  }, [refresh]);
  return health;
}

// Every connection starts with a complete snapshot, including any events missed during a reconnect.
export function useProject(route) {
  const [project, setProject] = useState(route.initial || null);
  const [error, setError] = useState('');
  const [connection, setConnection] = useState(null);
  useEffect(() => {
    const request = new AbortController();
    let stream;
    function subscribe(id) {
      stream = new EventSource(apiUrl('/api/projects/' + encodeURIComponent(id) + '/events'));
      const listen = (name, handle) => stream.addEventListener(name, event => {
        if (!event.data || request.signal.aborted) return;
        setConnection(null);
        handle(JSON.parse(event.data));
      });
      listen('snapshot', state => {
        setProject(state);
        if (isFinished(state.status)) stream.close();
      });
      const applyStatus = update => {
        setProject(state => ({
          ...state, status: update.status, status_message: update.message,
          media_progress: update.media_progress ?? (update.status === 'generating' && update.step_number
            ? { stage: 'footage', step_number: update.step_number } : state.media_progress),
        }));
        if (isFinished(update.status)) stream.close();
      };
      listen('status', applyStatus);
      listen('project_error', applyStatus);
      listen('error', applyStatus);
      listen('plan_ready', plan => setProject(state => ({ ...state, plan })));
      listen('activity', event => setProject(state => {
        const history = state.activity || [];
        const present = history.some(entry => entry.id === event.id);
        const activity = present ? history.map(entry => entry.id === event.id ? event : entry) : [...history, event];
        return { ...state, activity };
      }));
      const applyDecisions = data => setProject(state => ({ ...state, decisions_log: data.decisions || [] }));
      listen('research_decisions', applyDecisions);
      listen('research_complete', applyDecisions);
      const applyAsset = asset => setProject(state => ({
        ...state, media: { ...state.media, [asset.step_number]: asset },
      }));
      listen('step_media_started', applyAsset);
      listen('step_media_ready', applyAsset);
      listen('step_media_error', applyAsset);
      listen('full_video_ready', data => setProject(state => ({
        ...state,
        full_video_url: data.video_url,
        full_captions_url: data.captions_url,
        media: { ...state.media, ...data.media },
      })));
      stream.onerror = event => {
        if (event.data || request.signal.aborted) return;
        const stopped = stream.readyState === EventSource.CLOSED;
        setConnection(stopped
          ? { kind: 'error', message: 'Live updates stopped. Refresh to check the latest version of this guide.' }
          : { kind: 'busy', message: 'Connection interrupted. Reconnecting to your guide…' });
      };
    }
    async function restore() {
      try {
        let state = route.initial;
        if (!state) {
          const path = route.id ? apiUrl('/api/projects/' + encodeURIComponent(route.id)) : '/static/example/project.json';
          const response = await fetch(path, { signal: request.signal });
          if (!response.ok) throw new Error(route.id && response.status === 404
            ? 'This guide could not be found. Create a new one or open the example.'
            : 'Could not load this guide. Refresh to try again.');
          try { state = await response.json(); }
          catch { throw new Error('Could not load this guide. Refresh to try again.'); }
        }
        if (request.signal.aborted) return;
        setProject(state);
        if (route.id && !isFinished(state.status)) subscribe(state.project_id);
      } catch (problem) {
        if (!request.signal.aborted) setError(problem instanceof TypeError ? 'Could not reach the server. Refresh to try again.' : problem.message);
      }
    }
    restore();
    return () => { request.abort(); stream?.close(); };
  }, [route]);
  return { project, error, connection };
}

export function readSelection(id) {
  try {
    const saved = JSON.parse(sessionStorage.getItem('workbench-' + id));
    if (saved && Number.isInteger(saved.index) && saved.checked && typeof saved.checked === 'object') return saved;
  } catch { /* Keep the guide usable when storage is unavailable. */ }
  return { index: 0, checked: {} };
}

export function chapterStarts(steps, media = {}) {
  // Older saved guides may have measured durations without explicit chapter markers.
  let nextStart = 0;
  return steps.map(step => {
    const asset = media[step.step_number];
    const start = Number.isFinite(asset?.chapter_start_seconds) ? asset.chapter_start_seconds : nextStart;
    nextStart = start !== null && asset?.duration_seconds > 0 ? start + asset.duration_seconds : null;
    return start;
  });
}

export function timeLabel(seconds) {
  const time = Math.floor(seconds);
  return Math.floor(time / 60) + ':' + String(time % 60).padStart(2, '0');
}
