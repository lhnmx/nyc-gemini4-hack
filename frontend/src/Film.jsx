import { useRef, useState } from 'react';
import { legacyMediaStage, mediaActivity, timeLabel } from './project.js';

const stageNames = {
  submitting: 'Sending your project',
  queued: 'Your guide is queued',
  researching: 'Researching tutorials',
  planning: 'Planning your guide',
  generating: 'Making your film',
  completed: 'Written guide ready',
  partial: 'Film partially complete',
  failed: 'Generation stopped',
};

const clock = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit', second: '2-digit' });
const datedClock = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' });

function RecordedTime({ timestamp }) {
  const date = new Date(timestamp);
  if (!timestamp || Number.isNaN(date.getTime())) return null;
  const format = date.toDateString() === new Date().toDateString() ? clock : datedClock;
  return <time dateTime={date.toISOString()} title={date.toLocaleString()}>{format.format(date)}</time>;
}

function label(value) {
  const words = value?.replaceAll('_', ' ').toLowerCase() || '';
  return words.charAt(0).toUpperCase() + words.slice(1);
}

const stages = [
  ['queued', 'Queue'], ['research', 'Research'], ['planning', 'Planning'],
  ['motion', 'Motion'], ['narration', 'Narration'], ['assembly', 'Assembly'],
];

const stateNames = {
  waiting: 'Waiting', active: 'In progress', recorded: 'Recorded', failed: 'Failed', unknown: 'No record',
  interrupted: 'Interrupted', unconfirmed: 'Finish unconfirmed', offline: 'Last reported active',
};

function operationState(entry, status, connection) {
  if (entry.state !== 'started') return entry.state;
  if (status === 'failed' || status === 'partial') return 'interrupted';
  if (status === 'completed' || (entry.stage === 'research' && ['planning', 'generating'].includes(status))
    || (entry.stage === 'planning' && status === 'generating')) return 'unconfirmed';
  return connection ? 'offline' : 'started';
}

const operationLabels = {
  started: 'In progress', completed: 'Done', failed: 'Failed', interrupted: 'Interrupted',
  unconfirmed: 'Finish unconfirmed', offline: 'Last reported active',
};

