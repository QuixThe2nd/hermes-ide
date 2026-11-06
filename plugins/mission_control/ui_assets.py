"""Static UI assets for the mission-control server: the CSS/JS/HTML
string constants and the one pure helper that render them. Moved
byte-identical from server.py; the server facade imports these names.
"""

import html
from string import Template

SHELL_CSS = """
:root {
  --rail: #1e1f22;          /* server rail */
  --sidebar: #2b2d31;       /* channel sidebar */
  --chat: #313338;          /* main conversation surface */
  --chat-hover: #2e3035;    /* message hover */
  --sb-hover: #35373c;      /* sidebar row hover */
  --sb-active: #404249;     /* selected sidebar row */
  --panel: #232428;         /* user panel strip */
  --field: #1e1f22;         /* search input */
  --composer: #383a40;      /* message composer */
  --embed: #2b2d31;         /* embeds: tool groups, activity, banners */
  --line: #3f4147;          /* hairlines */
  --line-strong: #24262b;   /* header/sidebar separators */
  --ink: #f2f3f5;           /* header-primary */
  --ink-2: #dbdee1;         /* text-normal */
  --muted: #949ba4;         /* text-muted */
  --faint: #80848e;
  --blurple: #5865f2;
  --blurple-hover: #4752c4;
  --agent-name: #949cf7;    /* agent author names (light blurple) */
  --green: #23a55a;
  --yellow: #f0b232;
  --red: #f23f43;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas,
          "Liberation Mono", monospace;
  --topbar-h: 48px;
  --sidebar-w: 240px;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html, body { height: 100%; }
body {
  margin: 0; display: flex; height: 100vh; height: 100dvh;
  overflow: hidden;
  background: var(--chat);
  color: var(--ink-2);
  font-family: system-ui, ui-sans-serif, -apple-system, "Segoe UI",
               Roboto, "Helvetica Neue", Arial, sans-serif;
  font-size: 14px;
  line-height: 1.45;
  -webkit-font-smoothing: antialiased;
}
::selection { background: rgba(88,101,242,0.45); color: #fff; }

/* ---- left rail: the real profile filters ---------------------------- */
.rail {
  flex: none; width: 72px; height: 100vh; height: 100dvh;
  background: var(--rail);
  display: flex; flex-direction: column; align-items: center;
  padding: 12px 0; gap: 8px;
  overflow-y: auto; overflow-x: hidden;
  scrollbar-width: none;
}
.rail::-webkit-scrollbar { display: none; }
.rail-item {
  position: relative; flex: none; width: 72px; height: 48px;
  display: flex; align-items: center; justify-content: center;
  text-decoration: none;
}
/* the pill on the rail's left edge: grows hover -> selected */
.rail-item::before {
  content: ""; position: absolute; left: 0; top: 50%;
  width: 4px; height: 0; border-radius: 0 4px 4px 0;
  background: var(--ink); transform: translateY(-50%);
  transition: height 0.15s ease;
}
.rail-item:hover::before { height: 20px; }
.rail-item.is-selected::before { height: 40px; }
.rail-item:focus-visible { outline: 2px solid var(--blurple); outline-offset: -2px; }
.rail-ico {
  position: relative; width: 48px; height: 48px; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--sidebar); color: var(--ink-2);
  font-size: 13px; font-weight: 600; overflow: hidden;
  transition: border-radius 0.15s ease, background-color 0.15s ease,
              color 0.15s ease;
}
/* an optional local avatar image fills the disc; the letter badge it
   covers is the fallback when the file is absent or fails to load */
.rail-ico .av-img {
  position: absolute; top: 0; left: 0; width: 100%; height: 100%;
  object-fit: cover; display: block; border-radius: inherit;
}
.rail-ico .av-img.is-broken { display: none; }
.rail-item:hover .rail-ico, .rail-item.is-selected .rail-ico {
  border-radius: 16px;
}
.rail-item:hover .rail-all, .rail-item.is-selected .rail-all {
  background: var(--blurple); color: #fff;
}
/* live-presence badge: at least one Active session for this profile */
.rail-item .pres {
  position: absolute; right: 10px; bottom: -1px; width: 12px; height: 12px;
  border-radius: 50%; background: var(--green);
  border: 3px solid var(--rail);
}
.rail-sep {
  flex: none; width: 32px; height: 2px; border-radius: 1px;
  background: var(--sb-hover);
}

/* ---- conversation sidebar ------------------------------------------- */
.sidebar {
  flex: none; width: var(--sidebar-w); height: 100vh; height: 100dvh;
  background: var(--sidebar);
  display: flex; flex-direction: column; min-width: 0;
}
.sb-head {
  flex: none; height: var(--topbar-h); padding: 0 16px;
  display: flex; align-items: center;
  border-bottom: 1px solid var(--line-strong);
}
.sb-title {
  margin: 0; font-size: 15px; font-weight: 700; color: var(--ink);
  letter-spacing: 0.01em;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.sb-tools { flex: none; padding: 10px 8px 8px; display: grid; gap: 8px; }
.search { position: relative; }
.search input {
  width: 100%; height: 28px; border: none; border-radius: 4px;
  background: var(--field); color: var(--ink-2);
  padding: 0 26px 0 8px; font: inherit; font-size: 13px;
}
.search input::placeholder { color: var(--muted); }
.search input:focus { outline: none; box-shadow: 0 0 0 2px var(--blurple); }
.search kbd {
  position: absolute; right: 6px; top: 50%; transform: translateY(-50%);
  pointer-events: none; font: 600 10px/1 inherit; font-family: inherit;
  color: var(--muted); border: 1px solid var(--line); border-radius: 3px;
  padding: 2px 5px; background: var(--sidebar);
}
/* the rail's profile filters as chips — mobile only (media query below) */
.rail-chips { display: none; gap: 6px; overflow-x: auto; scrollbar-width: none; }
.rail-chips::-webkit-scrollbar { display: none; }
.chip-filter {
  flex: none; display: inline-flex; align-items: center; height: 26px;
  padding: 0 10px; border-radius: 999px; text-decoration: none;
  background: var(--field); color: var(--ink-2);
  font-size: 12px; font-weight: 600; white-space: nowrap;
}
.chip-filter:hover { color: var(--ink); }
.chip-filter.is-selected { background: var(--blurple); color: #fff; }
.chip-filter:focus-visible { outline: 2px solid var(--blurple); outline-offset: 1px; }
.sb-meta { display: flex; align-items: center; gap: 8px; padding: 0 2px; }
.new-chat {
  flex: none; display: inline-flex; align-items: center; height: 28px;
  padding: 0 10px; border-radius: 4px; text-decoration: none;
  background: var(--blurple); color: #fff;
  font-size: 13px; font-weight: 600; white-space: nowrap;
}
.new-chat:hover { background: var(--blurple-hover); }
.new-chat:focus-visible { outline: 2px solid #fff; outline-offset: 1px; }
.shown {
  font-size: 11.5px; color: var(--muted);
  font-variant-numeric: tabular-nums; white-space: nowrap;
}
.noscript-note { font-size: 11.5px; color: var(--muted); }
.sb-scroll {
  flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden;
  padding: 2px 8px 8px;
}
.sb-scroll::-webkit-scrollbar, .scroller::-webkit-scrollbar { width: 8px; }
.sb-scroll::-webkit-scrollbar-thumb, .scroller::-webkit-scrollbar-thumb {
  background: #1a1b1e; border-radius: 4px;
  border: 2px solid transparent; background-clip: padding-box;
}
.sb-scroll::-webkit-scrollbar-track, .scroller::-webkit-scrollbar-track {
  background: transparent;
}

/* skip notes */
.notes { list-style: none; margin: 4px 0 8px; padding: 0; display: grid; gap: 6px; }
.notes li {
  border: 1px solid rgba(240,178,50,0.40); border-left: 3px solid var(--yellow);
  background: rgba(240,178,50,0.08); color: var(--ink-2);
  border-radius: 4px; padding: 7px 10px; font-size: 12px;
}
.notes strong { color: var(--yellow); font-weight: 600; margin-right: 4px; }

/* ---- conversation sections and rows ----------------------------------
   Discord DM-list rows: circular profile avatar (a green presence badge
   while the session is Active), title with a small relative time on the
   right, one preview line (owning profile, last message, optional
   last-tool chip). Hover and selected are flat surface fills, never
   cards. */
.convsec { min-width: 0; }
.convsec-head {
  display: flex; align-items: center; gap: 6px;
  margin: 0; padding: 14px 4px 4px;
  font-size: 11px; font-weight: 700; letter-spacing: 0.02em;
  text-transform: uppercase; color: var(--muted);
}
.convsec-title { color: inherit; }
/* Closed is a collapsed-by-default <details>; the caret chevron mirrors
   a collapsed Discord category. */
summary.convsec-head { cursor: pointer; list-style: none; user-select: none; }
summary.convsec-head::-webkit-details-marker { display: none; }
summary.convsec-head .convsec-title {
  margin: 0; font-size: inherit; font-weight: inherit;
}
summary.convsec-head:focus-visible {
  outline: 2px solid var(--blurple); outline-offset: -2px;
  border-radius: 4px;
}
.convsec-caret {
  flex: none; width: 8px; height: 8px; position: relative;
}
.convsec-caret::before {
  content: ""; position: absolute; inset: 0; margin: auto;
  width: 0; height: 0;
  border-left: 5px solid var(--muted);
  border-top: 4px solid transparent;
  border-bottom: 4px solid transparent;
  transition: transform 0.12s ease;
}
details.convsec[open] .convsec-caret::before { transform: rotate(90deg); }
.sec-count {
  flex: none; font-weight: 600; font-size: 10.5px; line-height: 16px;
  color: var(--muted); background: var(--field);
  padding: 0 7px; border-radius: 999px;
  font-variant-numeric: tabular-nums;
}
.sec-dot {
  width: 7px; height: 7px; border-radius: 50%; background: var(--green);
  position: relative; flex: none;
}
.sec-dot::after {
  content: ""; position: absolute; inset: -3px; border-radius: 50%;
  border: 1px solid var(--green);
  animation: pulse 2.4s ease-out infinite;
}
@keyframes pulse {
  0% { transform: scale(0.6); opacity: 0.7; }
  70%, 100% { transform: scale(1.5); opacity: 0; }
}
.conv { min-width: 0; }
.conv-link {
  display: flex; align-items: center; gap: 10px;
  padding: 6px 8px; border-radius: 4px;
  color: inherit; text-decoration: none;
}
.conv-link:hover { background: var(--sb-hover); }
.conv.is-selected .conv-link,
.conv.is-selected .conv-link:hover { background: var(--sb-active); }
.conv-link:focus-visible {
  outline: 2px solid var(--blurple); outline-offset: -2px;
}
.avatar {
  flex: none; position: relative; width: 32px; height: 32px;
  border-radius: 50%; overflow: hidden;
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--composer); color: var(--muted);
  font-size: 12px; font-weight: 700;
}
/* the optional avatar photo sits on top of the letter badge; the
   error listener below adds is-broken so the letter shows through */
.avatar .av-img {
  position: absolute; top: 0; left: 0; width: 100%; height: 100%;
  border-radius: 50%; object-fit: cover; display: block;
}
.avatar .av-img.is-broken { display: none; }
.conv .avatar .pres {
  position: absolute; right: -2px; bottom: -2px; width: 12px; height: 12px;
  border-radius: 50%; background: var(--green);
  border: 3px solid var(--sidebar);
}
.conv-main { flex: 1; min-width: 0; }
.conv-top { display: flex; align-items: baseline; gap: 6px; }
.conv-title {
  flex: 1; min-width: 0; margin: 0;
  font-size: 14px; font-weight: 600; color: var(--ink-2);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.conv-link:hover .conv-title, .conv.is-selected .conv-title { color: var(--ink); }
.conv-when {
  flex: none; white-space: nowrap; text-align: right;
  font-variant-numeric: tabular-nums;
}
.conv-when .rel { color: var(--faint); font-size: 11px; }
.conv-preview-row {
  display: flex; align-items: center; gap: 6px;
  margin-top: 1px; min-width: 0;
}
.conv-preview {
  flex: 1; min-width: 0; margin: 0; font-size: 12px; color: var(--muted);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.conv-prof { color: var(--faint); font-weight: 600; }
.conv-tool {
  flex: none; max-width: 45%; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; height: 16px; line-height: 16px; padding: 0 6px;
  border-radius: 3px; background: var(--field); color: var(--muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas,
               "Liberation Mono", monospace;
  font-size: 10px;
}
.conv-archived {
  flex: none; white-space: nowrap; height: 16px; line-height: 15px;
  padding: 0 6px; border-radius: 3px;
  background: rgba(240,178,50,0.12); color: var(--yellow);
  font-size: 10px; font-weight: 700; letter-spacing: 0.04em;
  text-transform: uppercase;
}
/* Closed rows step back as a group while the title stays readable. */
.conv[data-state="closed"] .avatar { opacity: 0.65; }
.conv[data-state="closed"] .conv-preview,
.conv[data-state="closed"] .conv-when .rel { color: var(--faint); }

.state {
  margin: 10px 4px; padding: 16px 12px; text-align: center;
  border: 1px dashed var(--line); border-radius: 8px;
  color: var(--muted); font-size: 12.5px;
}
.state.state-slim { margin: 6px 4px; padding: 10px; font-size: 12px; }

/* ---- user panel (sidebar footer) ------------------------------------- */
.sb-user {
  flex: none; display: flex; align-items: center; gap: 8px;
  min-height: 53px; padding: 8px 10px;
  background: var(--panel);
}
.sb-user-meta { flex: 1; min-width: 0; display: grid; line-height: 1.3; }
.sb-user-name {
  font-size: 13px; font-weight: 600; color: var(--ink);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.sb-user-status {
  display: flex; align-items: center; gap: 5px; min-width: 0;
  font-size: 11px; color: var(--muted);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.sync { display: inline-flex; align-items: center; gap: 5px; flex: none; }
.sync .dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--green); position: relative; flex: none;
}
.sync .dot::after {
  content: ""; position: absolute; inset: -3px; border-radius: 50%;
  border: 1px solid var(--green);
  animation: pulse 2.4s ease-out infinite;
}
.sync.stale .dot { background: var(--yellow); }
.sync.stale .dot::after { border-color: var(--yellow); }
.sync.stale .label { color: var(--yellow); }
.as-of { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.as-of time { color: var(--ink-2); font-variant-numeric: tabular-nums; }

/* ---- main panel: inbox splash ---------------------------------------- */
.main {
  flex: 1; min-width: 0; height: 100vh; height: 100dvh;
  background: var(--chat); position: relative;
  display: flex; flex-direction: column;
  --composer-h: 92px;
}
body.view-list .main {
  align-items: center; justify-content: center; overflow: auto;
}
.splash { text-align: center; padding: 40px 24px; max-width: 460px; }
.splash-mark {
  display: inline-flex; width: 72px; height: 72px; border-radius: 24px;
  align-items: center; justify-content: center;
  background: linear-gradient(135deg, #5865f2 0%, #3d44a8 100%);
  color: #fff; font-size: 38px; font-weight: 800; line-height: 1;
}
.splash-title { margin: 18px 0 6px; font-size: 24px; font-weight: 700; color: var(--ink); }
.splash-sub {
  margin: 0 0 22px; font-size: 14px; color: var(--muted); line-height: 1.55;
}
.btn-blurple {
  display: inline-flex; align-items: center; height: 38px; padding: 0 18px;
  border-radius: 6px; background: var(--blurple); color: #fff;
  font-size: 14px; font-weight: 600; text-decoration: none;
}
.btn-blurple:hover { background: var(--blurple-hover); }
.btn-blurple:focus-visible { outline: 2px solid #fff; outline-offset: 2px; }
.splash-meta { margin: 24px 0 0; font-size: 12px; color: var(--faint); }
.splash-meta time { color: var(--muted); font-variant-numeric: tabular-nums; }

/* ---- chat header ------------------------------------------------------ */
.chat-topbar {
  flex: none; height: var(--topbar-h); padding: 0 12px;
  display: flex; align-items: center; gap: 8px; min-width: 0;
  background: var(--chat);
  border-bottom: 1px solid var(--line-strong);
}
.back {
  flex: none; display: none; align-items: center; height: 32px;
  padding: 0 8px; border-radius: 4px; text-decoration: none;
  color: var(--ink-2); font-size: 13px; font-weight: 600;
  white-space: nowrap;
}
.back:hover { background: var(--chat-hover); color: var(--ink); }
.back:focus-visible { outline: 2px solid var(--blurple); outline-offset: 1px; }
.back .arrow { margin-right: 6px; }
.chan-hash {
  flex: none; color: var(--faint); font-size: 20px; font-weight: 600;
}
.chat-title-block { flex: none; max-width: 55%; min-width: 0; }
.chat-title {
  margin: 0; font-size: 15px; font-weight: 700; color: var(--ink);
  line-height: 1.25;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.topbar-div { flex: none; width: 1px; height: 20px; background: var(--line); }
.chat-topic {
  flex: 1; min-width: 0; font-size: 13px; color: var(--muted);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.session-toggle {
  flex: none; height: 28px; padding: 0 10px; border-radius: 4px;
  border: 1px solid var(--line); background: transparent;
  color: var(--ink-2); font: inherit; font-size: 12.5px; font-weight: 600;
  cursor: pointer; white-space: nowrap;
}
.session-toggle:hover:not(:disabled) {
  border-color: var(--blurple); color: var(--ink);
}
.session-toggle:disabled { opacity: 0.5; cursor: default; }
.session-toggle:focus-visible {
  outline: 2px solid var(--blurple); outline-offset: 2px;
}

/* ---- conversation: Discord flat message groups -------------------------
   A group starts with the 40px avatar, the author name and a timestamp;
   follow-up messages from the same sender repeat neither, just a small
   gutter timestamp on hover. Hover shades the whole row. No bubbles. */
.scroller { flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden; }
.chat-pad {
  min-height: 100%; padding: 8px 0 0;
  display: flex; flex-direction: column; justify-content: flex-end;
}
.msgs { list-style: none; margin: 0; padding: 8px 0 4px; }
.msg {
  display: flex; gap: 16px; min-width: 0; position: relative;
  padding: 3px 48px 3px 16px; margin-top: 14px;
}
.msg.cont { margin-top: 3px; }
.msg:hover { background: var(--chat-hover); }
.msg .avatar { width: 40px; height: 40px; margin-top: 1px; font-size: 15px; }
.msg-gutter { flex: none; width: 40px; display: flex; justify-content: flex-end; }
.msg-gutter .mtime {
  visibility: hidden; font-size: 10.5px; color: var(--faint);
  font-variant-numeric: tabular-nums; line-height: 1.9;
}
.msg.cont:hover .msg-gutter .mtime { visibility: visible; }
.msg-body { flex: 1; min-width: 0; }
.msg-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 1px; }
.msg-author { font-size: 15px; font-weight: 500; color: var(--ink); }
.msg.from-agent .msg-author { color: var(--agent-name); }
.msg-head .mtime {
  font-size: 11.5px; color: var(--faint);
  font-variant-numeric: tabular-nums; white-space: nowrap;
}
.text {
  margin: 0; min-width: 0; white-space: pre-wrap; word-break: break-word;
  overflow-wrap: anywhere; color: var(--ink-2);
  font-size: 15px; line-height: 1.4;
}

/* delivery ticks under an optimistic/sent user message */
.ticks {
  display: flex; align-items: center; gap: 4px;
  margin-top: 2px; font-size: 11px; color: var(--faint);
  white-space: nowrap;
}
.ticks .tick-glyph { letter-spacing: -1.5px; }
.ticks.is-read { color: var(--agent-name); font-weight: 600; }
.ticks.is-failed { color: var(--red); }

/* ---- tool groups: compact embeds, never bubbles ----------------------- */
.tool-group { margin: 6px 48px 4px 72px; min-width: 0; }
.tg {
  width: fit-content; min-width: 240px; max-width: 100%;
  background: var(--embed); border-radius: 4px;
  border-left: 4px solid #4e5058;
}
.tg-sum {
  display: flex; align-items: center; flex-wrap: wrap; gap: 5px 8px;
  padding: 8px 12px; cursor: pointer; user-select: none;
  font-size: 12.5px; color: var(--ink-2); list-style: none;
}
.tg-sum::-webkit-details-marker { display: none; }
.tg-sum::before { content: "\25b8"; color: var(--muted); font-size: 10px; }
.tg[open] > .tg-sum::before { content: "\25be"; }
.tg-sum:focus-visible { outline: 2px solid var(--blurple); outline-offset: -2px; }
.tg-count { font-weight: 600; white-space: nowrap; }
.tg-chips { display: flex; flex-wrap: wrap; gap: 4px; min-width: 0; }
.tg-when {
  margin-left: auto; padding-left: 10px; color: var(--faint);
  font-size: 11px; font-variant-numeric: tabular-nums; white-space: nowrap;
}
.chip {
  display: inline-block; max-width: 170px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
  height: 18px; line-height: 18px; padding: 0 6px; border-radius: 3px;
  background: var(--field); color: var(--muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas,
               "Liberation Mono", monospace;
  font-size: 10.5px;
}
.chip.chip-more { font-family: inherit; color: var(--faint); }
.tg-list {
  list-style: none; margin: 0; padding: 6px 12px 10px 24px;
  display: grid; gap: 6px; border-top: 1px solid var(--line-strong);
}
.tg-item {
  min-width: 0; font-size: 12.5px;
  display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap;
}
.tg-time {
  color: var(--faint); font-size: 11px;
  font-variant-numeric: tabular-nums; white-space: nowrap;
}
.tool-detail { flex-basis: 100%; margin-top: 2px; }
.tool-detail summary {
  cursor: pointer; user-select: none; font-size: 11.5px;
  color: var(--muted); display: inline-block;
}
.tool-detail summary:hover { color: var(--ink-2); }
.tool-detail pre {
  margin: 5px 0 0; padding: 8px 10px; max-height: 260px; overflow: auto;
  background: var(--field); border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas,
               "Liberation Mono", monospace;
  font-size: 11.5px; line-height: 1.45; color: var(--ink-2);
  white-space: pre-wrap; word-break: break-word;
}

/* ---- inline sub-agent child cards --------------------------------------
   One slim status card per child, merged into the transcript at the
   child's dispatch time (not a chat bubble, never inside a tool group):
   a status dot, the "Sub-agent" tag, the clamped goal label, and the
   child's own profile/state/activity line — the whole card is a link
   into the child's own transcript. */
.subagent-item {
  display: flex; margin: 6px 48px 2px 72px; min-width: 0;
}
@media (max-width: 900px) {
  .subagent-item { margin: 6px 12px 2px 64px; }
}
.sa-card {
  display: flex; align-items: flex-start; gap: 9px;
  width: fit-content; min-width: 240px; max-width: 100%;
  padding: 6px 12px; text-decoration: none; color: inherit;
  background: var(--embed); border-radius: 4px;
  border-left: 4px solid var(--blurple);
}
.sa-card:hover { background: rgba(78,80,88,0.30); }
.sa-card:focus-visible {
  outline: 2px solid var(--blurple); outline-offset: -2px;
}
.sa-dot {
  flex: none; width: 8px; height: 8px; border-radius: 50%;
  margin-top: 5px; background: var(--muted);
}
.sa-dot.sa-done { background: var(--green); }
.sa-dot.sa-running {
  background: var(--blurple);
  animation: sa-pulse 2.4s ease-out infinite;
}
.sa-dot.sa-interrupted { background: var(--yellow); }
.sa-dot.sa-failed { background: var(--red); }
@keyframes sa-pulse {
  0% { box-shadow: 0 0 0 0 rgba(88,101,242,0.45); }
  70%, 100% { box-shadow: 0 0 0 6px rgba(88,101,242,0); }
}
.sa-body { flex: 1; min-width: 0; }
.sa-tag {
  display: block; font-size: 10.5px; font-weight: 700;
  letter-spacing: 0.04em; text-transform: uppercase;
  color: var(--agent-name);
}
.sa-label {
  display: -webkit-box; -webkit-box-orient: vertical;
  -webkit-line-clamp: 2; line-clamp: 2; overflow: hidden;
  font-size: 13px; line-height: 1.4; color: var(--ink-2);
}
.sa-meta {
  display: block; margin-top: 1px; font-size: 11px;
  color: var(--faint); font-variant-numeric: tabular-nums;
}
.sa-go {
  flex: none; align-self: center; font-size: 11px;
  color: var(--faint); white-space: nowrap;
}
.sa-card:hover .sa-go { color: var(--ink-2); }

/* ---- inline delegate dispatch cards ------------------------------------
   The spawn-shaped delegate tool call rendered as its own embed at the
   call carrier's position (replacing the generic delegate_* tool row):
   the same slim card language as inline child rows, but unmistakably a
   dispatch — a green accent, the exact delegate_* tool name as a mono
   chip beside the tag, and a tinted body. A card with no safe child
   route renders identical chrome as a static span (no link, no Open);
   a Claude card with a validated spawn receipt is the same anchor
   pointing at the run's live viewer instead, with a Watch chip. */
.delegate-item {
  display: flex; margin: 6px 48px 2px 72px; min-width: 0;
}
.dlg-card {
  border-left-color: var(--green);
  background:
    linear-gradient(rgba(35,165,90,0.07), rgba(35,165,90,0.07)),
    var(--embed);
}
.dlg-card:hover { background:
    linear-gradient(rgba(35,165,90,0.10), rgba(35,165,90,0.10)),
    rgba(78,80,88,0.30); }
span.dlg-card { cursor: default; }
.dlg-tool {
  display: inline-block; margin-left: 4px; padding: 0 5px;
  border-radius: 3px; background: var(--field);
  font-family: var(--mono); font-size: 10px; font-weight: 400;
  letter-spacing: 0; text-transform: none; color: var(--muted);
  vertical-align: 1px;
}
@media (max-width: 900px) {
  .delegate-item { margin: 6px 12px 2px 64px; }
}

/* ---- next sub-agent ------------------------------------------------------
   A floating pill over the composer that steps through the inline
   child and dispatch cards: next row below the current scroll
   position, wrapping to the first after the last. Present only while
   a keyed row exists. */
.next-subagent {
  position: absolute; right: 24px;
  bottom: calc(var(--composer-h) + 12px); z-index: 10;
  display: inline-flex; align-items: center; gap: 6px;
  height: 32px; padding: 0 12px; border-radius: 999px;
  background: var(--green); color: #fff; text-decoration: none;
  font-size: 12.5px; font-weight: 600;
  box-shadow: 0 4px 12px rgba(0,0,0,0.40);
}
.next-subagent:hover { filter: brightness(1.1); }
.next-subagent:focus-visible { outline: 2px solid #fff; outline-offset: 2px; }
.next-subagent.is-hidden { display: none; }

/* ---- closed banner ----------------------------------------------------- */
.closed-banner {
  margin: 12px 48px 0 72px; padding: 8px 12px; border-radius: 4px;
  border-left: 4px solid var(--yellow); background: var(--embed);
  color: var(--ink-2); font-size: 13px;
}

/* ---- chat empty state ---------------------------------------------------
   Discord's channel-intro shape: left-aligned in the message column, no
   box — a bold title and a muted line. */
#empty-state {
  align-self: stretch; margin: 8px 48px 16px 72px; padding: 8px 0;
  border: none; background: none; text-align: left; font-size: 14px;
}
#empty-state .empty-title {
  margin: 0 0 4px; font-size: 20px; font-weight: 700; color: var(--ink);
}
#empty-state p { color: var(--muted); }
@media (max-width: 900px) {
  #empty-state { margin: 8px 12px 16px; }
}

/* End-of-transcript anchor for the Jump-to-latest link. */
.latest { height: 1px; margin: 16px 0 8px; }

/* ---- jump to latest ---------------------------------------------------- */
.jump-latest {
  position: absolute; right: 24px;
  bottom: calc(var(--composer-h) + 12px); z-index: 10;
  display: inline-flex; align-items: center; gap: 6px;
  height: 32px; padding: 0 12px; border-radius: 999px;
  background: var(--blurple); color: #fff; text-decoration: none;
  font-size: 12.5px; font-weight: 600;
  box-shadow: 0 4px 12px rgba(0,0,0,0.40);
}
.jump-latest:hover { background: var(--blurple-hover); }
.jump-latest:focus-visible { outline: 2px solid #fff; outline-offset: 2px; }
.jump-latest.is-hidden { display: none; }

/* ---- composer -----------------------------------------------------------
   Discord's rounded bar pinned to the bottom of the main panel, in flow
   (the transcript scroller is its own region above it, so the composer
   is always above the viewport bottom). */
.composer {
  flex: none; position: relative; padding: 0 16px 24px;
  background: var(--chat);
}
.composer-box {
  display: flex; align-items: flex-end;
  background: var(--composer); border-radius: 8px;
}
.composer-box:focus-within { box-shadow: 0 0 0 1px var(--blurple); }
.composer textarea {
  flex: 1; min-width: 0; display: block; resize: none;
  min-height: 44px; max-height: 160px; overflow-y: auto;
  background: transparent; border: none; color: var(--ink-2);
  padding: 11px 4px 11px 16px; font: inherit;
  font-size: 15px; line-height: 22px;
}
.composer textarea::placeholder { color: var(--muted); }
.composer textarea:focus { outline: none; }
.composer textarea:disabled, .composer .send:disabled {
  opacity: 0.5; cursor: default;
}
.composer .send {
  flex: none; height: 32px; margin: 6px 6px 6px 4px; padding: 0 12px;
  border: none; border-radius: 4px; background: transparent;
  color: var(--muted); font: inherit; font-size: 13px; font-weight: 600;
  cursor: pointer;
}
.composer .send:hover:not(:disabled) {
  color: #fff; background: rgba(78,80,88,0.60);
}
.composer .send:focus-visible {
  outline: 2px solid var(--blurple); outline-offset: 1px;
}
/* transient notes (busy/409/failure) hover just above the bar */
.composer-flash {
  position: absolute; top: -36px; left: 16px;
  max-width: calc(100% - 32px);
  padding: 7px 12px; border-radius: 6px; font-size: 12px;
  color: var(--yellow); background: var(--field);
  border: 1px solid rgba(240,178,50,0.40);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.composer-flash[hidden] { display: none; }

/* ---- single-pane mobile layout -----------------------------------------
   Under 900px the rail collapses into a chip row inside the sidebar,
   the inbox is the sidebar alone, and a transcript is the chat alone
   with a visible back link. No desktop rails squeezed into slivers. */
@media (max-width: 900px) {
  .rail { display: none; }
  .rail-chips { display: flex; }
  .sidebar { width: auto; flex: 1; }
  body.view-list .main { display: none; }
  body.view-chat .sidebar { display: none; }
  .back { display: inline-flex; }
  .search kbd { display: none; }
  .msg { padding: 3px 12px; gap: 12px; margin-top: 12px; }
  .msg.cont { margin-top: 3px; }
  .tool-group { margin: 6px 12px 4px 64px; }
  .subagents, .closed-banner { margin: 12px 12px 0 64px; }
  .composer { padding: 0 12px 16px; }
  .jump-latest { right: 12px; }
  .chat-title-block { max-width: none; flex: 1; }
}
@media (max-width: 560px) {
  .chat-topic, .topbar-div { display: none; }
  .back .back-label { display: none; }
  .back .arrow { margin-right: 0; }
  .tg-when { display: none; }
}
@media (prefers-reduced-motion: reduce) {
  .sec-dot::after, .sync .dot::after { animation: none; opacity: 0; }
  .sa-dot.sa-running { animation: none; }
  .rail-item::before, .rail-ico { transition: none; }
}
"""


