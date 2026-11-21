import { useState } from 'react';
import LoopingSourceClip from './LoopingSourceClip.jsx';
import { operationState } from './activity.js';
import { timeLabel } from './project.js';
import { sourcePreviewLinks } from './sourcePreview.js';
import './researchPreview.css';

const analysisLabels = {
  started: 'Analyzing footage', completed: 'Analysis complete', failed: 'Analysis failed',
  interrupted: 'Analysis interrupted', offline: 'Last report · Analyzing footage', unconfirmed: 'Analysis finish unconfirmed',
};
const reviewLabels = {
  started: 'Reviewing tutorial', completed: 'Review complete', failed: 'Review failed',
  interrupted: 'Review interrupted', offline: 'Last report · Reviewing tutorial', unconfirmed: 'Review finish unconfirmed',
};

function researchState(source) {
  if (source.analysisState) return analysisLabels[source.analysisState];
  if (source.decision?.choice === 'KEEP_TUTORIAL') return 'Kept in review';
  if (['SKIP_FILLER', 'REJECT'].includes(source.decision?.choice)) return 'Skipped in review';
  if (source.cited) return 'Cited in guide';
  return reviewLabels[source.reviewState] || 'Tutorial discovered';
}

function Thumbnail({ url, duration }) {
  const id = new URL(url).searchParams.get('v');
  const [failed, setFailed] = useState(null);
  return <div className="research-art">
    {failed !== id ? <img className="research-thumbnail" src={'https://i.ytimg.com/vi/' + id + '/hqdefault.jpg'}
      alt="" referrerPolicy="no-referrer" onError={() => setFailed(id)} /> : <span className="research-no-art">YouTube</span>}
    {duration > 0 && <time dateTime={'PT' + duration + 'S'}>{timeLabel(duration)}</time>}
  </div>;
}

function intervalKey(clip) {
  return clip.start_seconds + '-' + clip.end_seconds;
}

function PendingSearch({ project, connection }) {
  const search = (project.activity || []).findLast(entry => entry.id.startsWith('source-search'));
  const state = search && operationState(search, project.status, connection);
  const message = {
    started: 'Searching YouTube', completed: 'Search complete', failed: 'Search failed', interrupted: 'Search interrupted',
    offline: 'Last report · Searching YouTube', unconfirmed: 'Search finish unconfirmed',
  }[state] || 'Waiting for source details';
  return <section className="research-preview" aria-label="Research preview">
    <header className="research-heading"><h2>YouTube research</h2></header>
    <div className="research-pending">
      <p className="research-state" data-active={state === 'started'} role="status">{message}</p>
      {search?.detail && <p>{search.detail}</p>}
    </div>
  </section>;
}

export default function ResearchPreview({ sources, project, connection, recorded, playing, togglePreview, stopPreview }) {
  const [chosen, setChosen] = useState(null);
  const [showAll, setShowAll] = useState(false);
  const source = sources.find(item => item.url === chosen)
    || sources.find(item => item.analysisState === 'started') || sources.find(item => item.clips.length)
    || sources.find(item => item.cited) || sources.find(item => item.decision?.choice === 'KEEP_TUTORIAL')
    || sources.find(item => item.reviewState === 'started') || sources[0];
  const links = source && sourcePreviewLinks(source.url);
  if (!links) return recorded ? null : <PendingSearch project={project} connection={connection} />;
  const current = playing?.owner === 'research' && playing.url === source.url ? playing.interval : null;
  const selectedClip = source.clips.find(clip => current === intervalKey(clip));
  const playerOpen = current === 'tutorial' || Boolean(selectedClip);
  const state = researchState(source);
  const clips = source.clips;
  const visibleClips = showAll ? clips : clips.slice(0, 1);
  function watch(interval) {
    setChosen(source.url);
    togglePreview('research', source.url, interval);
  }
  function pick(url) {
    setChosen(url);
    setShowAll(false);
    stopPreview('research');
  }

  return <section id="research-preview" className="research-preview" aria-label={recorded ? 'Research replay' : 'Research preview'}>
    <header className="research-heading">
      <h2>{recorded ? 'Recorded research' : 'YouTube research'}</h2>
      {sources.length > 1 && <label className="research-source-choice">
        <span aria-hidden="true">Source {sources.indexOf(source) + 1} of {sources.length}</span>
        <select aria-label="Research source" value={source.url} onChange={event => pick(event.target.value)}>
          {sources.map(item => <option key={item.url} value={item.url}>{item.title}</option>)}
        </select>
      </label>}
    </header>
    <div className="research-card">
      {!recorded && !playerOpen && <Thumbnail url={links.href} duration={source.duration_seconds} />}
      <div className="research-info">
        <p className="research-state" data-active={source.analysisState === 'started' || (!source.decision && source.reviewState === 'started')}
          role="status">{state}</p>
        <h3>{source.title}</h3>
        {source.channel && <p className="research-channel">{source.channel}</p>}
        <div className="research-actions">
          <button type="button" className="research-watch" aria-expanded={playerOpen}
            onClick={() => playerOpen ? stopPreview('research') : watch('tutorial')}>
            <i aria-hidden="true" data-stop={playerOpen} />{playerOpen ? 'Stop preview' : 'Watch tutorial'}
          </button>
          <a href={selectedClip?.href || links.href} target="_blank" rel="noreferrer">YouTube <span aria-hidden="true">↗</span></a>
        </div>
      </div>
    </div>
    {playerOpen && <div className="research-player"><LoopingSourceClip key={source.url + '/' + current}
      url={source.url} segment={selectedClip || null} title={source.title + (selectedClip
        ? ' · ' + timeLabel(selectedClip.start_seconds) + '–' + timeLabel(selectedClip.end_seconds) : ' · Tutorial preview')} /></div>}
    {clips.length > 0 && <div className="research-findings">
      <h4>Observed footage</h4>
      <ul>{visibleClips.map(clip => {
        const interval = timeLabel(clip.start_seconds) + '–' + timeLabel(clip.end_seconds);
        const technique = clip.technique || 'Source clip';
        const active = current === intervalKey(clip);
        return <li className="research-finding" key={intervalKey(clip)}>
          <button type="button" className="research-loop" aria-pressed={active} onClick={() => watch(intervalKey(clip))}
            aria-label={(active ? 'Stop ' : 'Loop ') + technique + ', ' + interval}>
            <span>{active ? '■' : '↻'} {interval}</span><strong>{technique}</strong>
          </button>
          {clip.hand_action && <p>{clip.hand_action}</p>}
          {clip.tools_visible?.length > 0 && <p className="research-tools">Visible tools · {clip.tools_visible.join(', ')}</p>}
        </li>;
      })}</ul>
      {clips.length > 1 && <button type="button" className="research-more" aria-expanded={showAll}
        onClick={() => {
          if (showAll && selectedClip && selectedClip !== clips[0]) stopPreview('research');
          setShowAll(!showAll);
        }}>{showAll ? 'Show fewer clips' : 'Show ' + (clips.length - 1) + ' more ' + (clips.length === 2 ? 'clip' : 'clips')}</button>}
    </div>}
    {source.analysis?.model && <p className="research-credit">{[source.analysis.agent, source.analysis.model].filter(Boolean).join(' · ')}</p>}
  </section>;
}
