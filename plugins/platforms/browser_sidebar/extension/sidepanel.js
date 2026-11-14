// Big Steve — side panel UI.
//
// Renders the chat transcript (persisted in chrome.storage.local), streams
// assistant deltas into a live bubble (deltas are suffix-only appends), swaps
// in the final text, and shows a banner while the worker's socket is down.
// All socket I/O lives in background.js; this page only exchanges messages.

'use strict';

const TRANSCRIPT_KEY = 'transcript';
const TRANSCRIPT_MAX = 200;
const WHO_LABEL = { user: 'You', steve: 'Big Steve', error: 'Error' };

const bannerEl = document.getElementById('banner');
const transcriptEl = document.getElementById('transcript');
const inputEl = document.getElementById('input');

// Persisted entries: {who: 'user' | 'steve' | 'error', text}.
let transcript = [];
// DOM node currently receiving deltas for an in-flight response; null between
// turns so the first delta after a final opens a fresh bubble.
let streamBubble = null;

// ─── Rendering ──────────────────────────────────────────────────────────────

function makeBubble(who) {
  const bubble = document.createElement('div');
  bubble.className = 'bubble' + (who === 'user' || who === 'error' ? ' ' + who : '');
  const label = document.createElement('div');
  label.className = 'who';
  label.textContent = WHO_LABEL[who] || who;
  const text = document.createElement('div');
  text.className = 'text';
  bubble.append(label, text);
  transcriptEl.append(bubble);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
  return { bubble, text };
}

function appendEntry(who, text) {
  transcript.push({ who, text });
  if (transcript.length > TRANSCRIPT_MAX) transcript = transcript.slice(-TRANSCRIPT_MAX);
  makeBubble(who).text.textContent = text;
  saveTranscript();
}

function saveTranscript() {
  chrome.storage.local.set({ [TRANSCRIPT_KEY]: transcript }).catch(() => {});
}

function ensureStreamBubble() {
  if (streamBubble && streamBubble.bubble.isConnected && streamBubble.bubble.classList.contains('streaming')) {
    return streamBubble;
  }
  const made = makeBubble('steve');
  made.bubble.classList.add('streaming');
  streamBubble = made;
  return made;
}

// ─── Frames from the worker ─────────────────────────────────────────────────

function handleFrame(frame) {
  if (!frame || typeof frame !== 'object') return;
  switch (frame.type) {
    case 'delta': {
      const chunk = typeof frame.text === 'string' ? frame.text : frame.chunk;
      if (typeof chunk !== 'string' || !chunk) return;
      ensureStreamBubble().text.textContent += chunk; // suffix-only append
      transcriptEl.scrollTop = transcriptEl.scrollHeight;
      return;
    }
    case 'final': {
      const text = typeof frame.text === 'string' ? frame.text : '';
      if (!streamBubble) {
        appendEntry('steve', text); // final without any delta
        return;
      }
      streamBubble.text.textContent = text;
      streamBubble.bubble.classList.remove('streaming');
      streamBubble = null;
      transcript.push({ who: 'steve', text });
      if (transcript.length > TRANSCRIPT_MAX) transcript = transcript.slice(-TRANSCRIPT_MAX);
      saveTranscript();
      return;
    }
    case 'error': {
      if (streamBubble) { // failed turn: keep the partial text, stop the cursor
        streamBubble.bubble.classList.remove('streaming');
        streamBubble = null;
      }
      const message = frame.message || frame.text || 'Unknown error';
      appendEntry('error', typeof message === 'string' ? message : String(message));
      return;
    }
    case 'ready':
      return; // connection status arrives via {kind:'status'} messages
  }
}

chrome.runtime.onMessage.addListener((msg) => {
  if (!msg || typeof msg !== 'object') return;
  if (msg.kind === 'status') applyStatus(msg);
  else if (msg.kind === 'frame') handleFrame(msg.frame);
});

function applyStatus(status) {
  const isConnected = !!(status && status.connected);
  bannerEl.hidden = isConnected;
}

// ─── Sending ────────────────────────────────────────────────────────────────

async function send() {
  const text = inputEl.value.trim();
  if (!text) return;
  let response;
  try {
    response = await chrome.runtime.sendMessage({ kind: 'send_chat', text });
  } catch (err) {
    response = { ok: false, error: 'extension error: ' + String((err && err.message) || err) };
  }
  if (!response || !response.ok) {
    inputEl.value = text; // restore the draft so it is not lost
    appendEntry('error', (response && response.error) || 'send failed');
    return;
  }
  inputEl.value = '';
  appendEntry('user', text);
}

inputEl.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return; // Shift+Enter = newline
  event.preventDefault();
  void send();
});

// ─── Init ───────────────────────────────────────────────────────────────────

chrome.storage.local
  .get(TRANSCRIPT_KEY)
  .then((stored) => {
    const saved = stored && stored[TRANSCRIPT_KEY];
    if (Array.isArray(saved)) {
      transcript = saved.filter((e) => e && typeof e.text === 'string');
      for (const entry of transcript) makeBubble(entry.who).text.textContent = entry.text;
    }
  })
  .catch(() => {});

chrome.runtime
  .sendMessage({ kind: 'get_status' })
  .then(applyStatus)
  .catch(() => {});