# Shared sidebar behavior for both pages: the client-side search filter
# (over every section, hiding empty ones while a filter is active), the
# rail/chip profile filters (in-place on the inbox, plain navigation from
# chat pages), relative-time ticking and the "/" focus shortcut. No "$"
# on purpose: this is substituted into string.Template shells.
# window.MC exposes re-bind hooks so the inbox's refresh swap can
# re-apply everything to the fresh rows.
SIDEBAR_JS = """"use strict";
// Cover photos are optional: when an avatar image element fails to
// load, hide it (capture phase — img error events do not bubble) so
// the letter badge underneath shows instead.
document.addEventListener("error", function (e) {
  var t = e.target;
  if (t && t.tagName === "IMG" && t.classList.contains("av-img")) {
    t.classList.add("is-broken");
  }
}, true);
window.MC = (function () {
  var input = document.getElementById("filter");
  var countEl = document.getElementById("shown");
  var list = document.getElementById("rows");
  // The rail filters in place only on the inbox (data-refresh); on chat
  // pages its links simply navigate to the filtered inbox.
  var inPlace = document.body.hasAttribute("data-refresh");

  var profileFilter = "";
  try {
    var pm = /[?&]profile=([^&]*)/.exec(window.location.search);
    if (pm) profileFilter = decodeURIComponent(pm[1].replace(/\\+/g, " "));
  } catch (e0) {}

  var savedQuery = "";
  try {
    savedQuery = window.localStorage.getItem("mission-control.filter") || "";
  } catch (e1) {}
  if (input) input.value = savedQuery;

  function dataRows() {
    return list ? list.querySelectorAll(".conv[data-q]") : [];
  }

  function paintRail() {
    var items = document.querySelectorAll("[data-profile-filter]");
    for (var i = 0; i < items.length; i++) {
      var on = (items[i].getAttribute("data-profile-filter") || "") ===
               profileFilter;
      items[i].classList.toggle("is-selected", on);
    }
  }

  function filterLabel(q) {
    if (q) return '"' + input.value.trim() + '"';
    if (profileFilter) {
      var sel = document.querySelector(".rail-item.is-selected[title]");
      if (sel) return sel.getAttribute("title");
      return profileFilter;
    }
    return "this filter";
  }

  // ---- Closed section disclosure --------------------------------------
  // Collapsed by default in the served HTML; the user's toggle persists
  // in localStorage, survives the inbox refresh swap (rebind + restore),
  // and a text search with matching closed rows expands it temporarily
  // without overwriting the saved choice.
  var CLOSED_KEY = "mission-control.closed-open";
  var closedSaved = false;
  try {
    closedSaved = window.localStorage.getItem(CLOSED_KEY) === "1";
  } catch (e4) {}
  var closedTemp = false;   // a search expansion, not a user choice

  function closedDetails() {
    return list
      ? list.querySelector('details.convsec[data-section="closed"]')
      : null;
  }

  function bindClosed() {
    var d = closedDetails();
    if (!d) return;
    d.addEventListener("toggle", function () {
      if (closedTemp) return;  // search-driven, never a saved choice
      d.removeAttribute("data-keep");  // a manual toggle unpins it
      closedSaved = d.open;
      try {
        window.localStorage.setItem(CLOSED_KEY, d.open ? "1" : "0");
      } catch (e5) {}
    });
  }

  function applyClosed(q) {
    var d = closedDetails();
    if (!d) return;
    if (q) {
      var rows = d.querySelectorAll(".conv[data-q]");
      var any = false;
      for (var i = 0; i < rows.length; i++) {
        if (rows[i].getAttribute("data-q").indexOf(q) !== -1) {
          any = true;
          break;
        }
      }
      closedTemp = any;
      d.open = any || closedSaved || d.hasAttribute("data-keep");
    } else {
      closedTemp = false;
      // a chat page pins its selected Closed row open via data-keep
      if (!d.hasAttribute("data-keep")) d.open = closedSaved;
    }
  }

  // The search covers every section: rows match wherever they sit, and
  // a section with zero visible children hides itself (header and all)
  // while a text or profile filter is active. The shown count stays
  // global, not per section.
  function applyFilter() {
    if (!list) return;
    var q = input ? input.value.trim().toLowerCase() : "";
    var filtering = !!(q || profileFilter);
    var rows = dataRows(), shown = 0;
    var secVisible = {};
    for (var i = 0; i < rows.length; i++) {
      var hit = (!q || rows[i].getAttribute("data-q").indexOf(q) !== -1) &&
                (!profileFilter ||
                 rows[i].getAttribute("data-profile") === profileFilter);
      rows[i].hidden = !hit;
      if (hit) {
        shown++;
        var sec = rows[i].closest ? rows[i].closest(".convsec") : null;
        var key = sec ? sec.getAttribute("data-section") : "";
        if (key) secVisible[key] = (secVisible[key] || 0) + 1;
      }
    }
    var secs = list.querySelectorAll(".convsec[data-section]");
    for (var j = 0; j < secs.length; j++) {
      var k = secs[j].getAttribute("data-section");
      secs[j].hidden = filtering && !secVisible[k];
    }
    if (countEl) {
      countEl.textContent = filtering
        ? shown + " of " + rows.length + " shown"
        : rows.length + " shown";
      countEl.hidden = rows.length === 0;
    }
    var noMatch = document.getElementById("no-match");
    var noMatchQ = document.getElementById("no-match-q");
    if (noMatch) {
      noMatch.hidden = !(filtering && shown === 0 && rows.length > 0);
      if (filtering && noMatchQ) noMatchQ.textContent = filterLabel(q);
    }
    try {
      if (q) window.localStorage.setItem("mission-control.filter", q);
      else window.localStorage.removeItem("mission-control.filter");
    } catch (e2) {}
    applyClosed(q);
  }

  function relTime(tsSec, nowMs) {
    var s = Math.max(0, Math.floor(nowMs / 1000 - tsSec));
    if (s < 15) return "just now";
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  }

  function tickRelatives() {
    if (!list) return;
    var nowMs = Date.now();
    var cells = list.querySelectorAll(".conv-when[data-ts]");
    for (var i = 0; i < cells.length; i++) {
      var ts = parseFloat(cells[i].getAttribute("data-ts"));
      var rel = cells[i].querySelector(".rel");
      if (!isNaN(ts) && rel) rel.textContent = relTime(ts, nowMs);
    }
  }

  // Delegated so rail swaps and mobile chips both work: on the inbox the
  // click filters in place (URL updated, no reload); elsewhere the link
  // navigates to the filtered inbox.
  document.addEventListener("click", function (e) {
    var t = e.target && e.target.closest
      ? e.target.closest("[data-profile-filter]") : null;
    if (!t || !inPlace) return;
    e.preventDefault();
    profileFilter = t.getAttribute("data-profile-filter") || "";
    try {
      window.history.replaceState(null, "", profileFilter
        ? "/?profile=" + encodeURIComponent(profileFilter) : "/");
    } catch (e3) {}
    paintRail();
    applyFilter();
  });

  document.addEventListener("keydown", function (e) {
    var ae = document.activeElement;
    var typing = ae && (ae.tagName === "INPUT" || ae.tagName === "TEXTAREA");
    if (e.key === "/" && ae !== input && !typing) {
      e.preventDefault();
      if (input) { input.focus(); input.select(); }
    } else if (e.key === "Escape" && input && ae === input) {
      input.value = "";
      applyFilter();
      input.blur();
    }
  });

  if (input) input.addEventListener("input", applyFilter);
  paintRail();
  bindClosed();
  applyFilter();
  tickRelatives();
  window.setInterval(tickRelatives, 15000);

  // A chat page lands with its conversation selected in the sidebar:
  // bring that row into view once. block:"nearest" only scrolls when
  // the row is off-screen, so the inbox (no selection) and a chat
  // already on screen are untouched.
  var selRow = list ? list.querySelector(".conv.is-selected") : null;
  if (selRow) {
    try { selRow.scrollIntoView({ block: "nearest" }); } catch (e6) {}
  }

  // ---- no-reload sidebar refresh -------------------------------------
  // Both pages can ask for a fresh conversation list without leaving
  // the page: fetch this very URL, parse it, and swap in only the new
  // #rows. The server keeps rendering the sections, their counts, the
  // ordering and the selected row — the client never recomputes any
  // of it. The filter box, the profile rail and the page's own state
  // (transcript scroll, composer) sit outside #rows, so they survive
  // the swap untouched; the Closed disclosure, the search filter and
  // the relative times rebind over the fresh rows.
  var rowsGen = 0;   // monotonic: only the newest fetch may swap
  function refreshRows() {
    var gen = ++rowsGen;
    window.fetch(window.location.href, { cache: "no-store" })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.text();
      })
      .then(function (text) {
        if (gen !== rowsGen) return;  // a newer request already won
        var doc = new DOMParser().parseFromString(text, "text/html");
        var fresh = doc.getElementById("rows");
        var current = document.getElementById("rows");
        if (!fresh || !current || !current.parentNode) return;
        current.parentNode.replaceChild(fresh, current);
        list = fresh;
        bindClosed();     // the saved open/closed choice still applies
        applyFilter();    // the live filter text/profile re-applies
        tickRelatives();  // relative times re-tick over fresh rows
      })
      .catch(function () {
        // any failure keeps the old sidebar; nothing else disturbs
      });
  }

  return {
    apply: applyFilter,
    tick: tickRelatives,
    refreshRows: refreshRows,
    relist: function () {
      // the refresh swapped #rows: re-find it and rebind the Closed
      // disclosure so the saved choice survives (applyClosed runs via
      // the apply() that follows).
      list = document.getElementById("rows");
      bindClosed();
    }
  };
})();
"""


