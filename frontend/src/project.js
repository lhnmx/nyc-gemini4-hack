import { useEffect, useState } from 'react';

const apiBase = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/+$/, '');
export const apiUrl = path => apiBase + path;
export const isFinished = status => ['completed', 'partial', 'failed'].includes(status);

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

export function useHealth(refresh) {
  const [health, setHealth] = useState({ label: 'Checking services', detail: 'Checking guide generation services…' });
  useEffect(() => {
    const request = new AbortController();
    async function check() {
      try {
        const response = await fetch(apiUrl('/api/health'), { signal: request.signal });
        if (!response.ok) throw new Error();
        const data = await response.json();
        const missing = [];
        if (!data.gemini_configured) missing.push('Gemini');
        if (!data.youtube_configured) missing.push('YouTube');
        const blocked = data.generation_ready === false || missing.length > 0;
        const keyStatus = missing.length
          ? 'Guide generation needs the ' + missing.join(' and ') + ' API ' + (missing.length === 1 ? 'key' : 'keys') + ' configured on the server.'
          : 'The server has API keys for Gemini and YouTube.';
        const detail = data.generation_message || (blocked && !missing.length ? 'Guide generation is unavailable on this server.' : keyStatus);
        setHealth({ label: blocked ? 'Setup needed' : 'Services ready', detail, blocked });
      } catch {
        if (!request.signal.aborted) setHealth({ label: 'Server unavailable', detail: 'The guide generation server could not be reached.', blocked: true });
      }
    }
    check();
    return () => request.abort();
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
        setProject(state => ({ ...state, status: update.status, status_message: update.message }));
        if (isFinished(update.status)) stream.close();
      };
      listen('status', applyStatus);
      listen('project_error', applyStatus);
      listen('error', applyStatus);
      listen('plan_ready', plan => setProject(state => ({ ...state, plan })));
      const applyAsset = asset => setProject(state => ({
        ...state, media: { ...state.media, [asset.step_number]: asset },
      }));
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

export function guideStatus(project, example, hasFilm) {
  if (!project) return { kind: 'busy', message: 'Opening guide…' };
  if (example) return { message: '' };
  const { status, status_message: detail, plan } = project;
  if (status === 'completed') return { message: hasFilm ? '' : 'Your written guide is ready. A full video is unavailable.' };
  if (status === 'partial') return { kind: 'partial', message: detail || 'Your written guide is ready. The full video could not be assembled.' };
  if (status === 'failed') return { kind: 'error', message: detail || 'Guide generation failed. You can submit your project again.' };
  const message = {
    queued: 'Your guide is queued.',
    researching: 'Finding and reviewing craft tutorials…',
    planning: 'Writing your steps and materials list…',
    generating: detail || (plan ? 'Your written steps are ready. Preparing the film and narration…' : 'Preparing the film and narration…'),
  }[status];
  return { kind: 'busy', message: message || 'Preparing your guide…' };
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
