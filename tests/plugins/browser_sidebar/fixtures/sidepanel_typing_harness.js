// Fake DOM/chrome for sidepanel.js: exercises the typing-indicator row — shown
// on {type:'typing', active:true} or right after a successful send, hidden by
// a delta/final/error/active:false, and safety-timed out after 10s unless a
// fresh active:true frame refreshes it.
'use strict';

const assert = require('node:assert/strict');

const sidepanelScript = process.argv[2];

// ── Fake timers (sidepanel.js's only use of setTimeout is the typing safety
// timeout, so a tiny manual clock is enough to drive it deterministically). ──
let now = 0;
let nextTimerId = 1;
const timers = new Map();

global.setTimeout = (fn, ms) => {
  const id = nextTimerId++;
  timers.set(id, { fn, at: now + (ms || 0) });
  return id;
};
global.clearTimeout = (id) => {
  timers.delete(id);
};

function advance(ms) {
  now += ms;
  for (const [id, timer] of [...timers.entries()]) {
    if (timer.at <= now) {
      timers.delete(id);
      timer.fn();
    }
  }
}

// ── Fake DOM ──────────────────────────────────────────────────────────────
class FakeElement {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.parent = null;
    this._classes = new Set();
    this._text = '';
    this.value = '';
    this.hidden = false;
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.listeners = {};
    const self = this;
    this.classList = {
      add(c) { self._classes.add(c); },
      remove(c) { self._classes.delete(c); },
      contains(c) { return self._classes.has(c); },
    };
  }
  get className() { return [...this._classes].join(' '); }
  set className(value) { this._classes = new Set(String(value).split(' ').filter(Boolean)); }
  get textContent() { return this._text; }
  set textContent(value) { this._text = String(value); }
  get isConnected() {
    let node = this;
    while (node) {
      if (node._root) return true;
      node = node.parent;
    }
    return false;
  }
  append(...nodes) {
    for (const node of nodes) {
      node.parent = this;
      this.children.push(node);
    }
  }
  remove() {
    if (this.parent) {
      this.parent.children = this.parent.children.filter((c) => c !== this);
      this.parent = null;
    }
  }
  addEventListener(type, handler) {
    (this.listeners[type] = this.listeners[type] || []).push(handler);
  }
}

const bannerEl = new FakeElement('div');
const transcriptEl = new FakeElement('div');
transcriptEl._root = true; // everything appended under it counts as "connected"
const inputEl = new FakeElement('textarea');

global.document = {
  getElementById(id) {
    return { banner: bannerEl, transcript: transcriptEl, input: inputEl }[id];
  },
  createElement(tag) { return new FakeElement(tag); },
};

// ── Fake chrome ───────────────────────────────────────────────────────────
const onMessageListeners = [];

global.chrome = {
  runtime: {
    onMessage: { addListener: (fn) => onMessageListeners.push(fn) },
    sendMessage(msg) {
      if (msg && msg.kind === 'send_chat') return Promise.resolve({ ok: true, id: 'req' });
      if (msg && msg.kind === 'get_status') return Promise.resolve({ connected: true });
      return Promise.resolve({});
    },
    connect() { return { onMessage: { addListener() {} } }; },
  },
  storage: {
    local: {
      get: () => Promise.resolve({}),
      set: () => Promise.resolve(),
    },
  },
  windows: {
    getCurrent: () => Promise.resolve({ id: 1 }),
  },
};

function deliver(frame) {
  for (const fn of onMessageListeners) fn({ kind: 'frame', frame });
}

function typingRowVisible() {
  return transcriptEl.children.some((c) => c.isConnected && c.classList.contains('typing'));
}

async function flush() {
  for (let i = 0; i < 5; i += 1) await Promise.resolve();
}

async function main() {
  require(sidepanelScript);
  await flush();

  // active:true shows the row.
  deliver({ type: 'typing', active: true });
  assert.equal(typingRowVisible(), true, 'active:true should show the typing row');

  // The first delta hides it (the streaming bubble signals activity instead).
  deliver({ type: 'delta', text: 'Hel' });
  assert.equal(typingRowVisible(), false, 'a delta should hide the typing row');

  // A further active:true while streaming is a no-op (the bubble already shows activity).
  deliver({ type: 'typing', active: true });
  assert.equal(typingRowVisible(), false, 'active:true must not show dots while a stream is live');

  // final closes out the turn and hides it (also from a fresh, non-streaming state below).
  deliver({ type: 'final', text: 'Hello' });
  assert.equal(typingRowVisible(), false, 'final should hide the typing row');

  // active:false hides a shown row.
  deliver({ type: 'typing', active: true });
  assert.equal(typingRowVisible(), true, 'active:true should show the typing row again');
  deliver({ type: 'typing', active: false });
  assert.equal(typingRowVisible(), false, 'active:false should hide the typing row');

  // final hides it even without a preceding delta.
  deliver({ type: 'typing', active: true });
  assert.equal(typingRowVisible(), true);
  deliver({ type: 'final', text: 'done' });
  assert.equal(typingRowVisible(), false, 'final without a delta should still hide the typing row');

  // error hides it too.
  deliver({ type: 'typing', active: true });
  assert.equal(typingRowVisible(), true);
  deliver({ type: 'error', message: 'boom' });
  assert.equal(typingRowVisible(), false, 'error should hide the typing row');

  // 10s safety timeout hides it with no further frames.
  deliver({ type: 'typing', active: true });
  assert.equal(typingRowVisible(), true);
  advance(9999);
  assert.equal(typingRowVisible(), true, 'should still be visible just under 10s');
  advance(1);
  assert.equal(typingRowVisible(), false, 'should hide once the 10s safety timeout elapses');

  // A refreshing active:true resets the 10s window.
  deliver({ type: 'typing', active: true });
  advance(8000);
  deliver({ type: 'typing', active: true }); // refresh before the first window would have elapsed
  advance(8000); // 16s since the first frame, but only 8s since the refresh
  assert.equal(typingRowVisible(), true, 'a refreshing active:true should extend the timeout');
  advance(2001); // 10s+ since the refresh
  assert.equal(typingRowVisible(), false, 'should hide 10s after the last refreshing frame');

  // A successful send optimistically shows the row before any server frame arrives.
  inputEl.value = 'hi there';
  const onKeydown = inputEl.listeners.keydown[0];
  onKeydown({ key: 'Enter', shiftKey: false, isComposing: false, preventDefault() {} });
  await flush();
  assert.equal(typingRowVisible(), true, 'a successful send should optimistically show the typing row');

  console.log('OK');
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
