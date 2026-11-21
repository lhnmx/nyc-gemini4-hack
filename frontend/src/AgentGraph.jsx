import { useLayoutEffect, useRef, useState } from 'react';
import { decisionByline, operationLabels, operationState, stageProgress, stateNames } from './activity.js';
import { graphEdges, reviewLabel } from './graphData.js';
import { chapterSourceLinks, timeLabel } from './project.js';
import { sourcePreviewLinks } from './sourcePreview.js';
import LoopingSourceClip from './LoopingSourceClip.jsx';
import './agentGraph.css';

const paths = {
  graph: <><rect x="2" y="8" width="5" height="5" rx="1.3" /><rect x="15" y="2" width="5" height="5" rx="1.3" /><rect x="15" y="14" width="5" height="5" rx="1.3" /><path d="M7 10.5h3m0 0v-6h5m-5 6v6h5" /></>,
  research: <><circle cx="9" cy="9" r="5.5" /><path d="m13.5 13.5 5.2 5.2" /></>,
  planning: <><rect x="4" y="3" width="14" height="16" rx="2" /><path d="M8 7h6M8 11h6M8 15h3" /></>,
  motion: <><rect x="2" y="5" width="12" height="12" rx="2" /><path d="m14 9 6-3v10l-6-3" /></>,
  source: <><rect x="2.5" y="4.5" width="17" height="13" rx="3" /><path d="m9 8 5 3-5 3Z" /></>,
  kept: <path d="m4 11 4.5 4.5L18 6" />,
  skipped: <path d="m6 6 10 10M16 6 6 16" />,
  external: <><path d="M9 4H5a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-4M12 3h7v7m0-7L9 13" /></>,
};
function Icon({ name }) {
  return <svg className="graph-icon" aria-hidden="true" viewBox="0 0 22 22" fill="none" stroke="currentColor"
    strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>;
}

function edgePath(edge, boxes, compact) {
  const a = boxes[edge.from], b = boxes[edge.to];
  if (!a || !b) return null;
  const ay = (a.top + a.bottom) / 2, by = (b.top + b.bottom) / 2;
  if (!compact || edge.type === 'review') {
    const middle = (a.right + b.left) / 2;
    return 'M ' + a.right + ' ' + ay + ' C ' + middle + ' ' + ay + ', ' + middle + ' ' + by + ', ' + (b.left - 2) + ' ' + by;
  }
  if (edge.type === 'branch' || edge.type === 'merge') {
    const branch = edge.type === 'branch';
    const start = branch ? a.left : a.right;
    const end = branch ? b.left : b.right;
    const turn = branch ? 8 : boxes.width - 8;
    const direction = branch ? 1 : -1;
    const bend = 5;
    return 'M ' + start + ' ' + ay + ' H ' + (turn + direction * bend) + ' Q ' + turn + ' ' + ay + ' ' + turn + ' ' + (ay + bend)
      + ' V ' + (by - bend) + ' Q ' + turn + ' ' + by + ' ' + (turn + direction * bend) + ' ' + by + ' H ' + (end - direction * 2);
  }
  const ax = (a.left + a.right) / 2, bx = (b.left + b.right) / 2;
  return 'M ' + ax + ' ' + a.bottom + ' L ' + bx + ' ' + (b.top - 2);
}

