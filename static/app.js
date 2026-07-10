/*
 * app.js — Voxniac ONE live call UI
 *
 * WebSocket protocol (/ws/call):
 *   Client sends:
 *     - binary frames: PCM16 mono 16kHz mic audio
 *     - {"type":"start_call","llm":"<model_id>"}
 *     - {"type":"end_call"}
 *
 *   Server sends:
 *     - {"type":"call_started","opening":"<text>"}
 *     - {"type":"stt_partial","text":"..."}
 *     - {"type":"stt_final","text":"..."}
 *     - {"type":"agent_token","token":"..."}
 *     - {"type":"agent_done","text":"...","ttft":<seconds>}
 *     - binary frames: PCM16 mono 24kHz TTS audio (play gapless)
 *     - {"type":"barge_in"}: stop TTS playback immediately
 *     - {"type":"metrics","stt_final_s":<s>,"ttft_s":<s>,"ttfa_s":<s>,"e2e_s":<s>}
 *     - {"type":"error","stage":"...","kind":"...","detail":"..."}
 *
 * WebSocket protocol (/ws/interview) — Phase 3 onboarding interviewer:
 *   Client sends:
 *     - {"type":"user_msg","text":"..."}
 *     - {"type":"approve"} / {"type":"back"} / {"type":"reset"}
 *
 *   Server sends:
 *     - {"type":"interviewer_token","token":"..."}
 *     - {"type":"interviewer_done","text":"..."}
 *     - {"type":"state","state":"...","fields":{...},"messages":[...],"draft":{...}|null}
 *     - {"type":"profile_written"}
 *     - {"type":"error","stage":"interviewer","kind":"...","detail":"..."}
 *
 * REST (Phase 3 "Call a prospect"):
 *   GET  /call/status -> {"tunnel_up":bool,"public_host":str|null}
 *   POST /call {"to":"+1..."} -> {"sid","to","status"} | {"error":{...}}
 *
 * WebSocket protocol (/ws/monitor) — Phase 3.5 P1 live call monitor:
 *   Server sends (no client messages): {"channel","call_id","event":{...}}
 *   where `event` is any /ws/call-shaped event a live Twilio call saw
 *   (stt_partial/stt_final/agent_token/agent_done/metrics/error), plus a
 *   synthetic {"type":"call_ended"} when the call's session tears down.
 *
 * REST (Phase 3.5 P2 "Agent Setup" voice note + interviewer model):
 *   POST /interview/audio (raw body = recorded blob, Content-Type = its
 *     mime type) -> {"transcript":"..."} | {"error":{...}}; the
 *     interviewer's reply streams back over the existing /ws/interview
 *     socket as interviewer_token/interviewer_done, same as a typed message.
 *   POST /interviewer/model {"model_id":"..."} -> {"model_id":"..."} | {"error":{...}}
 *
 * REST (Phase 4 Etapa B "Profile Editor" — manual agent_profile.json edit):
 *   GET  /profile -> {"agent_opening","prompt_blocks":{personality,
 *     environment,tone,goal,guardrails},"truth_base":{...},"voice","llm_model"}
 *   POST /profile {same shape, truth_base may be a JSON-encoded string} ->
 *     same shape (the reloaded profile) | {"error":{...}}
 *
 * REST (Phase 4 Etapa C "Campaigns" — leads):
 *   POST /leads/import (multipart "file" field, Apollo-export-shaped CSV) ->
 *     {"imported","skipped"} | {"error":{...}}
 *   GET  /leads[?status=COLD|WARM|HOT] -> [lead, ...]
 *   PATCH /leads/{id} {any editable field} -> lead | {"error":{...}}
 *   POST /leads/{id}/generate_prompt -> lead (with customFirstMessage/
 *     customSystemPrompt filled in) | {"error":{...}}
 *   POST /leads/{id}/call {first_message?,system_prompt?} -> {"sid","to",
 *     "status","lead_id","demo_safe_mode"} | {"error":{...}}
 *   Live transcript for a lead call reuses the existing /ws/monitor
 *   connection (see connectMonitorWs) — same event shapes as the Phone
 *   Outreach panel, routed to the Campaigns lead detail panel instead while
 *   a lead call is the most recently placed one (single concurrent call,
 *   same assumption the rest of this file already makes for Phone Outreach).
 */

const MIC_SAMPLE_RATE = 16000;
const TTS_SAMPLE_RATE = 24000;

const state = {
  ws: null,
  wsReady: false,
  inCall: false,
  config: null,

  // Mic capture
  mediaStream: null,
  audioContext: null,
  sourceNode: null,
  processorNode: null,

  // TTS playback
  ttsAudioContext: null,
  scheduledSources: [],
  playheadTime: 0,

  // Transcript state
  currentSttPartial: null,
  currentAgentToken: null,
  turnMetrics: null,

  // Interviewer (/ws/interview) state
  interviewWs: null,
  interviewState: null,
  interviewBusy: false,

  // Phase 3.5 P2: voice-note recording state
  mediaRecorder: null,
  recordedChunks: [],
  isRecordingNote: false,

  // Phase 3.5 P1: /ws/monitor state (live Twilio call transcript)
  monitorWs: null,
  monitorCallId: null,
  monitorSttPartial: null,
  monitorAgentToken: null,
  currentProspectPhone: '',

  // Phase 4 Etapa C: Campaigns (leads) state
  leads: {
    all: [],
    selectedId: null,
    // Set the moment "Call" is clicked for a lead; routes the next /ws/
    // monitor events into this lead's transcript panel instead of the
    // Phone Outreach one (single concurrent call assumption, same as
    // currentProspectPhone above).
    activeCallLeadId: null,
    monitorCallId: null,
    monitorSttPartial: null,
    monitorAgentToken: null,
  },
};

// ── DOM refs ─────────────────────────────────────────────────────────────
const el = {
  wsDot: document.getElementById('wsDot'),
  wsStatusTxt: document.getElementById('wsStatusTxt'),
  llmSelect: document.getElementById('llmSelect'),
  agentOpening: document.getElementById('agentOpening'),
  callBtn: document.getElementById('callBtn'),
  callHint: document.getElementById('callHint'),
  transcriptMessages: document.getElementById('transcriptMessages'),
  statusArea: document.getElementById('statusArea'),
  latStt: document.getElementById('latStt'),
  latTtft: document.getElementById('latTtft'),
  latTtfa: document.getElementById('latTtfa'),
  latE2e: document.getElementById('latE2e'),
  historyBody: document.getElementById('historyBody'),

  // Call a prospect
  tunnelDot: document.getElementById('tunnelDot'),
  tunnelStatusTxt: document.getElementById('tunnelStatusTxt'),
  prospectPhone: document.getElementById('prospectPhone'),
  prospectCallBtn: document.getElementById('prospectCallBtn'),
  prospectCallResult: document.getElementById('prospectCallResult'),
  prospectCallStatus: document.getElementById('prospectCallStatus'),
  prospectTranscriptMessages: document.getElementById('prospectTranscriptMessages'),

  // Agent Setup (interviewer)
  interviewStateBadge: document.getElementById('interviewStateBadge'),
  interviewMessages: document.getElementById('interviewMessages'),
  interviewInput: document.getElementById('interviewInput'),
  interviewSendBtn: document.getElementById('interviewSendBtn'),
  interviewApproveBtn: document.getElementById('interviewApproveBtn'),
  interviewBackBtn: document.getElementById('interviewBackBtn'),
  interviewResetBtn: document.getElementById('interviewResetBtn'),
  interviewStatus: document.getElementById('interviewStatus'),
  interviewMicBtn: document.getElementById('interviewMicBtn'),
  interviewerModelSelect: document.getElementById('interviewerModelSelect'),
  profileReloadBtn: document.getElementById('profileReloadBtn'),
  profileReloadStatus: document.getElementById('profileReloadStatus'),

  // Profile Editor (Etapa B, GET/POST /profile)
  profileAgentOpening: document.getElementById('profileAgentOpening'),
  profileVoice: document.getElementById('profileVoice'),
  profileLlmModel: document.getElementById('profileLlmModel'),
  profileBlockPersonality: document.getElementById('profileBlockPersonality'),
  profileBlockEnvironment: document.getElementById('profileBlockEnvironment'),
  profileBlockTone: document.getElementById('profileBlockTone'),
  profileBlockGoal: document.getElementById('profileBlockGoal'),
  profileBlockGuardrails: document.getElementById('profileBlockGuardrails'),
  profileTruthBase: document.getElementById('profileTruthBase'),
  profileSaveBtn: document.getElementById('profileSaveBtn'),
  profileEditorStatus: document.getElementById('profileEditorStatus'),

  // Campaigns (Phase 4 Etapa C)
  leadsCountHot: document.getElementById('leadsCountHot'),
  leadsCountWarm: document.getElementById('leadsCountWarm'),
  leadsCountCold: document.getElementById('leadsCountCold'),
  demoSafeBadge: document.getElementById('demoSafeBadge'),
  demoSafeBadgeText: document.getElementById('demoSafeBadgeText'),
  leadsImportInput: document.getElementById('leadsImportInput'),
  leadsImportStatus: document.getElementById('leadsImportStatus'),
  leadsTableBody: document.getElementById('leadsTableBody'),
  leadsTableCount: document.getElementById('leadsTableCount'),
  leadDetailEmpty: document.getElementById('leadDetailEmpty'),
  leadDetailBody: document.getElementById('leadDetailBody'),
  leadDetailName: document.getElementById('leadDetailName'),
  leadDetailSub: document.getElementById('leadDetailSub'),
  leadDetailStatus: document.getElementById('leadDetailStatus'),
  leadDetailBallena: document.getElementById('leadDetailBallena'),
  leadDetailPainPoints: document.getElementById('leadDetailPainPoints'),
  leadDetailFirstMessage: document.getElementById('leadDetailFirstMessage'),
  leadDetailSystemPrompt: document.getElementById('leadDetailSystemPrompt'),
  leadGeneratePromptBtn: document.getElementById('leadGeneratePromptBtn'),
  leadSaveBtn: document.getElementById('leadSaveBtn'),
  leadCallBtn: document.getElementById('leadCallBtn'),
  leadDetailStatusMsg: document.getElementById('leadDetailStatusMsg'),
  leadClassification: document.getElementById('leadClassification'),
  leadClassificationText: document.getElementById('leadClassificationText'),
  leadTranscriptMessages: document.getElementById('leadTranscriptMessages'),
};

