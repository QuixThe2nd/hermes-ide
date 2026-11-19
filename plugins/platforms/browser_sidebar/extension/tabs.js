// Big Steve — per-tab session helpers, shared by the side panel scripts.
// Loaded via <script src="tabs.js"> in sidepanel.html; the object is attached
// to globalThis so the classic sidepanel.js script can read it (and so Node
// harnesses can require() this file before loading the page script).
//
// Wire convention: the SERVER stamps the originating tab id on outbound
// frames as a flat integer field `tab_id` (delta/final/typing/tool). Frames
// without it come from an older server build and belong to the single legacy
// conversation, which the side panel renders on whichever tab is active.
'use strict';

const SidebarTabs = {
  // Integer tab id carried by a server frame, or null for legacy frames.
  frameTabId(frame) {
    if (!frame || typeof frame !== 'object') return null;
    return Number.isInteger(frame.tab_id) ? frame.tab_id : null;
  },

  // New-chat request for one tab's session: the worker turns this into a
  // chat frame whose tab id selects that tab's Hermes session, so the
  // gateway's existing /new handling resets only that conversation.
  newChatRequest(tabId) {
    const msg = { kind: 'send_chat', text: '/new' };
    if (Number.isInteger(tabId)) msg.tabId = tabId;
    return msg;
  },
};

globalThis.SidebarTabs = SidebarTabs;

if (typeof module !== 'undefined' && module.exports) {
  module.exports = SidebarTabs;
}
