// Fake document/chrome for content.js: Command/Ctrl+B must reach the
// browser's own chrome.commands handling, so the content script must not
// preventDefault() the key or send an open_sidebar message for it.
'use strict';

const assert = require('node:assert/strict');

const contentScript = process.argv[2];

const listeners = { keydown: [] };
global.document = {
  addEventListener(type, handler) {
    (listeners[type] = listeners[type] || []).push(handler);
  },
};

const sentKinds = [];
global.chrome = {
  runtime: {
    sendMessage(msg) {
      sentKinds.push(msg && msg.kind);
      return Promise.resolve({});
    },
  },
};

require(contentScript);

let preventDefaultCalled = false;
let stopPropagationCalled = false;
const event = {
  key: 'b',
  metaKey: true,
  ctrlKey: false,
  altKey: false,
  shiftKey: false,
  repeat: false,
  preventDefault: () => { preventDefaultCalled = true; },
  stopPropagation: () => { stopPropagationCalled = true; },
};

for (const handler of listeners.keydown) handler(event);

assert.equal(preventDefaultCalled, false);
assert.equal(stopPropagationCalled, false);
assert.ok(!sentKinds.includes('open_sidebar'));
console.log('OK');