# The inbox shell: a string.Template, so CSS/JS contain literal % and {}
# freely and only $tokens are substituted. The body is the Discord-style
# shell: $sidebar (rail + conversation sidebar) plus the select-a-chat
# splash in the main panel. The inline script contains no "$" on purpose.
# Every shell — this one, CHAT_SHELL and the error/404 chrome — declares
# an empty inline data-URI icon: without one a browser auto-requests
# /favicon.ico, and the 404 lands as an error-level console entry. The
# data URI needs no shipped file and no host/port, so the no-external-
# assets, domain-independent contract holds.
PAGE_SHELL = Template("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta http-equiv="refresh" content="$refresh_seconds">
$csrf_meta
<link rel="icon" href="data:,">
<title>Mission Control &mdash; chats, last 24 hours</title>
<style>$shell_css</style>
</head>
<body class="view-list" data-refresh="$refresh_seconds">

$sidebar
<main class="main">
  <div class="splash">
    <span class="splash-mark" aria-hidden="true">#</span>
    <h2 class="splash-title">Mission Control</h2>
    <p class="splash-sub">Hermes sessions from the last 24 hours, live.
      Pick a conversation from the sidebar, or start a new chat.</p>
    <a class="btn-blurple" href="/new">Start a new chat</a>
    <p class="splash-meta">Generated <time id="generated">$generated</time>
      &middot; auto-refresh every $refresh_seconds&thinsp;s</p>
  </div>
</main>

<script>$sidebar_js</script>
<script>
"use strict";
(function () {
  var refreshSeconds = parseInt(document.body.getAttribute("data-refresh"), 10) || 30;

  // JavaScript owns auto-refresh from here on; the meta tag stays only
  // as the no-JS fallback, so neutralize it before it can fire.
  var metaRefresh = document.querySelector('meta[http-equiv="refresh"]');
  if (metaRefresh) {
    metaRefresh.setAttribute("content", "2147483647");
    metaRefresh.remove();
  }

  var syncEl = document.getElementById("sync");
  var syncLabel = document.getElementById("sync-label");
  var asOfEl = document.getElementById("as-of");

  function setSync(ok) {
    if (!syncEl) return;
    syncEl.classList.toggle("stale", !ok);
    if (syncLabel) syncLabel.textContent = ok ? "live" : "stale";
  }

  // Re-fetch "/" and swap in the dynamic regions; scroll, focus and the
  // filters survive because the page itself is never reloaded.
  function refresh() {
    if (document.hidden) return;
    window.fetch("/", { cache: "no-store" }).then(function (resp) {
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.text();
    }).then(function (text) {
      var doc = new DOMParser().parseFromString(text, "text/html");
      var ids = ["total", "notes", "no-rows", "rows", "generated"];
      for (var i = 0; i < ids.length; i++) {
        var fresh = doc.getElementById(ids[i]);
        var current = document.getElementById(ids[i]);
        if (fresh && current && current.parentNode) {
          current.parentNode.replaceChild(fresh, current);
        }
      }
      var freshAsOf = doc.getElementById("as-of");
      if (freshAsOf && asOfEl) asOfEl.textContent = freshAsOf.textContent;
      if (window.MC) {
        window.MC.relist();
        window.MC.apply();
        window.MC.tick();
      }
      setSync(true);
    }).catch(function () { setSync(false); });
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refresh();
  });
  window.setInterval(refresh, refreshSeconds * 1000);
})();
</script>
</body>
</html>
""")


# Chat transcript shell: the same Discord-style app shell as the inbox
# ($sidebar carries the rail and the conversation list with the current
# session selected), with the transcript in the main panel — a 48px
# channel-style header (back link on mobile, # title, profile/time
# topic, Close/Reopen), then a scrolling region of flat message groups
# (40px avatar, author name, timestamp; same-sender follow-ups repeat
# neither), tool runs as compact left-accented embeds, the sub-agents
# and live-activity embeds, and Discord's rounded bottom composer. A
# tiny dependency-free script lands the scroller near the newest
# message, floats a "Jump to latest" button that only shows while away
# from the bottom, polls the /feed endpoint every couple of seconds to
# append new rows in place, and drives the composer (Enter sends,
# Shift+Enter is a newline) through POST .../reply — or through
# POST /s/new on the blank-composer variant, whose fast 202 hands over
# to bounded job-status polling that navigates to the fresh session's
# transcript as soon as the correlated session id is published.
# Like PAGE_SHELL it is a string.Template so CSS braces need no escaping,
# and it deliberately contains no "$" outside its tokens.
CHAT_SHELL = Template("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
$csrf_meta
<link rel="icon" href="data:,">
<title>Mission Control &mdash; transcript: $title</title>
<style>$shell_css
$clarify_css
$live_css
$typing_css
$waiting_css</style>
</head>
<body class="view-chat" data-mode="$mode" data-profile="$profile_attr" data-session="$session_attr" data-poll-ms="$poll_ms" data-last-id="$last_id" data-av-user="$avatar_user_attr" data-archived="$archived_state">

$sidebar
<main class="main">
  <header class="chat-topbar">
    <a class="back" href="/" aria-label="Back to chats"><span class="arrow" aria-hidden="true">&larr;</span><span class="back-label">Chats</span></a>
    <span class="chan-hash" aria-hidden="true">#</span>
    <div class="chat-title-block">
      <h1 class="chat-title" title="$full_title">$title</h1>
    </div>
    <span class="topbar-div" aria-hidden="true"></span>
    <span class="chat-topic" title="$when_title">$profile &middot; $when</span>
$session_toggle  </header>
  <div class="scroller" id="scroller">
    <div class="chat-pad" id="chat-pad">
$closed_banner
$empty_state
      <ol class="msgs">
$rows$typing_row$waiting_row      </ol>
$live_activity      <div class="latest" id="latest" aria-hidden="true"></div>
    </div>
  </div>
$clarify_card  <form class="composer" id="composer" autocomplete="off">
    <p class="composer-flash" id="composer-flash" role="status" hidden></p>
    <div class="composer-box">
      <textarea id="composer-text" rows="1" placeholder="$composer_placeholder" aria-label="Message text"$composer_disabled></textarea>
      <button class="send" id="composer-send" type="submit"$composer_disabled>Send</button>
    </div>
  </form>
  <a class="jump-latest" id="jump-latest" href="#latest">
    <span aria-hidden="true">&darr;</span> Jump to latest
  </a>
$next_subagent</main>

<script>$sidebar_js</script>
<script>
"use strict";
(function () {
  var body = document.body;
  var mode = body.getAttribute("data-mode") || "chat";
  var profile = body.getAttribute("data-profile") || "";
  var session = body.getAttribute("data-session") || "";
  var pollMs = parseInt(body.getAttribute("data-poll-ms"), 10) || 2000;
  var busyPollMs = 700;   // while a reply is in flight, land it sooner
  var cursor = parseInt(body.getAttribute("data-last-id"), 10) || 0;
  // The user's optional avatar URL ("" when none is served): the
  // optimistic twin of a sent message layers it over the U badge
  // exactly like the server-rendered rows do.
  var avUserSrc = body.getAttribute("data-av-user") || "";

  var mainEl = document.querySelector(".main");
  var scroller = document.getElementById("scroller");
  var pad = document.getElementById("chat-pad");
  var list = document.querySelector(".msgs");
  // The typing row only exists on a real session's page, so the server
  // supplies its selector too ("" on /new — nothing to find there).
  var typingSel = $typing_selector;
  var typingRow = typingSel ? document.querySelector(typingSel) : null;
  // The waiting row exists on both pages (a send on /new waits for its
  // first response too); it is hidden until a send is accepted.
  var waitingRow = document.getElementById("waiting-row");
  var form = document.getElementById("composer");
  var box = document.getElementById("composer-text");
  var sendBtn = document.getElementById("composer-send");
  var flash = document.getElementById("composer-flash");
  var emptyState = document.getElementById("empty-state");
  // The floating child-stepper exists on the server render only when
  // the page already had children; polling builds it on demand.
  var nextSub = document.getElementById("next-subagent");
  // data-child of the row the stepper last targeted. Steps advance
  // from this key by identity, never by measured scroll position: a
  // short viewport clamps scrollIntoView at the scroller's maximum,
  // where a lower row can still sit "below the line" — a position-
  // based next would reselect that same row forever.
  var lastSubKey = null;
  var jump = document.getElementById("jump-latest");
  var end = document.getElementById("latest");
  var toggleBtn = document.getElementById("session-toggle");
  var closedBanner = document.getElementById("closed-banner");

  var busy = false;      // the feed says a reply is running here
  var everBusy = false;  // busy has been seen since the last send
  var waiting = false;   // we sent something and expect rows to land
  var sending = false;   // a POST is on the wire right now
  var holding = false;   // a /s/new launch job is running; stay locked
  var locked = false;    // navigating away after /s/new; keep it locked
  var archived = body.getAttribute("data-archived") === "1";  // closed
  var flashTimer = null;
  var pollTimer = null;
  // A pending clarify card is server-rendered above the composer; the
  // feed keeps it current (applyClarify). While one is live the normal
  // composer stays disabled — the card is the only way to answer.
  var clarifyActive = !!document.getElementById("clarify-card");
  var clarifying = false;  // a clarify answer POST is on the wire

  // ---- same-origin POSTs --------------------------------------------
  // The page carries this server's CSRF token in a meta tag; every
  // state-changing request sends it in the non-simple X-CSRF-Token
  // header alongside a JSON content type, which is exactly the shape a
  // plain HTML form cannot produce.
  var csrfMeta = document.querySelector(
    'meta[name="mission-control-csrf"]');
  var CSRF = csrfMeta ? (csrfMeta.getAttribute("content") || "") : "";
  function postJson(url, body) {
    return window.fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json",
                 "X-CSRF-Token": CSRF },
      body: typeof body === "string" ? body : JSON.stringify(body || {})
    });
  }
  // Last-request-wins guard over /feed responses: a poll that started
  // before a newer one (or before an accepted send bumped this) never
  // applies its stale busy verdict — it is dropped, and the newer poll
  // re-fetches anything it would have carried.
  var feedSeq = 0;

  // ---- first-response scoping ---------------------------------------
  // A turn "awaits its first response" from the moment its POST is
  // accepted (202) until the first assistant/tool output *of that
  // turn* is persisted. turnFloor anchors the scope: the feed cursor
  // at acceptance, raised to the turn's own user row the moment the
  // feed echoes it back (adoption) — so output persisted before this
  // turn (a historical row arriving through a lagging cursor) can
  // never satisfy the new turn.
  var turnFloor = 0;
  var turnOutputs = [];    // row ids of non-user items seen since acceptance
  var turnResponded = true;

  // The composer's open placeholder is whatever the server rendered;
  // while the session is closed both the banner and this swap apply,
  // and while a clarify card is live the placeholder points up at it.
  var CLOSED_PLACEHOLDER = "This session is closed.";
  var CLARIFY_PLACEHOLDER = "Answer the question above";
  var openPlaceholder = box ? (box.getAttribute("placeholder") || "") : "";

  // One place that makes the page match the session's archive state:
  // toggle label/action, closed banner, composer enable/disable and
  // placeholder. Called at boot, after a close/reopen answer, and on
  // every feed poll that carries session_state (so a Discord-side
  // change lands on an open page without a reload).
  function applySessionState(st) {
    if (!st || typeof st !== "object") return;
    var wasArchived = archived;
    archived = !!st.archived;
    if (toggleBtn) {
      toggleBtn.textContent = archived ? "Reopen" : "Close";
      toggleBtn.setAttribute("data-action", archived ? "reopen" : "close");
      toggleBtn.setAttribute("aria-pressed", archived ? "true" : "false");
      toggleBtn.title = archived
        ? "Reopen this session (unarchives the Discord thread)"
        : "Close this session (archives the Discord thread)";
    }
    if (closedBanner) closedBanner.hidden = !archived;
    if (box) {
      box.disabled = archived || sending || clarifyActive;
      box.placeholder = archived ? CLOSED_PLACEHOLDER
        : clarifyActive ? CLARIFY_PLACEHOLDER : openPlaceholder;
    }
    if (sendBtn) sendBtn.disabled = archived || sending || clarifyActive;
    // An archive-state transition moves this session's sidebar row into
    // or out of the Closed disclosure — re-render it from the server's
    // own classification, only on the transition itself (never per
    // poll), exactly like the busy-state transition in the feed handler.
    if (wasArchived !== archived) refreshSidebar();
  }

  function reduced() {
    try {
      return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch (e) { return false; }
  }
  // The transcript scrolls inside its own region (the shell is
  // fixed-height), so all scroll math is on #scroller, not the window.
  function nearBottom() {
    if (!scroller) return true;
    return scroller.scrollTop + scroller.clientHeight >=
           scroller.scrollHeight - 140;
  }
  function toBottom() {
    if (!scroller) return;
    if (reduced() || !end) {
      scroller.scrollTop = scroller.scrollHeight;
      return;
    }
    try { end.scrollIntoView({ behavior: "smooth", block: "end" }); }
    catch (err) { scroller.scrollTop = scroller.scrollHeight; }
  }
  function updateJump() {
    if (jump) jump.classList.toggle("is-hidden", nearBottom());
  }
  // The jump button floats above the composer; keep the offset in step
  // with the composer's real height as the textarea grows.
  function syncComposerVar() {
    if (mainEl && form) {
      mainEl.style.setProperty("--composer-h", form.offsetHeight + "px");
    }
  }

  function sessionUrl(tail) {
    return "/s/" + encodeURIComponent(profile) + "/" +
           encodeURIComponent(session) + tail;
  }

  // The conversation sidebar follows the session without a reload:
  // MC.refreshRows re-fetches this page's own URL and swaps #rows, so
  // the row lands in Active (or back in its resting section) exactly
  // as the server classifies it. Only a real session's page has a
  // section that can change; /new leaves for the fresh transcript.
  function refreshSidebar() {
    if (mode === "chat" && window.MC && window.MC.refreshRows) {
      window.MC.refreshRows();
    }
  }

  function showFlash(msg, ms) {
    if (!flash) return;
    flash.textContent = msg;
    flash.hidden = false;
    if (flashTimer) window.clearTimeout(flashTimer);
    flashTimer = window.setTimeout(function () {
      flash.hidden = true;
    }, ms || 6000);
  }

  function setTyping() {
    // Typing dots only on a real session, and only while the feed says
    // hermes is actually working — never on /new, never merely because
    // a send is waiting for its answer, never beside the waiting row
    // (the pre-first-output view) or a Live activity strip (each of
    // those is the honest, more specific view for its phase).
    if (typingRow) {
      typingRow.hidden = !(mode === "chat" && busy &&
                           !document.getElementById("live-activity") &&
                           !(waiting && !turnResponded));
    }
  }

  function setWaiting() {
    // The pre-response half of send progress: visible from acceptance
    // until this turn's first assistant/tool output lands — never
    // beside the Live activity strip (once output exists the strip
    // owns the tail) and never after a failure or a settled turn.
    if (waitingRow) {
      waitingRow.hidden = !(waiting && !turnResponded &&
                            !document.getElementById("live-activity"));
    }
  }

  function beginTurn() {
    // Called the instant a send is ACCEPTED (202): transport is done
    // ("Sent"), and the waiting indicator owns the tail from here.
    waiting = true;
    everBusy = false;
    turnFloor = cursor;
    turnOutputs = [];
    turnResponded = false;
    // any /feed request still on the wire predates the job this send
    // just created — its busy=false verdict must never read as a
    // settled turn and move the sidebar row back early
    feedSeq++;
    setWaiting();
  }

  // ---- optimistic sends ---------------------------------------------
  // Every send appends a real-looking user message at once and walks it
  // through Sending -> Sent -> Delivered -> Read (or Failed); once the
  // feed echoes the stored user row, that server-rendered row replaces
  // the optimistic twin so the message is never doubled.
  var outgoing = [];
  var TICK_ORDER = { sending: 0, sent: 1, delivered: 2, read: 3 };

  function normText(s) {
    return String(s || "").replace(/\\s+/g, " ").trim().toLowerCase();
  }

  function paintTicks(ticks, state) {
    var glyph = "", word;
    if (state === "sending") { word = "Sending…"; }
    else if (state === "sent") { glyph = "✓"; word = "Sent"; }
    else if (state === "delivered") { glyph = "✓✓"; word = "Delivered"; }
    else if (state === "read") { glyph = "✓✓"; word = "Read"; }
    else { word = "Failed"; }
    ticks.classList.toggle("is-read", state === "read");
    ticks.classList.toggle("is-failed", state === "failed");
    ticks.setAttribute("aria-label", word);
    while (ticks.firstChild) ticks.removeChild(ticks.firstChild);
    if (glyph) {
      var g = document.createElement("span");
      g.className = "tick-glyph";
      g.setAttribute("aria-hidden", "true");
      g.textContent = glyph;
      ticks.appendChild(g);
    }
    var w = document.createElement("span");
    w.textContent = word;
    ticks.appendChild(w);
  }

  function setTickState(rec, state) {
    if (rec.state === state) return;
    // a failed send is terminal: no later feed echo, busy poll or job
    // event may promote a rejected row back up the ladder
    if (rec.state === "failed") return;
    // the delivery path only ever moves forward
    if (rec.state in TICK_ORDER && state in TICK_ORDER &&
        TICK_ORDER[state] < TICK_ORDER[rec.state]) return;
    rec.state = state;
    paintTicks(rec.ticks, state);
  }

  function markOutgoing(state) {
    for (var i = 0; i < outgoing.length; i++) setTickState(outgoing[i], state);
  }

  function findOutgoing(text) {
    for (var i = 0; i < outgoing.length; i++) {
      // A failed send was never stored, so its row can never be the
      // twin of a server echo — the retry that lands is its own rec.
      if (!outgoing[i].adopted && outgoing[i].state !== "failed" &&
          outgoing[i].text === text) {
        return outgoing[i];
      }
    }
    return null;
  }

  function shortNow() {
    try {
      return new Date().toLocaleString(undefined, {
        month: "short", day: "numeric",
        hour: "2-digit", minute: "2-digit"});
    } catch (e) { return ""; }
  }

  // The optimistic twin: exactly the markup of a real user message plus
  // a ticks line under it. Built with DOM calls so message text is
  // never interpolated as HTML.
  function addOutgoing(text) {
    if (!list) return null;
    var li = document.createElement("li");
    li.className = "msg from-user";
    var av = document.createElement("span");
    av.className = "avatar";
    av.title = "user message";
    var letter = document.createElement("span");
    letter.setAttribute("aria-hidden", "true");
    letter.textContent = "U";
    av.appendChild(letter);
    // The optional user photo over the letter, same layering as the
    // server-rendered rows; skipped entirely when none is served.
    if (avUserSrc) {
      var img = document.createElement("img");
      img.className = "av-img";
      img.src = avUserSrc;
      img.alt = "You";
      img.width = 40;
      img.height = 40;
      av.appendChild(img);
    }
    var msgBody = document.createElement("div");
    msgBody.className = "msg-body";
    var head = document.createElement("div");
    head.className = "msg-head";
    var author = document.createElement("span");
    author.className = "msg-author";
    author.textContent = "You";
    var tm = document.createElement("span");
    tm.className = "mtime";
    tm.textContent = shortNow();
    head.appendChild(author);
    head.appendChild(tm);
    var p = document.createElement("p");
    p.className = "text";
    p.textContent = text;
    var ticks = document.createElement("span");
    ticks.className = "ticks";
    ticks.setAttribute("role", "status");
    msgBody.appendChild(head);
    msgBody.appendChild(p);
    msgBody.appendChild(ticks);
    li.appendChild(av);
    li.appendChild(msgBody);
    // the typing row (when it exists) stays the last thing in the list
    if (typingRow && typingRow.parentNode === list) {
      list.insertBefore(li, typingRow);
    } else {
      list.appendChild(li);
    }
    var rec = { text: normText(text), el: li, ticks: ticks,
                state: "", adopted: false };
    outgoing.push(rec);
    setTickState(rec, "sending");
    return rec;
  }

  // The feed returned this send's own user row: drop the optimistic
  // twin, keep the server row, carry the ticks over onto it while this
  // is still the latest send. rowId also tightens the turn's floor:
  // from here, only output newer than OUR user row can count as this
  // turn's first response.
  function adoptServerRow(rec, html, rowId) {
    if (!html) return;
    rec.el.insertAdjacentHTML("beforebegin", html);
    var real = rec.el.previousElementSibling;
    setTickState(rec, "delivered");
    var latest = outgoing.length > 0 &&
                 outgoing[outgoing.length - 1] === rec;
    if (real && real !== rec.el && latest) {
      var realBody = real.querySelector(".msg-body");
      if (realBody) realBody.appendChild(rec.ticks);
    }
    if (rec.el.parentNode) rec.el.parentNode.removeChild(rec.el);
    rec.adopted = true;
    if (typeof rowId === "number" && rowId > turnFloor) {
      turnFloor = rowId;
    }
  }

  function failSend(rec, text, msg) {
    if (rec) setTickState(rec, "failed");
    waiting = false;
    turnResponded = true;
    holding = false;
    setWaiting();
    setSending(false);  // both modes: the composer comes back
    if (box) {
      // Restore the submitted text without clobbering anything typed
      // after the send: keep both, failed text first so a retry is
      // one press away, newer edit below it — deterministic order,
      // nothing lost either way.
      var current = box.value;
      if (!current || current === text) {
        box.value = text;
      } else {
        box.value = text + "\\n" + current;
      }
      autosize();
    }
    showFlash(msg, 9000);
  }

  // ---- clarify card ---------------------------------------------------
  // The pending clarify question renders as an escaped server-built
  // card above the composer. Single-select choices submit themselves
  // on click; multi-select toggles and answers through Submit (the
  // UI-only Other contributes its typed text, never the label
  // "Other"); free-text questions use the input. The card freezes
  // while its answer is on the wire; a refusal flashes a safe note and
  // re-polls (a new clarify id replaces the card and resets the
  // selection, the same id preserves it exactly as the user left it).
  // Answers go to the local clarify proxy with the exact clarify_id —
  // never to /reply, never as a normal message.
  function clarifyFlash(cardEl, msg) {
    if (!cardEl) return;
    var f = cardEl.querySelector(".clarify-flash");
    if (!f) return;
    f.textContent = msg;
    f.hidden = false;
    window.setTimeout(function () { f.hidden = true; }, 6000);
  }

  function setClarifyDisabled(cardEl, on) {
    if (!cardEl) return;
    var els = cardEl.querySelectorAll("button, input");
    for (var i = 0; i < els.length; i++) els[i].disabled = on;
  }

  function submitClarify(response) {
    var cardEl = document.getElementById("clarify-card");
    if (!cardEl || clarifying) return;
    clarifying = true;
    setClarifyDisabled(cardEl, true);  // frozen while the answer sends
    postJson(sessionUrl("/clarify"), {
      clarify_id: cardEl.getAttribute("data-clarify-id") || "",
      response: response
    }).then(function (resp) {
      return resp.json().catch(function () { return null; })
        .then(function () { return resp.status; });
    }).then(function (status) {
      clarifying = false;
      if (status === 200) {
        // Resolved: drop the card at once; the next poll confirms.
        applyClarify({ active: false, id: "", html: "" });
        return;
      }
      // Refused or stale: recover safely and re-poll for the truth —
      // a new clarify id replaces the card, the same one stays put.
      setClarifyDisabled(cardEl, false);
      clarifyFlash(cardEl, status === 400
        ? "That answer was refused; adjust it and try again."
        : "This question is no longer waiting; refreshing.");
      window.setTimeout(pollOnce, 400);
    }).catch(function () {
      clarifying = false;
      setClarifyDisabled(cardEl, false);
      clarifyFlash(cardEl, "Sending the answer failed; try again.");
      window.setTimeout(pollOnce, 400);
    });
  }

  function wireClarify() {
    var cardEl = document.getElementById("clarify-card");
    if (!cardEl || cardEl.getAttribute("data-wired") === "1") return;
    cardEl.setAttribute("data-wired", "1");
    var multi = cardEl.getAttribute("data-multi") === "1";
    var otherBox = cardEl.querySelector(".clarify-other-box");
    var otherToggle = cardEl.querySelector(".clarify-other-toggle");
    var otherInput = cardEl.querySelector(".clarify-other-input");
    var otherSend = cardEl.querySelector(".clarify-other-send");
    var submitBtn = cardEl.querySelector(".clarify-submit");

    function otherValue() {
      return otherInput ? String(otherInput.value || "").trim() : "";
    }

    // One choice button: single-select submits itself at once;
    // multi-select toggles. (The Other toggle carries no data-value —
    // its label can never become a response.)
    var btns = cardEl.querySelectorAll(".clarify-choice[data-value]");
    for (var i = 0; i < btns.length; i++) {
      (function (btn) {
        btn.addEventListener("click", function () {
          if (multi) {
            var on = btn.getAttribute("aria-pressed") === "true";
            btn.setAttribute("aria-pressed", on ? "false" : "true");
          } else {
            submitClarify(btn.getAttribute("data-value"));
          }
        });
      })(btns[i]);
    }

    if (otherToggle) otherToggle.addEventListener("click", function () {
      var on = otherToggle.getAttribute("aria-pressed") === "true";
      otherToggle.setAttribute("aria-pressed", on ? "false" : "true");
      if (otherBox) otherBox.hidden = on;
      if (!on && otherInput) otherInput.focus();
    });

    if (otherSend) otherSend.addEventListener("click", function () {
      var text = otherValue();
      if (!text) {
        clarifyFlash(cardEl, "Type an answer first.");
        if (otherInput) otherInput.focus();
        return;
      }
      submitClarify(text);
    });

    if (otherInput) otherInput.addEventListener("keydown",
      function (e) {
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
          e.preventDefault();
          if (otherSend) otherSend.click();
          else if (submitBtn) submitBtn.click();
        }
      });

    if (submitBtn) submitBtn.addEventListener("click", function () {
      // Multi-select: every toggled choice, plus the Other text when
      // present — never the literal "Other" label, never an empty list.
      var vals = [];
      var sel = cardEl.querySelectorAll(
        '.clarify-choice[data-value][aria-pressed="true"]');
      for (var j = 0; j < sel.length; j++) {
        vals.push(sel[j].getAttribute("data-value"));
      }
      var text = otherValue();
      if (text) vals.push(text);
      if (!vals.length) {
        clarifyFlash(cardEl, "Pick at least one choice.");
        return;
      }
      submitClarify(vals);
    });
  }

  function applyClarify(cl) {
    // No field at all (a core API error) leaves the page exactly as
    // it is; {active: false} removes the card; an active card replaces
    // the DOM only when its clarify id is NEW — the same id keeps the
    // card untouched, selection included.
    if (!cl || typeof cl !== "object") return;
    var active = !!(cl.active && cl.id && cl.html);
    var current = document.getElementById("clarify-card");
    if (!active) {
      if (current && current.parentNode) {
        current.parentNode.removeChild(current);
      }
      if (clarifyActive) {
        clarifyActive = false;
        applySessionState({ archived: archived });
      }
      return;
    }
    if (current && current.getAttribute("data-clarify-id") === cl.id) {
      return;
    }
    if (current && current.parentNode) {
      current.parentNode.removeChild(current);
    }
    if (form && form.parentNode) {
      form.insertAdjacentHTML("beforebegin", cl.html);
    }
    wireClarify();
    clarifyActive = true;
    applySessionState({ archived: archived });
    syncComposerVar();
    updateJump();
  }

  // ---- inline sub-agent children + Next sub-agent -------------------
  // Child rows and delegate dispatch cards are <li data-child="<key>">
  // elements inside the message list, each carrying its dispatch time
  // in data-ts. Every poll ships the full keyed list; reconciliation
  // diffs it against the DOM by key — independent of the message
  // cursor, so a card inserts and updates whether or not its parent's
  // tool-call carrier has landed (and a static Claude card upgrades in
  // place, no reload, the moment its viewer receipt appears).

  function childRows() {
    if (!list) return [];
    return Array.prototype.slice.call(
      list.querySelectorAll("li[data-child]"));
  }

  function findChildRow(key) {
    if (!list) return null;
    var rows = list.children;
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].getAttribute &&
          rows[i].getAttribute("data-child") === key) {
        return rows[i];
      }
    }
    return null;
  }

  // Park a fresh keyed row at the tail, then walk the timeline to its
  // chronological position: before the first existing row whose data-ts
  // is newer. Every server-rendered row carries data-ts (text bubbles
  // their message time, a tool group its oldest row's time, keyed rows
  // their dispatch time), so a late-discovered dispatch card slots in
  // before later transcript output that is already on screen. Rows
  // without data-ts (an optimistic user bubble, the typing/waiting
  // tails) count as newest, so the card lands above them; one
  // already-rendered tool group the card's time falls inside is not
  // split client-side — the next full render shows the true split.
  function insertChildRow(key, html, ts) {
    if (!list) return;
    if (typingRow) typingRow.insertAdjacentHTML("beforebegin", html);
    else list.insertAdjacentHTML("beforeend", html);
    var el = findChildRow(key);
    if (!el) return;
    var rows = list.children;
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      if (r === el || r === typingRow || r === waitingRow) continue;
      if (!r.hasAttribute || !r.hasAttribute("data-ts")) continue;
      var rts = parseFloat(r.getAttribute("data-ts"));
      if (!isNaN(rts) && rts > ts) {
        list.insertBefore(el, r);
        return;
      }
    }
  }

  // Full-list reconciliation, keyed by data-child: a missing key
  // inserts at its chronological spot, a present key whose rev moved
  // replaces the row in place (state, label, activity, viewer link), a
  // key the server dropped takes its row away. Nothing can duplicate.
  function applySubagents(sub) {
    if (!sub || typeof sub !== "object" || !list) return;
    var items = sub.items;
    if (!Array.isArray(items)) return;
    var seen = {};
    var changed = false;
    for (var i = 0; i < items.length; i++) {
      var c = items[i];
      if (!c || typeof c !== "object" ||
          typeof c.key !== "string" || !c.key ||
          typeof c.html !== "string" || !c.html) continue;
      seen[c.key] = true;
      var current = findChildRow(c.key);
      if (current) {
        if (current.getAttribute("data-rev") !== String(c.rev || "")) {
          current.insertAdjacentHTML("beforebegin", c.html);
          if (current.parentNode) {
            current.parentNode.removeChild(current);
          }
          changed = true;
        }
      } else {
        var ts = parseFloat(c.started);
        insertChildRow(c.key, c.html, isNaN(ts) ? 0 : ts);
        changed = true;
      }
    }
    var rows = childRows();
    for (var r = 0; r < rows.length; r++) {
      if (!seen[rows[r].getAttribute("data-child")] &&
          rows[r].parentNode) {
        rows[r].parentNode.removeChild(rows[r]);
        changed = true;
      }
    }
    // A stepped row the server dropped ends that sequence cleanly:
    // the next click restarts from the fallback, never from a key with
    // no row behind it. (stepNextSub revalidates the key too; this
    // just keeps the state honest at the reconciliation boundary.)
    if (lastSubKey && !findChildRow(lastSubKey)) lastSubKey = null;
    if (changed) updateNextSub();
  }

  // Build the floating control the moment a first keyed row exists
  // (the server omits it entirely on pages without children); its
  // markup is fixed text, so no child-controlled string is ever
  // interpolated. A poll-built anchor appears AFTER the one-time
  // startup wiring ran, so it attaches its own stepper here — exactly
  // once (the early return guards re-entry), mirroring the listener the
  // server-rendered pill gets at startup; without it the dynamic
  // control stays a dead href="#" anchor.
  function ensureNextSub() {
    if (nextSub || !mainEl) return;
    var a = document.createElement("a");
    a.className = "next-subagent";
    a.id = "next-subagent";
    a.href = "#";
    a.innerHTML = '<span aria-hidden="true">&darr;</span> ' +
                  'Next sub-agent';
    a.addEventListener("click", stepNextSub);
    mainEl.appendChild(a);
    nextSub = a;
  }

  // The control tracks row existence only — never scroll position.
  function updateNextSub() {
    if (!childRows().length) {
      if (nextSub) nextSub.classList.add("is-hidden");
      return;
    }
    ensureNextSub();
    if (nextSub) nextSub.classList.remove("is-hidden");
  }

  function rowTopInScroller(el) {
    return el.getBoundingClientRect().top -
           scroller.getBoundingClientRect().top + scroller.scrollTop;
  }

  // One step: advance from the last stepped row by stable identity
  // (data-child), wrapping past the last back to the first — identity,
  // not measured scroll position, because a short viewport clamps
  // scrollIntoView at the scroller's maximum, where a lower row still
  // reads as "below the line" and a position-based next would reselect
  // it forever. Only a first click (or one after the stepped row
  // vanished from the feed) falls back to geometry, and even then an
  // exact/near-bottom scroller wraps to the first row instead of
  // chasing a row it can never scroll up. The anchor's href follows
  // the target so a follow-up plain navigation (or a no-JS reload of
  // the same page) keeps pointing somewhere honest.
  function stepNextSub(e) {
    if (e) e.preventDefault();
    var rows = childRows();
    if (!rows.length || !scroller) return;
    var target = null;
    var lastIdx = -1;
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].getAttribute("data-child") === lastSubKey) {
        lastIdx = i;
        break;
      }
    }
    if (lastIdx >= 0) {
      target = rows[(lastIdx + 1) % rows.length];
    } else {
      // Exact/near bottom (2px slack for fractional layout): the
      // scroller sits at its maximum, so no strictly lower row exists.
      var maxTop = scroller.scrollHeight - scroller.clientHeight;
      if (scroller.scrollTop < maxTop - 2) {
        var line = scroller.scrollTop + 8;
        for (var j = 0; j < rows.length; j++) {
          if (rowTopInScroller(rows[j]) > line) { target = rows[j]; break; }
        }
      }
      if (!target) target = rows[0];
    }
    lastSubKey = target.getAttribute("data-child") || null;
    if (nextSub && target.id) {
      nextSub.setAttribute("href", "#" + target.id);
    }
    try {
      target.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      scroller.scrollTop = rowTopInScroller(target);
    }
  }

  // The Live activity strip rides every poll like the subagents
  // section: whatever the server sends replaces the current strip (or
  // removes it), so in-flight tool calls appear the moment their
  // carrier lands and the strip vanishes the moment the turn settles.
  // It sits directly under the message list — never inside it — so it
  // can never come between a new row and the typing row.
  function applyActivity(act) {
    var current = document.getElementById("live-activity");
    var html = (act && typeof act === "object" && act.html) || "";
    var had = !!current;
    if (current && current.parentNode) {
      current.parentNode.removeChild(current);
    }
    if (html && list && list.parentNode) {
      list.insertAdjacentHTML("afterend", html);
    }
    return html ? !had : had;  // did the strip's presence change?
  }

  // One feed poll: append display items newer than the cursor (server
  // rendered, same markup as the page), swap optimistic twins for the
  // real rows, keep the typing and waiting states honest, surface any
  // one-shot failure note.
  function applyFeed(data) {
    if (!data || typeof data !== "object") return;
    var msgs = data.messages || [];
    var stick = nearBottom();  // decided before anything is appended
    var grew = false;
    var agentLanded = false;
    for (var i = 0; i < msgs.length; i++) {
      var m = msgs[i];
      if (!m || typeof m.id !== "number" || m.id <= cursor) continue;
      if (m.kind === "text" && m.role !== "user") agentLanded = true;
      // first-response bookkeeping: remember every non-user row id of
      // this turn; whether one of them counts is decided after the
      // loop, against the final floor (adoption may have raised it)
      if (!turnResponded &&
          (m.kind !== "text" || m.role !== "user")) {
        turnOutputs.push(m.id);
      }
      var twin = null;
      if (m.kind === "text" && m.role === "user" && m.html) {
        twin = findOutgoing(normText(m.text));
      }
      if (twin) {
        adoptServerRow(twin, m.html, m.id);
        grew = true;
      } else if (m.html) {
        // Tool-group seam: when this poll re-delivers a maximal run
        // the page already drew part of (same first row id), replace
        // the older, shorter group instead of appending — one run
        // stays one group however its rows split across polls, and
        // the first_id match can never bridge intervening text.
        var merged = false;
        if (m.kind === "tools" && typeof m.first_id === "number" &&
            list) {
          var prev = (typingRow && typingRow.parentNode === list)
            ? typingRow.previousElementSibling
            : list.lastElementChild;
          if (prev && prev.classList.contains("tool-group") &&
              prev.getAttribute("data-first-id") ===
                String(m.first_id)) {
            prev.insertAdjacentHTML("beforebegin", m.html);
            if (prev.parentNode) prev.parentNode.removeChild(prev);
            merged = true;
          }
        }
        if (!merged) {
          // new rows land just before the typing row, so it stays last
          if (typingRow) {
            typingRow.insertAdjacentHTML("beforebegin", m.html);
          } else if (list) {
            list.insertAdjacentHTML("beforeend", m.html);
          }
        }
        grew = true;
      }
      cursor = m.id;
    }
    if (!turnResponded) {
      for (var t = 0; t < turnOutputs.length; t++) {
        if (turnOutputs[t] > turnFloor) { turnResponded = true; break; }
      }
    }
    if (typeof data.last_id === "number" && data.last_id > cursor) {
      cursor = data.last_id;
    }
    if (busy) everBusy = true;
    var wasBusy = busy;
    busy = !!data.busy;
    // A busy-state transition moves this session's sidebar row between
    // Active and its resting section — re-render it from the server's
    // own classification, only on the transition itself (never per
    // poll).
    if (wasBusy !== busy) refreshSidebar();
    // hermes is working on it (or has answered): everything sent so
    // far has at least been Read.
    if (busy || agentLanded) markOutgoing("read");
    // Stop waiting once THIS turn's first output landed, the job
    // reported failure, or a busy run finished — never on the pre-job
    // gap after a send, and never on a historical row.
    if (turnResponded || data.note || (everBusy && !busy)) {
      waiting = false;
      everBusy = false;
    }
    if (applyActivity(data.activity)) grew = true;
    setTyping();
    setWaiting();
    if (data.note) showFlash(data.note, 9000);
    applySubagents(data.subagents);
    applySessionState(data.session_state);
    applyClarify(data.clarify);
    if (grew) {
      if (emptyState) emptyState.hidden = true;
      if (stick) toBottom();
      updateJump();
    }
  }

  function schedulePoll() {
    if (mode !== "chat") return;
    if (pollTimer) window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(
      pollOnce, (busy || waiting) ? busyPollMs : pollMs);
  }
  function pollOnce() {
    if (document.hidden) { schedulePoll(); return; }
    var seq = ++feedSeq;
    window.fetch(sessionUrl("/feed?after=" + cursor), { cache: "no-store" })
      .then(function (resp) { return resp.ok ? resp.json() : null; })
      .then(function (data) {
        // last-request-wins: a superseded poll's busy flag (and rows)
        // are dropped — the newer poll re-fetches everything it needs
        if (data && seq === feedSeq) applyFeed(data);
      })
      .catch(function () {})
      .then(schedulePoll);
  }

  function autosize() {
    if (!box) return;
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight, 160) + "px";
    syncComposerVar();
  }
  function setSending(on) {
    sending = on;
    if (box) box.disabled = on || archived || clarifyActive;
    if (sendBtn) sendBtn.disabled = on || archived || clarifyActive;
    if (!on) autosize();
  }

  // ---- new-session job polling ---------------------------------------
  // The launch runs server-side after a fast 202; the client only
  // watches GET <status_url> and leaves for the session page the
  // moment the correlated session id is published — long before the
  // run finishes. Polling is bounded (the server kills the run at
  // its own 900 s timeout; JOB_MAX_MS leaves that headroom); a
  // terminal failure or an unknown job never retries the launch, it
  // fails the send instead.
  var jobTimer = null;
  var JOB_POLL_MS = 700;
  var JOB_MAX_MS = 960000;
  function stopJobPoll() {
    if (jobTimer) { window.clearTimeout(jobTimer); jobTimer = null; }
  }
  function pollJob(url, rec, text) {
    stopJobPoll();
    var deadline = Date.now() + JOB_MAX_MS;
    var tick = function () {
      window.fetch(url, { cache: "no-store" })
        .then(function (resp) {
          if (resp.status === 404) {
            // unknown/pruned job: terminal, never retried
            throw { terminal: true };
          }
          if (!resp.ok) throw {};  // transient server error: retry
          return resp.json();
        })
        .then(function (st) {
          if (st && st.status === "failed") {
            throw { terminal: true, note: st.error || "" };
          }
          if (st && st.session_id) {
            // the correlated session row exists — go to it now,
            // without waiting for the run to finish
            if (rec) setTickState(rec, "delivered");
            stopJobPoll();
            locked = true;  // the browser is off to the new transcript
            window.location.assign(
              "/s/default/" + encodeURIComponent(st.session_id));
            return;
          }
          if (Date.now() >= deadline) {
            throw { terminal: true,
                    note: "the launch is taking too long" };
          }
          jobTimer = window.setTimeout(tick, JOB_POLL_MS);
        })
        .catch(function (err) {
          if (err && err.terminal) {
            failSend(rec, text, err.note
              ? ("Starting the session failed — " + err.note +
                 ". Your text is back in the composer.")
              : "The session could not be started; your text is back " +
                "in the composer.");
            return;
          }
          // a network blip is not a verdict: keep polling to the bound
          if (Date.now() >= deadline) {
            failSend(rec, text, "Starting the session is taking too " +
              "long; it was given up. Your text is back in the composer.");
            return;
          }
          jobTimer = window.setTimeout(tick, JOB_POLL_MS);
        });
    };
    tick();
  }

  function send() {
    if (sending || locked || holding || archived || clarifyActive ||
        !box) return;
    var text = box.value;
    if (!text.trim()) { box.focus(); return; }
    // The message is on screen before anything leaves the client, so a
    // long /s/new launch still shows the message and its status.
    var rec = addOutgoing(text);
    if (emptyState) emptyState.hidden = true;
    box.value = "";
    autosize();
    toBottom();
    updateJump();
    setSending(true);
    var url = mode === "new" ? "/s/new" : sessionUrl("/reply");
    postJson(url, { text: text }).then(function (resp) {
      if (mode === "new") {
        // "Sending\N{HORIZONTAL ELLIPSIS}" ends the moment the POST
        // resolves: a 202 is an accepted launch (job polling owns the
        // rest), anything else is a refusal.
        if (resp.status === 202) {
          return resp.json().then(function (accepted) {
            if (!accepted || !accepted.status_url) {
              throw new Error("no job");
            }
            if (rec) setTickState(rec, "sent");
            beginTurn();
            holding = true;  // composer stays locked while the job runs
            pollJob(accepted.status_url, rec, text);
          });
        }
        if (resp.status === 409) {
          failSend(rec, text, "A new session is already starting; " +
            "try again in a moment.");
          return;
        }
        if (resp.status === 400 || resp.status === 413) {
          failSend(rec, text, "That message was refused (empty or too " +
            "long).");
          return;
        }
        throw new Error("HTTP " + resp.status);
      }
      if (resp.status === 202) {
        // Accepted: transport done, the waiting indicator takes over
        // until this turn's first output is persisted. The job already
        // owns the session server-side, so the sidebar's selected row
        // re-renders under Active right now — no reload involved.
        if (rec) setTickState(rec, "sent");
        beginTurn();
        refreshSidebar();
        schedulePoll();
        return;
      }
      if (resp.status === 409) {
        // The turn was refused — another reply already running, or the
        // session closed. The text never reached the session, so the
        // optimistic row must fail (never Sent/Read) and the composer
        // must come back with the exact text restored for retry.
        failSend(rec, text, "A reply is already running here; your " +
          "message was not sent and is back in the composer.");
        return;
      }
      if (resp.status === 404) {
        failSend(rec, text, "This session can no longer be found.");
        return;
      }
      if (resp.status === 400 || resp.status === 413) {
        failSend(rec, text, "That message was refused (empty or too long).");
        return;
      }
      throw new Error("HTTP " + resp.status);
    }).catch(function () {
      failSend(rec, text, mode === "new"
        ? "Could not start the session; please try again."
        : "The send failed; nothing was added to the session.");
    }).then(function () {
      if (!locked && !holding) {
        // /reply's POST is tiny: the composer is usable again right
        // away. /new stays locked while its launch job runs (released
        // by failSend on failure; navigation leaves the page on
        // success) with the text restored by failSend where it failed.
        setSending(false);
        if (mode !== "new") box.focus();
        autosize();
      }
    });
  }

  // Close/Reopen: POST the toggle endpoint with the button disabled
  // while the request is in flight, flash on any failure, then apply
  // the returned state (never the hoped-for one — the server only
  // reports ok after Discord confirmed, or the local flip stuck).
  if (toggleBtn) toggleBtn.addEventListener("click", function () {
    var action = toggleBtn.getAttribute("data-action") ||
                 (archived ? "reopen" : "close");
    toggleBtn.disabled = true;
    postJson(sessionUrl("/" + action), {})
      .then(function (resp) {
        return resp.json().catch(function () { return null; })
          .then(function (data) { return { status: resp.status, data: data }; });
      })
      .then(function (r) {
        if (r.data && r.data.ok) {
          applySessionState({ archived: r.data.archived });
        } else {
          showFlash((r.data && r.data.error) ||
                    "The session state change failed.", 9000);
        }
      })
      .catch(function () {
        showFlash("The session state change failed.", 9000);
      })
      .then(function () { toggleBtn.disabled = false; });
  });

  if (form) form.addEventListener("submit", function (e) {
    e.preventDefault();
    send();
  });
  if (box) {
    // Enter sends; Shift+Enter (or any modifier-Enter) is a newline.
    box.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey &&
          !e.metaKey && !e.altKey && !e.isComposing) {
        e.preventDefault();
        send();
      }
    });
    box.addEventListener("input", autosize);
  }
  if (jump && end && scroller) {
    jump.addEventListener("click", function (e) {
      e.preventDefault();
      toBottom();
    });
    scroller.addEventListener("scroll", updateJump, { passive: true });
    window.addEventListener("resize", function () {
      syncComposerVar();
      updateJump();
    });
  }
  if (nextSub) nextSub.addEventListener("click", stepNextSub);

  // Land near the newest message, then sync the button — and the child
  // stepper — with whatever the server already rendered.
  if (scroller) scroller.scrollTop = scroller.scrollHeight;
  autosize();
  syncComposerVar();
  updateJump();
  updateNextSub();
  setTyping();
  setWaiting();
  applySessionState({ archived: archived });
  wireClarify();   // a server-rendered card is interactive from boot
  schedulePoll();
})();
</script>
</body>
</html>
""")


