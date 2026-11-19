// Fake DOM/chrome for popup.js: clicking #open-sidebar must call
// chrome.windows.getCurrent() then chrome.sidePanel.open({windowId}) from
// the popup itself (never via runtime.sendMessage), then close the popup.
'use strict';

const assert = require('node:assert/strict');

const popupScript = process.argv[2];
const WINDOW_ID = 42;

class FakeElement {
  constructor() {
    this.listeners = {};
    this.value = '';
    this.textContent = '';
    this.hidden = false;
    this.className = '';
  }
  addEventListener(type, handler) {
    (this.listeners[type] = this.listeners[type] || []).push(handler);
  }
  dispatch(type, event) {
    for (const handler of this.listeners[type] || []) handler(event);
  }
}

const elements = {
  status: new FakeElement(),
  error: new FakeElement(),
  'open-sidebar': new FakeElement(),
  'token-form': new FakeElement(),
  token: new FakeElement(),
};

global.document = { getElementById: (id) => elements[id] };

let closed = false;
global.window = { close: () => { closed = true; } };

const sentKinds = [];
let openedWindowId = null;

global.chrome = {
  runtime: {
    onMessage: { addListener() {} },
    sendMessage(msg) {
      sentKinds.push(msg && msg.kind);
      return Promise.resolve({});
    },
  },
  storage: {
    sync: {
      get: () => Promise.resolve({}),
      set: () => Promise.resolve(),
    },
  },
  windows: { getCurrent: () => Promise.resolve({ id: WINDOW_ID }) },
  sidePanel: {
    open(options) {
      openedWindowId = options && options.windowId;
      return Promise.resolve();
    },
  },
};

require(popupScript);

elements['open-sidebar'].dispatch('click');

setTimeout(() => {
  assert.equal(openedWindowId, WINDOW_ID);
  assert.equal(closed, true);
  assert.ok(!sentKinds.includes('open_sidebar'));
  console.log('OK');
}, 0);