// ── Status area (fail-loud, no alert) ───────────────────────────────────
function setStatus(msg, level = 'info') {
  el.statusArea.textContent = msg;
  el.statusArea.className = 'status-area status-' + level;
}

function clearStatusSoon(ms = 4000) {
  setTimeout(() => {
    if (el.statusArea.classList.contains('status-error')) return;
    setStatus('', 'info');
  }, ms);
}

// ── Config: populate LLM selector ─────────────────────────────────────────
function populateSelect(selectEl, options) {
  selectEl.innerHTML = '';
  let firstAvailable = null;
  for (const opt of options) {
    const o = document.createElement('option');
    o.value = opt.key;
    o.textContent = opt.available
      ? opt.label
      : `${opt.label} (unavailable: ${opt.reason || 'no key'})`;
    o.disabled = !opt.available;
    if (opt.available && firstAvailable === null) {
      firstAvailable = opt.key;
    }
    selectEl.appendChild(o);
  }
  if (firstAvailable !== null) {
    selectEl.value = firstAvailable;
  }
}

async function loadConfig() {
  try {
    const resp = await fetch('/config');
    if (!resp.ok) {
      throw new Error(`GET /config HTTP ${resp.status}`);
    }
    const cfg = await resp.json();
    state.config = cfg;

    // Populate LLM selector only
    if (cfg.llm) {
      populateSelect(el.llmSelect, cfg.llm);
    }

    // Show agent opening preview (server sends it under profile.agent_opening)
    const opening = (cfg.profile && cfg.profile.agent_opening) || cfg.agent_opening;
    if (opening) {
      el.agentOpening.textContent = opening;
    }

    // Phase 4 Etapa B: Profile Editor's LLM model select reuses the exact
    // same options as the Voice Engine's own llmSelect.
    if (cfg.llm && el.profileLlmModel) {
      populateSelect(el.profileLlmModel, cfg.llm);
    }

    // Phase 3.5 P2: interviewer model select (120B quality / 20B fast)
    if (cfg.interviewer && el.interviewerModelSelect) {
      el.interviewerModelSelect.innerHTML = '';
      for (const choice of cfg.interviewer.choices || []) {
        const o = document.createElement('option');
        o.value = choice.key;
        o.textContent = choice.label;
        el.interviewerModelSelect.appendChild(o);
      }
      if (cfg.interviewer.model_id) {
        el.interviewerModelSelect.value = cfg.interviewer.model_id;
      }
    }

    // Phase 4 Etapa C: DEMO SAFE MODE badge (Campaigns toolbar)
    if (typeof cfg.demo_safe_mode === 'boolean') {
      applyDemoSafeMode(cfg.demo_safe_mode);
    }
  } catch (err) {
    setStatus(`Error loading /config: ${err.message}`, 'error');
  }
}

// ── Interviewer model switch (Phase 3.5 P2) ──────────────────────────────
if (el.interviewerModelSelect) {
  el.interviewerModelSelect.addEventListener('change', async () => {
    const model_id = el.interviewerModelSelect.value;
    try {
      const resp = await fetch('/interviewer/model', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model_id }),
      });
      const data = await resp.json();
      if (!resp.ok || data.error) {
        setInterviewStatus(
          `Could not switch model: ${data.error ? data.error.detail : resp.status}`, 'error',
        );
      } else {
        setInterviewStatus(`Interviewer model set to ${model_id}.`, 'ok');
      }
    } catch (err) {
      setInterviewStatus(`Request failed: ${err.message}`, 'error');
    }
  });
}

