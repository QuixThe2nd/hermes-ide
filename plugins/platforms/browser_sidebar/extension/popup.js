// Big Steve — toolbar popup.
//
// Saves the connection token to chrome.storage.sync (the service worker
// connects with it as the WebSocket subprotocol) and shows live connection
// status. '/', '+', '=' are rejected: they cannot appear in a subprotocol token.

'use strict';

const BAD_TOKEN_CHARS = /[\/+=]/;

const statusEl = document.getElementById('status');
const errorEl = document.getElementById('error');
const openSidebarEl = document.getElementById('open-sidebar');
const formEl = document.getElementById('token-form');
const tokenEl = document.getElementById('token');

function applyStatus(status) {
  const isConnected = !!(status && status.connected);
  statusEl.textContent = isConnected ? 'connected' : 'disconnected';
  statusEl.className = isConnected ? 'connected' : 'disconnected';
  if (status && status.error) {
    errorEl.textContent = status.error;
    errorEl.hidden = false;
  } else {
    errorEl.hidden = true;
  }
}

function showFormError(message) {
  errorEl.textContent = message;
  errorEl.hidden = false;
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.kind === 'status') applyStatus(msg);
});

// Opens the side panel from the popup's own click gesture — calling
// sidePanel.open() from the service worker after a runtime.sendMessage
// round trip loses the user-gesture context Chrome requires.
openSidebarEl.addEventListener('click', () => {
  chrome.windows
    .getCurrent()
    .then((win) => chrome.sidePanel.open({ windowId: win.id }))
    .then(() => window.close())
    .catch((err) => showFormError('Could not open sidebar: ' + String((err && err.message) || err)));
});

formEl.addEventListener('submit', (event) => {
  event.preventDefault();
  const token = tokenEl.value.trim();
  if (!token) {
    showFormError('Enter a token first.');
    return;
  }
  if (BAD_TOKEN_CHARS.test(token)) {
    showFormError('Token must not contain "/", "+", or "=".');
    return;
  }
  chrome.storage.sync
    .set({ token })
    .then(() => {
      errorEl.hidden = true; // the worker reconnects and broadcasts status
    })
    .catch((err) => showFormError('Could not save: ' + String((err && err.message) || err)));
});

// Populate the field with the saved token and get the current status.
chrome.storage.sync
  .get('token')
  .then((stored) => {
    if (stored && typeof stored.token === 'string') tokenEl.value = stored.token;
  })
  .catch(() => {});

chrome.runtime
  .sendMessage({ kind: 'get_status' })
  .then(applyStatus)
  .catch(() => {});
