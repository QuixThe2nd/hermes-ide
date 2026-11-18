// Big Steve — side panel UI.
//
// One Hermes conversation per browser tab: the panel tracks its window's
// active tab and keeps each tab's transcript in its own bucket (per-tab
// buckets persist to chrome.storage.session; the legacy no-tab bucket keeps
// the old chrome.storage.local 'transcript' key so a server downgrade still
// finds the pre-per-tab history). Frames the server stamps with `tab_id`
// belong to that tab — rendered live when it is active, stored silently
// otherwise; frames without a tab id (older server) land on the active tab.
//
// Streaming: deltas append into a live bubble for the active tab; deltas for
// a background tab accumulate in that bucket's streamText and seal on final.
// The "new chat" button sends /new for ONLY the current tab's session and
// clears that tab's transcript.
//
// All socket I/O lives in background.js; this page only exchanges messages.
// It also holds a 'sidepanel:<windowId>' port open to background.js for the
// toggle command and closes itself on a {kind:'close'} reply.

'use strict';

const TRANSCRIPT_KEY = 'transcript';
const TRANSCRIPT_MAX = 200;
const WHO_LABEL = { user: 'You', steve: 'Big Steve', error: 'Error' };

const bannerEl = document.getElementById('banner');
const transcriptEl = document.getElementById('transcript');
const inputEl = document.getElementById('input');
const newChatEl = document.getElementById('new-chat');

// tabId (integer) → that tab's conversation; null → the legacy single
// conversation. Buckets hold {entries, streamText, streaming, loading}.
const buckets = new Map();
let activeTabId = null; // null until the window's active tab resolves
let streamBubble = null; // DOM node receiving deltas for the active bucket

// ─── Per-tab transcript buckets ─────────────────────────────────────────────

function bucketFor(tabId) {
  let bucket = buckets.get(tabId);
  if (!bucket) {
    bucket = { entries: [], streamText: '', streaming: false, loading: false };
    buckets.set(tabId, bucket);
  }
  return bucket;
}

function storageArea(tabId) {
  return tabId == null ? chrome.storage.local : chrome.storage.session;
}

function storageKey(tabId) {
  return tabId == null ? TRANSCRIPT_KEY : 'tabTranscript:' + tabId;
}

function saveBucket(tabId) {
  const bucket = buckets.get(tabId);
  if (!bucket || bucket.loading) return;
  storageArea(tabId)
    .set({ [storageKey(tabId)]: bucket.entries })
    .catch(() => {});
}

function loadBucket(tabId) {
  const bucket = bucketFor(tabId);
  if (bucket.loading) return;
  bucket.loading = true;
  storageArea(tabId)
    .get(storageKey(tabId))
    .then((stored) => {
      const saved = stored && stored[storageKey(tabId)];
      // Stored history first; anything appended while the load was in
      // flight is newer and stays after it.
      const loaded = Array.isArray(saved)
        ? saved.filter((e) => e && typeof e.text === 'string')
        : [];
      bucket.entries = loaded.concat(bucket.entries);
      bucket.loading = false;
      if (tabId === activeTabId) renderActive();
    })
    .catch(() => {
      bucket.loading = false;
    });
}

function dropBucket(tabId) {
  buckets.delete(tabId);
  if (tabId != null) {
    chrome.storage.session.remove(storageKey(tabId)).catch(() => {});
  }
}

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

function clearTranscriptDom() {
  while (transcriptEl.firstChild) transcriptEl.removeChild(transcriptEl.firstChild);
  streamBubble = null;
}

function appendEntry(who, text) {
  const bucket = bucketFor(activeTabId);
  bucket.entries.push({ who, text });
  if (bucket.entries.length > TRANSCRIPT_MAX) {
    bucket.entries = bucket.entries.slice(-TRANSCRIPT_MAX);
  }
  makeBubble(who).text.textContent = text;
  saveBucket(activeTabId);
}

