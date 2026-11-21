import { useEffect, useRef, useState } from 'react';
import { readSelection, timeLabel } from './project.js';
import Materials from './Materials.jsx';

export function useSelection(projectId, stepCount) {
  const [selection, setSelection] = useState(() => readSelection(projectId));
  const index = Math.max(0, Math.min(selection.index, stepCount - 1));
  useEffect(() => {
    if (!projectId) return;
    try { sessionStorage.setItem('workbench-' + projectId, JSON.stringify(selection)); } catch { /* Optional storage. */ }
  }, [projectId, selection]);
  return {
    index, checked: selection.checked,
    setIndex: index => setSelection(old => old.index === index ? old : { ...old, index }),
    setChecked: (id, checked) => setSelection(old => ({ ...old, checked: { ...old.checked, [id]: checked } })),
  };
}

const chapterLabels = {
  queued: 'Queued', waiting: 'Awaiting assembly', rendering: 'Rendering',
  ready: 'Play chapter', error: 'Video unavailable', skipped: 'Written only',
};

function chapterLabel(chapter) {
  if (chapter.state === 'ready' && chapter.media?.media_mode === 'illustrated_frames') return 'Play illustrated chapter';
  if (['error', 'skipped'].includes(chapter.state)) {
    if (chapter.audio) return 'Narration available';
    if (chapter.sources.length) return 'YouTube reference';
  }
  return chapterLabels[chapter.state];
}

function ChapterList({ chapters, index, onSelect, id }) {
  return <ol className="step-list" id={id}>
    {chapters.map((chapter, i) => {
      const { step, state, start } = chapter;
      return <li key={step.step_number}>
        <button type="button" data-chapter-state={state} aria-current={i === index ? 'step' : undefined} onClick={() => onSelect(i)}>
          <span className="chapter-index" aria-hidden="true">{String(i + 1).padStart(2, '0')}</span>
          <span className="chapter-label"><span>{step.title}</span>
            {state !== 'timed' && <small>{chapterLabel(chapter)}</small>}
          </span>
          {state === 'timed'
            ? <time className="chapter-time" dateTime={'PT' + start + 'S'}>{timeLabel(start)}</time>
            : <i className={'chapter-icon ' + state} aria-hidden="true" />}
        </button>
      </li>;
    })}
  </ol>;
}

export function Sidebar({ chapters, index, onSelect, example, sources, onNew, health, submitting }) {
  return <aside className="sidebar" aria-label="Workbench navigation">
    <a className="wordmark" href="/" aria-label="CraftPOV home"><span className="mark" aria-hidden="true"><i /></span>CraftPOV</a>
    <div className="rail-content">
      <p className="rail-label">Library</p>
      <button className="new-guide" id="new-guide" type="button" onClick={onNew} disabled={submitting}><span aria-hidden="true">＋</span> New guide</button>
      <a className={'library-link' + (example ? ' active' : '')} href="/" aria-current={example ? 'page' : undefined}>
        <span className="book-icon" aria-hidden="true" />Example guide
      </a>
      {chapters.length > 0 && <nav id="outline" aria-labelledby="outline-label">
        <p id="outline-label" className="rail-label">Chapters</p>
        <ChapterList chapters={chapters} index={index} onSelect={onSelect} id="step-list" />
        <div className="rail-links">
          <a href="#supplies-title">Materials &amp; tools</a>
          {sources.length > 0 && <a href="#sources-title">Source tutorials</a>}
        </div>
      </nav>}
    </div>
    <details className="service" data-state={health.blocked ? 'warning' : health.blocked === false ? 'ready' : undefined}>
      <summary><span className="status-dot" aria-hidden="true" />{health.label}</summary>
      <p>{health.detail}</p>
    </details>
    <button className="compact-new" type="button" disabled={submitting} onClick={onNew}><span aria-hidden="true">＋</span> New guide</button>
  </aside>;
}

export function GuideMeta({ plan, onShop }) {
  const materials = plan.materials?.length || 0;
  const difficulty = plan.difficulty?.trim();
  return <ul className="meta guide-facts" id="plan-meta" aria-label="Guide details">
    {difficulty && <li>{difficulty.charAt(0).toUpperCase() + difficulty.slice(1)}</li>}
    {plan.estimated_time_minutes > 0 && <li>{plan.estimated_time_minutes} min</li>}
    <li>{plan.steps.length} {plan.steps.length === 1 ? 'step' : 'steps'}</li>
    {materials > 0 && <li><a href="#supplies-title" className="shop-materials" onClick={onShop}>
      Shop materials <span aria-hidden="true">↓</span></a></li>}
  </ul>;
}

