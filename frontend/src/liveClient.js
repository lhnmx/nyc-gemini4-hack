// Browser client for Gemini Multimodal Live API (direct WebSocket with ephemeral token).

const WORKLET_CODE = `
class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = [];
    this.targetRate = 16000;
  }
  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input || !input.length) return true;
    const ratio = sampleRate / this.targetRate;
    const outLength = Math.max(1, Math.floor(input.length / ratio));
    for (let i = 0; i < outLength; i++) {
      const idx = Math.min(input.length - 1, Math.floor(i * ratio));
      const clamped = Math.max(-1, Math.min(1, input[idx]));
      this.buffer.push(clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff);
    }
    if (this.buffer.length >= 1600) {
      const chunk = new Int16Array(this.buffer.splice(0, 1600));
      this.port.postMessage(chunk.buffer, [chunk.buffer]);
    }
    return true;
  }
}
registerProcessor('pcm-capture-processor', PcmCaptureProcessor);
`;

function bytesToBase64(bytes) {
  let binary = '';
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
  }
  return btoa(binary);
}

function base64ToInt16(base64) {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new Int16Array(bytes.buffer);
}

export class LiveCoachClient {
  constructor({ onStatus, onInputTranscript, onOutputTranscript, onTurnComplete, onToolCall, onSpeakingChange, onError }) {
    this.onStatus = onStatus || (() => {});
    this.onInputTranscript = onInputTranscript || (() => {});
    this.onOutputTranscript = onOutputTranscript || (() => {});
    this.onTurnComplete = onTurnComplete || (() => {});
    this.onToolCall = onToolCall || (() => ({ ok: true }));
    this.onSpeakingChange = onSpeakingChange || (() => {});
    this.onError = onError || (() => {});

    this.ws = null;
    this.ready = false;
    this.muted = false;
    this.cameraEnabled = true;

    this.captureCtx = null;
    this.captureNode = null;
    this.captureSource = null;
    this.workletUrl = null;

    this.playbackCtx = null;
    this.nextPlayTime = 0;
    this.activeSources = new Set();

    this.frameTimer = null;
    this.frameCanvas = null;
    this.videoEl = null;
  }

  async connect(session, { mediaStream, videoEl }) {
    this.videoEl = videoEl;
    this.onStatus('connecting');

    this.playbackCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
    await this.playbackCtx.resume().catch(() => {});

    await new Promise((resolve, reject) => {
      const ws = new WebSocket(session.ws_url);
      this.ws = ws;
      let settled = false;

      ws.onopen = () => {
        const setup = {
          setup: {
            model: session.model,
            generationConfig: {
              responseModalities: ['AUDIO'],
              mediaResolution: 'MEDIA_RESOLUTION_MEDIUM',
              speechConfig: {
                voiceConfig: {
                  prebuiltVoiceConfig: { voiceName: session.voice || 'Aoede' },
                },
              },
            },
            systemInstruction: {
              parts: [{ text: session.system_instruction }],
            },
            proactivity: { proactiveAudio: true },
            inputAudioTranscription: {},
            outputAudioTranscription: {},
            tools: session.tools || [],
          },
        };
        ws.send(JSON.stringify(setup));
      };

      ws.onmessage = async event => {
        try {
          const text = typeof event.data === 'string' ? event.data : await event.data.text();
          const msg = JSON.parse(text);
          if (msg.setupComplete) {
            this.ready = true;
            if (!settled) {
              settled = true;
              resolve();
            }
            this.onStatus('live');
            return;
          }
          this._handleMessage(msg);
        } catch (err) {
          this.onError(err?.message || 'Could not read coach message.');
        }
      };

      ws.onerror = () => {
        const err = new Error('Live coach connection failed.');
        if (!settled) {
          settled = true;
          reject(err);
        } else {
          this.onError(err.message);
        }
      };

      ws.onclose = event => {
        const wasReady = this.ready;
        this.ready = false;
        this._stopMediaLoops();
        if (!settled) {
          settled = true;
          reject(new Error(event.reason || 'Live coach closed before connecting.'));
        } else if (wasReady && event.code !== 1000) {
          this.onError(event.reason || 'Live coach disconnected.');
          this.onStatus('error');
        } else {
          this.onStatus('idle');
        }
      };
    });

    if (mediaStream) {
      await this._startMicCapture(mediaStream);
    }
    this._startVideoCapture();
  }

