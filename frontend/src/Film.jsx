import { useEffect, useRef, useState } from 'react';
import AgentGraph from './AgentGraph.jsx';
import LoopingSourceClip from './LoopingSourceClip.jsx';
import ResearchPreview from './ResearchPreview.jsx';
import { stageProgress } from './activity.js';
import { graphSources } from './graphData.js';
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

function FilmPlaceholder({ project, filmError, connection, inWorkflow }) {
  const status = project?.status;
  const terminal = ['completed', 'partial', 'failed'].includes(status);
  const history = project?.activity || [];
  const phase = { researching: 'research', planning: 'planning' }[status];
  const current = phase && history.filter(entry => entry.stage === phase && entry.state === 'started')
    .sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp))[0];
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
  const visualWork = new Set(ongoing.map(entry => entry.stage));
  const visual = visualWork.has('motion') ? (visualWork.has('illustration') ? 'chapter footage' : 'motion')
    : { Illustration: 'illustrations', Motion: 'motion', Footage: 'chapter footage' }[visualName];
  const producing = stitching ? 'Assembling the full film'
    : active.includes('motion') && active.includes('narration') ? 'Creating ' + visual + ' and narration'
    : active.includes('motion') ? 'Creating ' + visual
    : active.includes('narration') ? 'Recording narration'
    : assemblingStep && active.includes('assembly') ? 'Assembling chapter ' + assemblingStep
    : active.includes('assembly') ? 'Assembling chapters' : null;
  const waitingForVideo = status === 'generating' && mediaProgress?.stage === 'waiting';
  const stageTitle = waitingForVideo ? 'Video queued' : (status === 'generating' && producing) || stageNames[status] || 'Opening your guide';
  const disconnected = connection && !terminal;
  const title = filmError ? 'Playback unavailable' : disconnected ? 'Live updates interrupted' : stageTitle;
  const activeSteps = (project?.plan?.steps || []).filter(step => legacyStage?.step_number === step.step_number
    || ongoing.some(entry => entry.step_number === step.step_number));
  const chapterNames = activeSteps.slice(0, 2).map(step => 'Step ' + step.step_number + ' · ' + step.title);
  if (activeSteps.length > 2) chapterNames.push('+' + (activeSteps.length - 2) + ' more');
  const mediaDetail = chapterNames.join(' / ') || (stitching ? 'Joining the chapters and their captions.' : 'Preparing the next video stage.');
  const reviewingSources = current?.id.startsWith('source-selection') && current.sources?.length;
  const operation = reviewingSources ? 'Reviewing ' + reviewingSources + (reviewingSources === 1 ? ' YouTube tutorial.' : ' YouTube tutorials.')
    : current?.detail || current?.label;
  const detail = filmError ? 'You can follow the written steps.' : disconnected ? 'Last report · ' + stageTitle : {
    submitting: '', queued: 'Research starts automatically when a spot opens. You can keep this page open for updates.',
    researching: operation || project?.status_message,
    planning: operation || project?.status_message,
    generating: records.length || activeSteps.length ? mediaDetail : project?.status_message || 'Preparing the footage and narration.',
    completed: project?.status_message || 'The written steps are available.',
    partial: project?.status_message || 'Follow the available written steps.',
    failed: project?.status_message || 'Open a new guide to try again.',
  }[status];
  return <div className={inWorkflow ? 'workflow-progress' : 'film-placeholder'}>
    <div className="film-foot">{!inWorkflow && <span className="film-bug"><i className="viewfinder" aria-hidden="true" /> Guide film</span>}
      {total > 0 && !filmError && <span>{count} of {total} chapters ready</span>}
      {Number.isFinite(mediaProgress?.elapsed_seconds) && !terminal && !waitingForVideo && <span>Media time {timeLabel(mediaProgress.elapsed_seconds)}</span>}
    </div>
    <div className="film-output" role="status" aria-atomic="true">
      <strong>{title}</strong>
      <span id="status" data-kind={filmError || status === 'failed' ? 'error' : status}>{detail}</span>
    </div>
  </div>;
}

