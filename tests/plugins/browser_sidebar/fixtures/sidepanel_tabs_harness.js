// Fake DOM/chrome for sidepanel.js per-tab session logic: transcripts are
// kept per tab (chrome.storage.session keyed by tab id), switching tabs
// re-renders that tab's history, frames for non-active tabs are stored not
// rendered, legacy frames (no tab_id) land on the active tab, the new-chat
// button resets only the current tab, closing a tab drops its transcript,
// re-activating a tab never duplicates its transcript (stored history
// hydrates once per bucket), entries landing during the initial load stay
// after the stored history, and a cleared (new-chat) transcript never
// resurrects.
'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');

const sidepanelScript = process.argv[2];
const tabsScript = path.join(path.resolve(path.dirname(sidepanelScript)), 'tabs.js');

// ── Fake timers (typing safety timeout) ────────────────────────────────────
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

// ── Fake DOM ───────────────────────────────────────────────────────────────
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
  get firstChild() { return this.children[0] || null; }
  append(...nodes) {
    for (const node of nodes) {
      node.parent = this;
      this.children.push(node);
    }
  }
  removeChild(node) {
    this.children = this.children.filter((c) => c !== node);
    node.parent = null;
    return node;
  }
  remove() {
    if (this.parent) this.parent.removeChild(this);
  }
  addEventListener(type, handler) {
    (this.listeners[type] = this.listeners[type] || []).push(handler);
  }
}

const bannerEl = new FakeElement('div');
const transcriptEl = new FakeElement('div');
transcriptEl._root = true;
const inputEl = new FakeElement('textarea');
const newChatEl = new FakeElement('button');

global.document = {
  getElementById(id) {
    return {
      banner: bannerEl, transcript: transcriptEl,
      input: inputEl, 'new-chat': newChatEl,
    }[id];
  },
  createElement(tag) { return new FakeElement(tag); },
};

// ── Fake chrome ────────────────────────────────────────────────────────────
const onMessageListeners = [];
const sentMessages = [];
const activatedEvent = { listeners: [], addListener(fn) { this.listeners.push(fn); } };
const removedEvent = { listeners: [], addListener(fn) { this.listeners.push(fn); } };
const updatedEvent = { listeners: [], addListener(fn) { this.listeners.push(fn); } };
const focusChangedEvent = { listeners: [], addListener(fn) { this.listeners.push(fn); } };
const sessionStore = new Map();
const localStore = new Map();

function storageArea(store) {
  return {
    get(keys) {
      const list = Array.isArray(keys) ? keys : [keys];
      const out = {};
      for (const key of list) if (store.has(key)) out[key] = store.get(key);
      return Promise.resolve(out);
    },
    set(items) {
      for (const [key, value] of Object.entries(items)) store.set(key, value);
      return Promise.resolve();
    },
    remove(keys) {
      for (const key of Array.isArray(keys) ? keys : [keys]) store.delete(key);
      return Promise.resolve();
    },
  };
}

global.chrome = {
  runtime: {
    onMessage: { addListener: (fn) => onMessageListeners.push(fn) },
    sendMessage(msg) {
      sentMessages.push(msg);
      return Promise.resolve({ ok: true, id: 'req' });
    },
    connect() { return { onMessage: { addListener() {} } }; },
  },
  storage: {
    local: storageArea(localStore),
    session: storageArea(sessionStore),
  },
  windows: {
    getCurrent: () => Promise.resolve({ id: 7 }),
    onFocusChanged: focusChangedEvent,
  },
  tabs: {
    onActivated: activatedEvent,
    onRemoved: removedEvent,
    onUpdated: updatedEvent,
    query(options) {
      assert.equal(options.windowId, 7, 'tab queries must be scoped to the panel window');
      return Promise.resolve([{ id: 11, active: true, windowId: 7 }]);
    },
  },
};

function fire(event, ...payload) {
  for (const fn of event.listeners) fn(...payload);
}

function deliver(frame) {
  for (const fn of onMessageListeners) fn({ kind: 'frame', frame });
}

function domTexts() {
  const texts = [];
  const walk = (node) => {
    for (const child of node.children) {
      if (child._text) texts.push(child._text);
      walk(child);
    }
  };
  walk(transcriptEl);
  return texts;
}