# The pending clarify card rides the chat shell (never /new: no session
# means nothing can be asking), so its CSS is a token render_chat()
# fills in and render_new() leaves empty.
CLARIFY_CARD_CSS = """/* ---- clarify card ------------------------------------------------------
   A pending clarify question as a Discord-style embed pinned directly
   above the composer while the agent waits for the answer. */
.clarify-card {
  flex: none; margin: 0 16px 12px; padding: 10px 14px 12px;
  background: var(--embed); border-radius: 4px;
  border-left: 4px solid var(--blurple);
}
.clarify-tag {
  display: inline-block; margin-bottom: 6px;
  font-size: 10.5px; font-weight: 700; letter-spacing: 0.4px;
  color: var(--agent-name); text-transform: uppercase;
}
.clarify-question {
  margin: 0 0 8px; font-size: 14px; line-height: 20px;
  color: var(--ink-2); white-space: pre-wrap; overflow-wrap: anywhere;
}
.clarify-choices {
  display: flex; flex-wrap: wrap; gap: 6px;
}
.clarify-choice {
  height: 30px; padding: 0 12px; border-radius: 4px;
  border: 1px solid var(--line); background: var(--field);
  color: var(--ink-2); font: inherit; font-size: 13px; cursor: pointer;
  max-width: 100%; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap;
}
.clarify-choice:hover:not(:disabled) {
  border-color: var(--blurple); color: var(--ink);
}
.clarify-choice:focus-visible {
  outline: 2px solid var(--blurple); outline-offset: 1px;
}
.clarify-choice[aria-pressed="true"] {
  background: var(--blurple); border-color: var(--blurple); color: #fff;
}
.clarify-choice:disabled { opacity: 0.5; cursor: default; }
.clarify-other-box {
  display: flex; gap: 6px; margin-top: 8px; min-width: 0;
}
.clarify-other-box[hidden] { display: none; }
.clarify-other-input {
  flex: 1; min-width: 0; height: 34px; padding: 0 10px;
  border-radius: 4px; border: 1px solid var(--line);
  background: var(--field); color: var(--ink-2); font: inherit;
  font-size: 13px;
}
.clarify-other-input:focus { outline: none; border-color: var(--blurple); }
.clarify-send {
  flex: none; height: 34px; padding: 0 14px; border: none;
  border-radius: 4px; background: var(--blurple); color: #fff;
  font: inherit; font-size: 13px; font-weight: 600; cursor: pointer;
}
.clarify-send:hover:not(:disabled) { background: var(--blurple-hover); }
.clarify-send:focus-visible {
  outline: 2px solid var(--blurple); outline-offset: 1px;
}
.clarify-send:disabled { opacity: 0.5; cursor: default; }
.clarify-submit { display: block; margin-top: 8px; }
.clarify-flash {
  margin: 8px 0 0; font-size: 12px; color: var(--yellow);
}
.clarify-flash[hidden] { display: none; }
@media (max-width: 900px) { .clarify-card { margin: 0 12px 8px; } }
"""


