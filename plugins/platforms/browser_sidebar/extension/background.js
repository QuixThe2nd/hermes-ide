// Big Steve — service worker.
//
// Owns the WebSocket to Hermes (token as the sole subprotocol), the side
// panel open paths, and the run_tab_js tool bridge. Chat frames in:
//   {type:"chat", id, text, tab:{id,url,title}}
// Frames out: ready / delta{id,text} (suffix-only append) / final{id,text} /
// error{message} / tool{request_id,name,run_tab_js,args:{code}}.
// Tool replies: {type:"tool_result", request_id, ok, result|error}.

'use strict';

const WS_URL = 'ws://server.tailea4ecb.ts.net:8796/ws';
const KEEPALIVE_ALARM = 'bs-keepalive';
const BACKOFF_BASE_MS = 1000;
const BACKOFF_MAX_MS = 60000;
const TOOL_TIMEOUT_MS = 15000;
const TOOL_RESULT_MAX = 100000;

// Connection state.
let ws = null;
let connected = false;
let fatalConnectError = null; // WebSocket constructor threw: config error, stop retrying.
let connectError = null; // Last close reason, for surfaces that ask.
let failedAttempts = 0;
let reconnectTimer = null;

// Synchronous source of a window id for chrome.sidePanel.open(); the gesture
// requirement means open() must be called before any await in the handlers.
let lastFocusedNormalWindowId = null;

// ─── Side panel opening ─────────────────────────────────────────────────────

function openSidePanel(windowId) {
  if (typeof windowId === 'number') {
    chrome.sidePanel.open({ windowId }).catch((err) => {
      console.error('sidePanel.open failed:', err);
    });
    return;
  }
  // Degraded path: no synchronous window id known (worker just woke and no
  // focus event has arrived yet). The gesture may be lost, but try anyway.
  chrome.windows
    .getLastFocused({ windowType: 'normal' })
    .then((win) => chrome.sidePanel.open({ windowId: win.id }))
    .catch((err) => {
      console.error('sidePanel.open failed:', err);
    });
}

// Registered synchronously at worker start so events always have listeners.
chrome.commands.onCommand.addListener((command, tab) => {
  if (command !== 'open-sidebar') return;
  openSidePanel(tab && typeof tab.windowId === 'number' ? tab.windowId : lastFocusedNormalWindowId);
});

chrome.action.onClicked.addListener((tab) => {
  openSidePanel(tab && typeof tab.windowId === 'number' ? tab.windowId : lastFocusedNormalWindowId);
});

chrome.windows.onFocusChange.addListener((windowId) => {
  if (typeof windowId !== 'number' || windowId <= 0) return;
  chrome.windows
    .get(windowId)
    .then((win) => {
      if (win && win.type === 'normal') lastFocusedNormalWindowId = windowId;
    })
    .catch(() => {});
});

// ─── Messaging from popup / sidepanel / content scripts ────────────────────

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg !== 'object') return;
  switch (msg.kind) {
    case 'get_status':
      sendResponse({ connected, error: fatalConnectError || connectError });
      return;
    case 'open_sidebar': // content script saw Command/Ctrl+B
      openSidePanel(lastFocusedNormalWindowId);
      sendResponse({ ok: true });
      return;
    case 'send_chat':
      sendChat(msg.text).then(sendResponse, (err) =>
        sendResponse({ ok: false, error: String((err && err.message) || err) }),
      );
      return true; // async sendResponse
  }
});

async function activeTabContext() {
  try {
    const tabs = await chrome.tabs.query({
      active: true,
      lastFocusedWindow: true,
      windowType: 'normal',
    });
    const tab = tabs.find((t) => t && typeof t.id === 'number');
    return tab ? { id: tab.id, url: tab.url || null, title: tab.title || null } : { id: null, url: null, title: null };
  } catch {
    return { id: null, url: null, title: null };
  }
}

async function sendChat(text) {
  if (typeof text !== 'string' || !text.trim()) return { ok: false, error: 'empty message' };
  if (!connected || !ws || ws.readyState !== WebSocket.OPEN) return { ok: false, error: 'disconnected' };
  const tab = await activeTabContext();
  // Re-check after the await: the socket may have dropped meanwhile.
  if (!ws || ws.readyState !== WebSocket.OPEN) return { ok: false, error: 'disconnected' };
  const frame = { type: 'chat', id: crypto.randomUUID(), text, tab };
  try {
    ws.send(JSON.stringify(frame));
  } catch (err) {
    return { ok: false, error: 'send failed: ' + String((err && err.message) || err) };
  }
  return { ok: true, id: frame.id };
}

// ─── WebSocket connection ───────────────────────────────────────────────────

function sendFrame(obj) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  try {
    ws.send(JSON.stringify(obj));
  } catch (err) {
    console.error('send failed:', err);
  }
}

function broadcast(msg) {
  chrome.runtime.sendMessage(msg).catch(() => {}); // no listener is fine
}

function setStatus(isConnected, error) {
  connected = isConnected;
  connectError = error || null;
  broadcast({ kind: 'status', connected, error: connectError });
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  if (fatalConnectError) return;
  const delay = Math.min(BACKOFF_BASE_MS * 2 ** failedAttempts, BACKOFF_MAX_MS);
  failedAttempts += 1;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, delay);
}