// ── WebSocket connection ──────────────────────────────────────────────────
function connectWs() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws/call`);
  ws.binaryType = 'arraybuffer';
  state.ws = ws;

  ws.onopen = () => {
    state.wsReady = true;
    updateStatusPill();
    el.callBtn.disabled = false;
    el.callHint.textContent = 'Press to start a call';
    setStatus('Connected to server', 'ok');
    clearStatusSoon(2000);
  };

  ws.onclose = () => {
    state.wsReady = false;
    state.inCall = false;
    updateStatusPill();
    el.callBtn.disabled = true;
    el.callBtn.classList.remove('active');
    el.callBtn.textContent = 'Start Call';
    el.callHint.textContent = 'Disconnected. Reconnecting…';
    setStatus('WebSocket disconnected. Retrying in 2s…', 'error');
    setTimeout(connectWs, 2000);
  };

  ws.onerror = () => {
    setStatus('WebSocket error. Check that the server is running.', 'error');
  };

  ws.onmessage = (event) => {
    if (typeof event.data === 'string') {
      // JSON message
      const msg = JSON.parse(event.data);
      handleJsonMessage(msg);
    } else {
      // Binary TTS audio (ArrayBuffer)
      handleTtsAudio(event.data);
    }
  };
}

function updateStatusPill() {
  if (!state.wsReady) {
    el.wsDot.className = 'dot';
    el.wsStatusTxt.textContent = 'Disconnected';
  } else if (state.inCall) {
    el.wsDot.className = 'dot in-call';
    el.wsStatusTxt.textContent = 'In call — listening';
  } else {
    el.wsDot.className = 'dot connected';
    el.wsStatusTxt.textContent = 'Connected';
  }
}

function setAgentSpeaking(speaking) {
  if (speaking) {
    el.wsDot.className = 'dot speaking';
    el.wsStatusTxt.textContent = 'In call — speaking';
  } else if (state.inCall) {
    el.wsDot.className = 'dot in-call';
    el.wsStatusTxt.textContent = 'In call — listening';
  }
}

// ── JSON message handling ─────────────────────────────────────────────────
function handleJsonMessage(msg) {
  switch (msg.type) {
    case 'call_started': {
      // Agent speaks first with opening text
      addTranscriptBubble(el.transcriptMessages, 'agent', msg.opening, false);
      break;
    }
    case 'stt_partial': {
      // User speech partial (italic, muted)
      state.currentSttPartial = msg.text;
      if (state.currentSttPartial) {
        addOrUpdateTranscriptBubble(el.transcriptMessages, 'user', state.currentSttPartial, true);
      }
      break;
    }
    case 'stt_final': {
      // User speech finalized (solid)
      state.currentSttPartial = null;
      addOrUpdateTranscriptBubble(el.transcriptMessages, 'user', msg.text, false);
      break;
    }
    case 'agent_token': {
      // Append token to agent's streaming response
      state.currentAgentToken = (state.currentAgentToken || '') + (msg.token || '');
      addOrUpdateTranscriptBubble(el.transcriptMessages, 'agent', state.currentAgentToken, true);
      setAgentSpeaking(true);
      break;
    }
    case 'agent_done': {
      // Agent turn complete; finalize the bubble
      state.currentAgentToken = null;
      addOrUpdateTranscriptBubble(el.transcriptMessages, 'agent', msg.text || '', false);
      setAgentSpeaking(false);
      break;
    }
    case 'barge_in': {
      // User interrupted; stop all TTS playback immediately
      stopAllTtsPlayback();
      state.currentAgentToken = null;
      setAgentSpeaking(false);
      break;
    }
    case 'metrics': {
      // Update latency HUD and add history row
      state.turnMetrics = msg;
      el.latStt.textContent = fmtSeconds(msg.stt_final_s);
      el.latTtft.textContent = fmtSeconds(msg.ttft_s);
      el.latTtfa.textContent = fmtSeconds(msg.ttfa_s);
      el.latE2e.textContent = fmtSeconds(msg.e2e_s);
      addHistoryRow(msg);
      setStatus('Turn complete.', 'ok');
      clearStatusSoon(2000);
      break;
    }
    case 'error': {
      // Fail loud: display error with stage + kind + detail
      setStatus(`[${msg.stage}] error (${msg.kind}): ${msg.detail}`, 'error');
      break;
    }
    default: {
      // Unknown message type, silently ignore
      break;
    }
  }
}

// ── Transcript bubble management ──────────────────────────────────────────
// container-agnostic: used for both the Live Call transcript
// (el.transcriptMessages) and the "Call a prospect" live monitor transcript
// (el.prospectTranscriptMessages, Phase 3.5 P1) — same bubble markup/CSS.
function addTranscriptBubble(container, speaker, text, isPartial = false) {
  const bubble = document.createElement('div');
  bubble.className = `transcript-bubble ${speaker}${isPartial ? ' partial' : ''}`;

  const speakerEl = document.createElement('div');
  speakerEl.className = 'transcript-speaker';
  speakerEl.textContent = speaker === 'user' ? 'You' : 'Voxniac';

  const textEl = document.createElement('div');
  textEl.className = 'transcript-text';
  textEl.textContent = text || '';

  bubble.appendChild(speakerEl);
  bubble.appendChild(textEl);
  container.appendChild(bubble);

  // Auto-scroll to latest
  if (container.parentElement) {
    container.parentElement.scrollTop = container.parentElement.scrollHeight;
  }

  return bubble;
}

function addOrUpdateTranscriptBubble(container, speaker, text, isPartial = false) {
  // Find the last bubble of this speaker
  const bubbles = container.querySelectorAll(`.transcript-bubble.${speaker}`);
  const lastBubble = bubbles.length > 0 ? bubbles[bubbles.length - 1] : null;

  // Only update an existing bubble if it is still partial (in progress).
  // A finalized bubble is history — new content always gets a new bubble,
  // otherwise a second turn would overwrite the previous message.
  if (lastBubble && lastBubble.classList.contains('partial')) {
    const textEl = lastBubble.querySelector('.transcript-text');
    if (textEl) {
      textEl.textContent = text || '';
    }
    if (!isPartial) {
      lastBubble.classList.remove('partial');
    }
  } else {
    // Create a new bubble (partial or final)
    addTranscriptBubble(container, speaker, text, isPartial);
  }

  // Auto-scroll to latest
  if (container.parentElement) {
    container.parentElement.scrollTop = container.parentElement.scrollHeight;
  }
}

// ── TTS audio playback (gapless, streaming) ──────────────────────────────
function handleTtsAudio(arrayBuffer) {
  if (!state.ttsAudioContext) {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    state.ttsAudioContext = new AudioCtx();
    state.playheadTime = state.ttsAudioContext.currentTime + 0.05;
  }

  // Decode PCM16 mono -> Float32
  const int16Array = new Int16Array(arrayBuffer);
  const float32Array = new Float32Array(int16Array.length);
  for (let i = 0; i < int16Array.length; i++) {
    float32Array[i] = int16Array[i] / 32768;
  }

  // Create audio buffer at TTS sample rate
  const audioBuffer = state.ttsAudioContext.createBuffer(1, float32Array.length, TTS_SAMPLE_RATE);
  audioBuffer.getChannelData(0).set(float32Array);

  // Schedule playback at playheadTime (gapless)
  const source = state.ttsAudioContext.createBufferSource();
  source.buffer = audioBuffer;
  source.connect(state.ttsAudioContext.destination);

  const scheduleTime = Math.max(state.ttsAudioContext.currentTime + 0.05, state.playheadTime);
  source.start(scheduleTime);

  state.scheduledSources.push(source);
  state.playheadTime = scheduleTime + audioBuffer.duration;

  // Mark agent as speaking while TTS plays
  setAgentSpeaking(true);

  // Clean up when playback finishes
  source.onended = () => {
    const idx = state.scheduledSources.indexOf(source);
    if (idx !== -1) {
      state.scheduledSources.splice(idx, 1);
    }
    if (state.scheduledSources.length === 0) {
      setAgentSpeaking(false);
    }
  };
}

function stopAllTtsPlayback() {
  // Hard cut: stop all scheduled sources and reset
  state.scheduledSources.forEach((src) => {
    try {
      src.stop();
    } catch (e) {
      // Already stopped or context closed; silently ignore
    }
  });
  state.scheduledSources = [];
  state.playheadTime = 0;

  // Close and recreate TTS context for fresh start
  if (state.ttsAudioContext) {
    state.ttsAudioContext.close().catch(() => {});
    state.ttsAudioContext = null;
  }

  setAgentSpeaking(false);
}

// ── Mic capture (AudioWorklet preferred, ScriptProcessor fallback) ───────
async function startCall() {
  if (!state.wsReady) {
    setStatus('Not connected to server', 'error');
    return;
  }

  try {
    state.mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: false,
      },
    });
  } catch (err) {
    setStatus(`Microphone access denied: ${err.message}`, 'error');
    return;
  }

  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  state.audioContext = new AudioCtx();
  state.sourceNode = state.audioContext.createMediaStreamSource(state.mediaStream);

  const inputSampleRate = state.audioContext.sampleRate;

  // Try AudioWorklet first; fall back to ScriptProcessor if not available
  let processor = null;

  if (state.audioContext.audioWorklet) {
    try {
      // Inline AudioWorklet module
      const workletCode = `
        class MicProcessor extends AudioWorkletProcessor {
          constructor() {
            super();
            this.port.onmessage = (e) => {
              // Optional: handle messages from main thread
            };
          }
          process(inputs, outputs, parameters) {
            const input = inputs[0];
            if (input && input.length > 0) {
              const channelData = input[0];
              this.port.postMessage(new Float32Array(channelData));
            }
            return true;
          }
        }
        registerProcessor('mic-processor', MicProcessor);
      `;
      const blob = new Blob([workletCode], { type: 'application/javascript' });
      const url = URL.createObjectURL(blob);
      await state.audioContext.audioWorklet.addModule(url);
      processor = new AudioWorkletNode(state.audioContext, 'mic-processor');
    } catch (err) {
      // AudioWorklet failed; fall back to ScriptProcessor
      console.warn('AudioWorklet failed, using ScriptProcessor:', err);
      processor = null;
    }
  }

  if (!processor) {
    // ScriptProcessor fallback (deprecated but widely supported)
    const bufferSize = 4096;
    processor = state.audioContext.createScriptProcessor(bufferSize, 1, 1);
  }

  state.processorNode = processor;

  // Handler for audio data (works for both AudioWorklet and ScriptProcessor).
  // AudioWorklet delivers tiny 128-sample frames (~375 messages/s at 48 kHz),
  // which would flood the WebSocket — so we accumulate into ~85 ms batches
  // (4096 samples at input rate) before downsampling and sending one frame.
  const SEND_BATCH_SAMPLES = 4096;
  let micAccum = new Float32Array(0);

  const handleAudioData = (inputFloat32) => {
    if (!state.inCall || !state.wsReady) return;

    // Accumulate until we have a full batch
    const merged = new Float32Array(micAccum.length + inputFloat32.length);
    merged.set(micAccum, 0);
    merged.set(inputFloat32, micAccum.length);
    micAccum = merged;
    if (micAccum.length < SEND_BATCH_SAMPLES) return;

    const batch = micAccum;
    micAccum = new Float32Array(0);

    // Downsample to MIC_SAMPLE_RATE if needed
    let downsampled = batch;
    if (inputSampleRate !== MIC_SAMPLE_RATE) {
      downsampled = downsampleBuffer(batch, inputSampleRate, MIC_SAMPLE_RATE);
    }

    // Convert Float32 -> Int16 PCM
    const pcm16 = floatTo16BitPcm(downsampled);

    // Send binary frame to server
    try {
      state.ws.send(pcm16);
    } catch (err) {
      setStatus(`Error sending audio: ${err.message}`, 'error');
      endCall();
    }
  };

  if (processor instanceof AudioWorkletNode) {
    // AudioWorklet: listen to messages
    processor.port.onmessage = (e) => {
      handleAudioData(e.data);
    };
  } else {
    // ScriptProcessor: use onaudioprocess
    processor.onaudioprocess = (e) => {
      const input = e.inputBuffer.getChannelData(0);
      handleAudioData(input);
    };
  }

  state.sourceNode.connect(state.processorNode);
  state.processorNode.connect(state.audioContext.destination);

  // Clear transcript from previous call
  el.transcriptMessages.innerHTML = '';
  state.currentSttPartial = null;
  state.currentAgentToken = null;

  // Reset TTS playhead
  state.playheadTime = 0;
  stopAllTtsPlayback();

  // Send start_call message with selected LLM
  const llmModel = el.llmSelect.value;
  state.ws.send(JSON.stringify({
    type: 'start_call',
    llm: llmModel,
  }));

  // Update UI
  state.inCall = true;
  el.callBtn.classList.add('active');
  el.callBtn.textContent = 'End Call';
  el.callHint.textContent = 'Call active — speak now';
  updateStatusPill();
  setStatus('Call started. Listening…', 'ok');
  clearStatusSoon(2000);
}

function endCall() {
  if (!state.inCall) return;

  state.inCall = false;
  el.callBtn.classList.remove('active');
  el.callBtn.textContent = 'Start Call';
  el.callHint.textContent = 'Press to start a call';
  updateStatusPill();

  // Stop mic capture
  if (state.processorNode) {
    state.processorNode.disconnect();
    state.processorNode.onaudioprocess = null;
    state.processorNode = null;
  }
  if (state.sourceNode) {
    state.sourceNode.disconnect();
    state.sourceNode = null;
  }
  if (state.mediaStream) {
    state.mediaStream.getTracks().forEach((t) => t.stop());
    state.mediaStream = null;
  }
  if (state.audioContext) {
    state.audioContext.close().catch(() => {});
    state.audioContext = null;
  }

  // Stop TTS playback
  stopAllTtsPlayback();

  // Send end_call to server (keep WS open for next call)
  if (state.wsReady) {
    try {
      state.ws.send(JSON.stringify({ type: 'end_call' }));
    } catch (err) {
      // Ignore; connection may be closing
    }
  }

  setStatus('Call ended.', 'info');
  clearStatusSoon(2000);
}

el.callBtn.addEventListener('click', () => {
  if (!state.wsReady) return;
  if (state.inCall) {
    endCall();
  } else {
    startCall();
  }
});

// ── Audio utilities ──────────────────────────────────────────────────────
function floatTo16BitPcm(float32Array) {
  const buffer = new ArrayBuffer(float32Array.length * 2);
  const view = new DataView(buffer);
  for (let i = 0; i < float32Array.length; i++) {
    let s = Math.max(-1, Math.min(1, float32Array[i]));
    s = s < 0 ? s * 0x8000 : s * 0x7fff;
    view.setInt16(i * 2, s, true);
  }
  return buffer;
}

function downsampleBuffer(buffer, inputRate, outputRate) {
  if (outputRate === inputRate) return buffer;
  const ratio = inputRate / outputRate;
  const newLength = Math.round(buffer.length / ratio);
  const result = new Float32Array(newLength);
  let offsetResult = 0;
  let offsetBuffer = 0;
  while (offsetResult < result.length) {
    const nextOffsetBuffer = Math.round((offsetResult + 1) * ratio);
    let accum = 0;
    let count = 0;
    for (let i = offsetBuffer; i < nextOffsetBuffer && i < buffer.length; i++) {
      accum += buffer[i];
      count++;
    }
    result[offsetResult] = count > 0 ? accum / count : 0;
    offsetResult++;
    offsetBuffer = nextOffsetBuffer;
  }
  return result;
}

// ── History table ───────────────────────────────────────────────────────
function fmtSeconds(s) {
  if (s === null || s === undefined || Number.isNaN(s)) return '—';
  return `${s.toFixed(2)}s`;
}

function addHistoryRow(metrics) {
  const tr = document.createElement('tr');
  const llmLabel = el.llmSelect.options[el.llmSelect.selectedIndex]?.text || el.llmSelect.value;
  tr.innerHTML = `
    <td>${el.historyBody.children.length + 1}</td>
    <td>${llmLabel}</td>
    <td>${fmtSeconds(metrics.stt_final_s)}</td>
    <td>${fmtSeconds(metrics.ttft_s)}</td>
    <td>${fmtSeconds(metrics.ttfa_s)}</td>
    <td>${fmtSeconds(metrics.e2e_s)}</td>
  `;
  el.historyBody.prepend(tr);
}

// ── Call a prospect (Phase 3 §A) ──────────────────────────────────────────
function setProspectResult(msg, level = 'info') {
  el.prospectCallResult.textContent = msg;
  el.prospectCallResult.className = 'prospect-call-result' + (level !== 'info' ? ' ' + level : '');
}

async function refreshTunnelStatus() {
  try {
    const resp = await fetch('/call/status');
    if (!resp.ok) throw new Error(`GET /call/status HTTP ${resp.status}`);
    const data = await resp.json();
    if (data.tunnel_up) {
      el.tunnelDot.className = 'dot connected';
      el.tunnelStatusTxt.textContent = data.public_host ? `Tunnel up (${data.public_host})` : 'Tunnel up';
      el.prospectCallBtn.disabled = false;
    } else {
      el.tunnelDot.className = 'dot';
      el.tunnelStatusTxt.textContent = 'Tunnel down';
      el.prospectCallBtn.disabled = true;
    }
  } catch (err) {
    el.tunnelDot.className = 'dot';
    el.tunnelStatusTxt.textContent = 'Status unavailable';
    el.prospectCallBtn.disabled = true;
  }
}

async function callProspect() {
  const to = el.prospectPhone.value.trim();
  if (!to) {
    setProspectResult('Enter a phone number in E.164 format, e.g. +13075550100.', 'error');
    return;
  }
  state.currentProspectPhone = to;
  el.prospectCallBtn.disabled = true;
  setProspectResult('Placing call…', 'info');
  try {
    const resp = await fetch('/call', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ to }),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) {
      const detail = data.error ? `[${data.error.stage}] ${data.error.kind}: ${data.error.detail}` : `HTTP ${resp.status}`;
      setProspectResult(detail, 'error');
    } else {
      setProspectResult(`Call placed. SID: ${data.sid}`, 'ok');
    }
  } catch (err) {
    setProspectResult(`Request failed: ${err.message}`, 'error');
  } finally {
    el.prospectCallBtn.disabled = false;
  }
}

el.prospectCallBtn.addEventListener('click', callProspect);

// ── Live call monitor (Phase 3.5 P1, WS /ws/monitor) ──────────────────────
// Fills the "Call a prospect" card's transcript live from any Twilio call's
// events, fanned out by event_bus.publish() server-side — same protocol
// shapes as /ws/call's own events (stt_partial/stt_final/agent_token/
// agent_done/metrics/error), plus a synthetic "call_ended" when the call's
// WebSocket session tears down.
function setProspectCallStatus(msg, level = 'info') {
  el.prospectCallStatus.textContent = msg;
  el.prospectCallStatus.className = 'prospect-call-status' + (level !== 'info' ? ' ' + level : '');
}

function handleMonitorEvent(callId, evt) {
  // Control Room: flag the Phone Outreach sidebar item (layer '3' since the
  // Etapa A reorder — Voice Engine/Agent Setup/Phone Outreach/Campaigns) if
  // the user isn't looking at it already, instead of yanking them away from
  // whatever they're doing (per PLAN_UI_CONTROL_ROOM.md's layout note).
  // `activeLayer` is declared later in this file but always initialized
  // before any WS message can arrive (see "Control Room layer navigation").
  if (typeof activeLayer !== 'undefined' && activeLayer !== '3') {
    const dot = document.querySelector('.pipeline-item[data-layer="3"] .pipeline-dot');
    if (dot) dot.classList.add('active-alert');
  }

  if (state.monitorCallId !== callId) {
    // A new call started (or the first one this page has seen) — reset the
    // live transcript so calls never bleed into each other.
    state.monitorCallId = callId;
    el.prospectTranscriptMessages.innerHTML = '';
    state.monitorSttPartial = null;
    state.monitorAgentToken = null;
    const label = state.currentProspectPhone || 'prospect';
    setProspectCallStatus(`📞 In call with ${label}…`, 'ok');
  }

  switch (evt.type) {
    case 'stt_partial': {
      state.monitorSttPartial = evt.text;
      if (state.monitorSttPartial) {
        addOrUpdateTranscriptBubble(el.prospectTranscriptMessages, 'user', state.monitorSttPartial, true);
      }
      break;
    }
    case 'stt_final': {
      state.monitorSttPartial = null;
      addOrUpdateTranscriptBubble(el.prospectTranscriptMessages, 'user', evt.text || '', false);
      break;
    }
    case 'agent_token': {
      state.monitorAgentToken = (state.monitorAgentToken || '') + (evt.token || '');
      addOrUpdateTranscriptBubble(el.prospectTranscriptMessages, 'agent', state.monitorAgentToken, true);
      break;
    }
    case 'agent_done': {
      state.monitorAgentToken = null;
      addOrUpdateTranscriptBubble(el.prospectTranscriptMessages, 'agent', evt.text || '', false);
      break;
    }
    case 'call_ended': {
      const label = state.currentProspectPhone || 'prospect';
      setProspectCallStatus(`📞 Call with ${label} ended (colgó).`, 'info');
      break;
    }
    case 'error': {
      setProspectCallStatus(`[${evt.stage}] ${evt.kind}: ${evt.detail}`, 'error');
      break;
    }
    default:
      break;
  }
}

function connectMonitorWs() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws/monitor`);
  state.monitorWs = ws;

  ws.onclose = () => {
    setTimeout(connectMonitorWs, 2000);
  };
  ws.onerror = () => {};
  ws.onmessage = (event) => {
    let envelope;
    try {
      envelope = JSON.parse(event.data);
    } catch (err) {
      return;
    }
    if (envelope.channel !== 'twilio') return;
    handleMonitorEvent(envelope.call_id, envelope.event || {});
    handleLeadMonitorEvent(envelope.call_id, envelope.event || {});
  };
}

