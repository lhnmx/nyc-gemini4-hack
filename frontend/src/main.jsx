import { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import BuildCoach from './BuildCoach.jsx';
import Builder from './Builder.jsx';
import { Guide, GuideMeta, Sidebar, useSelection } from './Guide.jsx';
import { chapterStarts, guideStatus, mediaUrl, useHealth, useProject, youtubeLinks } from './project.js';
import './workbench.css';

function Workbench({ route, onCreated }) {
  const { project, error, connection } = useProject(route);
  const [builder, setBuilder] = useState(null);
  const [building, setBuilding] = useState(false);
  const [failedFilm, setFailedFilm] = useState(null);
  const health = useHealth(Boolean(builder));
  const video = useRef(null);
  const heading = useRef(null);
  const titleRef = useRef(null);
  const focusFrame = useRef(null);
  const example = !route.id;
  const plan = project?.plan;
  const steps = plan?.steps || [];
  const selection = useSelection(route.id || 'paper-glider', steps.length);
  const sources = youtubeLinks(plan?.source_youtube_urls);
  const starts = chapterStarts(steps, project?.media);
  const asset = path => mediaUrl(path, project?.project_id, example);
  const film = asset(project?.full_video_url);
  const captions = asset(project?.full_captions_url);
  const poster = asset(project?.media?.[steps[0]?.step_number]?.keyframe_url);
  const filmError = !!film && failedFilm === film;
  const hasFilm = !!film && !filmError;
  const status = error ? { kind: 'error', message: error } : connection || guideStatus(project, example, hasFilm);
  const title = error ? 'Guide unavailable' : plan?.project_title || project?.goal || 'Opening guide…';
  const canEdit = !!error || project?.status === 'failed' || project?.status === 'partial';

  useEffect(() => { document.title = title + ' · CraftPOV'; }, [title]);
  useEffect(() => {
    if (route.initial) titleRef.current.focus();
    return () => cancelAnimationFrame(focusFrame.current);
  }, [route]);
  function selectStep(index) {
    if (!steps[index]) return;
    if (video.current?.readyState >= 1 && starts[index] != null) video.current.currentTime = starts[index];
    selection.setIndex(index);
    cancelAnimationFrame(focusFrame.current);
    focusFrame.current = requestAnimationFrame(() => {
      heading.current?.focus({ preventScroll: true });
      (video.current || heading.current)?.scrollIntoView({ block: 'nearest' });
    });
  }
  function newGuide(edit = false) {
    video.current?.pause();
    setBuilder({ initial: edit ? project : null });
  }
  function openBuildCoach() {
    video.current?.pause();
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
  return <>
    <a className="skip-link" href="#guide-title">Skip to guide</a>
    <div className="shell">
      <Sidebar steps={steps} starts={starts} index={selection.index} onSelect={selectStep} hasFilm={hasFilm}
        sources={sources} example={example} health={health} onNew={() => newGuide()} />
      <main className="main" id="workspace" aria-labelledby="guide-title">
        <header className="topbar">
          <div className="breadcrumb"><span>Workbench</span><span className="crumb-slash" aria-hidden="true">/</span>
            <span id="guide-kind">{example ? 'Example guide' : 'Your guide'}</span>
          </div>
          <div className="topbar-actions">
            {steps.length > 0 && <button className="build-now" id="build-now" type="button" onClick={openBuildCoach}>
              Let's build now
            </button>}
            <button className="compact-new" type="button" onClick={() => newGuide()}><span aria-hidden="true">＋</span> New guide</button>
          </div>
        </header>
        <div className="document">
          <h1 id="guide-title" ref={titleRef} tabIndex="-1">{title}</h1>
          {plan && <GuideMeta plan={plan} />}
          <p className="status" id="status" data-kind={status.kind} role="status" aria-atomic="true">{status.message}</p>
          {steps.length > 0 && <Guide plan={plan} sources={sources} film={film} poster={poster} captions={captions}
            starts={starts} selection={selection} onSelect={selectStep} videoRef={video} headingRef={heading}
            filmError={filmError} onFilmError={() => setFailedFilm(film)} />}
          {canEdit && <div className="recovery">
            <button className="secondary" type="button" onClick={() => newGuide(true)}>{project ? 'Edit project' : 'New guide'}</button>
            <a className="text-link" href="/">Open example guide</a>
          </div>}
        </div>
      </main>
    </div>
    {builder && <Builder initial={builder.initial} health={health} onClose={() => setBuilder(null)} onCreated={onCreated} />}
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
