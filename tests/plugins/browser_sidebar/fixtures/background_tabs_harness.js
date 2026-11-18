// Fake chrome/worker environment for background.js per-tab wiring:
// - send_chat carries the caller-supplied tab id in the chat frame's tab.id
// - run_tab_js targets the tab the conversation belongs to (frame.tab_id),
//   not whichever tab happens to be focused; without a tab_id it falls back
//   to the focused tab (legacy behavior).
'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');

const backgroundScript = process.argv[2];

function makeEvent() {
  return { listeners: [], addListener(fn) { this.listeners.push(fn); } };
}

const onMessageEvent = makeEvent();
const instances = [];
const sentFrames = [];
const executeScriptCalls = [];
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
  },
  tabs: {
    get: (tabId) => (tabsById.has(tabId)
      ? Promise.resolve(tabsById.get(tabId))
      : Promise.reject(new Error('No tab with id ' + tabId))),
    query: () => Promise.resolve([tabsById.get(999)]),
  },
  scripting: {
    executeScript(options) {
      executeScriptCalls.push(options);
      return Promise.resolve([{ result: [null, '"ok"'] }]);
    },
  },
};

async function flush(rounds = 10) {
  for (let i = 0; i < rounds; i += 1) await Promise.resolve();
}

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
  wireFrame({
    type: 'tool', name: 'run_tab_js',
    args: { code: 'document.title' }, request_id: 'req-42', tab_id: 42,
  });
  await flush();
  assert.equal(executeScriptCalls.length, 1, 'one scripting injection');
  assert.deepEqual(executeScriptCalls[0].target, { tabId: 42 },
    'run_tab_js must script the conversation tab, not the focused tab');
  assert.equal(executeScriptCalls[0].world, 'MAIN');
  const result42 = sentFrames.find((f) => f.type === 'tool_result');
  assert.ok(result42 && result42.ok, 'tool result flows back');

  // 3. A tool frame without tab_id (legacy server) falls back to the focused tab.
  executeScriptCalls.length = 0;
  wireFrame({
    type: 'tool', name: 'run_tab_js',
    args: { code: '1' }, request_id: 'req-legacy',
  });
  await flush();
  assert.deepEqual(executeScriptCalls[0].target, { tabId: 999 },
    'legacy tool frames target the focused tab');

  // 4. A tool frame for a closed tab fails with a clear error, not a wrong tab.
  executeScriptCalls.length = 0;
  sentFrames.length = 0;
  wireFrame({
    type: 'tool', name: 'run_tab_js',
    args: { code: '1' }, request_id: 'req-gone', tab_id: 4242,
  });
  await flush();
  assert.equal(executeScriptCalls.length, 0, 'closed tab must not be scripted');
  const resultGone = sentFrames.find((f) => f.type === 'tool_result');
  assert.ok(resultGone && resultGone.ok === false, 'closed tab yields an error result');
  assert.match(resultGone.error, /closed/i);

  console.log('OK');
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