export function Film({ project, film, filmError, captions, poster, videoRef, starts, index, setIndex, onFilmError, connection,
  example, preview, sourcePreview, chapterHelp, audioRef, onPreviewError, onClosePreview, continuingPreview }) {
  const previewPlayback = useRef(null);
  const [playing, setPlaying] = useState(null);
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
  const playable = fullFilmReady && !preview && !sourcePreview;
  const sources = project ? graphSources(project, connection) : [];
  const linked = sources.filter(source => source.url);
  const liveResearch = !project?.plan && ['researching', 'planning'].includes(project?.status);
  const stopPreview = owner => setPlaying(previous => previous?.owner === owner ? null : previous);
  function togglePreview(owner, url, interval) {
    if (playing?.owner === owner && playing.url === url && playing.interval === interval) {
      stopPreview(owner);
      return;
    }
    videoRef.current?.pause();
    audioRef.current?.pause();
    if (sourcePreview) onClosePreview(false);
    setPlaying({ owner, url, interval });
  }
  useEffect(() => { if (sourcePreview) setPlaying(null); }, [sourcePreview]);
  useEffect(() => {
    setPlaying(previous => previous?.owner === 'research' ? null : previous);
  }, [liveResearch]);
  const research = <ResearchPreview sources={linked} recorded={!liveResearch} project={project} connection={connection}
    playing={playing} togglePreview={togglePreview} stopPreview={stopPreview} />;
  const steps = project?.plan?.steps || [];
  const durations = steps.map(step => project.media?.[step.step_number]?.duration_seconds);
  const duration = durations.length && durations.every(t => Number.isFinite(t) && t > 0)
    ? durations.reduce((sum, t) => sum + t, 0) : null;
  return <><section className="film-section" aria-label="Guide film">
    <div className="player" id={playable || preview || sourcePreview ? undefined : 'generation-panel'} data-state={filmError ? 'failed' : project?.status}
      onPlayCapture={() => setPlaying(null)}>
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
        onError={event => onPreviewError(event.currentTarget.getAttribute('src'))}>
        {preview.captions && <track kind="captions" label="English" srcLang="en" src={preview.captions} />}
      </video> : sourcePreview ? <LoopingSourceClip iframeId="chapter-source-video" key={sourcePreview.embed}
        url={sourcePreview.segment.source_url} segment={sourcePreview.segment} loop={false}
        title={steps[index].title + ' · Source clip ' + timeLabel(sourcePreview.segment.start_seconds) + '–' + timeLabel(sourcePreview.segment.end_seconds)}
      />
        : chapterHelp || <FilmPlaceholder project={project} filmError={filmError} connection={connection} />}
      {playable && <div className="film-caption" role="status"><span><i className="viewfinder" aria-hidden="true" />
        {example ? 'Example film' : 'Guide film ready'}</span>
        <span>{[duration && timeLabel(duration), captions && 'CC'].filter(Boolean).join(' · ')}</span>
      </div>}
      {(preview || sourcePreview) && <div className="film-caption">
        {sourcePreview ? <a href={sourcePreview.href} rel="noopener noreferrer" target="_blank">
          Source clip · {timeLabel(sourcePreview.segment.start_seconds)}–{timeLabel(sourcePreview.segment.end_seconds)} on YouTube ↗</a>
          : <span role="status">Chapter {index + 1} {preview.media?.media_mode === 'illustrated_frames' ? '· Illustrated preview' : 'preview'}</span>}
        <button type="button" className="return-progress" onClick={onClosePreview}>
          {fullFilmReady ? 'Return to full film' : sourcePreview ? 'Back to guide' : 'Return to progress'}
        </button>
      </div>}
      {project && <AgentGraph project={project} connection={connection} playback={Boolean(playable || preview || sourcePreview)}
        sources={sources} playing={playing} togglePreview={togglePreview} stopPreview={stopPreview}
        progress={chapterHelp && <FilmPlaceholder inWorkflow project={project} filmError={filmError} connection={connection} />}
        replayPreview={!liveResearch && linked.length > 0 ? research : null} />}
    </div>
    {connection && <p className="stream-connection" role="status">{connection.message}</p>}
  </section>{liveResearch && research}</>;
}