function Edges({ edges, topology, selected }) {
  const svg = useRef(null);
  const [geometry, setGeometry] = useState(null);
  useLayoutEffect(() => {
    const root = svg.current.parentElement;
    const nodes = [...root.querySelectorAll('[data-node]')];
    const measure = () => {
      const origin = root.getBoundingClientRect();
      const boxes = { width: origin.width };
      for (const node of nodes) {
        const rect = node.getBoundingClientRect();
        boxes[node.dataset.node] = { left: rect.left - origin.left, right: rect.right - origin.left,
          top: rect.top - origin.top, bottom: rect.bottom - origin.top };
      }
      setGeometry({ boxes, compact: getComputedStyle(root).getPropertyValue('--graph-flow').trim() === 'vertical' });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(root);
    nodes.forEach(node => observer.observe(node));
    return () => observer.disconnect();
  }, [topology]);
  return <svg ref={svg} className="graph-edges" aria-hidden="true">
    <defs>{['regular', 'selected'].map(tone => <marker id={'arrow-' + tone} key={tone}
      markerUnits="userSpaceOnUse" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
      <path className={'graph-arrow ' + tone} d="m1 1 5.5 3L1 7" fill="none" strokeWidth="1.6" strokeLinejoin="round" />
    </marker>)}</defs>
    {geometry && edges.map(edge => {
      const highlighted = selected && (selected.id === edge.from || selected.id === edge.to || (edge.branch && selected.source?.key === edge.branch));
      return <path key={edge.from + ':' + edge.to} data-from={edge.from} data-to={edge.to} data-selected={Boolean(highlighted)}
        data-pending={edge.pending} d={edgePath(edge, geometry.boxes, geometry.compact)} fill="none"
        markerEnd={'url(#arrow-' + (highlighted ? 'selected' : 'regular') + ')'} />;
    })}
  </svg>;
}

function graphStages(project, connection, sources) {
  const history = project.activity || [];
  const progress = stageProgress(project, connection);
  const filmParts = ['motion', 'illustration', 'narration', 'assembly'];
  return progress.filter(stage => ['research', 'planning', 'motion'].includes(stage.key)).map(stage => {
    const film = stage.key === 'motion';
    const parts = film ? filmParts : [stage.key];
    const records = history.filter(event => parts.includes(event.stage)).sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp));
    const current = records.find(record => ['started', 'offline'].includes(operationState(record, project.status, connection)));
    const partStates = progress.filter(item => parts.includes(item.key)).map(item => item.state);
    let state = ['active', 'offline', 'failed', 'interrupted', 'unconfirmed', 'recorded', 'waiting', 'unknown']
      .find(state => partStates.includes(state));
    if (film && state === 'waiting' && project.status === 'generating') state = connection ? 'offline' : 'active';
    if (stage.key === 'research' && sources.length && ['waiting', 'unknown'].includes(state)) state = 'recorded';
    return { id: stage.key, title: film ? 'Create video guide' : stage.name, state, records, current, kind: 'agent' };
  });
}

function graphNodes(stages, sources) {
  const rows = sources.flatMap(source => [
    { id: 'source-' + source.key, title: source.title, kind: 'source', source },
    { id: 'review-' + source.key, title: reviewLabel(source), kind: 'review', source },
  ]);
  return [stages[0], ...rows, ...stages.slice(1)];
}

function GraphNode({ node, row, selected, select }) {
  const { kind, source } = node;
  const style = source ? { '--row': row, '--mobile-row': row + 1 } : undefined;
  const isStage = kind === 'agent';
  const choice = kind === 'review' ? node.title : null;
  const reviewIcon = ['Failed', 'Skipped'].includes(choice) ? 'skipped'
    : ['Kept', 'Cited', 'Reviewed', 'Analyzed'].includes(choice) ? 'kept' : 'research';
  const subtitle = source && 'Tutorial ' + source.number;
  const clipStatus = source?.analysisState === 'started' ? 'Analyzing' : source?.clips.length ? clipCount(source.clips.length) : null;

  const name = kind === 'review' ? node.title + ' · ' + source.title
    : kind === 'source' ? [subtitle, source.title, clipStatus].filter(Boolean).join(' · ') : node.title + ' · ' + stateNames[node.state];
  return <button type="button" className={'graph-node ' + kind} data-node={node.id} data-state={node.state} data-branch={source?.key}
    data-outcome={choice} style={style} aria-label={name} aria-pressed={selected} aria-controls="graph-detail" onClick={() => select(node.id)}>
    {isStage ? <>
      <span className="node-icon"><Icon name={node.id} /></span><strong>{node.title}</strong>
      <span className="node-state"><i />{stateNames[node.state]}</span>
      {node.current?.label && <small className="node-operation">{node.current.step_number && 'Step ' + node.current.step_number + ' · '}{node.current.label}</small>}
    </> : kind === 'source' ? <>
      <small className="node-type"><Icon name="source" />YouTube{clipStatus && ' · ' + clipStatus}</small><strong title={source.title}>{source.title}</strong>
    </> : <>
      <strong><Icon name={reviewIcon} />{node.title}</strong>
    </>}
  </button>;
}

