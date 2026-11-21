import { useEffect, useState } from 'react';
import { readSelection, timeLabel } from './project.js';

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

export function Sidebar({ steps, index, onSelect, starts, hasFilm, example, sources, onNew, health }) {
  return <aside className="sidebar" aria-label="Workbench navigation">
    <a className="wordmark" href="/" aria-label="CraftPOV home"><span className="mark" aria-hidden="true"><i /></span>CraftPOV</a>
    <div className="rail-content">
      <p className="rail-label">Guides</p>
      <button className="new-guide" id="new-guide" type="button" onClick={onNew}><span aria-hidden="true">＋</span> New guide</button>
      <a className={'library-link' + (example ? ' active' : '')} href="/" aria-current={example ? 'page' : undefined}>
        <span className="book-icon" aria-hidden="true" />Example guide
      </a>
      {steps.length > 0 && <nav id="outline" aria-labelledby="outline-label">
        <p id="outline-label" className="rail-label">In this guide</p>
        <ol className="step-list" id="step-list">
          {steps.map((step, i) => <li key={step.step_number}>
            <button type="button" aria-current={i === index ? 'step' : undefined} onClick={() => onSelect(i)}>
              <span aria-hidden="true">{String(i + 1).padStart(2, '0')}</span>
              <span>{step.title}</span>
              {hasFilm && starts[i] != null && <time className="chapter-time" dateTime={'PT' + starts[i] + 'S'}>{timeLabel(starts[i])}</time>}
            </button>
          </li>)}
        </ol>
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
  </aside>;
}

export function GuideMeta({ plan }) {
  const supplyCount = plan.materials?.length || 0;
  const facts = [
    plan.difficulty && plan.difficulty.charAt(0).toUpperCase() + plan.difficulty.slice(1),
    plan.estimated_time_minutes && plan.estimated_time_minutes + ' min',
    plan.steps.length + (plan.steps.length === 1 ? ' step' : ' steps'),
  ].filter(Boolean);
  return <p className="meta" id="plan-meta">
    {facts.join(' · ')}
    {supplyCount > 0 && <> · <a href="#supplies-title">{supplyCount} {supplyCount === 1 ? 'supply' : 'supplies'}</a></>}
  </p>;
}

function StepNotes({ step }) {
  const state = step.physical_state || {};
  const notes = [
    ['Before you start', [state.start_state]],
    ['Hand movement', [state.hand_kinematics]],
    ['Check your work', [state.end_state, ...(state.verification_cues || [])]],
    ['Tips', step.tips || []],
  ];
  return <div id="step-notes">{notes.map(([label, entries]) => {
    const lines = entries.filter(Boolean);
    if (!lines.length) return null;
    return <section key={label}>
      <h3>{label}</h3>
      {lines.length === 1 ? <p>{lines[0]}</p> : <ul>{lines.map((text, i) => <li key={i}>{text}</li>)}</ul>}
    </section>;
  })}</div>;
}

function Materials({ materials, checked, setChecked }) {
  const gathered = item => Object.hasOwn(checked, item.id)
    ? checked[item.id] : ['OWNED_EXACT', 'OWNED_SUBSTITUTE'].includes(item.status);
  return <section className="supplies" aria-labelledby="supplies-title">
    <h2 id="supplies-title" tabIndex="-1">Materials &amp; tools</h2>
    <p className="meta" id="supply-count">{materials.length ? materials.filter(gathered).length + ' of ' + materials.length + ' gathered' : 'No supplies listed.'}</p>
    <ul id="materials">{materials.map(item => <li key={item.id}>
      <label>
        <input type="checkbox" checked={gathered(item)} onChange={event => setChecked(item.id, event.target.checked)} />
        <span>{item.name}<small>{[item.quantity, item.required === false && 'Optional', item.substitute_note].filter(Boolean).join(' · ')}</small></span>
      </label>
    </li>)}</ul>
  </section>;
}

export function Guide({ plan, sources, film, captions, poster, videoRef, headingRef, selection, starts, onSelect, filmError, onFilmError }) {
  const { steps } = plan;
  const { index, setIndex, checked, setChecked } = selection;
  const step = steps[index];
  const timelineComplete = starts.every((start, i) => start != null && (i === 0 || start > starts[i - 1]));
  function syncStep(event) {
    const video = event.currentTarget;
    if (!timelineComplete || video.seeking || video.readyState < 1) return;
    const active = starts.findLastIndex(start => start <= video.currentTime + 0.0001);
    if (active >= 0 && active !== index) setIndex(active);
  }
  return <div id="plan">
    <div className="mobile-picker"><label htmlFor="step-picker">Jump to step</label>
      <select id="step-picker" value={index} onChange={event => onSelect(Number(event.target.value))}>
        {steps.map((item, i) => <option key={item.step_number} value={i}>{i + 1}. {item.title}</option>)}
      </select>
    </div>
    <article className="step-card" aria-labelledby="step-title">
      {film && !filmError && <div className="player">
        <video id="video" ref={videoRef} controls playsInline preload="metadata" crossOrigin="anonymous"
          src={film} poster={poster} aria-label={plan.project_title + ' · Full guide video'}
          onLoadedMetadata={event => {
            if (starts[index] != null) event.currentTarget.currentTime = starts[index];
          }}
          onTimeUpdate={syncStep} onSeeked={syncStep} onError={onFilmError}>
          {captions && <track kind="captions" label="English" srcLang="en" src={captions} />}
        </video>
      </div>}
      {filmError && <p className="media-message" role="status">The full video could not play. You can still follow every written step.</p>}
      <div className="step-heading">
        <div><p className="step-count">Step {index + 1} of {steps.length}</p><h2 id="step-title" ref={headingRef} tabIndex="-1">{step.title}</h2></div>
        <div className="nav" role="group" aria-label="Step controls">
          <button type="button" aria-label="Previous step" disabled={index === 0} onClick={() => onSelect(index - 1)}>←</button>
          <button type="button" aria-label="Next step" disabled={index === steps.length - 1} onClick={() => onSelect(index + 1)}>→</button>
        </div>
      </div>
      <p id="instruction">{step.instruction_text}</p>
      <StepNotes step={step} />
      {step.narration_script && <details id="transcript" key={step.step_number}><summary>Narration transcript</summary><p>{step.narration_script}</p></details>}
    </article>
    <div className="references">
      <Materials materials={plan.materials || []} checked={checked} setChecked={setChecked} />
      {sources.length > 0 && <section aria-labelledby="sources-title">
        <h2 id="sources-title" tabIndex="-1">Source tutorials</h2>
        <ul id="source-links">{sources.map((href, i) => <li key={href}><a href={href} rel="noopener noreferrer" target="_blank">Watch tutorial {i + 1} on YouTube</a></li>)}</ul>
      </section>}
    </div>
  </div>;
}