def render_clarify_card(card):
    """The pending clarify card -> its escaped Discord-style embed HTML
    (rendered directly above the composer).

    Every field is HTML-escaped, question and choices included; each
    choice value also rides a data-value attribute (escaped with
    quotes) so the client sends back exactly what it received. "Other"
    is a UI-only affordance — its label is never a response — and the
    open-text input covers both free-text questions and the Other
    path. The card-level Submit button exists only for multi-select
    cards; a single-select choice submits itself the moment it is
    clicked."""
    if not card:
        return ""
    esc = html.escape
    multi = bool(card.get("multi_select"))
    choices = card.get("choices") or []
    out = [
        '<div class="clarify-card" id="clarify-card" '
        'data-clarify-id="%s" data-multi="%d" role="group" '
        'aria-label="Clarification question">\n'
        % (esc(card["clarify_id"], quote=True), 1 if multi else 0),
        '  <span class="clarify-tag">Clarify</span>\n',
        '  <p class="clarify-question">%s</p>\n' % esc(card["question"]),
    ]
    if choices:
        out.append('  <div class="clarify-choices">\n')
        for choice in choices:
            out.append(
                '    <button type="button" class="clarify-choice" '
                'data-value="%s">%s</button>\n'
                % (esc(choice, quote=True), esc(choice)))
        out.append('    <button type="button" class="clarify-choice '
                   'clarify-other-toggle" aria-pressed="false" '
                   'data-other="1">Other</button>\n')
        out.append('  </div>\n')
    # The open-text input: visible from the start on a free-text
    # question, revealed by the Other toggle otherwise. Its own send
    # button exists only when it is the sole submit path (single
    # select); a multi-select card answers through its own Submit.
    other_send = "" if (multi and choices) else (
        '    <button type="button" class="clarify-send '
        'clarify-other-send">Submit</button>\n')
    out.append(
        '  <div class="clarify-other-box"%s>\n'
        '    <input class="clarify-other-input" type="text" '
        'placeholder="Type your answer" aria-label="Your answer">\n'
        '%s'
        '  </div>\n' % ("" if not choices else " hidden", other_send))
    if multi and choices:
        out.append('  <button type="button" class="clarify-send '
                   'clarify-submit">Submit</button>\n')
    out.append('  <p class="clarify-flash" role="status" hidden></p>\n')
    out.append('</div>\n')
    return "".join(out)