async function flush(rounds = 8) {
  for (let i = 0; i < rounds; i += 1) await Promise.resolve();
}

// Bubble "who" labels (sidepanel.js WHO_LABEL) also carry text content;
// strip them so entry assertions see only message bodies.
const WHO_LABELS = new Set(['You', 'Big Steve', 'Error']);
const entryTexts = () => domTexts().filter((t) => !WHO_LABELS.has(t));
const occurrences = (list, value) => list.filter((t) => t === value).length;
const storedTexts = (tabId) =>
  (sessionStore.get('tabTranscript:' + tabId) || []).map((e) => e && e.text);

async function main() {
  require(tabsScript);
  require(sidepanelScript);
  await flush();

  // The panel resolves its window and the currently active tab (tab 11).
  fire(activatedEvent, { tabId: 11, windowId: 7 });
  await flush();

  // A user message goes out tagged with the active tab.
  inputEl.value = 'hello tab 11';
  const onKeydown = inputEl.listeners.keydown[0];
  onKeydown({ key: 'Enter', shiftKey: false, isComposing: false, preventDefault() {} });
  await flush();
  assert.deepEqual(
    sentMessages[sentMessages.length - 1],
    { kind: 'send_chat', text: 'hello tab 11', tabId: 11 },
    'send must carry the active tab id',
  );
  assert.ok(domTexts().includes('hello tab 11'), 'own message renders');

  // Streaming reply for tab 11 renders live.
  deliver({ type: 'typing', active: true, tab_id: 11 });
  deliver({ type: 'delta', text: 'answer ', tab_id: 11 });
  deliver({ type: 'delta', text: 'part 2', tab_id: 11 });
  await flush();
  assert.ok(domTexts().includes('answer part 2'), 'deltas accumulate');
  deliver({ type: 'final', text: 'full answer', tab_id: 11 });
  await flush();
  assert.ok(domTexts().includes('full answer'), 'final replaces the draft');

  // Frames for a background tab are stored, never rendered.
  deliver({ type: 'delta', text: 'bg ', tab_id: 42 });
  deliver({ type: 'delta', text: 'draft', tab_id: 42 });
  deliver({ type: 'final', text: 'bg final', tab_id: 42 });
  await flush();
  assert.ok(!domTexts().some((t) => t.includes('bg')), 'background tab frames must not render');
  assert.ok(
    sessionStore.get('tabTranscript:42').some((e) => e.text === 'bg final'),
    'background tab transcript persists',
  );

  // Switching tabs re-renders that tab's history without new extension traffic.
  const sentBeforeSwitch = sentMessages.length;
  fire(activatedEvent, { tabId: 42, windowId: 7 });
  await flush();
  assert.ok(domTexts().includes('bg final'), 'switch shows the background tab history');
  assert.ok(!domTexts().includes('full answer'), "other tab's history is not shown");
  assert.equal(sentMessages.length, sentBeforeSwitch, 'tab switches issue no extension messages');

  // Switching back restores tab 11's earlier conversation.
  fire(activatedEvent, { tabId: 11, windowId: 7 });
  await flush();
  assert.ok(domTexts().includes('hello tab 11') && domTexts().includes('full answer'),
    'switching back restores the earlier conversation');
  assert.ok(!domTexts().includes('bg final'));

  // Legacy frames (older server, no tab_id) land on the active tab.
  deliver({ type: 'error', message: 'legacy error' });
  await flush();
  assert.ok(domTexts().includes('legacy error'), 'legacy frames render on the active tab');

  // New chat resets ONLY the current tab's conversation.
  fire(activatedEvent, { tabId: 42, windowId: 7 });
  await flush();
  const onClick = newChatEl.listeners.click[0];
  onClick();
  await flush();
  assert.deepEqual(
    sentMessages[sentMessages.length - 1],
    { kind: 'send_chat', text: '/new', tabId: 42 },
    'new chat issues /new for the current tab only',
  );
  assert.equal(domTexts().length, 0, 'current tab transcript clears');
  fire(activatedEvent, { tabId: 11, windowId: 7 });
  await flush();
  assert.ok(domTexts().includes('full answer'), 'other tabs keep their conversations');

  // Closing a tab drops its local transcript.
  fire(removedEvent, 11, { windowId: 7 });
  await flush();
  assert.ok(!sessionStore.has('tabTranscript:11'), 'closed tab transcript is dropped');

  // ── Regression: re-activating a tab must not duplicate its transcript ────
  // loadBucket used to re-merge the stored history into the already-hydrated
  // bucket on every activation, compounding one extra copy per return visit
  // (and persisting the doubled list on the next save).
  fire(activatedEvent, { tabId: 99, windowId: 7 });
  await flush();
  inputEl.value = 'tab 99 message';
  inputEl.listeners.keydown[0]({ key: 'Enter', shiftKey: false, isComposing: false, preventDefault() {} });
  deliver({ type: 'final', text: 'tab 99 reply', tab_id: 99 });
  await flush();
  for (let round = 0; round < 3; round += 1) {
    fire(activatedEvent, { tabId: 88, windowId: 7 });
    await flush();
    fire(activatedEvent, { tabId: 99, windowId: 7 });
    await flush();
  }
  deliver({ type: 'final', text: 'tab 99 later reply', tab_id: 99 }); // forces the next save
  await flush();
  for (const text of ['tab 99 message', 'tab 99 reply', 'tab 99 later reply']) {
    assert.equal(occurrences(entryTexts(), text), 1, `${text} renders exactly once after repeated switches`);
    assert.equal(occurrences(storedTexts(99), text), 1, `${text} is stored exactly once after repeated switches`);
  }

  // ── Entries arriving while the initial hydration is in flight ────────────
  // Stored history lands first; entries appended before the load resolves
  // stay after it — each exactly once, in the DOM and in the next save.
  sessionStore.set('tabTranscript:77', [
    { who: 'user', text: 'stored user line' },
    { who: 'steve', text: 'stored steve line' },
  ]);
  fire(activatedEvent, { tabId: 77, windowId: 7 }); // starts loading the stored history
  deliver({ type: 'final', text: 'in-flight reply', tab_id: 77 }); // arrives before the load resolves
  await flush();
  assert.deepEqual(
    entryTexts(),
    ['stored user line', 'stored steve line', 'in-flight reply'],
    'stored history hydrates first; in-flight entries follow it',
  );
  deliver({ type: 'final', text: 'post-load reply', tab_id: 77 }); // forces the next save
  await flush();
  assert.deepEqual(
    storedTexts(77),
    ['stored user line', 'stored steve line', 'in-flight reply', 'post-load reply'],
    'save after hydration keeps every entry exactly once',
  );
  fire(activatedEvent, { tabId: 99, windowId: 7 });
  await flush();
  fire(activatedEvent, { tabId: 77, windowId: 7 });
  await flush();
  assert.deepEqual(
    entryTexts(),
    ['stored user line', 'stored steve line', 'in-flight reply', 'post-load reply'],
    're-activating the hydrated tab keeps the transcript stable',
  );

  // ── New chat: switching away and back must not resurrect the old chat ────
  fire(activatedEvent, { tabId: 66, windowId: 7 });
  await flush();
  inputEl.value = 'tab 66 doomed message';
  inputEl.listeners.keydown[0]({ key: 'Enter', shiftKey: false, isComposing: false, preventDefault() {} });
  deliver({ type: 'final', text: 'tab 66 doomed reply', tab_id: 66 });
  await flush();
  assert.ok(domTexts().includes('tab 66 doomed message'), 'message renders before new chat');
  newChatEl.listeners.click[0]();
  await flush();
  assert.equal(entryTexts().length, 0, 'new chat clears the visible transcript');
  assert.deepEqual(storedTexts(66), [], 'new chat overwrites the stored history');
  fire(activatedEvent, { tabId: 77, windowId: 7 });
  await flush();
  fire(activatedEvent, { tabId: 66, windowId: 7 });
  await flush();
  assert.equal(entryTexts().length, 0, 'switching away and back does not resurrect the cleared chat');
  assert.ok(!domTexts().some((t) => t.includes('doomed')), 'cleared messages stay gone');

  console.log('OK');
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