function clipCount(count) {
  return count + (count === 1 ? ' clip' : ' clips');
}

function SourcePlayer({ url, title, clip }) {
  const links = sourcePreviewLinks(url, clip);
  return <div className="workflow-preview">
    <LoopingSourceClip url={url} segment={clip || null} title={title} />
    <p>If playback is restricted, <a href={links.href} target="_blank" rel="noreferrer">
      {clip ? 'open the clip on YouTube' : 'open the tutorial on YouTube'} <Icon name="external" /></a>.</p>
  </div>;
}

function SourceFootage({ source, playing, togglePreview, stopPreview }) {
  const preview = playing?.owner === 'graph' && playing.url === source.url ? playing.interval : null;
  const links = sourcePreviewLinks(source.url);
  if (!links) return source.hasTitle && <a href={'https://www.youtube.com/results?search_query=' + encodeURIComponent(source.title)}
    target="_blank" rel="noreferrer">Search YouTube for this title <Icon name="external" /></a>;

  function toggle(key) {
    togglePreview('graph', source.url, key);
  }
  function clipRow(clip) {
    const interval = timeLabel(clip.start_seconds) + '–' + timeLabel(clip.end_seconds);
    const title = clip.technique || 'Source footage';
    const open = preview === interval;
    return <li key={interval}>
      <button type="button" className="graph-clip" onClick={() => toggle(interval)} aria-expanded={open}
        aria-label={(open ? 'Close preview of ' : 'Preview ') + title + ', ' + interval}>
        <Icon name="source" /><span>{interval}</span><strong>{title}</strong>
      </button>
      {clip.hand_action && <p>{clip.hand_action}</p>}
      {clip.tools_visible?.length > 0 && <p className="clip-tools">Visible tools · {clip.tools_visible.join(', ')}</p>}
      {clip.notes && <p>{clip.notes}</p>}
      {open && <SourcePlayer url={source.url} title={source.title + ' · ' + title + ' · ' + interval} clip={clip} />}
    </li>;
  }
  const clips = source.clips;
  const full = preview === 'tutorial';
  return <div className="source-footage">
    <div className="source-links">
      <button type="button" className="graph-preview-button" onClick={() => toggle('tutorial')} aria-expanded={full}>
        <Icon name="source" />{full ? 'Close tutorial preview' : 'Preview tutorial'}
      </button>
      <a href={links.href} target="_blank" rel="noreferrer">Watch on YouTube <Icon name="external" /></a>
    </div>
    {full && <SourcePlayer url={source.url} title={source.title + ' · Tutorial preview'} />}
    {clips.length > 0 && <div className="observed-clips">
      <h4>Observed clips</h4>
      <ul>{clipRow(clips[0])}</ul>
      {clips.length > 1 && <details className="graph-more" onToggle={event => {
        if (!event.currentTarget.open && preview !== 'tutorial'
          && preview !== timeLabel(clips[0].start_seconds) + '–' + timeLabel(clips[0].end_seconds)) stopPreview('graph');
      }}>
        <summary>{clips.length - 1} more {clips.length === 2 ? 'clip' : 'clips'}</summary><ul>{clips.slice(1).map(clipRow)}</ul>
      </details>}
    </div>}
  </div>;
}