// ── Agent Setup / onboarding interviewer (Phase 3 §B, WS /ws/interview) ───
function setInterviewStatus(msg, level = 'info') {
  el.interviewStatus.textContent = msg;
  el.interviewStatus.className = 'interview-status' + (level !== 'info' ? ' status-' + level : '');
}

function setInterviewBusy(busy) {
  state.interviewBusy = busy;
  const canType = !busy && state.interviewState === 'INTERVIEWING';
  el.interviewInput.disabled = !canType;
  el.interviewSendBtn.disabled = !canType;
  if (el.interviewMicBtn) {
    el.interviewMicBtn.disabled = !canType;
  }
}

function addInterviewBubble(speaker, text, partial = false) {
  const bubble = document.createElement('div');
  bubble.className = `interview-bubble ${speaker}${partial ? ' partial' : ''}`;
  bubble.textContent = text || '';
  el.interviewMessages.appendChild(bubble);
  el.interviewMessages.scrollTop = el.interviewMessages.scrollHeight;
  return bubble;
}

function renderInterviewTranscript(messages) {
  el.interviewMessages.innerHTML = '';
  for (const m of messages || []) {
    addInterviewBubble(m.role === 'user' ? 'user' : 'assistant', m.content, false);
  }
}

function renderPlanPreview(draft) {
  const existing = el.interviewMessages.querySelector('.interview-plan-preview');
  if (existing) existing.remove();
  if (!draft) return;
  const pre = document.createElement('div');
  pre.className = 'interview-plan-preview';
  const objections = (draft.truth_base && draft.truth_base.objections) || [];
  const objText = objections.map((o, i) => `  ${i + 1}. ${o.objection}\n     -> ${o.response}`).join('\n');

  // Phase 3.5 P3: the draft's system prompt now comes as prompt_blocks
  // (personality/environment/tone/goal/guardrails). Falls back to a legacy
  // flat "system_prompt" string if an old-shaped draft is ever resumed.
  const blocks = draft.prompt_blocks || null;
  let promptSection;
  if (blocks) {
    promptSection = ['personality', 'environment', 'tone', 'goal', 'guardrails']
      .filter((key) => blocks[key])
      .map((key) => `## ${key.toUpperCase()}\n${blocks[key]}`)
      .join('\n\n');
  } else {
    promptSection = draft.system_prompt || '—';
  }

  pre.textContent =
    `AGENT OPENING\n${draft.agent_opening}\n\n` +
    `PROMPT BLOCKS\n${promptSection}\n\n` +
    `TRUTH BASE\n` +
    `  business_name: ${draft.truth_base?.business_name || '—'}\n` +
    `  services_and_prices: ${draft.truth_base?.services_and_prices || '—'}\n` +
    `  dream_outcome: ${draft.truth_base?.dream_outcome || '—'}\n` +
    `  icp: ${draft.truth_base?.icp || '—'}\n` +
    `  icp_pain_points: ${draft.truth_base?.icp_pain_points || '—'}\n` +
    `  opening_line_preferences: ${draft.truth_base?.opening_line_preferences || '—'}\n` +
    `  escalation_rule: ${draft.truth_base?.escalation_rule || '—'}\n` +
    `  objections:\n${objText}`;
  el.interviewMessages.appendChild(pre);
  el.interviewMessages.scrollTop = el.interviewMessages.scrollHeight;
}

