// Big Steve — content script.
//
// Intentionally does nothing: the sidebar shortcut is handled by Chrome's
// commands API (see manifest.json "commands") and chrome.sidePanel.open in
// background.js, so no page-level keydown interception is needed. A content
// script that preventDefault()s Command/Ctrl+B would swallow the keypress
// before the browser dispatches the chrome.commands event.

'use strict';