function SourceDetail({ source, reviewing, playing, togglePreview, stopPreview }) {
  const { decision, analysis } = source;
  const choice = reviewLabel(source);
  const nativeScore = decision?.rationale?.startsWith('Decisions API probabilities:') || decision?.rationale === 'Decisions API choice';
  let reason = decision?.rationale;
  if (nativeScore) {
    const scores = Object.fromEntries([...reason.matchAll(/(KEEP_TUTORIAL|SKIP_FILLER)=(\d+(?:\.\d+)?)/g)]
      .map(([, option, score]) => [option, Math.round(Number(score) * 100)]));
    if (Number.isFinite(scores.KEEP_TUTORIAL) && Number.isFinite(scores.SKIP_FILLER)) {
      reason = 'The selector scored this source ' + scores.KEEP_TUTORIAL + '% for hands-on instruction and ' + scores.SKIP_FILLER + '% for filler.';
    } else if (choice === 'Kept') reason = 'The selector classified this as a hands-on, step-by-step tutorial.';
    else if (choice === 'Skipped') reason = 'The selector classified this as filler or unrelated to the project.';
  }
  if (decision && !reason) reason = 'The selector did not record a reason.';
  const observation = analysis?.detail?.startsWith(source.title + ' · ') ? analysis.detail.slice(source.title.length + 3) : analysis?.detail;
  const match = Number.isFinite(decision?.confidence) ? Math.round(decision.confidence * 100) + '% confidence' : null;
  const badge = decision ? decisionByline(decision) : null;
  const attribution = <>
    {reason && <p>{reason}</p>}
    {observation && !source.clips.length && <p className="detail-observation">{observation}</p>}
    {analysis && <div className="operation-credit">{[analysis.agent, analysis.model, operationLabels[source.analysisState]].filter(Boolean).join(' · ')}</div>}
    {(badge || match) && <div className="detail-badges">{[badge, match].filter(Boolean).map(text => <span key={text}>{text}</span>)}</div>}
  </>;
  return <>
    <div className="detail-eyebrow"><span>Tutorial {source.number}</span><span className="detail-outcome" data-outcome={choice}>{choice}</span>
      {source.cited && choice !== 'Cited' && <span className="detail-cited">Cited in plan</span>}</div>
    <h3>{source.title}</h3>
    {(source.channel || source.duration_seconds > 0) && <p className="detail-byline">
      {[source.channel, source.duration_seconds > 0 && timeLabel(source.duration_seconds)].filter(Boolean).join(' · ')}
    </p>}
    {reviewing && attribution}
    {source.cited && !decision && !analysis && <p>The build plan lists this tutorial as a source.</p>}
    <SourceFootage source={source} playing={playing} togglePreview={togglePreview} stopPreview={stopPreview} />
    {!reviewing && (source.clips.length && (analysis || decision)
      ? <details className="graph-more"><summary>Research details</summary>{attribution}</details> : attribution)}
  </>;
}

