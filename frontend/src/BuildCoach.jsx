import { useEffect, useRef, useState } from 'react';
import { LiveCoachClient } from './liveClient.js';
import { apiUrl } from './project.js';

export default function BuildCoach({ project, plan, media, assetUrl, example, stepIndex, onSelectStep, onBack, liveCoachReady }) {
  const steps = plan.steps || [];
  const step = steps[stepIndex] || steps[0];
  const [status, setStatus] = useState('idle'); // idle | connecting | live | error
  const [speaking, setSpeaking] = useState(false);
  const [inspecting, setInspecting] = useState(false);
  const [muted, setMuted] = useState(false);
  const [cameraOn, setCameraOn] = useState(true);
  const [cameras, setCameras] = useState([]);
  const [selectedCameraId, setSelectedCameraId] = useState('');
  const [activeCameraLabel, setActiveCameraLabel] = useState('');
  const [error, setError] = useState('');
  const [observation, setObservation] = useState('');
  const [mistake, setMistake] = useState(null);
  const [completedSteps, setCompletedSteps] = useState(() => ({}));
  const [log, setLog] = useState([]);

  const videoRef = useRef(null);
  const streamRef = useRef(null);
  const clientRef = useRef(null);
  const userPickedCameraRef = useRef(false);
  const inputDraftRef = useRef('');
  const outputDraftRef = useRef('');

  const isDeskView = device => /desk\s*view|overhead/i.test(device?.label || '');

  async function refreshCameras({ preferDeskView = false } = {}) {
    if (!navigator.mediaDevices?.enumerateDevices) return [];
    const devices = await navigator.mediaDevices.enumerateDevices();
    const videoInputs = devices.filter(d => d.kind === 'videoinput' && d.deviceId);
    setCameras(videoInputs);
    if (preferDeskView && !userPickedCameraRef.current) {
      const desk = videoInputs.find(isDeskView);
      if (desk?.deviceId) setSelectedCameraId(desk.deviceId);
    }
    return videoInputs;
  }

  useEffect(() => {
    refreshCameras({ preferDeskView: true }).catch(() => {});
    const media = navigator.mediaDevices;
    if (!media?.addEventListener) return;
    const handleChange = () => refreshCameras({ preferDeskView: true }).catch(() => {});
    media.addEventListener('devicechange', handleChange);
    return () => media.removeEventListener('devicechange', handleChange);
  }, []);

  function appendLog(role, text) {
    const clean = text.trim();
    if (!clean) return;
    setLog(prev => [...prev.slice(-14), { id: Date.now() + Math.random(), role, text: clean }]);
  }

  function flushDrafts() {
    if (inputDraftRef.current.trim()) {
      appendLog('you', inputDraftRef.current);
      inputDraftRef.current = '';
    }
    if (outputDraftRef.current.trim()) {
      appendLog('coach', outputDraftRef.current);
      outputDraftRef.current = '';
    }
  }

  function stopCamera() {
    if (streamRef.current) {
      for (const track of streamRef.current.getTracks()) track.stop();
      streamRef.current = null;
    }
    setActiveCameraLabel('');
    if (videoRef.current) {
      videoRef.current.srcObject = null;
    }
  }

  function stopSession() {
    flushDrafts();
    clientRef.current?.disconnect();
    clientRef.current = null;
    stopCamera();
    setSpeaking(false);
    setStatus('idle');
  }

  useEffect(() => {
    return () => {
      clientRef.current?.disconnect();
      clientRef.current = null;
      if (streamRef.current) {
        for (const track of streamRef.current.getTracks()) track.stop();
        streamRef.current = null;
      }
    };
  }, []);

  function findStepIndex(stepNumber) {
    return steps.findIndex(item => item.step_number === Number(stepNumber));
  }

  async function fetchStepReference(stepNumber) {
    const rawPath = media?.[stepNumber]?.keyframe_url;
    const url = rawPath && assetUrl ? assetUrl(rawPath) : null;
    if (!url) return null;
    try {
      const resp = await fetch(url);
      if (!resp.ok) return null;
      const blob = await resp.blob();
      const base64 = await new Promise(resolve => {
        const reader = new FileReader();
        reader.onloadend = () => {
          const out = typeof reader.result === 'string' ? reader.result.split(',')[1] : null;
          resolve(out || null);
        };
        reader.onerror = () => resolve(null);
        reader.readAsDataURL(blob);
      });
      return { blob, base64, mimeType: blob.type || 'image/jpeg' };
    } catch {
      return null;
    }
  }

  async function sendStepReference(client, targetStep) {
    if (!client || !targetStep) return;
    const ref = await fetchStepReference(targetStep.step_number);
    if (ref?.base64) {
      client.sendReferenceImage(targetStep.step_number, targetStep.title, ref.base64, ref.mimeType);
    }
  }

  async function runInspection(stepNumber, userQuestion = '') {
    const client = clientRef.current;
    if (!client) return { ok: false, error: 'Coach is not connected.' };
    const frameBlob = await client.captureFrameBlob({ maxWidth: 1280, quality: 0.9 });
    if (!frameBlob) {
      return { verdict: 'UNCLEAR', what_i_see: 'No clear camera frame available.', fix: 'Hold your workpiece up to the camera.' };
    }
    setInspecting(true);
    try {
      const projectId = example ? 'example' : project.project_id;
      const form = new FormData();
      form.append('step_number', String(stepNumber || step?.step_number || 1));
      form.append('user_question', userQuestion || '');
      if (example) form.append('plan_json', JSON.stringify(plan));
      form.append('frame', frameBlob, 'frame.jpg');
      const ref = await fetchStepReference(stepNumber || step?.step_number || 1);
      if (ref?.blob) {
        form.append('reference_frame', ref.blob, 'reference.jpg');
      }
      const resp = await fetch(apiUrl('/api/projects/' + encodeURIComponent(projectId) + '/coach/inspect'), {
        method: 'POST',
        body: form,
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        return { ok: false, error: data.detail || 'Inspection failed.' };
      }
      if (data.verdict === 'ADJUST' || data.verdict === 'REDO') {
        const issue = (data.mistakes && data.mistakes.length > 0)
          ? data.mistakes.join(' ')
          : data.what_i_see;
        setMistake({
          step_number: data.step_number,
          issue,
          fix: data.fix || '',
        });
      } else if (data.verdict === 'PASS') {
        setMistake(null);
        setCompletedSteps(prev => ({ ...prev, [data.step_number]: true }));
      }
      if (data.what_i_see) setObservation(data.what_i_see);
      return data;
    } catch (err) {
      return { ok: false, error: err?.message || 'Inspection request failed.' };
    } finally {
      setInspecting(false);
    }
  }

  async function switchVideoTrack(deviceId) {
    // Release existing video tracks first — macOS Desk View shares hardware with the main camera.
    const audioTracks = streamRef.current ? streamRef.current.getAudioTracks() : [];
    if (streamRef.current) {
      for (const oldTrack of streamRef.current.getVideoTracks()) {
        oldTrack.stop();
      }
    }
    const newVideoStream = await navigator.mediaDevices.getUserMedia({
      video: { deviceId: { exact: deviceId } },
    });
    const [newTrack] = newVideoStream.getVideoTracks();
    if (!newTrack) return;
    newTrack.enabled = cameraOn;
    const nextStream = new MediaStream([...audioTracks, newTrack]);
    streamRef.current = nextStream;
    setActiveCameraLabel(newTrack.label || '');
    setSelectedCameraId(newTrack.getSettings?.().deviceId || deviceId);
    if (videoRef.current) {
      videoRef.current.srcObject = nextStream;
      await videoRef.current.play().catch(() => {});
    }
  }

  async function handleCameraSelect(event) {
    const nextId = event.target.value;
    userPickedCameraRef.current = true;
    setSelectedCameraId(nextId);
    setError('');
    if (streamRef.current && nextId) {
      try {
        await switchVideoTrack(nextId);
      } catch (err) {
        setError(err?.message || 'Could not switch camera.');
      }
    }
  }

  async function startSession() {
    setError('');
    setMistake(null);
    setStatus('connecting');
    try {
      if (!navigator.mediaDevices?.getUserMedia) {
        if (!window.isSecureContext) {
          const host = window.location.hostname;
          const hint = host === '0.0.0.0'
            ? `Open http://localhost:${window.location.port || '8000'}${window.location.pathname}${window.location.search} instead of 0.0.0.0.`
            : 'Open this page over HTTPS or http://localhost.';
          throw new Error(`Camera and mic require a secure context. ${hint}`);
        }
        throw new Error('Your browser does not support webcam and microphone access.');
      }
      const initialDevices = await refreshCameras();
      const deskInitial = initialDevices.find(isDeskView);
      const preselected = userPickedCameraRef.current
        ? selectedCameraId
        : (deskInitial?.deviceId || selectedCameraId || '');
      const videoConstraints = preselected
        ? { deviceId: { exact: preselected } }
        : true;

      const mediaStream = await navigator.mediaDevices.getUserMedia({
        video: videoConstraints,
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      streamRef.current = mediaStream;

      // Labels unlock after permission is granted — auto-switch to Desk View Camera if available.
      const unlockedDevices = await refreshCameras();
      const activeTrack = mediaStream.getVideoTracks()[0];
      const activeId = activeTrack?.getSettings?.().deviceId || '';
      const deskView = unlockedDevices.find(isDeskView);
      if (!userPickedCameraRef.current && deskView?.deviceId && deskView.deviceId !== activeId) {
        await switchVideoTrack(deskView.deviceId);
      } else {
        setActiveCameraLabel(activeTrack?.label || '');
        if (activeId) setSelectedCameraId(activeId);
        if (videoRef.current) {
          videoRef.current.srcObject = streamRef.current;
          await videoRef.current.play().catch(() => {});
        }
      }

      const projectId = example ? 'example' : project.project_id;
      const response = await fetch(apiUrl('/api/projects/' + encodeURIComponent(projectId) + '/coach/session'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          current_step: step?.step_number || 1,
          plan: example ? plan : undefined,
        }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.detail || 'Could not start the live coach session.');
      }

      const client = new LiveCoachClient({
        onStatus: next => setStatus(next),
        onSpeakingChange: active => setSpeaking(active),
        onInputTranscript: chunk => {
          inputDraftRef.current += chunk;
        },
        onOutputTranscript: chunk => {
          if (inputDraftRef.current.trim()) {
            appendLog('you', inputDraftRef.current);
            inputDraftRef.current = '';
          }
          outputDraftRef.current += chunk;
        },
        onTurnComplete: () => flushDrafts(),
        onError: message => {
          setError(message);
        },
        onToolCall: async (name, args) => {
          if (name === 'inspect_workpiece') {
            return await runInspection(args.step_number || step?.step_number || 1, args.user_question || '');
          }
          if (name === 'sync_current_step') {
            const target = findStepIndex(args.step_number);
            if (target >= 0) onSelectStep(target);
            if (args.observation) setObservation(args.observation);
            return { ok: true, active_step: args.step_number };
          }
          if (name === 'flag_mistake') {
            setMistake({
              step_number: args.step_number,
              issue: args.issue || 'Check your alignment on this step.',
              fix: args.fix || '',
            });
            return { ok: true, flagged: true };
          }
          if (name === 'complete_step') {
            setCompletedSteps(prev => ({ ...prev, [args.step_number]: true }));
            setMistake(null);
            const nextNum = args.next_step_number ?? (Number(args.step_number) + 1);
            const nextIdx = findStepIndex(nextNum);
            if (nextIdx >= 0) onSelectStep(nextIdx);
            return { ok: true, completed: args.step_number };
          }
          return { ok: false, error: 'Unknown tool: ' + name };
        },
      });
      client.setMuted(muted);
      client.setCameraEnabled(cameraOn);
      clientRef.current = client;
      await client.connect(payload, { mediaStream: streamRef.current, videoEl: videoRef.current });
      sendStepReference(client, step).catch(() => {});
      appendLog('system', 'Live coach connected. Hold your work up to the camera anytime or ask out loud.');
    } catch (err) {
      stopCamera();
      clientRef.current?.disconnect();
      clientRef.current = null;
      setStatus('error');
      setError(err?.message || 'Could not start live coach.');
    }
  }

  function toggleMute() {
    const next = !muted;
    setMuted(next);
    clientRef.current?.setMuted(next);
    if (streamRef.current) {
      for (const track of streamRef.current.getAudioTracks()) track.enabled = !next;
    }
  }

  function toggleCamera() {
    const next = !cameraOn;
    setCameraOn(next);
    clientRef.current?.setCameraEnabled(next);
    if (streamRef.current) {
      for (const track of streamRef.current.getVideoTracks()) track.enabled = next;
    }
  }

  function selectStep(nextIndex) {
    if (!steps[nextIndex]) return;
    onSelectStep(nextIndex);
    setMistake(null);
    const chosen = steps[nextIndex];
    if (status === 'live') {
      clientRef.current?.sendPrompt(
        `[UI update] I switched to Step ${chosen.step_number}: ${chosen.title}.`,
        { turnComplete: false },
      );
      sendStepReference(clientRef.current, chosen).catch(() => {});
    }
  }

  function askCoach(question) {
    if (status !== 'live') return;
    appendLog('you', question);
    clientRef.current?.sendPrompt(question, { turnComplete: true });
  }

  function handleBack() {
    stopSession();
    onBack();
  }

  const badgeLabel = status === 'live'
    ? (inspecting ? 'Inspecting closely…' : speaking ? 'Coach speaking' : muted ? 'Watching · Mic muted' : 'Live · Watching & listening')
    : status === 'connecting'
      ? 'Connecting…'
      : status === 'error'
        ? 'Connection issue'
        : 'Ready to build';

  const ps = step?.physical_state || {};

  return <div className="coach-screen" aria-labelledby="coach-title">
    <header className="coach-topbar">
      <button className="secondary" type="button" onClick={handleBack}>← Back to guide</button>
      <div className="coach-heading">
        <p className="step-count">Live Build Coach</p>
        <h1 id="coach-title">{plan.project_title}</h1>
      </div>
      <span className="coach-badge" data-state={status} data-speaking={speaking || undefined}>
        <span className="coach-dot" aria-hidden="true" />
        {badgeLabel}
      </span>
    </header>

    <div className="coach-layout">
      <section className="coach-stage" aria-label="Live camera and voice coach">
        <div className="camera-frame">
          <video ref={videoRef} autoPlay playsInline muted className={cameraOn ? '' : 'camera-hidden'} />
          {status !== 'live' && status !== 'connecting' && <div className="camera-placeholder">
            <p>Start the live coach, then tilt your screen down or hold your piece up to check.</p>
            <p className="camera-sub">It stays quiet while you work, inspects closely when you hold your work up, and answers whenever you ask what to do next.</p>
          </div>}
          {!cameraOn && (status === 'live' || status === 'connecting') && <div className="camera-placeholder">
            <p>Camera paused</p>
          </div>}
          {(observation || activeCameraLabel) && status === 'live' && <div className="coach-observation">
            {observation || activeCameraLabel}
          </div>}
          {mistake && <div className="mistake-banner" role="alert">
            <div>
              <strong>Adjustment · Step {mistake.step_number}</strong>
              <p>{mistake.issue}{mistake.fix ? ' — ' + mistake.fix : ''}</p>
            </div>
            <button type="button" className="dismiss-mistake" aria-label="Dismiss adjustment" onClick={() => setMistake(null)}>×</button>
          </div>}
        </div>

        <div className="coach-controls">
          {status !== 'live' && status !== 'connecting'
            ? <button className="primary coach-start" type="button" disabled={liveCoachReady === false} onClick={startSession}>
                Start live coach
              </button>
            : <button className="secondary coach-stop" type="button" onClick={stopSession}>
                End session
              </button>}
          <button className="secondary" type="button" disabled={status !== 'live'} onClick={toggleMute}>
            {muted ? 'Unmute mic' : 'Mute mic'}
          </button>
          <button className="secondary" type="button" disabled={status !== 'live'} onClick={toggleCamera}>
            {cameraOn ? 'Camera off' : 'Camera on'}
          </button>
          {cameras.length > 0 && <label className="camera-picker">
            <span className="sr-only">Camera</span>
            <select aria-label="Camera" value={selectedCameraId} onChange={handleCameraSelect}>
              {cameras.map((cam, i) => <option key={cam.deviceId || i} value={cam.deviceId}>
                {cam.label || `Camera ${i + 1}`}
              </option>)}
            </select>
          </label>}
          <div className="coach-quick">
            <button className="secondary" type="button" disabled={status !== 'live'} onClick={() => askCoach('What do I do next?')}>
              What do I do next?
            </button>
            <button className="secondary" type="button" disabled={status !== 'live'} onClick={() => askCoach('Check my work — am I doing this step right?')}>
              Check my work
            </button>
          </div>
        </div>

        {liveCoachReady === false && <p className="status" data-kind="error" role="status">
          Live coach needs GEMINI_API_KEY configured on the server.
        </p>}
        {error && <p className="status" data-kind="error" role="alert">{error}</p>}

        <div className="coach-feed" aria-live="polite">
          <h2>Session notes</h2>
          {log.length === 0
            ? <p className="meta">Your voice questions and coach tips will appear here.</p>
            : <ul>{log.map(entry => <li key={entry.id} data-role={entry.role}>
                <span>{entry.role === 'you' ? 'You' : entry.role === 'coach' ? 'Coach' : 'Status'}</span>
                <p>{entry.text}</p>
              </li>)}</ul>}
        </div>
      </section>

      <aside className="coach-companion" aria-label="Current step reference">
        <div className="step-heading coach-step-head">
          <div>
            <p className="step-count">Step {stepIndex + 1} of {steps.length}</p>
            <h2>{step.title}</h2>
          </div>
          <div className="nav" role="group" aria-label="Step controls">
            <button type="button" aria-label="Previous step" disabled={stepIndex === 0} onClick={() => selectStep(stepIndex - 1)}>←</button>
            <button type="button" aria-label="Next step" disabled={stepIndex === steps.length - 1} onClick={() => selectStep(stepIndex + 1)}>→</button>
          </div>
        </div>

        <p className="coach-instruction">{step.instruction_text}</p>

        {ps.hand_kinematics && <section className="coach-note">
          <h3>Hand movement</h3>
          <p>{ps.hand_kinematics}</p>
        </section>}

        {(ps.end_state || (ps.verification_cues && ps.verification_cues.length > 0)) && <section className="coach-note">
          <h3>Check your work</h3>
          <ul>
            {ps.end_state && <li>{ps.end_state}</li>}
            {(ps.verification_cues || []).map((cue, i) => <li key={i}>{cue}</li>)}
          </ul>
        </section>}

        <section className="coach-steps-outline">
          <h3>All steps</h3>
          <ol className="step-list">
            {steps.map((item, i) => <li key={item.step_number}>
              <button type="button" aria-current={i === stepIndex ? 'step' : undefined} onClick={() => selectStep(i)}>
                <span aria-hidden="true">{completedSteps[item.step_number] ? '✓' : String(i + 1).padStart(2, '0')}</span>
                <span>{item.title}</span>
              </button>
            </li>)}
          </ol>
        </section>
      </aside>
    </div>
  </div>;
}
