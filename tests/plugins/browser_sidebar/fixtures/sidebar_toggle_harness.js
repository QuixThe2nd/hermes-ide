const assert = require('node:assert/strict');

const backgroundScript = process.argv[2];

function makeEvent() {
  const listeners = [];
  return { addListener(fn) { listeners.push(fn); }, listeners };
}

function makePort(name) {
  const messages = [];
  const disconnectListeners = [];
  return {
    name,
    postMessage(msg) { messages.push(msg); },
    onDisconnect: { addListener(fn) { disconnectListeners.push(fn); } },
    messages,
    disconnect() { for (const fn of disconnectListeners) fn(); },
  };
}

const actionEvent = makeEvent();
const alarmsEvent = makeEvent();
const commandsEvent = makeEvent();
const connectEvent = makeEvent();
const onMessageEvent = makeEvent();
const storageChangedEvent = makeEvent();
const focusChangedEvent = makeEvent();

const openCalls = [];
const closeCalls = [];

global.chrome = {
  action: { onClicked: actionEvent },
  alarms: {
    onAlarm: alarmsEvent,
    create() { return undefined; },
  },
  commands: { onCommand: commandsEvent },
  runtime: {
    onConnect: connectEvent,
    onMessage: onMessageEvent,
    sendMessage() { return Promise.resolve(); },
  },
  storage: {
    onChanged: storageChangedEvent,
    sync: { async get() { return {}; } },
  },
  windows: {
    onFocusChanged: focusChangedEvent,
    getLastFocused() { return Promise.resolve({ id: 1 }); },
    get() { return Promise.resolve({ type: 'normal' }); },
  },
  sidePanel: {
    open(opts) { openCalls.push(opts); return Promise.resolve(); },
    close(opts) { closeCalls.push(opts); return undefined; },
  },
};

require(backgroundScript);

assert.equal(commandsEvent.listeners.length, 1);
assert.equal(connectEvent.listeners.length, 1);
const onCommand = commandsEvent.listeners[0];
const onConnect = connectEvent.listeners[0];

// a. No panel connected for window 7: the command opens it synchronously.
onCommand('open-sidebar', { windowId: 7 });
assert.deepEqual(openCalls, [{ windowId: 7 }]);
assert.deepEqual(closeCalls, []);

// A side panel page connects for window 7.
const port7 = makePort('sidepanel:7');
onConnect(port7);

// b. The command now closes it via chrome.sidePanel.close.
onCommand('open-sidebar', { windowId: 7 });
assert.deepEqual(openCalls, [{ windowId: 7 }]);
assert.deepEqual(closeCalls, [{ windowId: 7 }]);
assert.deepEqual(port7.messages, []);

// c. Without chrome.sidePanel.close, the fallback posts {kind:'close'} on the port.
delete chrome.sidePanel.close;
onCommand('open-sidebar', { windowId: 7 });
assert.deepEqual(openCalls, [{ windowId: 7 }]);
assert.deepEqual(closeCalls, [{ windowId: 7 }]);
assert.deepEqual(port7.messages, [{ kind: 'close' }]);

// d. Once that port disconnects, the command opens the panel again.
port7.disconnect();
onCommand('open-sidebar', { windowId: 7 });
assert.deepEqual(openCalls, [{ windowId: 7 }, { windowId: 7 }]);

// e. A port with an unrelated name is ignored and does not affect window 7.
const other = makePort('other');
onConnect(other);
onCommand('open-sidebar', { windowId: 7 });
assert.deepEqual(openCalls, [{ windowId: 7 }, { windowId: 7 }, { windowId: 7 }]);
assert.deepEqual(other.messages, []);

console.log('OK');