// Rebuild the DOM from the active tab's bucket (tab switches, resets).
function renderActive() {
  clearTranscriptDom();
  hideTypingIndicator();
  const bucket = bucketFor(activeTabId);
  for (const entry of bucket.entries) {
    makeBubble(entry.who).text.textContent = entry.text;
  }
  if (bucket.streaming && bucket.streamText) {
    const made = makeBubble('steve');
    made.bubble.classList.add('streaming');
    made.text.textContent = bucket.streamText;
    streamBubble = made;
  }
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
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

// ─── Typing indicator (not persisted; lives only in the live DOM) ──────────

const TYPING_TIMEOUT_MS = 10000;
let typingRow = null;
let typingTimer = null;

function showTypingIndicator() {
  if (streamBubble) return; // the streaming bubble already signals activity
  if (!typingRow) {
    const row = document.createElement('div');
    row.className = 'bubble typing';
    const label = document.createElement('div');
    label.className = 'who';
    label.textContent = WHO_LABEL.steve;
    const dots = document.createElement('div');
    dots.className = 'dots';
    dots.append(document.createElement('span'), document.createElement('span'), document.createElement('span'));
    row.append(label, dots);
    typingRow = row;
  }
  if (!typingRow.isConnected) transcriptEl.append(typingRow);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
  clearTimeout(typingTimer);
  typingTimer = setTimeout(hideTypingIndicator, TYPING_TIMEOUT_MS);
}

function hideTypingIndicator() {
  clearTimeout(typingTimer);
  typingTimer = null;
  if (typingRow && typingRow.isConnected) typingRow.remove();
}

// ─── Frames from the worker ─────────────────────────────────────────────────

function handleFrame(frame) {
  if (!frame || typeof frame !== 'object') return;
  const frameTabId = SidebarTabs.frameTabId(frame);
  // Legacy frames (older server) have no tab: they belong to whichever tab
  // the panel is showing, exactly like the single-session era.
  const targetTabId = frameTabId == null ? activeTabId : frameTabId;
  const live = targetTabId === activeTabId; // render only the visible tab
  switch (frame.type) {
    case 'typing': {
      if (!live) return; // a background tab's typing is invisible anyway
      if (frame.active) showTypingIndicator();
      else hideTypingIndicator();
      return;
    }
    case 'delta': {
      const chunk = typeof frame.text === 'string' ? frame.text : frame.chunk;
      if (typeof chunk !== 'string' || !chunk) return;
      const bucket = bucketFor(targetTabId);
      bucket.streaming = true;
      bucket.streamText += chunk; // suffix-only append
      if (!live) return;
      hideTypingIndicator(); // the live bubble below signals activity instead
      ensureStreamBubble().text.textContent = bucket.streamText;
      transcriptEl.scrollTop = transcriptEl.scrollHeight;
      return;
    }
    case 'final': {
      const text = typeof frame.text === 'string' ? frame.text : '';
      const bucket = bucketFor(targetTabId);
      bucket.streaming = false;
      bucket.streamText = '';
      bucket.entries.push({ who: 'steve', text });
      if (bucket.entries.length > TRANSCRIPT_MAX) {
        bucket.entries = bucket.entries.slice(-TRANSCRIPT_MAX);
      }
      saveBucket(targetTabId);
      if (!live) return;
      hideTypingIndicator();
      if (streamBubble) {
        streamBubble.text.textContent = text;
        streamBubble.bubble.classList.remove('streaming');
        streamBubble = null;
      } else {
        makeBubble('steve').text.textContent = text; // final without any delta
      }
      return;
    }
    case 'error': {
      const bucket = bucketFor(targetTabId);
      if (bucket.streaming) { // failed turn: keep the partial text as an entry
        bucket.entries.push({ who: 'steve', text: bucket.streamText });
        bucket.streaming = false;
        bucket.streamText = '';
      }
      const message = frame.message || frame.text || 'Unknown error';
      bucket.entries.push({ who: 'error', text: typeof message === 'string' ? message : String(message) });
      if (bucket.entries.length > TRANSCRIPT_MAX) {
        bucket.entries = bucket.entries.slice(-TRANSCRIPT_MAX);
      }
      saveBucket(targetTabId);
      if (!live) return;
      hideTypingIndicator();
      if (streamBubble) {
        streamBubble.bubble.classList.remove('streaming');
        streamBubble = null;
      }
      makeBubble('error').text.textContent = bucket.entries[bucket.entries.length - 1].text;
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
    response = await chrome.runtime.sendMessage({ kind: 'send_chat', text, tabId: activeTabId });
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
  showTypingIndicator(); // optimistic: the real typing/delta frames take over from here
}

inputEl.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return; // Shift+Enter = newline
  event.preventDefault();
  void send();
});

// New chat: /new for the CURRENT tab's session only. The worker turns this
// into a chat frame tagged with that tab, so the gateway resets exactly that
// conversation; the local transcript clears immediately.
async function startNewChat() {
  const tabId = activeTabId;
  const bucket = bucketFor(tabId);
  bucket.entries = [];
  bucket.streamText = '';
  bucket.streaming = false;
  saveBucket(tabId);
  renderActive();
  let response;
  try {
    response = await chrome.runtime.sendMessage(SidebarTabs.newChatRequest(tabId));
  } catch (err) {
    response = { ok: false, error: 'extension error: ' + String((err && err.message) || err) };
  }
  if (!response || !response.ok) {
    appendEntry('error', (response && response.error) || 'new chat failed');
  }
}

if (newChatEl) {
  newChatEl.addEventListener('click', () => void startNewChat());
}

// ─── Active-tab tracking ────────────────────────────────────────────────────

// Held for the life of the page: background.js maps this port's name to a
// windowId to know whether that window's panel is open.
let sidePanelPort = null;
let ownWindowId = null;

function rememberActiveTab(tabId) {
  const next = Number.isInteger(tabId) ? tabId : null;
  if (next === activeTabId) return;
  activeTabId = next;
  renderActive();
  loadBucket(activeTabId);
}

function refreshActiveTab() {
  if (ownWindowId == null || !chrome.tabs || !chrome.tabs.query) return;
  chrome.tabs
    .query({ active: true, windowId: ownWindowId })
    .then((tabs) => {
      const tab = Array.isArray(tabs) ? tabs.find((t) => t && Number.isInteger(t.id)) : null;
      if (tab) rememberActiveTab(tab.id);
    })
    .catch(() => {});
}

function watchTabs() {
  if (chrome.tabs && chrome.tabs.onActivated) {
    chrome.tabs.onActivated.addListener(({ tabId, windowId }) => {
      if (ownWindowId != null && windowId !== ownWindowId) return;
      rememberActiveTab(tabId);
    });
  }
  if (chrome.tabs && chrome.tabs.onUpdated) {
    // Activation that arrives via an update (e.g. a tab replacing another):
    // re-assert from the source of truth instead of trusting stale state.
    chrome.tabs.onUpdated.addListener((_tabId, info, tab) => {
      if (info && info.active && tab && Number.isInteger(tab.id)) {
        if (ownWindowId == null || tab.windowId === ownWindowId) rememberActiveTab(tab.id);
      }
    });
  }
  if (chrome.windows && chrome.windows.onFocusChanged) {
    chrome.windows.onFocusChanged.addListener((windowId) => {
      if (ownWindowId == null || windowId !== ownWindowId) return;
      refreshActiveTab(); // focus came back: the active tab may have changed while away
    });
  }
  if (chrome.tabs && chrome.tabs.onRemoved) {
    chrome.tabs.onRemoved.addListener((tabId, info) => {
      // A closed tab's conversation cannot come back; drop its transcript.
      if (info && ownWindowId != null && info.windowId !== ownWindowId) return;
      dropBucket(tabId);
      if (tabId === activeTabId) {
        activeTabId = null;
        renderActive();
      }
    });
  }
}

// ─── Init ───────────────────────────────────────────────────────────────────

loadBucket(null); // legacy conversation (older server / no tab resolved yet)

chrome.runtime
  .sendMessage({ kind: 'get_status' })
  .then(applyStatus)
  .catch(() => {});

chrome.windows
  .getCurrent()
  .then((win) => {
    ownWindowId = win && Number.isInteger(win.id) ? win.id : null;
    sidePanelPort = chrome.runtime.connect({ name: 'sidepanel:' + (ownWindowId == null ? '' : ownWindowId) });
    sidePanelPort.onMessage.addListener((msg) => {
      if (msg && msg.kind === 'close') window.close();
    });
    refreshActiveTab();
  })
  .catch(() => {});

watchTabs();