# The typing indicator and the live activity strip exist only on a real
# session's page: /new must never carry either — nor their CSS, nor the
# selector that would find them — so all of them live in tokens
# render_chat() fills in and render_new() leaves empty. The waiting
# row is the exception: it shows on BOTH pages (a first send on /new
# waits for its first response exactly like a reply does), so both
# renderers fill its tokens with the default-profile identity.
LIVE_CSS = """/* ---- live activity strip ---------------------------------------------
   The current turn's in-flight tool state, directly under the
   transcript rows: one slim row per unresolved call (tool chip, state
   label, bounded redacted argument summary) or the single weakest
   truthful state while hermes works between tools. It only ever
   represents the present — completed tool history stays in its
   collapsed tool group — and it disappears the moment the turn ends.
   The generic typing row never shows beside it. Rendered as a
   blurple-accented embed, aligned with the message text column. */
.live-activity {
  margin: 12px 48px 0 72px; padding: 8px 12px 10px;
  background: var(--embed); border-radius: 4px;
  border-left: 4px solid var(--blurple);
}
.la-head {
  display: flex; align-items: center; gap: 7px;
  margin: 0; font-size: 11px; font-weight: 700;
  letter-spacing: 0.04em; text-transform: uppercase;
  color: #c9cdfb;
}
.la-dot {
  width: 7px; height: 7px; border-radius: 50%; background: var(--blurple);
  position: relative; flex: none;
}
.la-dot::after {
  content: ""; position: absolute; inset: -3px; border-radius: 50%;
  border: 1px solid var(--blurple);
  animation: la-pulse 2.4s ease-out infinite;
}
@keyframes la-pulse {
  0% { transform: scale(0.6); opacity: 0.7; }
  70%, 100% { transform: scale(1.5); opacity: 0; }
}
.la-list { list-style: none; margin: 7px 0 0; padding: 0; display: grid; gap: 5px; }
.la-row {
  display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap;
  min-width: 0; font-size: 13px;
}
.la-row.la-only .la-state { color: var(--ink); }
.la-state { color: var(--ink-2); font-weight: 550; white-space: nowrap; }
.la-args {
  flex: 1; min-width: 0; color: var(--muted); font-size: 11px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas,
               "Liberation Mono", monospace;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
@media (max-width: 900px) {
  .live-activity { margin: 12px 12px 0 64px; }
}
@media (prefers-reduced-motion: reduce) {
  .la-dot::after { animation: none; opacity: 0; }
}
"""