function applyInterviewState(payload) {
  state.interviewState = payload.state;
  el.interviewStateBadge.textContent = payload.state;
  el.interviewStateBadge.className = 'state-badge ' + payload.state;

  el.interviewApproveBtn.disabled = payload.state !== 'REVIEWING_PLAN';
  el.interviewBackBtn.disabled = payload.state !== 'REVIEWING_PLAN';

  renderPlanPreview(payload.draft);
  setInterviewBusy(state.interviewBusy);
}

function connectInterviewWs() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws/interview`);
  state.interviewWs = ws;
  let sawInitialState = false;

  ws.onopen = () => {
    setInterviewStatus('Connected.', 'ok');
  };

  ws.onclose = () => {
    setInterviewStatus('Disconnected. Reconnecting…', 'error');
    setTimeout(connectInterviewWs, 2000);
  };

  ws.onerror = () => {
    setInterviewStatus('WebSocket error.', 'error');
  };

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    switch (msg.type) {
      case 'state': {
        // The very first "state" event on connect carries the full resumed
        // transcript; later ones (after each turn) just confirm state/draft —
        // the transcript itself is already rendered incrementally.
        if (!sawInitialState) {
          sawInitialState = true;
          renderInterviewTranscript(msg.messages);
        }
        applyInterviewState(msg);
        break;
      }
      case 'interviewer_token': {
        setInterviewBusy(true);
        const bubbles = el.interviewMessages.querySelectorAll('.interview-bubble.assistant');
        const last = bubbles.length ? bubbles[bubbles.length - 1] : null;
        if (last && last.classList.contains('partial')) {
          last.textContent = (last.textContent || '') + (msg.token || '');
        } else {
          addInterviewBubble('assistant', msg.token || '', true);
        }
        el.interviewMessages.scrollTop = el.interviewMessages.scrollHeight;
        break;
      }
      case 'interviewer_done': {
        const bubbles = el.interviewMessages.querySelectorAll('.interview-bubble.assistant.partial');
        const last = bubbles.length ? bubbles[bubbles.length - 1] : null;
        if (last) {
          last.textContent = msg.text || '';
          last.classList.remove('partial');
        } else {
          addInterviewBubble('assistant', msg.text || '', false);
        }
        setInterviewBusy(false);
        break;
      }
      case 'profile_written': {
        setInterviewStatus('Approved — agent_profile.json updated and hot-reloaded.', 'ok');
        loadConfig(); // re-fetch /config so the opening-line preview reflects the new profile
        loadProfileEditor(); // Etapa B: refresh the Profile Editor's fields with the approved plan
        break;
      }
      case 'error': {
        setInterviewStatus(`[${msg.stage}] ${msg.kind}: ${msg.detail}`, 'error');
        setInterviewBusy(false);
        break;
      }
      default:
        break;
    }
  };
}

function sendInterviewMessage() {
  const text = el.interviewInput.value.trim();
  if (!text || !state.interviewWs || state.interviewWs.readyState !== WebSocket.OPEN) return;
  addInterviewBubble('user', text, false);
  state.interviewWs.send(JSON.stringify({ type: 'user_msg', text }));
  el.interviewInput.value = '';
  setInterviewBusy(true);
}

el.interviewSendBtn.addEventListener('click', sendInterviewMessage);
el.interviewInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') sendInterviewMessage();
});
el.interviewApproveBtn.addEventListener('click', () => {
  if (!state.interviewWs) return;
  state.interviewWs.send(JSON.stringify({ type: 'approve' }));
});
el.interviewBackBtn.addEventListener('click', () => {
  if (!state.interviewWs) return;
  state.interviewWs.send(JSON.stringify({ type: 'back' }));
});
el.interviewResetBtn.addEventListener('click', () => {
  if (!state.interviewWs) return;
  if (!confirm('Reset the whole interview? All answers will be lost.')) return;
  state.interviewWs.send(JSON.stringify({ type: 'reset' }));
});

// ── Reload profile (Phase 3.5 P3, POST /profile/reload) ───────────────────
// For a hand edit to agent_profile.json made directly with a text editor,
// bypassing the interviewer UI entirely — the CEO doesn't have a terminal
// handy for curl, so this button is the only way to trigger the reload.
function setProfileReloadStatus(msg, level = 'info') {
  if (!el.profileReloadStatus) return;
  el.profileReloadStatus.textContent = msg;
  el.profileReloadStatus.className = 'profile-reload-status' + (level !== 'info' ? ' status-' + level : '');
}

if (el.profileReloadBtn) {
  el.profileReloadBtn.addEventListener('click', async () => {
    el.profileReloadBtn.disabled = true;
    setProfileReloadStatus('Reloading…', 'info');
    try {
      const resp = await fetch('/profile/reload', { method: 'POST' });
      const data = await resp.json();
      if (!resp.ok || data.error) {
        const detail = data.error ? `[${data.error.stage}] ${data.error.detail}` : `HTTP ${resp.status}`;
        setProfileReloadStatus(`Reload failed: ${detail}`, 'error');
      } else {
        setProfileReloadStatus('Profile reloaded ✓', 'ok');
        el.agentOpening.textContent = data.agent_opening || el.agentOpening.textContent;
      }
    } catch (err) {
      setProfileReloadStatus(`Request failed: ${err.message}`, 'error');
    } finally {
      el.profileReloadBtn.disabled = false;
    }
  });
}

// ── Profile Editor (Phase 4 Etapa B, GET/POST /profile) ───────────────────
// Manual edit of agent_opening/prompt_blocks.*/truth_base/voice/llm_model,
// a second way to write agent_profile.json besides the interviewer's own
// chat + Approve flow. Loaded the moment the Agent Setup layer is entered
// (see "Control Room layer navigation" below) and refreshed automatically
// after an Approve (see the 'profile_written' case above), so the editor
// never shows stale fields next to whatever the interviewer just wrote.
function setProfileEditorStatus(msg, level = 'info') {
  if (!el.profileEditorStatus) return;
  el.profileEditorStatus.textContent = msg;
  el.profileEditorStatus.className = 'profile-editor-status' + (level !== 'info' ? ' status-' + level : '');
}

function applyProfileEditorData(data) {
  if (!data) return;
  if (el.profileAgentOpening) el.profileAgentOpening.value = data.agent_opening || '';
  if (el.profileVoice) el.profileVoice.value = data.voice || '';
  if (el.profileLlmModel && data.llm_model) el.profileLlmModel.value = data.llm_model;
  const blocks = data.prompt_blocks || {};
  if (el.profileBlockPersonality) el.profileBlockPersonality.value = blocks.personality || '';
  if (el.profileBlockEnvironment) el.profileBlockEnvironment.value = blocks.environment || '';
  if (el.profileBlockTone) el.profileBlockTone.value = blocks.tone || '';
  if (el.profileBlockGoal) el.profileBlockGoal.value = blocks.goal || '';
  if (el.profileBlockGuardrails) el.profileBlockGuardrails.value = blocks.guardrails || '';
  if (el.profileTruthBase) el.profileTruthBase.value = JSON.stringify(data.truth_base || {}, null, 2);
}

async function loadProfileEditor() {
  if (!el.profileAgentOpening) return; // panel not present in this build (defensive)
  try {
    const resp = await fetch('/profile');
    const data = await resp.json();
    if (!resp.ok || data.error) {
      const detail = data.error ? `[${data.error.stage}] ${data.error.detail}` : `HTTP ${resp.status}`;
      setProfileEditorStatus(`Error loading profile: ${detail}`, 'error');
      return;
    }
    applyProfileEditorData(data);
    setProfileEditorStatus('', 'info');
  } catch (err) {
    setProfileEditorStatus(`Request failed: ${err.message}`, 'error');
  }
}

async function saveProfileEditor() {
  if (!el.profileSaveBtn) return;
  el.profileSaveBtn.disabled = true;
  setProfileEditorStatus('Saving…', 'info');
  const payload = {
    agent_opening: el.profileAgentOpening.value,
    voice: el.profileVoice.value,
    llm_model: el.profileLlmModel.value,
    prompt_blocks: {
      personality: el.profileBlockPersonality.value,
      environment: el.profileBlockEnvironment.value,
      tone: el.profileBlockTone.value,
      goal: el.profileBlockGoal.value,
      guardrails: el.profileBlockGuardrails.value,
    },
    // Sent as the raw textarea string; the server validates/parses it as
    // JSON (400 fail-loud on invalid JSON) — no client-side JSON.parse here
    // so a founder's typo shows the server's exact error, not a silent skip.
    truth_base: el.profileTruthBase.value,
  };
  try {
    const resp = await fetch('/profile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) {
      const detail = data.error ? `[${data.error.stage}] ${data.error.detail}` : `HTTP ${resp.status}`;
      setProfileEditorStatus(`Save failed: ${detail}`, 'error');
    } else {
      applyProfileEditorData(data); // canonical values back from the bulletproof loader
      setProfileEditorStatus('Profile saved and reloaded ✓', 'ok');
      loadConfig(); // Layer 1's opening-line preview should reflect the new profile too
    }
  } catch (err) {
    setProfileEditorStatus(`Request failed: ${err.message}`, 'error');
  } finally {
    el.profileSaveBtn.disabled = false;
  }
}

if (el.profileSaveBtn) {
  el.profileSaveBtn.addEventListener('click', saveProfileEditor);
}

// ── Voice-note setup (Phase 3.5 P2, POST /interview/audio) ────────────────
// Click to start recording (red pulsing button), click again to stop and
// send. The transcript comes back as {"transcript":"..."} in the POST
// response (echoed here as the user's own bubble); the interviewer's reply
// streams back over the already-open /ws/interview WebSocket as usual.
async function startInterviewRecording() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mimeType = (window.MediaRecorder && MediaRecorder.isTypeSupported('audio/webm;codecs=opus'))
      ? 'audio/webm;codecs=opus'
      : 'audio/webm';
    const recorder = new MediaRecorder(stream, { mimeType });
    state.mediaRecorder = recorder;
    state.recordedChunks = [];

    recorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) state.recordedChunks.push(e.data);
    };
    recorder.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      const blob = new Blob(state.recordedChunks, { type: recorder.mimeType || mimeType });
      state.recordedChunks = [];
      sendInterviewAudio(blob);
    };

    recorder.start();
    state.isRecordingNote = true;
    el.interviewMicBtn.classList.add('recording');
    el.interviewMicBtn.textContent = '⏹';
    setInterviewStatus('Recording… click the mic again to stop.', 'info');
  } catch (err) {
    setInterviewStatus(`Microphone access denied: ${err.message}`, 'error');
  }
}

function stopInterviewRecording() {
  if (state.mediaRecorder && state.isRecordingNote) {
    state.mediaRecorder.stop();
  }
  state.isRecordingNote = false;
  el.interviewMicBtn.classList.remove('recording');
  el.interviewMicBtn.textContent = '🎙';
}

async function sendInterviewAudio(blob) {
  setInterviewStatus('Transcribing…', 'info');
  setInterviewBusy(true);
  try {
    const resp = await fetch('/interview/audio', {
      method: 'POST',
      headers: { 'Content-Type': blob.type || 'audio/webm' },
      body: blob,
    });
    const data = await resp.json();
    if (!resp.ok || data.error) {
      const detail = data.error ? `[${data.error.stage}] ${data.error.kind}: ${data.error.detail}` : `HTTP ${resp.status}`;
      setInterviewStatus(detail, 'error');
      setInterviewBusy(false);
      return;
    }
    addInterviewBubble('user', data.transcript || '', false);
    setInterviewStatus('Transcribed. Waiting for the interviewer’s reply…', 'ok');
    // interviewer_token/interviewer_done arrive over the existing
    // /ws/interview socket (server already ran the turn on it); that's what
    // eventually calls setInterviewBusy(false).
  } catch (err) {
    setInterviewStatus(`Request failed: ${err.message}`, 'error');
    setInterviewBusy(false);
  }
}

if (el.interviewMicBtn) {
  el.interviewMicBtn.addEventListener('click', () => {
    if (state.isRecordingNote) {
      stopInterviewRecording();
    } else {
      startInterviewRecording();
    }
  });
}

// ── Campaigns / leads (Phase 4 Etapa C) ───────────────────────────────────
function applyDemoSafeMode(demoSafeMode) {
  if (!el.demoSafeBadge) return;
  el.demoSafeBadge.classList.toggle('off', !demoSafeMode);
  el.demoSafeBadgeText.textContent = demoSafeMode ? 'DEMO SAFE MODE' : 'DEMO SAFE MODE OFF';
}

function setLeadsImportStatus(msg, level = 'info') {
  if (!el.leadsImportStatus) return;
  el.leadsImportStatus.textContent = msg;
  el.leadsImportStatus.className = 'leads-import-status' + (level !== 'info' ? ' status-' + level : '');
}

function setLeadDetailStatus(msg, level = 'info') {
  if (!el.leadDetailStatusMsg) return;
  el.leadDetailStatusMsg.textContent = msg;
  el.leadDetailStatusMsg.className = 'lead-detail-status' + (level !== 'info' ? ' status-' + level : '');
}

function renderLeadsCounters(leadsList) {
  const counts = { HOT: 0, WARM: 0, COLD: 0 };
  for (const lead of leadsList) {
    if (counts[lead.status] !== undefined) counts[lead.status]++;
  }
  if (el.leadsCountHot) el.leadsCountHot.textContent = counts.HOT;
  if (el.leadsCountWarm) el.leadsCountWarm.textContent = counts.WARM;
  if (el.leadsCountCold) el.leadsCountCold.textContent = counts.COLD;
  if (el.leadsTableCount) {
    el.leadsTableCount.textContent = `${leadsList.length} lead${leadsList.length === 1 ? '' : 's'}`;
  }
}

function renderLeadsTable(leadsList) {
  if (!el.leadsTableBody) return;
  el.leadsTableBody.innerHTML = '';

  if (leadsList.length === 0) {
    const tr = document.createElement('tr');
    tr.className = 'leads-table-empty-row';
    const td = document.createElement('td');
    td.colSpan = 5;
    td.textContent = 'No leads yet — import a CSV to get started.';
    tr.appendChild(td);
    el.leadsTableBody.appendChild(tr);
    return;
  }

  for (const lead of leadsList) {
    const tr = document.createElement('tr');
    if (lead.id === state.leads.selectedId) tr.classList.add('selected');
    tr.addEventListener('click', () => selectLead(lead.id));

    const contactTd = document.createElement('td');
    const nameSpan = document.createElement('span');
    nameSpan.className = 'lead-contact-name';
    nameSpan.textContent = lead.contactName || '—';
    contactTd.appendChild(nameSpan);
    if (lead.isBallena) {
      const whale = document.createElement('span');
      whale.title = 'High-value account';
      whale.style.marginLeft = '6px';
      whale.textContent = '🐋';
      contactTd.appendChild(whale);
    }

    const companyTd = document.createElement('td');
    companyTd.textContent = lead.companyName || '—';

    const industryTd = document.createElement('td');
    industryTd.textContent = lead.industry || '—';

    const phoneTd = document.createElement('td');
    phoneTd.className = 'lead-phone-cell';
    phoneTd.textContent = lead.phone || '—';

    const statusTd = document.createElement('td');
    const chip = document.createElement('span');
    chip.className = `lead-status-chip ${lead.status}`;
    chip.textContent = lead.status;
    statusTd.appendChild(chip);

    tr.append(contactTd, companyTd, industryTd, phoneTd, statusTd);
    el.leadsTableBody.appendChild(tr);
  }
}

function renderLeadDetail(lead) {
  if (!lead) {
    if (el.leadDetailEmpty) el.leadDetailEmpty.style.display = '';
    if (el.leadDetailBody) el.leadDetailBody.style.display = 'none';
    return;
  }
  if (el.leadDetailEmpty) el.leadDetailEmpty.style.display = 'none';
  if (el.leadDetailBody) el.leadDetailBody.style.display = 'flex';

  el.leadDetailName.textContent = lead.contactName || 'Unknown Contact';
  const subParts = [lead.seniority, lead.companyName].filter(Boolean);
  el.leadDetailSub.textContent = subParts.length ? subParts.join(' @ ') : (lead.companyName || '—');

  el.leadDetailStatus.value = lead.status || 'COLD';
  el.leadDetailBallena.value = lead.isBallena ? 'true' : 'false';

  el.leadDetailPainPoints.innerHTML = '';
  const painPoints = Array.isArray(lead.painPoints) ? lead.painPoints : [];
  if (painPoints.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'lead-detail-pain-points-empty';
    empty.textContent = 'None recorded.';
    el.leadDetailPainPoints.appendChild(empty);
  } else {
    for (const point of painPoints) {
      const div = document.createElement('div');
      div.className = 'lead-detail-pain-point';
      div.textContent = point;
      el.leadDetailPainPoints.appendChild(div);
    }
  }

  el.leadDetailFirstMessage.value = lead.customFirstMessage || '';
  el.leadDetailSystemPrompt.value = lead.customSystemPrompt || '';

  if (lead.status !== 'COLD' && lead.classificationReasoning) {
    el.leadClassification.style.display = '';
    el.leadClassificationText.textContent = `${lead.status}: ${lead.classificationReasoning}`;
  } else if (lead.classificationReasoning) {
    el.leadClassification.style.display = '';
    el.leadClassificationText.textContent = `${lead.status}: ${lead.classificationReasoning}`;
  } else {
    el.leadClassification.style.display = 'none';
  }

  setLeadDetailStatus('', 'info');
}

function selectLead(leadId) {
  state.leads.selectedId = leadId;
  renderLeadsTable(state.leads.all);
  const lead = state.leads.all.find((l) => l.id === leadId) || null;
  renderLeadDetail(lead);
}

async function loadLeads() {
  try {
    const resp = await fetch('/leads');
    if (!resp.ok) throw new Error(`GET /leads HTTP ${resp.status}`);
    const data = await resp.json();
    state.leads.all = Array.isArray(data) ? data : [];
    renderLeadsCounters(state.leads.all);
    renderLeadsTable(state.leads.all);
    if (state.leads.selectedId) {
      const stillThere = state.leads.all.find((l) => l.id === state.leads.selectedId);
      if (stillThere) {
        renderLeadDetail(stillThere);
      } else {
        state.leads.selectedId = null;
        renderLeadDetail(null);
      }
    }
  } catch (err) {
    setLeadsImportStatus(`Could not load leads: ${err.message}`, 'error');
  }
}

async function importLeadsCsv(file) {
  if (!file) return;
  setLeadsImportStatus(`Importing ${file.name}…`, 'info');
  const formData = new FormData();
  formData.append('file', file);
  try {
    const resp = await fetch('/leads/import', { method: 'POST', body: formData });
    const data = await resp.json();
    if (!resp.ok || data.error) {
      const detail = data.error ? `[${data.error.stage}] ${data.error.detail}` : `HTTP ${resp.status}`;
      setLeadsImportStatus(`Import failed: ${detail}`, 'error');
      return;
    }
    setLeadsImportStatus(`Imported ${data.imported} lead(s), skipped ${data.skipped}.`, 'ok');
    await loadLeads();
  } catch (err) {
    setLeadsImportStatus(`Request failed: ${err.message}`, 'error');
  }
}

if (el.leadsImportInput) {
  el.leadsImportInput.addEventListener('change', () => {
    const file = el.leadsImportInput.files && el.leadsImportInput.files[0];
    importLeadsCsv(file);
    el.leadsImportInput.value = ''; // allow re-importing the same filename
  });
}

async function saveLeadDetail() {
  const leadId = state.leads.selectedId;
  if (!leadId) return;
  el.leadSaveBtn.disabled = true;
  setLeadDetailStatus('Saving…', 'info');
  const payload = {
    status: el.leadDetailStatus.value,
    isBallena: el.leadDetailBallena.value === 'true',
    customFirstMessage: el.leadDetailFirstMessage.value,
    customSystemPrompt: el.leadDetailSystemPrompt.value,
  };
  try {
    const resp = await fetch(`/leads/${leadId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) {
      const detail = data.error ? `[${data.error.stage}] ${data.error.detail}` : `HTTP ${resp.status}`;
      setLeadDetailStatus(`Save failed: ${detail}`, 'error');
      return;
    }
    setLeadDetailStatus('Saved ✓', 'ok');
    await loadLeads();
  } catch (err) {
    setLeadDetailStatus(`Request failed: ${err.message}`, 'error');
  } finally {
    el.leadSaveBtn.disabled = false;
  }
}