function Operation({ record, project, connection }) {
  const step = project.plan?.steps?.find(item => item.step_number === record.step_number);
  const detail = record.detail || (step && 'Step ' + step.step_number + ' · ' + step.title);
  const sources = chapterSourceLinks(step?.source_segments);
  const timestamp = new Date(record.timestamp);
  return <li data-operation-id={record.id}>
    <div className="operation-title"><strong>{record.label}</strong>
      <small>{operationLabels[operationState(record, project.status, connection)]}</small>
      {Number.isFinite(timestamp.getTime()) && <time dateTime={timestamp.toISOString()} title={timestamp.toLocaleString()}>
        {timestamp.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</time>}
    </div>
    {detail && <p>{detail}</p>}
    <div className="operation-credit">{[record.agent, record.model].filter(Boolean).join(' · ')}</div>
    {sources.length > 0 && <div className="chapter-evidence">{sources.map(segment =>
      <a key={segment.segment_id || segment.href} href={segment.href} target="_blank" rel="noreferrer">
        {segment.source_title || 'Source footage'} · {timeLabel(segment.start_seconds)} <Icon name="external" />
      </a>)}</div>}
  </li>;
}

function StageDetail({ stage, project, connection }) {
  const active = stage.records.filter(record => ['started', 'offline'].includes(operationState(record, project.status, connection)));
  const latest = active.length ? active : stage.records.slice(0, 1);
  const other = stage.records.filter(record => !latest.includes(record));
  const credits = stage.id === 'motion' && Object.values(project.media || {}).filter(asset => asset.video_model || asset.voice_model);
  const rows = records => <ul className="graph-operations">{records.map(record =>
    <Operation key={record.id} record={record} project={project} connection={connection} />)}</ul>;
  return <>
    {latest.length ? rows(latest) : <p>{stage.state === 'waiting' ? 'Waiting to start.' : 'No individual operations were recorded.'}</p>}
    {other.length > 0 && <details className="graph-more"><summary>Earlier operations ({other.length})</summary>{rows(other)}</details>}
    {credits?.length > 0 && <details className="graph-more"><summary>Chapter credits ({credits.length})</summary>
      <ul className="graph-operations">{credits.map(asset => <li key={asset.step_number}>
        <strong>Step {asset.step_number}</strong>
        {asset.video_model && <p>{asset.media_mode === 'illustrated_frames' ? 'Illustration' : 'Footage'} · {asset.video_model}</p>}
        {asset.voice_model && <p>Narration · {asset.voice_model}</p>}
      </li>)}</ul>
    </details>}
  </>;
}

export default function AgentGraph({ project, connection, playback, sources, playing, togglePreview, stopPreview, recordedResearch, progress }) {
  const section = useRef(null);
  const disclosure = useRef(null);
  const [picked, setPicked] = useState(null);
  const [allSources, setAllSources] = useState(false);
  const [focusedBranch, setFocusedBranch] = useState(null);
  const [expanded, setExpanded] = useState(!playback);
  useLayoutEffect(() => {
    const control = disclosure.current;
    const reveal = () => {
      if (['#workflow', '#agent-graph'].includes(window.location.hash)) {
        control.open = true;
        section.current.scrollIntoView({ block: 'start' });
      }
    };
    control.open = !playback || control.contains(document.activeElement);
    reveal();
    window.addEventListener('hashchange', reveal);
    return () => window.removeEventListener('hashchange', reveal);
  }, [playback]);
  const totalClips = sources.reduce((count, source) => count + source.clips.length, 0);
  const stages = graphStages(project, connection, sources);
  const inspectedSource = sources.find(source => picked === 'source-' + source.key || picked === 'review-' + source.key);
  const featured = sources.find(source => source.key === focusedBranch) || inspectedSource
    || sources.find(source => source.analysisState === 'started') || sources.find(source => source.clips.length) || sources.find(source => source.cited)
    || sources.find(source => source.decision?.choice === 'KEEP_TUTORIAL') || sources[0];
  const visible = allSources ? sources : featured ? [featured] : [];
  const nodes = graphNodes(stages, visible);
  const edges = graphEdges(visible, project);
  const selection = nodes.find(node => node.id === picked);
  const highlight = selection || stages.find(stage => ['active', 'offline'].includes(stage.state));
  function select(id) {
    stopPreview('graph');
    setPicked(previous => previous === id ? null : id);
  }
  return <section id="workflow" ref={section} className="agent-graph" aria-label="Workflow" tabIndex="-1"
    onFocus={event => setFocusedBranch(event.target.closest('[data-branch]')?.dataset.branch || null)}
    onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setFocusedBranch(null); }}>
    <details ref={disclosure} className="graph-disclosure" onToggle={event => {
      if (event.target !== disclosure.current) return;
      setExpanded(disclosure.current.open);
      if (!disclosure.current.open) {
        stopPreview('graph');
        if (recordedResearch) stopPreview('research');
      }
    }}>
      <summary><Icon name="graph" /><span>Workflow</span>
        {!!sources.length && <small>{sources.length} {sources.length === 1 ? 'source' : 'sources'}{totalClips > 0 && ' · ' + clipCount(totalClips)}</small>}
      </summary>
      {progress}
      {expanded && recordedResearch}
      <div className="graph-frame">
        <div className="graph-board" data-empty={!visible.length}
          style={{ '--rows': Math.max(visible.length, 1), '--mobile-plan': visible.length + 2, '--mobile-motion': visible.length + 3 }}>
          <Edges edges={edges} topology={nodes.map(node => node.id).join('|')} selected={highlight} />
          {nodes.map(node => <GraphNode key={node.id} node={node} row={visible.indexOf(node.source) + 1}
            selected={node.id === picked} select={select} />)}
        </div>
        {sources.length > 1 && <button type="button" className="graph-sources" aria-expanded={allSources}
          onClick={() => setAllSources(value => !value)}>
          {allSources ? 'Show one source' : 'Show all ' + sources.length + ' sources'}
        </button>}
        <div className="graph-detail" id="graph-detail" hidden={!selection} role="region" aria-label="Selected node details">
          {selection && expanded && (selection.source ? <SourceDetail key={selection.source.key} source={selection.source}
            reviewing={selection.kind === 'review'} playing={playing} togglePreview={togglePreview} stopPreview={stopPreview} /> :
            <StageDetail stage={selection} project={project} connection={connection} />)}
        </div>
      </div>
    </details>
  </section>;
}