function stageProgress(project, connection) {
  const history = project?.activity || [];
  const assets = Object.values(project?.media || {});
  const status = project?.status;
  const afterQueue = status && !['submitting', 'queued'].includes(status);
  const production = mediaActivity(project);
  const legacyStage = legacyMediaStage(project, production)?.stage;
  const startedClip = !production.length && !legacyStage && status === 'generating'
    && assets.some(asset => asset.status === 'generating');
  return stages.map(([key, baseName]) => {
    const entries = history.filter(item => item.stage === key);
    let name = baseName;
    if (key === 'motion') {
      if (assets.some(asset => asset.media_mode === 'illustrated_frames')) name = 'Illustration';
      else if (!assets.some(asset => asset.media_mode === 'motion') && !entries.length) name = 'Footage';
    }
    let state = 'waiting';
    if (key === 'motion' && assets.some(asset => asset.video_model)) state = 'recorded';
    if (key === 'narration' && assets.some(asset => asset.voice_model)) state = 'recorded';
    const outcomes = entries.map(entry => operationState(entry, status, connection));
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

export function Progress({ project, connection, compact = false }) {
  const progress = stageProgress(project, connection);
  return <section className={compact ? 'progress compact' : 'progress'} aria-label="Generation stages">
    {!compact && <h2>Generation progress</h2>}
    <ol>{progress.map(({ key, name, state }) => <li key={key} data-state={state} title={name + ' · ' + stateNames[state]}>
      <span className="stage-mark" aria-hidden="true" /><span>{name}</span>
      <small>{stateNames[state]}</small>
    </li>)}</ol>
  </section>;
}

function ActivityList({ records, status, connection }) {
  return <ol className="activity-list">
    {records.map(event => {
      const state = operationState(event, status, connection);
      const recordedState = state === 'started' ? 'Started' : state === 'completed' ? 'Completed' : operationLabels[state];
      return <li key={event.id}>
        <div className="activity-line">
          <strong>{event.label || label(event.stage)}{event.step_number ? ' · Step ' + event.step_number : ''}</strong>
          <RecordedTime timestamp={event.timestamp} />
        </div>
        <p>{[recordedState, event.agent, event.model].filter(Boolean).join(' · ')}</p>
        {event.detail && <p>{event.detail}</p>}
      </li>;
    })}
  </ol>;
}

function decisionSubject(decision) {
  const candidate = /^video_(\d+)_relevance$/.exec(decision.gate_name);
  return decision.source_title || (candidate ? 'Tutorial ' + candidate[1] : label(decision.gate_name));
}

function decisionByline(decision) {
  const via = decision.via_decisions_api === true ? 'Decisions API' : decision.via_decisions_api === false ? 'Model decision' : null;
  return [via, decision.model_used].filter(Boolean).join(' · ');
}

function decisionChoice(decision) {
  return { KEEP_TUTORIAL: 'Keep tutorial', SKIP_FILLER: 'Skip tutorial' }[decision.choice] || label(decision.choice);
}

function LiveActivity({ project, connection }) {
  const records = [...(project?.activity || [])].sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp));
  const decisions = project?.decisions_log || [];
  if (!records.length && !decisions.length) return null;
  const active = records.filter(entry => ['started', 'offline'].includes(operationState(entry, project.status, connection)));
  const recent = records.filter(entry => !active.includes(entry)).slice(0, Math.max(0, 2 - active.length));
  return <div className="live-activity">
    {records.length > 0 && <section className="agent-feed" aria-label="Agent activity">
      <h3>Agent activity</h3>
      <ol tabIndex="0" aria-label="Recent agent operations" aria-live="polite">{[...active, ...recent].map(entry => {
        const state = operationState(entry, project.status, connection);
        const step = project.plan?.steps?.find(step => step.step_number === entry.step_number);
        return <li className="agent-run" data-state={state} data-operation-id={entry.id} key={entry.id}>
          <span className="run-mark" aria-hidden="true" />
          <div className="run-body">
            <div className="run-title"><strong>{entry.label || label(entry.stage)}</strong><small>{operationLabels[state]}</small></div>
            {(entry.detail || step) && <p>{entry.detail || 'Step ' + entry.step_number + ' · ' + step.title}</p>}
            <div className="run-byline">{[entry.agent, entry.model].filter(Boolean).join(' · ')}</div>
          </div>
        </li>;
      })}</ol>
    </section>}
    {decisions.length > 0 && <section className="decision-brief" aria-label="Source decisions">
      <h3>Source decisions <span>{decisions.length}</span></h3>
      <ul tabIndex="0" aria-label="Tutorial choices" aria-live="polite">{decisions.map((decision, i) => <li key={decision.gate_name + i}>
        <div className="decision-title"><span data-choice={decision.choice}>{decisionChoice(decision)}</span><strong>{decisionSubject(decision)}</strong></div>
        {decision.rationale && <p>{decision.rationale}</p>}
        {decisionByline(decision) && <div className="run-byline">{decisionByline(decision)}</div>}
      </li>)}</ul>
    </section>}
  </div>;
}

function Activity({ project, connection }) {
  const activity = [...(project?.activity || [])].sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp));
  const decisions = project?.decisions_log || [];
  const credits = Object.values(project?.media || {}).filter(asset => asset.video_model || asset.voice_model);
  if (!activity.length && !decisions.length && !credits.length) return null;
  const summary = [
    activity.length > 0 && activity.length + (activity.length === 1 ? ' event' : ' events'),
    decisions.length > 0 && decisions.length + (decisions.length === 1 ? ' decision' : ' decisions'),
  ].filter(Boolean).join(' · ');
  const earlier = activity.slice(0, -4);
  return <details className="disclosure activity" id="generation-activity">
    <summary>Generation activity{summary && <small>{summary}</small>}</summary>
    {decisions.length > 0 && <details className="decision-detail">
      <summary>Research decisions ({decisions.length})</summary>
      {!activity.length && <p className="record-note">Timing is unavailable for these saved decisions.</p>}
      <ul>{decisions.map((decision, i) => <li key={decision.gate_name + i}>
        <strong>{decisionSubject(decision)} · {decisionChoice(decision)}</strong>
        <p>{decisionByline(decision)}</p>
        {decision.rationale && <p>{decision.rationale}</p>}
      </li>)}</ul>
    </details>}
    {credits.length > 0 && <details className="decision-detail">
      <summary>Chapter production ({credits.length})</summary>
      <ul>{credits.map(asset => <li key={asset.step_number}>
        <strong>Step {asset.step_number}</strong>
        {asset.video_model && <p>{asset.media_mode === 'illustrated_frames' ? 'Illustration' : 'Footage'} · {asset.video_model}</p>}
        {asset.voice_model && <p>Narration · {asset.voice_model}</p>}
      </li>)}</ul>
    </details>}
    {earlier.length > 0 && <details className="decision-detail">
      <summary>Earlier activity ({earlier.length})</summary><ActivityList records={earlier} status={project.status} connection={connection} />
    </details>}
    {!!activity.length && <ActivityList records={activity.slice(-4)} status={project.status} connection={connection} />}
  </details>;
}

