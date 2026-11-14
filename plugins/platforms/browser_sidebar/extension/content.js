// Big Steve — content script.
//
// Intercepts Command+B / Ctrl+B at document_start in the capture phase so the
// browser's own sidebar shortcut (Brave Leo) never wins, then asks the service
// worker to open the side panel. Runs in all frames.

'use strict';

document.addEventListener(
  'keydown',
  (event) => {
    if (event.key.toLowerCase() !== 'b') return;
    const meta = event.metaKey && !event.ctrlKey;
    const ctrl = event.ctrlKey && !event.metaKey;
    if (!(meta || ctrl) || event.altKey || event.shiftKey) return;

    // Swallow the keydown before the browser and the page can react, even for
    // auto-repeats; only the first press asks for the panel.
    event.preventDefault();
    event.stopPropagation();
    if (event.repeat) return;

    chrome.runtime.sendMessage({ kind: 'open_sidebar' }).catch(() => {});
  },
  { capture: true, passive: false },
);