async function connect() {
  if (fatalConnectError) return;
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;

  let token = null;
  try {
    const stored = await chrome.storage.sync.get('token');
    token = stored.token;
  } catch {
    token = null;
  }
  if (!token) {
    setStatus(false, 'no token saved');
    return;
  }

  let sock;
  try {
    // The token is the sole WebSocket subprotocol; a valid socket is the
    // authorized user on the Hermes side.
    sock = new WebSocket(WS_URL, [token]);
  } catch (err) {
    // Constructor throw (invalid URL / invalid subprotocol characters) is a
    // permanent configuration error: stop retrying and surface it.
    ws = null;
    fatalConnectError = 'cannot connect: ' + String((err && err.message) || err);
    setStatus(false, fatalConnectError);
    return;
  }

  ws = sock;
  sock.onopen = () => {
    if (ws !== sock) return; // stale socket from a replaced connection
    failedAttempts = 0;
    setStatus(true, null);
    sendFrame({
      type: 'hello',
      client: 'big-steve-extension',
      version: chrome.runtime.getManifest().version,
    });
  };
  sock.onmessage = (event) => {
    if (ws !== sock) return;
    handleWireFrame(event.data);
  };
  sock.onclose = (event) => {
    if (ws !== sock) return;
    ws = null;
    setStatus(false, event && event.reason ? String(event.reason) : null);
    scheduleReconnect();
  };
  sock.onerror = () => {}; // onclose always follows with the details
}

function handleWireFrame(data) {
  let frame;
  try {
    frame = JSON.parse(data);
  } catch {
    return;
  }
  if (!frame || typeof frame !== 'object') return;
  switch (frame.type) {
    case 'tool':
      if (frame.name === 'run_tab_js') {
        runTabJs(frame).then(sendFrame, (err) =>
          sendFrame({
            type: 'tool_result',
            request_id: frame.request_id,
            ok: false,
            error: String((err && err.message) || err),
          }),
        );
      }
      return; // tool traffic stays in the worker
    case 'ready':
    case 'delta':
    case 'final':
    case 'error':
      broadcast({ kind: 'frame', frame }); // UI surfaces render these
      return;
  }
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'sync' || !changes.token) return;
  fatalConnectError = null;
  failedAttempts = 0;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (ws) {
    try {
      ws.close(); // onclose → scheduleReconnect → connect() with the new token
    } catch {
      ws = null;
      connect();
    }
  } else {
    connect();
  }
});

// ─── run_tab_js tool ────────────────────────────────────────────────────────

// Runs code in the page's MAIN world and returns [errStringOrNull, jsonString].
function injectedRunner(source) {
  try {
    const encoded = JSON.stringify(eval(source));
    return [null, encoded === undefined ? 'undefined' : encoded];
  } catch (err) {
    return [String((err && err.message) || err), null];
  }
}

async function activeTabForScripting() {
  try {
    let windowId = lastFocusedNormalWindowId;
    if (typeof windowId !== 'number') {
      const win = await chrome.windows.getLastFocused({ windowType: 'normal' });
      windowId = win && typeof win.id === 'number' ? win.id : null;
    }
    if (typeof windowId !== 'number') return null;
    const tabs = await chrome.tabs.query({ active: true, windowId });
    const tab = tabs.find((t) => t && typeof t.id === 'number');
    return tab || null;
  } catch {
    return null;
  }
}

function withTimeout(promise, ms, label) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(label)), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (err) => {
        clearTimeout(timer);
        reject(err);
      },
    );
  });
}

async function runTabJs(frame) {
  const code = frame && frame.args && frame.args.code;
  if (typeof code !== 'string') {
    return { type: 'tool_result', request_id: frame && frame.request_id, ok: false, error: 'bad tool args' };
  }
  const tab = await activeTabForScripting();
  if (!tab) {
    return { type: 'tool_result', request_id: frame.request_id, ok: false, error: 'no active tab' };
  }

  let results;
  try {
    results = await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId: tab.id },
        world: 'MAIN',
        func: injectedRunner,
        args: [code],
      }),
      TOOL_TIMEOUT_MS,
      'timeout',
    );
  } catch (err) {
    return {
      type: 'tool_result',
      request_id: frame.request_id,
      ok: false,
      error: String((err && err.message) || err),
    };
  }

  const injection = Array.isArray(results) ? results[0] : null;
  if (!injection || injection.error) {
    return {
      type: 'tool_result',
      request_id: frame.request_id,
      ok: false,
      error: String((injection && injection.error) || 'no injection result'),
    };
  }
  const [errText, encoded] = Array.isArray(injection.result) ? injection.result : [null, null];
  if (errText !== null && errText !== undefined) {
    return { type: 'tool_result', request_id: frame.request_id, ok: false, error: errText };
  }
  if (typeof encoded !== 'string') {
    return { type: 'tool_result', request_id: frame.request_id, ok: false, error: 'no result' };
  }
  return {
    type: 'tool_result',
    request_id: frame.request_id,
    ok: true,
    result: encoded.slice(0, TOOL_RESULT_MAX),
  };
}

// ─── Keepalive + init ───────────────────────────────────────────────────────

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== KEEPALIVE_ALARM) return;
  if (fatalConnectError) return;
  if (!connected) connect();
});

function ensureKeepalive() {
  chrome.alarms.create(KEEPALIVE_ALARM, { delayInMinutes: 1, periodInMinutes: 1 }).catch?.(() => {});
}

// Runs on every worker start and wake: listeners above are already registered
// synchronously, so a restarted worker re-attaches and reconnects.
ensureKeepalive();
connect();
