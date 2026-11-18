// Big Steve — service worker.
//
// Owns the WebSocket to Hermes (token as the sole subprotocol), the side
// panel toggle paths, and the tool bridge. Chat frames in:
//   {type:"chat", id, text, tab:{id,url,title}} — one conversation per tab id.
// Frames out: ready / delta{id,text,tab_id?} (suffix-only append) /
// final{id,text,tab_id?} / error{message} / tool{request_id,name,run_tab_js,
// args:{code},tab_id?} — tab_id marks which tab's conversation the frame
// belongs to; legacy frames without it are pre-per-tab server builds.
// Tool replies: {type:"tool_result", request_id, ok, result|error}.
// Tool frames dispatch through TOOL_HANDLERS: run_tab_js (eval in the page's
// MAIN world, falling back to chrome.debugger Runtime.evaluate when the
// page's CSP blocks eval), navigate_tab (chrome.tabs.update + wait for
// onUpdated 'complete'), activate_tab (activate the tab, focus its window).
// Every handler resolves its target tab the same way: args.tab_id, then
// frame.tab_id, then the focused tab; a named-but-closed tab fails the tool.

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

// ─── Side panel toggling ────────────────────────────────────────────────────

// windowId -> the sidepanel.js port for that window's open side panel.
// Populated by chrome.runtime.onConnect, emptied by that port's onDisconnect.
const openSidePanels = new Map();

// Registered synchronously at worker start so a connecting side panel is
// never missed.
chrome.runtime.onConnect.addListener((port) => {
  if (typeof port.name !== 'string' || !port.name.startsWith('sidepanel:')) return;
  const windowId = parseInt(port.name.slice('sidepanel:'.length), 10);
  if (!Number.isInteger(windowId)) return;
  openSidePanels.set(windowId, port);
  port.onDisconnect.addListener(() => {
    // Only clear the entry if it still points at this port: a newer
    // connection for the same window may have already replaced it.
    if (openSidePanels.get(windowId) === port) openSidePanels.delete(windowId);
  });
});

function toggleSidePanel(windowId) {
  if (typeof windowId === 'number' && openSidePanels.has(windowId)) {
    const port = openSidePanels.get(windowId);
    try {
      if (typeof chrome.sidePanel.close === 'function') {
        const maybePromise = chrome.sidePanel.close({ windowId });
        if (maybePromise && typeof maybePromise.catch === 'function') {
          maybePromise.catch(() => port.postMessage({ kind: 'close' }));
        }
      } else {
        port.postMessage({ kind: 'close' });
      }
    } catch (err) {
      port.postMessage({ kind: 'close' });
    }
    return;
  }
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
  toggleSidePanel(tab && typeof tab.windowId === 'number' ? tab.windowId : lastFocusedNormalWindowId);
});

chrome.action.onClicked.addListener((tab) => {
  toggleSidePanel(tab && typeof tab.windowId === 'number' ? tab.windowId : lastFocusedNormalWindowId);
});

