// Fake chrome/worker environment for background.js tool wiring:
// - send_chat carries the caller-supplied tab id in the chat frame's tab.id
// - run_tab_js targets the tab the conversation belongs to (args.tab_id, then
//   frame.tab_id), not whichever tab happens to be focused; without one it
//   falls back to the focused tab (legacy behavior).
// - navigate_tab updates the url and replies only once that tab reports
//   onUpdated status 'complete', then removes its listener.
// - activate_tab activates the tab and focuses its window.
// - run_tab_js falls back to chrome.debugger (Runtime.evaluate) when CSP
//   blocks the MAIN-world eval; non-CSP errors must not touch the debugger.
'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');

const backgroundScript = process.argv[2];

const CSP_ERROR = 'EvalError: Refused to evaluate a string as JavaScript ' +
  "because 'unsafe-eval' is not an allowed source of script in the " +
  'following Content Security Policy directive';

function makeEvent() {
  return {
    listeners: [],
    addListener(fn) { this.listeners.push(fn); },
    removeListener(fn) {
      this.listeners = this.listeners.filter((f) => f !== fn);
    },
  };
}

const onMessageEvent = makeEvent();
const onUpdatedEvent = makeEvent();
const instances = [];
const sentFrames = [];
const executeScriptCalls = [];
const tabsUpdateCalls = [];
const windowsUpdateCalls = [];
const debuggerCalls = { attach: [], sendCommand: [], detach: [] };
// Per-scenario switches for the scripting injection and debugger behavior.
let executeScriptMode = 'ok';
let debuggerMode = 'ok';

const tabsById = new Map([
  [11, { id: 11, url: 'https://a.example/', title: 'A', windowId: 7 }],
  [42, { id: 42, url: 'https://b.example/', title: 'B', windowId: 7 }],
  // The "focused" tab per the last-focused-window query: NOT 42.
  [999, { id: 999, url: 'https://focused.example/', title: 'Focused', windowId: 7 }],
]);

class FakeWebSocket {
  constructor(url, protocols) {
    this.url = url;
    this.protocols = protocols;
    this.readyState = FakeWebSocket.CONNECTING;
    instances.push(this);
  }
  send(raw) { sentFrames.push(JSON.parse(raw)); }
  close() {
    this.readyState = FakeWebSocket.CLOSED;
    if (this.onclose) this.onclose({ reason: 'closed by test' });
  }
}
FakeWebSocket.CONNECTING = 0;
FakeWebSocket.OPEN = 1;
FakeWebSocket.CLOSING = 2;
FakeWebSocket.CLOSED = 3;
global.WebSocket = FakeWebSocket;

function emitUpdated(tabId, changeInfo, tab) {
  for (const fn of onUpdatedEvent.listeners.slice()) fn(tabId, changeInfo, tab);
}