  async _startMicCapture(mediaStream) {
    const audioTracks = mediaStream.getAudioTracks();
    if (!audioTracks.length) return;

    this.captureCtx = new (window.AudioContext || window.webkitAudioContext)();
    await this.captureCtx.resume().catch(() => {});

    const blob = new Blob([WORKLET_CODE], { type: 'application/javascript' });
    this.workletUrl = URL.createObjectURL(blob);
    await this.captureCtx.audioWorklet.addModule(this.workletUrl);

    const audioStream = new MediaStream(audioTracks);
    this.captureSource = this.captureCtx.createMediaStreamSource(audioStream);
    this.captureNode = new AudioWorkletNode(this.captureCtx, 'pcm-capture-processor');
    this.captureNode.port.onmessage = event => {
      if (this.muted || !this.ready || this.ws?.readyState !== WebSocket.OPEN) return;
      const bytes = new Uint8Array(event.data);
      this.ws.send(JSON.stringify({
        realtimeInput: {
          audio: {
            mimeType: 'audio/pcm;rate=16000',
            data: bytesToBase64(bytes),
          },
        },
      }));
    };
    this.captureSource.connect(this.captureNode);
  }

  _startVideoCapture() {
    if (!this.videoEl) return;
    this.frameCanvas = document.createElement('canvas');
    const sendFrame = () => {
      if (!this.cameraEnabled || !this.ready || this.ws?.readyState !== WebSocket.OPEN) return;
      const video = this.videoEl;
      if (!video || video.readyState < 2 || !video.videoWidth || !video.videoHeight) return;
      const maxWidth = 768;
      const scale = Math.min(1, maxWidth / video.videoWidth);
      const width = Math.max(1, Math.round(video.videoWidth * scale));
      const height = Math.max(1, Math.round(video.videoHeight * scale));
      if (this.frameCanvas.width !== width || this.frameCanvas.height !== height) {
        this.frameCanvas.width = width;
        this.frameCanvas.height = height;
      }
      const ctx = this.frameCanvas.getContext('2d');
      if (!ctx) return;
      ctx.drawImage(video, 0, 0, width, height);
      const dataUrl = this.frameCanvas.toDataURL('image/jpeg', 0.78);
      const base64 = dataUrl.split(',')[1];
      if (!base64) return;
      this.ws.send(JSON.stringify({
        realtimeInput: {
          video: {
            mimeType: 'image/jpeg',
            data: base64,
          },
        },
      }));
    };
    sendFrame();
    this.frameTimer = window.setInterval(sendFrame, 1500);
  }

  captureFrameBlob({ maxWidth = 1280, quality = 0.9 } = {}) {
    return new Promise(resolve => {
      const video = this.videoEl;
      if (!video || video.readyState < 2 || !video.videoWidth || !video.videoHeight) {
        resolve(null);
        return;
      }
      const canvas = document.createElement('canvas');
      const scale = Math.min(1, maxWidth / video.videoWidth);
      canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
      canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
      const ctx = canvas.getContext('2d');
      if (!ctx) {
        resolve(null);
        return;
      }
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      canvas.toBlob(blob => resolve(blob), 'image/jpeg', quality);
    });
  }

  sendReferenceImage(stepNumber, stepTitle, base64Jpeg, mimeType = 'image/jpeg') {
    if (!base64Jpeg || !this.ready || this.ws?.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify({
      clientContent: {
        turns: [{
          role: 'user',
          parts: [
            { text: `[Visual reference for Step ${stepNumber}: ${stepTitle}] Compare the user's workpiece against this target appearance.` },
            { inlineData: { mimeType, data: base64Jpeg } },
          ],
        }],
        turnComplete: false,
      },
    }));
    return true;
  }