function FilmPlaceholder({ project, filmError, connection }) {
  const status = project?.status;
  const terminal = ['completed', 'partial', 'failed'].includes(status);
  const history = project?.activity || [];
  const latest = history.reduce((last, entry) => !last || Date.parse(entry.timestamp) >= Date.parse(last.timestamp) ? entry : last, null);
  const mediaProgress = project?.media_progress;
  const published = Object.values(project?.media || {}).filter(asset => asset.status === 'ready').length;
  const total = mediaProgress?.step_count ?? project?.plan?.steps?.length ?? 0;
  const count = Math.min(total, Math.max(published, mediaProgress?.ready_count ?? 0));
  const progress = stageProgress(project);
  const active = progress.filter(item => item.state === 'active').map(item => item.key);
  const records = mediaActivity(project);
  const ongoing = records.filter(entry => entry.state === 'started');
  const legacyStage = legacyMediaStage(project, records);
  const assembling = ongoing.filter(entry => entry.stage === 'assembly');
  const stitching = assembling.some(entry => entry.step_number == null) || legacyStage?.stage === 'stitching';
  const assemblingStep = assembling.length === 1 ? assembling[0].step_number : legacyStage?.step_number;
  const visualName = progress.find(item => item.key === 'motion').name;
  const visual = { Illustration: 'illustrations', Motion: 'motion', Footage: 'chapter footage' }[visualName];
  const producing = stitching ? 'Assembling the full film'
    : active.includes('motion') && active.includes('narration') ? 'Creating ' + visual + ' and narration'
    : active.includes('motion') ? 'Creating ' + visual
    : active.includes('narration') ? 'Recording narration'
    : assemblingStep && active.includes('assembly') ? 'Assembling chapter ' + assemblingStep
    : active.includes('assembly') ? 'Assembling chapters' : null;
  const stageTitle = (status === 'generating' && producing) || stageNames[status] || 'Opening your guide';
  const disconnected = connection && !terminal;
  const title = filmError ? 'Playback unavailable' : disconnected ? 'Live updates interrupted' : stageTitle;
  const activeSteps = (project?.plan?.steps || []).filter(step => ongoing.some(entry => entry.step_number === step.step_number));
  const mediaDetail = activeSteps.length === 1 ? 'Step ' + activeSteps[0].step_number + ' · ' + activeSteps[0].title
    : activeSteps.length ? 'Working on steps ' + activeSteps.map(step => step.step_number).join(', ') + '.'
    : stitching ? 'Joining the chapters and their captions.' : 'Preparing the next video stage.';
  const detail = filmError ? 'You can follow the written steps.' : disconnected ? 'Last report · ' + stageTitle : {
    submitting: 'Submitting your request to the workbench.',
    queued: 'Waiting for generation to begin.',
    researching: project?.status_message || 'Finding hands-on sources for your project.',
    planning: project?.status_message || 'Preparing the steps and materials.',
    generating: records.length ? mediaDetail : project?.status_message || 'Preparing the footage and narration.',
    completed: project?.status_message || 'The written steps are available.',
    partial: project?.status_message || 'Follow the available written steps.',
    failed: project?.status_message || 'Open a new guide to try again.',
  }[status];
  return <div className="film-placeholder" id="generation-panel" data-state={filmError ? 'failed' : status}>
    <div className="film-bug"><span className="viewfinder" aria-hidden="true" /> Guide generation</div>
    <div className="film-output" role="status" aria-atomic="true">
      <strong>{title}</strong>
      <span id="status" data-kind={filmError || status === 'failed' ? 'error' : status}>{detail}</span>
    </div>
    <LiveActivity project={project} connection={connection} />
    <div className="film-foot">
      <span>{total > 0 && !filmError ? count + ' of ' + total + ' chapters ready' : ''}</span>
      {Number.isFinite(mediaProgress?.elapsed_seconds) && !terminal ? <span>Media time {timeLabel(mediaProgress.elapsed_seconds)}</span> :
        latest?.timestamp && !terminal && <span>Updated <RecordedTime timestamp={latest.timestamp} /></span>}
    </div>
  </div>;
}