const sentences = typeof Intl.Segmenter === 'function' ? new Intl.Segmenter(undefined, { granularity: 'sentence' }) : null;

function Instruction({ text }) {
  const actions = text?.length > 260 && sentences
    ? Array.from(sentences.segment(text), entry => entry.segment.trim()).filter(Boolean) : [];
  if (actions.length < 2) return <p id="instruction">{text}</p>;
  return <ul id="instruction" className="instruction-actions">{actions.map((action, i) => <li key={i}>{action}</li>)}</ul>;
}

function StepNotes({ step, sourceLinks }) {
  const state = step.physical_state || {};
  const cues = state.verification_cues || [];
  const check = cues[0] || state.end_state;
  const groups = [
    ['Starting position', [state.start_state]],
    ['Hand movement', [state.hand_kinematics]],
    ['Finished position', [state.end_state !== check && state.end_state, ...cues.slice(1)]],
    ['Tips', step.tips || []],
  ].map(([label, lines]) => [label, lines.filter(Boolean)]).filter(([, lines]) => lines.length);
  return <>
    {check && <div className="step-check"><h3>Check your work</h3><p>{check}</p></div>}
    <div className="step-details">
      {groups.length > 0 && <details className="disclosure">
        <summary>Technique &amp; tips</summary>
        <div id="step-notes">{groups.map(([label, lines]) => <section key={label}>
          <h3>{label}</h3>
          {lines.length === 1 ? <p>{lines[0]}</p> : <ul>{lines.map((line, i) => <li key={i}>{line}</li>)}</ul>}
        </section>)}</div>
      </details>}
      {sourceLinks.length > 0 && <details className="disclosure">
        <summary>Source footage</summary>
        <ul className="activity-list">{sourceLinks.map(segment => <li key={segment.segment_id}>
          <a className="text-link" href={segment.href} target="_blank" rel="noopener noreferrer">
            {segment.source_title || 'YouTube tutorial'} · {timeLabel(segment.start_seconds)}–{timeLabel(segment.end_seconds)}
            {' '}<span aria-hidden="true">↗</span>
          </a>
          <p>{segment.hand_action}</p>
        </li>)}</ul>
      </details>}
      {step.narration_script && <details className="disclosure" id="transcript">
        <summary>Narration transcript</summary><p>{step.narration_script}</p>
      </details>}
    </div>
  </>;
}

export function ChapterFallback({ chapter, inPlayer, audioRef, planSources, onWatchSource, onRetryClip }) {
  const { state, media, playbackError, sources, step } = chapter;
  const unavailable = state === 'error' || state === 'skipped';
  const error = playbackError ? 'This chapter could not play. You can still follow its written steps.'
    : media?.error || (state === 'error' ? 'This chapter’s video is unavailable.'
      : state === 'skipped' ? 'This step has no generated video.' : null);
  const reviews = (media?.shot_reviews || []).filter(review => review.status === 'rejected' || review.status === 'uncertain');
  const review = reviews.at(-1);
  const findings = [review?.contact, review?.result, review?.opening];
  const issue = findings.find(finding => finding?.verdict === 'contradicts')
    || findings.find(finding => finding?.verdict === 'unclear');
  if (!error && !sources.length) return null;
  const lead = error?.split(/(?<=[.!?])\s+/, 1)[0];
  const summary = !error || error.length <= 160 ? error
    : lead.length <= 160 ? lead : 'The chapter video is unavailable.';
  const detail = error && error !== summary ? (error.startsWith(summary) ? error.slice(summary.length).trim() : error) : '';
  const observation = issue?.observation && !error?.includes(issue.observation) ? issue.observation : '';
  return <div id={inPlayer ? 'chapter-panel' : undefined} className={inPlayer ? 'chapter-panel' : 'chapter-fallback'}>
    {inPlayer && <div className="chapter-panel-heading">
      <p>Step {step.step_number} · {state === 'skipped' ? 'Written step' : 'Video unavailable'}</p>
      <strong>{step.title}</strong>
    </div>}
    {summary && <p className="chapter-issue">{summary}</p>}
    {unavailable && chapter.audio && <div className="chapter-audio">
      <span>Chapter narration</span>
      <audio key={chapter.audio} ref={audioRef} controls preload="metadata" src={chapter.audio} aria-label={'Narration for ' + step.title} />
    </div>}
    {playbackError && <button className="secondary retry-chapter" type="button" onClick={onRetryClip}>Retry chapter video</button>}
    {sources.map((segment, i) => <div className="source-choice" key={segment.segment_id || segment.href + '-' + segment.end_seconds}>
      <button id={'step-' + step.step_number + '-source-' + i} type="button" className="secondary" onClick={event => onWatchSource(segment, event.currentTarget)}
        aria-label={'Preview source clip for ' + step.title + ' · ' + timeLabel(segment.start_seconds) + '–' + timeLabel(segment.end_seconds)}>
        <span aria-hidden="true">▶</span> Preview source clip · {timeLabel(segment.start_seconds)}–{timeLabel(segment.end_seconds)}
      </button>
      <a href={segment.href} rel="noopener noreferrer" target="_blank"
        aria-label={'Open ' + (segment.source_title || 'source tutorial') + ' on YouTube at ' + timeLabel(segment.start_seconds)}>
        Open on YouTube <span aria-hidden="true">↗</span></a>
      {segment.hand_action && <p>{segment.hand_action}</p>}
    </div>)}
    {unavailable && sources.length === 0 && (planSources.length === 1
      ? <a className="full-source" href={planSources[0]} rel="noopener noreferrer" target="_blank">Full source tutorial on YouTube ↗</a>
      : planSources.length > 1 && <a className="full-source" href="#sources-title">See the guide’s full source tutorials ↓</a>)}
    {(detail || observation) && <details className="review-note">
      <summary>{issue ? 'What the footage review found' : 'Video generation details'}</summary>
      {detail && <p>{detail}</p>}{observation && <p>{observation}</p>}
    </details>}
  </div>;
}