if (el.leadSaveBtn) {
  el.leadSaveBtn.addEventListener('click', saveLeadDetail);
}

async function generateLeadPrompt() {
  const leadId = state.leads.selectedId;
  if (!leadId) return;
  el.leadGeneratePromptBtn.disabled = true;
  setLeadDetailStatus('Generating prompt…', 'info');
  try {
    const resp = await fetch(`/leads/${leadId}/generate_prompt`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok || data.error) {
      const detail = data.error ? `[${data.error.stage}] ${data.error.kind}: ${data.error.detail}` : `HTTP ${resp.status}`;
      setLeadDetailStatus(`Generate failed: ${detail}`, 'error');
      return;
    }
    setLeadDetailStatus('Prompt generated ✓', 'ok');
    await loadLeads();
    renderLeadDetail(data);
  } catch (err) {
    setLeadDetailStatus(`Request failed: ${err.message}`, 'error');
  } finally {
    el.leadGeneratePromptBtn.disabled = false;
  }
}

if (el.leadGeneratePromptBtn) {
  el.leadGeneratePromptBtn.addEventListener('click', generateLeadPrompt);
}

async function callLead() {
  const leadId = state.leads.selectedId;
  if (!leadId) return;
  el.leadCallBtn.disabled = true;
  setLeadDetailStatus('Placing call…', 'info');
  const payload = {
    first_message: el.leadDetailFirstMessage.value,
    system_prompt: el.leadDetailSystemPrompt.value,
  };
  try {
    const resp = await fetch(`/leads/${leadId}/call`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) {
      const detail = data.error ? `[${data.error.stage}] ${data.error.kind}: ${data.error.detail}` : `HTTP ${resp.status}`;
      setLeadDetailStatus(`Call failed: ${detail}`, 'error');
      return;
    }
    state.leads.activeCallLeadId = leadId;
    el.leadTranscriptMessages.innerHTML = '';
    state.leads.monitorCallId = null;
    const modeNote = data.demo_safe_mode ? ' (DEMO SAFE MODE → CALL_ME_NUMBER)' : '';
    setLeadDetailStatus(`Call placed. SID: ${data.sid}${modeNote}`, 'ok');
  } catch (err) {
    setLeadDetailStatus(`Request failed: ${err.message}`, 'error');
  } finally {
    el.leadCallBtn.disabled = false;
  }
}