global.chrome = {
  action: { onClicked: makeEvent() },
  alarms: { onAlarm: makeEvent(), create() { return undefined; } },
  commands: { onCommand: makeEvent() },
  runtime: {
    onConnect: makeEvent(),
    onMessage: onMessageEvent,
    sendMessage() { return Promise.resolve(); },
    getManifest: () => ({ version: 'test' }),
  },
  storage: {
    onChanged: makeEvent(),
    sync: { async get() { return { token: 'test-token' }; } },
  },
  windows: {
    onFocusChanged: makeEvent(),
    getLastFocused: () => Promise.resolve({ id: 7 }),
    get: () => Promise.resolve({ type: 'normal' }),
    update(windowId, props) {
      windowsUpdateCalls.push({ windowId, props });
      return Promise.resolve({ id: windowId, focused: true });
    },
  },
  tabs: {
    onUpdated: onUpdatedEvent,
    get: (tabId) => (tabsById.has(tabId)
      ? Promise.resolve(tabsById.get(tabId))
      : Promise.reject(new Error('No tab with id ' + tabId))),
    query: () => Promise.resolve([tabsById.get(999)]),
    update(tabId, props) {
      tabsUpdateCalls.push({ tabId, props });
      const tab = tabsById.get(tabId);
      if (!tab) return Promise.reject(new Error('No tab with id ' + tabId));
      if (typeof props.url === 'string') tab.url = props.url;
      if (props.active === true) tab.active = true;
      // Loading fires on a microtask; 'complete' (with the new title) only
      // after a macrotask, so the harness can observe the wait in between.
      Promise.resolve().then(() => emitUpdated(tabId, { status: 'loading' }, tab));
      if (typeof props.url === 'string') {
        setTimeout(() => {
          tab.title = 'Loaded';
          emitUpdated(tabId, { status: 'complete' }, tab);
        }, 30);
      }
      return Promise.resolve(tab);
    },
  },
  scripting: {
    executeScript(options) {
      executeScriptCalls.push(options);
      if (executeScriptMode === 'reject-csp') return Promise.reject(new Error(CSP_ERROR));
      if (executeScriptMode === 'reject-other') {
        return Promise.reject(new Error('Cannot access a chrome:// URL'));
      }
      if (executeScriptMode === 'return-csp') {
        // The injected runner CATCHES the eval error and returns it as the
        // error string of its [err, value] pair.
        return Promise.resolve([{ result: [CSP_ERROR, null] }]);
      }
      return Promise.resolve([{ result: [null, '"ok"'] }]);
    },
  },
  debugger: {
    attach(target, version) {
      debuggerCalls.attach.push({ target, version });
      if (debuggerMode === 'attach-fails') return Promise.reject(new Error('cannot attach'));
      return Promise.resolve();
    },
    sendCommand(target, method, params) {
      debuggerCalls.sendCommand.push({ target, method, params });
      if (debuggerMode === 'eval-throws') return Promise.reject(new Error('Inspector error'));
      if (debuggerMode === 'exception') {
        return Promise.resolve({
          exceptionDetails: {
            text: 'Uncaught',
            exception: { description: 'ReferenceError: nope is not defined' },
          },
        });
      }
      return Promise.resolve({ result: { value: 42 } });
    },
    detach(target) {
      debuggerCalls.detach.push(target);
      return Promise.resolve();
    },
  },
};

