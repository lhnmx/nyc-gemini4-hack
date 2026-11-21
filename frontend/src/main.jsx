import { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import BuildCoach from './BuildCoach.jsx';
import Builder from './Builder.jsx';
import { Film } from './Film.jsx';
import { ChapterFallback, GuideMeta, GuideReferences, GuideStep, Sidebar, StepPicker, useSelection } from './Guide.jsx';
import { chapterRenderState, chapterSourceLinks, chapterStarts, isFinished, legacyMediaStage, mediaActivity, mediaUrl, useHealth, useProject, youtubeLinks } from './project.js';
import { sourcePreviewLinks } from './sourcePreview.js';
import './workbench.css';

function Workbench({ route, onCreated }) {
  const { project: saved, error, connection } = useProject(route);
  const [builder, setBuilder] = useState(null);
  const [submission, setSubmission] = useState(null);
  const [building, setBuilding] = useState(false);
  const [failedFilm, setFailedFilm] = useState(null);
  const [failedClips, setFailedClips] = useState(() => new Set());
  const [previewStep, setPreviewStep] = useState(null);
  const [sourceChoice, setSourceChoice] = useState(null);
  const health = useHealth(Boolean(builder));
  const video = useRef(null);
  const audio = useRef(null);
  const heading = useRef(null);
  const titleRef = useRef(null);
  const focusFrame = useRef(null);
  const sourceButton = useRef(null);
  const materials = useRef(null);
  const project = submission || saved;
  const unavailable = !submission && error;
  const example = !route.id && !submission;
  const plan = project?.plan;
  const steps = plan?.steps || [];
  const selection = useSelection(route.id || 'paper-plane', steps.length);
  const sources = youtubeLinks(plan?.source_youtube_urls);
  const starts = chapterStarts(steps, project?.media);
  const asset = path => mediaUrl(path, project?.project_id, example);
  const film = asset(project?.full_video_url);
  const captions = asset(project?.full_captions_url);
  const poster = asset(project?.media?.[steps[0]?.step_number]?.keyframe_url);
  const filmError = !!film && failedFilm === film;
  const hasFilm = !!film && !filmError;
  const production = mediaActivity(project);
  const legacyStage = legacyMediaStage(project, production);
  const chapters = steps.map((step, i) => {
    const media = project?.media?.[step.step_number];
    const clip = media?.status === 'ready' && asset(media.video_url);
    let state;
    if (hasFilm && starts[i] != null) state = 'timed';
    else if (clip && !failedClips.has(clip)) state = 'ready';
    else if (media?.status === 'error' || media?.status === 'ready') state = 'error';
    else if (media?.status === 'skipped' || isFinished(project?.status)) state = 'skipped';
    else state = chapterRenderState(step.step_number, media, production, legacyStage);
    return { step, state, start: starts[i], clip, media, sources: chapterSourceLinks(step.source_segments),
      playbackError: failedClips.has(clip), audio: asset(media?.audio_url),
      captions: asset(media?.captions_url), poster: asset(media?.keyframe_url) };
  });
  const selected = chapters[selection.index];
  const chapterPanel = !hasFilm && ['error', 'skipped'].includes(selected?.state);
  const preview = selected?.state === 'ready' && previewStep === selected.step.step_number ? selected : null;
  const sourcePreview = sourceChoice && selected?.step.step_number === sourceChoice.stepNumber &&
    selected.sources.some(segment => segment.href === sourceChoice.href && segment.end_seconds === sourceChoice.segment.end_seconds)
    ? sourceChoice : null;
  const title = unavailable ? 'Guide unavailable' : plan?.project_title || project?.goal || 'Opening guide…';
  const canEdit = !!unavailable || project?.status === 'failed' || project?.status === 'partial';

  useEffect(() => { document.title = title + ' · CraftPOV'; }, [title]);
  useEffect(() => {
    if (route.initial || submission) titleRef.current.focus({ preventScroll: true });
    return () => cancelAnimationFrame(focusFrame.current);
  }, [route, submission]);
  function selectStep(index) {
    if (!steps[index]) return;
    setSourceChoice(null);
    if (hasFilm && !preview && video.current?.readyState >= 1 && starts[index] != null) video.current.currentTime = starts[index];
    setPreviewStep(chapters[index].state === 'ready' ? steps[index].step_number : null);
    selection.setIndex(index);
    cancelAnimationFrame(focusFrame.current);
    focusFrame.current = requestAnimationFrame(() => {
      heading.current?.focus({ preventScroll: true });
      (video.current || document.getElementById('chapter-panel') || heading.current)?.scrollIntoView({ block: 'nearest' });
    });
  }
  function watchSource(segment, button) {
    const links = sourcePreviewLinks(segment.href, segment);
    if (!links) return;
    audio.current?.pause();
    sourceButton.current = button.id;
    setPreviewStep(null);
    setSourceChoice({ ...links, segment, stepNumber: selected.step.step_number });
    cancelAnimationFrame(focusFrame.current);
    focusFrame.current = requestAnimationFrame(() => document.getElementById('chapter-source-video')?.focus());
  }
  function closePreview(restoreFocus = true) {
    setPreviewStep(null);
    if (sourcePreview) {
      setSourceChoice(null);
      if (restoreFocus) {
        cancelAnimationFrame(focusFrame.current);
        focusFrame.current = requestAnimationFrame(() => {
          (document.getElementById(sourceButton.current) || heading.current)?.focus();
        });
      }
    }
  }
  function newGuide(edit = false) {
    video.current?.pause();
    audio.current?.pause();
    setSourceChoice(null);
    setBuilder({ initial: edit ? project : null });
  }
  function openBuildCoach() {
    video.current?.pause();
    setSourceChoice(null);
    setBuilding(true);
  }
  if (building && plan && steps.length > 0) {
    return <BuildCoach
      project={project}
      plan={plan}
      media={project?.media}
      assetUrl={asset}
      example={example}
      stepIndex={selection.index}
      onSelectStep={index => selection.setIndex(index)}
      onBack={() => setBuilding(false)}
      liveCoachReady={health.liveCoachReady}
    />;
  }
  const chapterHelp = selected && !['ready', 'timed'].includes(selected.state) &&
    <ChapterFallback key={selected.step.step_number} chapter={selected} inPlayer={chapterPanel} audioRef={audio}
      planSources={sources} onWatchSource={watchSource} onRetryClip={() => {
        setFailedClips(previous => {
          const next = new Set(previous);
          next.delete(selected.clip);
          return next;
        });
        setSourceChoice(null);
        setPreviewStep(selected.step.step_number);
      }} />;
  return <>
    <a className="skip-link" href="#guide-title">Skip to guide</a>
    <div className="shell">
      <Sidebar chapters={chapters} index={selection.index} onSelect={selectStep}
        sources={sources} example={example} health={health} onNew={() => newGuide()} submitting={!!submission} />
      <main className="main" id="workspace" aria-labelledby="guide-title">
        <header className="topbar">
          <div className="breadcrumb"><span>Workbench</span><span className="crumb-slash" aria-hidden="true">/</span>
            <span id="guide-kind">{example ? 'Example guide' : 'Your guide'}</span>
          </div>
          {steps.length > 0 && <button className="build-now" id="build-now" type="button" onClick={openBuildCoach}>
            Let's build now
          </button>}
        </header>
        <div className="document">
          <header className="guide-header">
            <h1 id="guide-title" ref={titleRef} tabIndex="-1">{title}</h1>
            {plan && <GuideMeta plan={plan} onShop={() => materials.current?.openShop()} />}
          </header>
          {unavailable ? <p className="status" id="status" data-kind="error" role="status">{unavailable}</p> :
            <div id="plan" className="workspace-grid">
              <Film project={project} film={film} poster={poster} captions={captions} starts={starts} index={selection.index}
                setIndex={selection.setIndex} videoRef={video} filmError={filmError} onFilmError={() => setFailedFilm(film)}
                connection={submission ? null : connection} example={example} preview={preview} sourcePreview={sourcePreview}
                chapterHelp={chapterPanel && chapterHelp} audioRef={audio}
                continuingPreview={previewStep != null && previewStep === selected?.step.step_number} onClosePreview={closePreview}
                onPreviewError={url => {
                  setFailedClips(previous => new Set(previous).add(url));
                  setPreviewStep(null);
                }} />
              {steps.length > 0 ? <div className="reading-area">
                <StepPicker chapters={chapters} index={selection.index} onSelect={selectStep} />
                <GuideStep key={selected.step.step_number} chapter={selected} count={steps.length} index={selection.index} headingRef={heading} onSelect={selectStep}
                  chapterHelp={!chapterPanel && chapterHelp}
                  onWatch={selected?.state === 'ready' && !preview ? selectStep : null} />
              </div> : null}
            </div>}
          {plan && <GuideReferences plan={plan} cart={project.cart} materialsRef={materials}
            sources={sources} checked={selection.checked} setChecked={selection.setChecked}
            catalogEnabled={health.catalogEnabled === true} />}
          {canEdit && <div className="recovery">
            <button className="secondary" type="button" onClick={() => newGuide(true)}>{project ? 'Edit project' : 'New guide'}</button>
            <a className="text-link" href="/">Open example guide</a>
          </div>}
        </div>
      </main>
    </div>
    {builder && <Builder initial={builder.initial} health={health} onClose={() => setBuilder(null)} onCreated={onCreated} onSubmitting={setSubmission} />}
  </>;
}

const currentRoute = () => ({ id: new URL(location.href).searchParams.get('project') });

function App() {
  const [route, setRoute] = useState(currentRoute);
  useEffect(() => {
    const restore = () => {
      const next = currentRoute();
      setRoute(previous => previous.id === next.id ? previous : next);
    };
    window.addEventListener('popstate', restore);
    return () => window.removeEventListener('popstate', restore);
  }, []);
  function openCreated(project) {
    const url = new URL(location.href);
    url.searchParams.set('project', project.project_id);
    url.hash = '';
    history.pushState(null, '', url);
    setRoute({ id: project.project_id, initial: project });
  }
  return <Workbench key={route.id || 'example'} route={route} onCreated={openCreated} />;
}

createRoot(document.getElementById('root')).render(<App />);