TYPING_CSS = """/* ---- typing indicator ----------------------------------------------
   Same flat group silhouette as an agent message, pinned to the tail
   of the list. Shown only while the feed reports the session busy
   (hermes really working) and hidden again as soon as that clears. */
.msg.typing-row { margin-top: 12px; }
.typing-dots { padding: 2px 0; }
.typing-dots i {
  display: inline-block; width: 7px; height: 7px; margin-right: 5px;
  border-radius: 50%; background: var(--muted);
  animation: typing 1.2s ease-in-out infinite;
}
.typing-dots i:last-child { margin-right: 0; }
.typing-dots i:nth-child(2) { animation-delay: 0.15s; }
.typing-dots i:nth-child(3) { animation-delay: 0.3s; }
@keyframes typing {
  0%, 60%, 100% { transform: translateY(0); opacity: 0.45; }
  30% { transform: translateY(-3px); opacity: 1; }
}
"""

WAITING_CSS = """/* ---- waiting-for-first-response row -----------------------------------
   The pre-response half of send progress: from the moment a send is
   accepted (202) until the first assistant or tool output of that same
   turn is persisted — after that the specific Live activity view owns
   the tail. Deliberately distinct from the delivery ticks under the
   message (transport only) and from the typing dots (hermes
   demonstrably working). role=status on the text keeps it announced
   politely; a quiet pulse marks the wait without motion overkill. */
.msg.waiting-row { margin-top: 12px; }
.waiting-text {
  display: flex; align-items: center; gap: 8px;
  color: var(--ink-2); font-weight: 550;
}
.waiting-text .w-dot {
  flex: none; width: 7px; height: 7px; border-radius: 50%;
  background: var(--muted); position: relative;
}
.waiting-text .w-dot::after {
  content: ""; position: absolute; inset: -3px; border-radius: 50%;
  border: 1px solid var(--muted);
  animation: wait-pulse 2.4s ease-out infinite;
}
@keyframes wait-pulse {
  0% { transform: scale(0.6); opacity: 0.7; }
  70%, 100% { transform: scale(1.5); opacity: 0; }
}
@media (prefers-reduced-motion: reduce) {
  .waiting-text .w-dot::after { animation: none; opacity: 0; }
}
"""