if (el.leadCallBtn) {
  el.leadCallBtn.addEventListener('click', callLead);
}

// Routes /ws/monitor events into the Campaigns lead transcript panel while a
// lead call is the most recently placed one (see callLead() above) — reuses
// the SAME socket/events as the Phone Outreach panel's handleMonitorEvent,
// just a second, independent rendering target. When the call ends, a fresh
// GET /leads (triggered a few seconds later, giving the background
// classification task time to run) picks up the updated HOT/WARM/COLD
// status + reasoning.
function handleLeadMonitorEvent(callId, evt) {
  if (!state.leads.activeCallLeadId) return;

  if (state.leads.monitorCallId !== callId) {
    state.leads.monitorCallId = callId;
    el.leadTranscriptMessages.innerHTML = '';
    state.leads.monitorSttPartial = null;
    state.leads.monitorAgentToken = null;
  }

  switch (evt.type) {
    case 'stt_partial': {
      state.leads.monitorSttPartial = evt.text;
      if (state.leads.monitorSttPartial) {
        addOrUpdateTranscriptBubble(el.leadTranscriptMessages, 'user', state.leads.monitorSttPartial, true);
      }
      break;
    }
    case 'stt_final': {
      state.leads.monitorSttPartial = null;
      addOrUpdateTranscriptBubble(el.leadTranscriptMessages, 'user', evt.text || '', false);
      break;
    }
    case 'agent_token': {
      state.leads.monitorAgentToken = (state.leads.monitorAgentToken || '') + (evt.token || '');
      addOrUpdateTranscriptBubble(el.leadTranscriptMessages, 'agent', state.leads.monitorAgentToken, true);
      break;
    }
    case 'agent_done': {
      state.leads.monitorAgentToken = null;
      addOrUpdateTranscriptBubble(el.leadTranscriptMessages, 'agent', evt.text || '', false);
      break;
    }
    case 'call_ended': {
      setLeadDetailStatus('Call ended — classifying…', 'info');
      // Give the server's background classification task a moment to run
      // (Fireworks call + DB write), then refresh so the lead's status/
      // reasoning shows up without the founder having to click anything.
      setTimeout(loadLeads, 3000);
      break;
    }
    case 'error': {
      setLeadDetailStatus(`[${evt.stage}] ${evt.kind}: ${evt.detail}`, 'error');
      break;
    }
    default:
      break;
  }
}