export function Film({ project, film, filmError, captions, poster, videoRef, starts, index, setIndex, onFilmError, connection, example, preview, onClosePreview, continuingPreview }) {
  const previewPlayback = useRef(null);
  const [failedPreview, setFailedPreview] = useState(null);
  const timelineComplete = starts.every((start, i) => start != null && (i === 0 || start > starts[i - 1]));
  function syncStep(event) {
    const video = event.currentTarget;
    if (!timelineComplete || video.seeking || video.readyState < 1) return;
    const active = starts.findLastIndex(start => start <= video.currentTime + 0.0001);
    if (active >= 0 && active !== index) setIndex(active);
  }
  function rememberPreview(event) {
    const video = event.currentTarget;
    previewPlayback.current = {
      step: preview.step.step_number, time: video.ended ? 0 : video.currentTime, playing: !video.paused && !video.ended,
    };
  }
  const fullFilmReady = Boolean(film && !filmError);
  const playable = fullFilmReady && !preview;
  const steps = project?.plan?.steps || [];
  const durations = steps.map(step => project.media?.[step.step_number]?.duration_seconds);
  const duration = durations.length && durations.every(t => Number.isFinite(t) && t > 0)
    ? durations.reduce((sum, t) => sum + t, 0) : null;
  return <section className="film-section" aria-label="Guide film">
    <div className="player">
      {playable ? <video key="full-film" id="video" ref={videoRef} controls playsInline preload="metadata" crossOrigin="anonymous"
        src={film} poster={poster} aria-label={(project?.plan?.project_title || project?.goal || 'Your guide') + ' · Full guide video'}
        onLoadedMetadata={event => {
          const video = event.currentTarget;
          const saved = continuingPreview && previewPlayback.current?.step === steps[index]?.step_number ? previewPlayback.current : null;
          if (starts[index] != null) {
            const maxOffset = Math.max(0, (durations[index] || 0) - 0.1);
            const target = starts[index] + Math.min(saved?.time || 0, maxOffset);
            if (Math.abs(video.currentTime - target) > 0.01) video.currentTime = target;
          }
          if (saved?.playing) video.play().catch(() => {});
          previewPlayback.current = null;
        }}
        onTimeUpdate={syncStep} onSeeked={syncStep} onError={onFilmError}>
        {captions && <track kind="captions" label="English" srcLang="en" src={captions} />}
      </video> : preview ? <video key={preview.clip} id="chapter-video" ref={videoRef} controls autoPlay playsInline
        crossOrigin="anonymous" src={preview.clip} poster={preview.poster} aria-label={preview.step.title + ' · Chapter video'}
        onTimeUpdate={rememberPreview} onSeeked={rememberPreview} onPlay={rememberPreview} onPause={rememberPreview} onEnded={rememberPreview}
        onLoadedMetadata={() => setFailedPreview(null)}
        onError={() => { setFailedPreview(preview.step.step_number); onClosePreview(); }}>
        {preview.captions && <track kind="captions" label="English" srcLang="en" src={preview.captions} />}
      </video> : <FilmPlaceholder project={project} filmError={filmError} connection={connection} />}
    </div>
    {playable && <div className="film-caption" role="status"><span><i className="viewfinder" aria-hidden="true" />
      {example ? 'Example film' : 'Guide film ready'}</span>
      <span>{[duration && timeLabel(duration), captions && 'CC'].filter(Boolean).join(' · ')}</span>
    </div>}
    {preview && <div className="film-caption"><span role="status">Chapter {index + 1} preview</span>
      <button type="button" className="return-progress" onClick={onClosePreview}>
        {fullFilmReady ? 'Return to full film' : 'Return to progress'}
      </button>
    </div>}
    {!preview && !playable && failedPreview && failedPreview === steps[index]?.step_number &&
      <p className="stream-connection" role="status">This chapter could not play. You can still follow its written steps.</p>}
    {preview && !fullFilmReady && <LiveActivity project={project} connection={connection} />}
    {!fullFilmReady && project?.plan && <Progress project={project} connection={connection} compact />}
    {connection && <p className="stream-connection" role="status">{connection.message}</p>}
    <Activity project={project} connection={connection} />
  </section>;
}