  async _handleMessage(msg) {
    if (msg.toolCall?.functionCalls?.length) {
      const responses = await Promise.all(msg.toolCall.functionCalls.map(async call => {
        let result = { ok: true };
        try {
          result = (await this.onToolCall(call.name, call.args || {})) || { ok: true };
        } catch (err) {
          result = { ok: false, error: err?.message || 'Tool execution failed' };
        }
        return {
          id: call.id,
          name: call.name,
          response: result,
        };
      }));
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({
          toolResponse: { functionResponses: responses },
        }));
      }
    }

    const serverContent = msg.serverContent;
    if (!serverContent) return;

    if (serverContent.interrupted) {
      this._stopPlayback();
    }

    if (serverContent.inputTranscription?.text) {
      this.onInputTranscript(serverContent.inputTranscription.text);
    }

    if (serverContent.outputTranscription?.text) {
      this.onOutputTranscript(serverContent.outputTranscription.text);
    }

    const parts = serverContent.modelTurn?.parts || [];
    for (const part of parts) {
      const inline = part.inlineData;
      if (inline?.data && inline.mimeType?.startsWith('audio/pcm')) {
        this._queuePcmAudio(inline.data);
      }
    }

    if (serverContent.turnComplete) {
      this.onTurnComplete();
    }
  }

  _queuePcmAudio(base64) {
    if (!this.playbackCtx) return;
    if (this.playbackCtx.state === 'suspended') {
      this.playbackCtx.resume().catch(() => {});
    }
    const pcm16 = base64ToInt16(base64);
    const buffer = this.playbackCtx.createBuffer(1, pcm16.length, 24000);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < pcm16.length; i++) {
      channel[i] = pcm16[i] / 32768;
    }
    const source = this.playbackCtx.createBufferSource();
    source.buffer = buffer;
    source.connect(this.playbackCtx.destination);
    const startAt = Math.max(this.playbackCtx.currentTime, this.nextPlayTime);
    this.nextPlayTime = startAt + buffer.duration;
    this.activeSources.add(source);
    if (this.activeSources.size === 1) this.onSpeakingChange(true);
    source.onended = () => {
      this.activeSources.delete(source);
      if (this.activeSources.size === 0) this.onSpeakingChange(false);
    };
    source.start(startAt);
  }

  _stopPlayback() {
    for (const source of this.activeSources) {
      try { source.stop(); } catch { /* Already ended. */ }
    }
    this.activeSources.clear();
    if (this.playbackCtx) {
      this.nextPlayTime = this.playbackCtx.currentTime;
    }
    this.onSpeakingChange(false);
  }

  setMuted(muted) {
    this.muted = Boolean(muted);
  }

  setCameraEnabled(enabled) {
    this.cameraEnabled = Boolean(enabled);
  }

  sendPrompt(text, { turnComplete = true } = {}) {
    if (!this.ready || this.ws?.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify({
      clientContent: {
        turns: [{ role: 'user', parts: [{ text }] }],
        turnComplete,
      },
    }));
    return true;
  }

  _stopMediaLoops() {
    if (this.frameTimer) {
      clearInterval(this.frameTimer);
      this.frameTimer = null;
    }
    if (this.captureNode) {
      this.captureNode.disconnect();
      this.captureNode = null;
    }
    if (this.captureSource) {
      this.captureSource.disconnect();
      this.captureSource = null;
    }
    if (this.captureCtx) {
      this.captureCtx.close().catch(() => {});
      this.captureCtx = null;
    }
    if (this.workletUrl) {
      URL.revokeObjectURL(this.workletUrl);
      this.workletUrl = null;
    }
    this._stopPlayback();
    if (this.playbackCtx) {
      this.playbackCtx.close().catch(() => {});
      this.playbackCtx = null;
    }
  }

  disconnect() {
    this.ready = false;
    this._stopMediaLoops();
    if (this.ws) {
      try {
        if (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING) {
          this.ws.close(1000, 'User ended session');
        }
      } catch { /* Ignore close errors. */ }
      this.ws = null;
    }
    this.onStatus('idle');
  }
}