// ── Control Room layer navigation (Phase 3.6 UI redesign) ─────────────────
// Switching layers only toggles a CSS class on the sidebar item + its
// matching panel — no panel is ever removed from the DOM, so /ws/call,
// /ws/interview and /ws/monitor (and all mic/TTS state) keep running
// underneath regardless of which layer is currently visible. This is the
// only new "change layer" behavior app.js gains per PLAN_UI_CONTROL_ROOM.md
// rule 1 (no existing id's behavior changes).
const pipelineItems = document.querySelectorAll('.pipeline-item');
const layerPanels = document.querySelectorAll('.layer-panel');
let activeLayer = '1';

function setActiveLayer(layer) {
  activeLayer = layer;
  pipelineItems.forEach((item) => {
    item.classList.toggle('active', item.dataset.layer === layer);
  });
  layerPanels.forEach((panel) => {
    panel.classList.toggle('active', panel.dataset.layerPanel === layer);
  });
  if (layer === '2') {
    // Agent Setup: (re)load the Profile Editor's fields from disk every time
    // the founder comes back to this layer, so a hand edit made elsewhere
    // (or a stale in-memory state) never shows outdated fields.
    loadProfileEditor();
  }
  if (layer === '3') {
    // Phone Outreach, since the Etapa A reorder (was layer '2' before).
    const dot = document.querySelector('.pipeline-item[data-layer="3"] .pipeline-dot');
    if (dot) dot.classList.remove('active-alert');
  }
  if (layer === '4') {
    // Campaigns (Phase 4 Etapa C): refresh the leads table every time the
    // founder comes back to this layer, same reasoning as layer 2's Profile
    // Editor reload above.
    loadLeads();
  }
}

pipelineItems.forEach((item) => {
  item.addEventListener('click', () => setActiveLayer(item.dataset.layer));
});

// ── Init ─────────────────────────────────────────────────────────────────
loadConfig();
connectWs();
refreshTunnelStatus();
setInterval(refreshTunnelStatus, 10000);
connectInterviewWs();
connectMonitorWs();
setActiveLayer('1');