export function GuideStep({ chapter, count, index, onSelect, headingRef, onWatch, chapterHelp }) {
  const { step, state, sources } = chapter;
  const hasVideo = state === 'ready' || state === 'timed';
  return <article className="step-card" aria-labelledby="step-title">
    <div className="step-controls">
      <p className="step-count">Step {index + 1} of {count}</p>
      <div className="nav" role="group" aria-label="Step controls">
        <button type="button" aria-label="Previous step" disabled={index === 0} onClick={() => onSelect(index - 1)}>←</button>
        <button type="button" aria-label="Next step" disabled={index === count - 1} onClick={() => onSelect(index + 1)}>→</button>
      </div>
    </div>
    <h2 id="step-title" ref={headingRef} tabIndex="-1">{step.title}</h2>
    <Instruction text={step.instruction_text} />
    {chapterHelp}
    <StepNotes step={step} sourceLinks={hasVideo ? sources : []} />
    {onWatch && <button className="chapter-watch secondary" type="button" onClick={() => onWatch(index)}>
      <i className="chapter-icon ready" aria-hidden="true" /> Watch this chapter
    </button>}
  </article>;
}

export function GuideReferences({ plan, cart, materialsRef, sources, checked, setChecked, catalogEnabled }) {
  return <div className="references">
    <Materials ref={materialsRef} materials={plan.materials || []} cart={cart}
      checked={checked} setChecked={setChecked} catalogEnabled={catalogEnabled} />
    {sources.length > 0 && <section className="sources" aria-labelledby="sources-title">
      <h2 id="sources-title" tabIndex="-1">Source tutorials</h2>
      <ul id="source-links">{sources.map((href, i) => <li key={href}>
        <a href={href} rel="noopener noreferrer" target="_blank">Tutorial {i + 1} on YouTube <span aria-hidden="true">↗</span></a>
      </li>)}</ul>
    </section>}
  </div>;
}

export function StepPicker({ chapters, index, onSelect }) {
  const picker = useRef(null);
  const chapter = chapters[index];
  return <details className="mobile-picker" id="step-picker" ref={picker}>
    <summary><span>Chapters</span>
      <span className="current-chapter">{index + 1} of {chapters.length}
        {chapter.state === 'timed' ? <time dateTime={'PT' + chapter.start + 'S'}>{timeLabel(chapter.start)}</time> :
          <><i className={'chapter-icon ' + chapter.state} aria-hidden="true" />{chapter.state === 'ready' ? 'Ready' : chapterLabel(chapter)}</>}
      </span>
    </summary>
    <ChapterList chapters={chapters} index={index} id="mobile-step-list" onSelect={i => {
      picker.current.open = false;
      onSelect(i);
    }} />
  </details>;
}