# A %-format exactly like TYPING_ROW: render_chat() fills in the
# session profile's identity (label, letter, optional avatar image),
# render_new() the default profile's — the waiting row shows on both
# pages, because a first send on /new waits for its first response too.
WAITING_ROW = """    <li class="msg from-agent waiting-row" id="waiting-row" hidden>
      <span class="avatar" title="%s (assistant)"><span aria-hidden="true">%s</span>%s</span>
      <div class="msg-body">
        <div class="msg-head"><span class="msg-author">%s</span></div>
        <p class="text waiting-text" role="status"><i class="w-dot" aria-hidden="true"></i><span>Waiting for first response&hellip;</span></p>
      </div>
    </li>
"""

# A %-format (not $tokens: substituted values are never re-scanned) —
# render_chat() fills in the current chat profile's label, badge letter
# and optional avatar image.
TYPING_ROW = """    <li class="msg from-agent typing-row" id="typing-row" hidden>
      <span class="avatar" title="%s (assistant)"><span aria-hidden="true">%s</span>%s</span>
      <div class="msg-body">
        <div class="msg-head"><span class="msg-author">%s</span></div>
        <p class="text typing-dots" role="status" aria-label="%s is working"><i aria-hidden="true"></i><i aria-hidden="true"></i><i aria-hidden="true"></i></p>
      </div>
    </li>
"""