chrome.windows.onFocusChanged.addListener((windowId) => {
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
    case 'send_chat':
      sendChat(msg.text, msg.tabId).then(sendResponse, (err) =>
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

async function tabContext(tabId) {
  // The conversation's tab when the panel named one; otherwise whichever
  // tab is focused (legacy single-session behavior).
  if (Number.isInteger(tabId)) {
    try {
      const tab = await chrome.tabs.get(tabId);
      if (tab && typeof tab.id === 'number') {
        return { id: tab.id, url: tab.url || null, title: tab.title || null };
      }
    } catch {
      // Tab closed mid-send: still tag the frame with the id so the server
      // routes to that conversation, not to whichever tab replaced it.
    }
    return { id: tabId, url: null, title: null };
  }
  return activeTabContext();
}

async function sendChat(text, tabId) {
  if (typeof text !== 'string' || !text.trim()) return { ok: false, error: 'empty message' };
  if (!connected || !ws || ws.readyState !== WebSocket.OPEN) return { ok: false, error: 'disconnected' };
  const tab = await tabContext(tabId);
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
    case 'tool': {
      const handler = TOOL_HANDLERS[frame.name];
      if (!handler) {
        sendFrame({
          type: 'tool_result',
          request_id: frame.request_id,
          ok: false,
          error: 'unknown tool: ' + String(frame.name),
        });
        return;
      }
      handler(frame).then(sendFrame, (err) =>
        sendFrame({
          type: 'tool_result',
          request_id: frame.request_id,
          ok: false,
          error: String((err && err.message) || err),
        }),
      );
      return; // tool traffic stays in the worker
    }
    case 'ready':
    case 'delta':
    case 'typing':
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

// ─── Tools (run_tab_js / navigate_tab / activate_tab) ───────────────────────

function toolOk(requestId, result) {
  return { type: 'tool_result', request_id: requestId, ok: true, result };
}

function toolErr(requestId, error) {
  return { type: 'tool_result', request_id: requestId, ok: false, error };
}

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

// ONE target-tab resolution shared by every tool: args.tab_id, then
// frame.tab_id, then (unless fallbackToFocused=false) the focused tab.
// Returns {tab} on success, {closed:true} when a named tab no longer exists,
// {missing:true} when no tab was named and the focused fallback is disabled.
async function resolveToolTab(frame, args, fallbackToFocused = true) {
  const explicit = args && Number.isInteger(args.tab_id) ? args.tab_id : null;
  const frameTabId = frame && Number.isInteger(frame.tab_id) ? frame.tab_id : null;
  const wanted = explicit !== null ? explicit : frameTabId;
  if (wanted === null) {
    if (!fallbackToFocused) return { missing: true };
    return { tab: await activeTabForScripting() };
  }
  try {
    const target = await chrome.tabs.get(wanted);
    if (target && typeof target.id === 'number') return { tab: target };
  } catch {
    // Named tab is gone: fail the tool, never act on another tab.
  }
  return { closed: true };
}

const NAVIGABLE_URL_RE = /^https?:/i;

async function navigateTab(frame) {
  const requestId = frame && frame.request_id;
  const url = frame && frame.args && frame.args.url;
  if (typeof url !== 'string' || !url.trim()) {
    return toolErr(requestId, 'bad tool args');
  }
  const trimmed = url.trim();
  if (trimmed !== 'about:blank' && !NAVIGABLE_URL_RE.test(trimmed)) {
    return toolErr(requestId, 'only http(s) URLs (or about:blank) can be navigated');
  }
  const { tab, closed } = await resolveToolTab(frame, frame.args);
  if (closed) return toolErr(requestId, 'conversation tab is closed');
  if (!tab) return toolErr(requestId, 'no active tab');
  const tabId = tab.id;
  // Listener registered BEFORE the update so the completion event of a
  // fast-loading page can never be missed.
  let listener;
  const loaded = new Promise((resolve) => {
    listener = (updatedId, changeInfo) => {
      if (updatedId !== tabId || !changeInfo || changeInfo.status !== 'complete') return;
      chrome.tabs.onUpdated.removeListener(listener);
      resolve(true);
    };
    chrome.tabs.onUpdated.addListener(listener);
    Promise.resolve(chrome.tabs.update(tabId, { url: trimmed })).catch(() => {});
  });
  try {
    await withTimeout(loaded, TOOL_TIMEOUT_MS, 'navigation timed out');
  } catch (err) {
    chrome.tabs.onUpdated.removeListener(listener);
    return toolErr(requestId, String((err && err.message) || err));
  }
  let final = null;
  try {
    final = await chrome.tabs.get(tabId);
  } catch {
    // Navigated away/closed right after load; report what we know.
  }
  const info = final && typeof final.id === 'number'
    ? final : { id: tabId, url: trimmed, title: null };
  return toolOk(requestId, JSON.stringify({
    tab_id: info.id, url: info.url || trimmed, title: info.title || null,
  }));
}

async function activateTab(frame) {
  const requestId = frame && frame.request_id;
  const { tab, closed, missing } = await resolveToolTab(
    frame, frame && frame.args, false);
  if (missing) return toolErr(requestId, 'bad tool args: tab_id is required');
  if (closed) return toolErr(requestId, 'conversation tab is closed');
  try {
    await chrome.tabs.update(tab.id, { active: true });
    if (typeof tab.windowId === 'number') {
      await chrome.windows.update(tab.windowId, { focused: true });
    }
  } catch (err) {
    return toolErr(requestId, String((err && err.message) || err));
  }
  let fresh = tab;
  try {
    fresh = await chrome.tabs.get(tab.id);
  } catch {
    // Closed between activate and read; report the pre-activation info.
  }
  return toolOk(requestId, JSON.stringify({
    tab_id: fresh.id, url: fresh.url || null, title: fresh.title || null,
  }));
}

// The page's CSP can block the MAIN-world eval entirely (e.g. GitHub). When
// the injection fails that way, rerun the code through the chrome.debugger
// protocol instead. While attached Chrome shows a "being debugged" infobar.
const CSP_EVAL_ERROR_RE = /EvalError|unsafe-eval|Content Security Policy/i;

async function runTabJsViaDebugger(tab, code) {
  const target = { tabId: tab.id };
  let attached = false;
  try {
    await chrome.debugger.attach(target, '1.3');
    attached = true;
    const out = await chrome.debugger.sendCommand(
      target, 'Runtime.evaluate',
      { expression: code, returnByValue: true, awaitPromise: true });
    if (out && out.exceptionDetails) {
      const details = out.exceptionDetails;
      const text = (details.exception && details.exception.description)
        || details.text || 'evaluation failed';
      return { ok: false, error: String(text) };
    }
    const value = out && out.result ? out.result.value : undefined;
    const encoded = value === undefined ? 'undefined' : JSON.stringify(value);
    return { ok: true, result: String(encoded).slice(0, TOOL_RESULT_MAX) };
  } finally {
    if (attached) {
      Promise.resolve(chrome.debugger.detach(target)).catch(() => {});
    }
  }
}

async function runTabJs(frame) {
  const code = frame && frame.args && frame.args.code;
  if (typeof code !== 'string') {
    return toolErr(frame && frame.request_id, 'bad tool args');
  }
  // Script the tab the conversation belongs to (args.tab_id, then
  // frame.tab_id), falling back to the focused tab for legacy frames.
  const { tab, closed } = await resolveToolTab(frame, frame.args);
  if (closed) return toolErr(frame.request_id, 'conversation tab is closed');
  if (!tab) return toolErr(frame.request_id, 'no active tab');

  // A rejected injection, an injection.error, or an error string returned by
  // the runner (it catches the eval error itself) all normalize to a failure;
  // a CSP-shaped one reruns through the chrome.debugger protocol.
  let injectionError = null;
  let encoded = null;
  try {
    const results = await withTimeout(
      chrome.scripting.executeScript({
        target: { tabId: tab.id },
        world: 'MAIN',
        func: injectedRunner,
        args: [code],
      }),
      TOOL_TIMEOUT_MS,
      'timeout',
    );
    const injection = Array.isArray(results) ? results[0] : null;
    if (!injection || injection.error) {
      injectionError = String((injection && injection.error) || 'no injection result');
    } else {
      const [errText, value] = Array.isArray(injection.result)
        ? injection.result : [null, null];
      if (errText !== null && errText !== undefined) injectionError = errText;
      else encoded = value;
    }
  } catch (err) {
    injectionError = String((err && err.message) || err);
  }

  if (injectionError !== null) {
    if (CSP_EVAL_ERROR_RE.test(injectionError)) {
      try {
        const out = await withTimeout(
          runTabJsViaDebugger(tab, code), TOOL_TIMEOUT_MS, 'timeout');
        return out.ok
          ? toolOk(frame.request_id, out.result)
          : toolErr(frame.request_id, out.error);
      } catch (err) {
        return toolErr(frame.request_id, String((err && err.message) || err));
      }
    }
    return toolErr(frame.request_id, injectionError);
  }
  if (typeof encoded !== 'string') {
    return toolErr(frame.request_id, 'no result');
  }
  return toolOk(frame.request_id, encoded.slice(0, TOOL_RESULT_MAX));
}

const TOOL_HANDLERS = {
  run_tab_js: runTabJs,
  navigate_tab: navigateTab,
  activate_tab: activateTab,
};

// ─── Keepalive + init ───────────────────────────────────────────────────────

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== KEEPALIVE_ALARM) return;
  if (fatalConnectError) return;
  if (!connected) connect();
});

function ensureKeepalive() {
  Promise.resolve(chrome.alarms.create(KEEPALIVE_ALARM, { delayInMinutes: 1, periodInMinutes: 1 })).catch(() => {});
}

// Runs on every worker start and wake: listeners above are already registered
// synchronously, so a restarted worker re-attaches and reconnects.
ensureKeepalive();
connect();