async function flush(rounds = 10) {
  for (let i = 0; i < rounds; i += 1) await Promise.resolve();
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function sendChatViaListener(text, tabId) {
  const response = await new Promise((resolve) => {
    onMessageEvent.listeners[0]({ kind: 'send_chat', text, tabId }, null, resolve);
  });
  await flush();
  return response;
}

function wireFrame(frame) {
  const sock = instances[instances.length - 1];
  sock.readyState = FakeWebSocket.OPEN;
  sock.onmessage({ data: JSON.stringify(frame) });
}

function toolFrame(name, args, requestId, tabId) {
  return tabId === undefined
    ? { type: 'tool', name, args, request_id: requestId }
    : { type: 'tool', name, args, request_id: requestId, tab_id: tabId };
}

function resultFor(requestId) {
  return sentFrames.find(
    (f) => f.type === 'tool_result' && f.request_id === requestId);
}

function resetScenario() {
  executeScriptMode = 'ok';
  debuggerMode = 'ok';
  sentFrames.length = 0;
  executeScriptCalls.length = 0;
  tabsUpdateCalls.length = 0;
  windowsUpdateCalls.length = 0;
  debuggerCalls.attach.length = 0;
  debuggerCalls.sendCommand.length = 0;
  debuggerCalls.detach.length = 0;
}

async function main() {
  require(backgroundScript);
  await flush();

  // The worker connects with the stored token and opens the socket.
  const sock = instances[instances.length - 1];
  assert.ok(sock, 'worker must open a WebSocket');
  sock.readyState = FakeWebSocket.OPEN;
  sock.onopen();
  await flush();
  assert.ok(sentFrames.some((f) => f.type === 'hello'), 'hello is sent on open');

  // 1. A sidepanel send for tab 42 stamps tab.id=42 on the chat frame.
  sentFrames.length = 0;
  const response = await sendChatViaListener('look at this page', 42);
  assert.ok(response && response.ok, 'send_chat must succeed');
  const chat = sentFrames.find((f) => f.type === 'chat');
  assert.ok(chat, 'chat frame was sent');
  assert.equal(chat.tab.id, 42, 'chat frame carries the sending tab id');
  assert.equal(chat.tab.url, 'https://b.example/');
  assert.equal(chat.tab.title, 'B');

  // 2. run_tab_js with tab_id targets THAT tab even though another is focused.
  sentFrames.length = 0;
  executeScriptCalls.length = 0;
  wireFrame(toolFrame('run_tab_js', { code: 'document.title' }, 'req-42', 42));
  await flush();
  assert.equal(executeScriptCalls.length, 1, 'one scripting injection');
  assert.deepEqual(executeScriptCalls[0].target, { tabId: 42 },
    'run_tab_js must script the conversation tab, not the focused tab');
  assert.equal(executeScriptCalls[0].world, 'MAIN');
  const result42 = resultFor('req-42');
  assert.ok(result42 && result42.ok, 'tool result flows back');

  // 3. A tool frame without tab_id (legacy server) falls back to the focused tab.
  executeScriptCalls.length = 0;
  wireFrame(toolFrame('run_tab_js', { code: '1' }, 'req-legacy'));
  await flush();
  assert.deepEqual(executeScriptCalls[0].target, { tabId: 999 },
    'legacy tool frames target the focused tab');

  // 4. A tool frame for a closed tab fails with a clear error, not a wrong tab.
  executeScriptCalls.length = 0;
  sentFrames.length = 0;
  wireFrame(toolFrame('run_tab_js', { code: '1' }, 'req-gone', 4242));
  await flush();
  assert.equal(executeScriptCalls.length, 0, 'closed tab must not be scripted');
  const resultGone = resultFor('req-gone');
  assert.ok(resultGone && resultGone.ok === false, 'closed tab yields an error result');
  assert.match(resultGone.error, /closed/i);

  // 5. args.tab_id takes precedence over frame.tab_id.
  executeScriptCalls.length = 0;
  wireFrame(toolFrame('run_tab_js', { code: '1', tab_id: 11 }, 'req-override', 42));
  await flush();
  assert.deepEqual(executeScriptCalls[0].target, { tabId: 11 },
    'args.tab_id must win over frame.tab_id');

  // (a) navigate_tab: updates the url, replies only after 'complete', no page JS.
  resetScenario();
  wireFrame(toolFrame('navigate_tab', { url: 'https://settings.example/' }, 'nav-1', 42));
  await flush();
  assert.deepEqual(tabsUpdateCalls, [{ tabId: 42, props: { url: 'https://settings.example/' } }],
    'navigate_tab must update the resolved tab');
  assert.equal(executeScriptCalls.length, 0, 'navigate_tab runs no page JS');
  // 'loading' has fired here but 'complete' has not: no reply yet.
  assert.equal(resultFor('nav-1'), undefined,
    'no tool_result before the tab reports complete');
  await sleep(100);
  const navResult = resultFor('nav-1');
  assert.ok(navResult && navResult.ok, 'navigate_tab replies after complete');
  const navInfo = JSON.parse(navResult.result);
  assert.equal(navInfo.tab_id, 42);
  assert.equal(navInfo.url, 'https://settings.example/');
  assert.equal(navInfo.title, 'Loaded');
  assert.equal(onUpdatedEvent.listeners.length, 0,
    'navigate_tab must remove its onUpdated listener');
  // args.tab_id overrides frame.tab_id on navigate too; about:blank is allowed;
  // non-http(s) schemes are refused without touching tabs.update.
  wireFrame(toolFrame('navigate_tab', { url: 'about:blank', tab_id: 11 }, 'nav-2', 42));
  await sleep(100);
  assert.ok(resultFor('nav-2').ok, 'about:blank is navigable');
  assert.equal(JSON.parse(resultFor('nav-2').result).tab_id, 11,
    'args.tab_id wins over frame.tab_id for navigate_tab');
  wireFrame(toolFrame('navigate_tab', { url: 'javascript:alert(1)' }, 'nav-bad', 42));
  await flush();
  await sleep(100);
  const navBad = resultFor('nav-bad');
  assert.ok(navBad && navBad.ok === false, 'non-http(s) urls are refused');
  assert.equal(tabsUpdateCalls.length, 2, 'refused url never reaches tabs.update');
  assert.equal(resultFor('nav-1').ok, true); // listeners of nav-1 still removed

  // (b) activate_tab activates the tab and focuses its window.
  resetScenario();
  wireFrame(toolFrame('activate_tab', { tab_id: 11 }, 'act-1'));
  await flush();
  assert.deepEqual(
    tabsUpdateCalls.find((c) => c.tabId === 11),
    { tabId: 11, props: { active: true } }, 'activate_tab must activate the tab');
  assert.deepEqual(
    windowsUpdateCalls.find((c) => c.windowId === 11 && c.props.focused === true) ||
    windowsUpdateCalls.find((c) => c.props.focused === true),
    { windowId: 7, props: { focused: true } },
    'activate_tab must focus the tab window');
  const actResult = resultFor('act-1');
  assert.ok(actResult && actResult.ok, 'activate_tab replies ok');
  assert.equal(JSON.parse(actResult.result).tab_id, 11);
  wireFrame(toolFrame('activate_tab', { tab_id: 7777 }, 'act-bad'));
  await flush();
  const actBad = resultFor('act-bad');
  assert.ok(actBad && actBad.ok === false, 'unknown tab gives ok:false');

  // (g) An unknown tool name fails without throwing.
  resetScenario();
  wireFrame(toolFrame('no_such_tool', {}, 'unk-1'));
  await flush();
  const unkResult = resultFor('unk-1');
  assert.ok(unkResult && unkResult.ok === false, 'unknown tool gives ok:false');
  assert.match(unkResult.error, /unknown tool/);

  // (c) executeScript rejects with the CSP error: the debugger path is used.
  resetScenario();
  executeScriptMode = 'reject-csp';
  wireFrame(toolFrame('run_tab_js', { code: '2+3' }, 'csp-1', 11));
  await flush();
  await sleep(20);
  assert.deepEqual(debuggerCalls.attach,
    [{ target: { tabId: 11 }, version: '1.3' }], 'attach with protocol 1.3');
  assert.equal(debuggerCalls.sendCommand.length, 1);
  assert.equal(debuggerCalls.sendCommand[0].method, 'Runtime.evaluate');
  assert.deepEqual(debuggerCalls.sendCommand[0].params, {
    expression: '2+3', returnByValue: true, awaitPromise: true,
  });
  assert.deepEqual(debuggerCalls.detach, [{ tabId: 11 }], 'detach after evaluate');
  const cspResult = resultFor('csp-1');
  assert.ok(cspResult && cspResult.ok, 'debugger path returns the value');
  assert.equal(cspResult.result, '42');
  // args.tab_id wins on the debugger path too.
  resetScenario();
  executeScriptMode = 'reject-csp';
  wireFrame(toolFrame('run_tab_js', { code: '2+3', tab_id: 42 }, 'csp-1b', 11));
  await flush();
  await sleep(20);
  assert.deepEqual(debuggerCalls.attach, [{ target: { tabId: 42 }, version: '1.3' }]);
  assert.ok(resultFor('csp-1b').ok);

  // (d) The runner RETURNS the CSP error string: the fallback is still used.
  resetScenario();
  executeScriptMode = 'return-csp';
  wireFrame(toolFrame('run_tab_js', { code: 'eval("x")' }, 'csp-2', 11));
  await flush();
  await sleep(20);
  assert.equal(debuggerCalls.attach.length, 1,
    'a returned CSP error string must also fall back');
  const csp2 = resultFor('csp-2');
  assert.ok(csp2 && csp2.ok, 'debugger path returns the value');
  assert.equal(csp2.result, '42');

  // (e) Runtime.evaluate fails (rejects or exceptionDetails): ok:false, detached.
  resetScenario();
  executeScriptMode = 'reject-csp';
  debuggerMode = 'eval-throws';
  wireFrame(toolFrame('run_tab_js', { code: '1' }, 'csp-3', 11));
  await flush();
  await sleep(20);
  const csp3 = resultFor('csp-3');
  assert.ok(csp3 && csp3.ok === false, 'debugger rejection fails the tool');
  assert.deepEqual(debuggerCalls.detach, [{ tabId: 11 }], 'detach still called');
  resetScenario();
  executeScriptMode = 'reject-csp';
  debuggerMode = 'exception';
  wireFrame(toolFrame('run_tab_js', { code: 'nope' }, 'csp-4', 11));
  await flush();
  await sleep(20);
  const csp4 = resultFor('csp-4');
  assert.ok(csp4 && csp4.ok === false, 'exceptionDetails fails the tool');
  assert.match(csp4.error, /ReferenceError|Uncaught/);
  assert.deepEqual(debuggerCalls.detach, [{ tabId: 11 }], 'detach still called');

  // (f) A non-CSP scripting error must NOT touch the debugger.
  resetScenario();
  executeScriptMode = 'reject-other';
  wireFrame(toolFrame('run_tab_js', { code: '1' }, 'other-1', 11));
  await flush();
  await sleep(20);
  const other1 = resultFor('other-1');
  assert.ok(other1 && other1.ok === false, 'non-CSP error still fails the tool');
  assert.match(other1.error, /chrome:\/\//);
  assert.equal(debuggerCalls.attach.length, 0, 'debugger must not be used');
  assert.equal(debuggerCalls.detach.length, 0);

  console.log('OK');
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
