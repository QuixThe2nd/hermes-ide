#!/usr/bin/env python3
"""Web view of Hermes sessions active in the last 24 hours, with live
updates and a composer that talks back to Hermes.

Serves a single page (GET /) listing every non-hidden session whose
last_activity_at falls in [now - 24h, now], across the main state.db
(profile "default") and each <HERMES_HOME>/profiles/*/state.db
discovered via get_hermes_home().
Each row also shows the session's last message — the newest messages
row with role 'user' or 'assistant' and usable text — and its last
tool — the newest messages row with role 'tool' and a tool_name —
each fetched with one batched query per DB. For messages, only the
first 800 characters of content are ever selected and Discord
bookkeeping (the "[Triggering message id: …]" block and the
"[username]" sender prefix) is stripped before display; for tools,
only session_id/tool_name/timestamp/id/role ever leave the DB.
Python 3 stdlib only, plus the repo's hermes_constants helpers for
Hermes-home discovery.

Sub-agent sessions (source='subagent') never appear as inbox rows:
the window query excludes them by stored source alone, so Discord
continuation sessions — which also carry a parent_session_id — keep
their rows. Instead, each conversation page shows a compact
"Sub-agents" section under the header: the session's direct children
by parent_session_id, one link per child with a status dot (Running
while ended_at IS NULL, Done for agent_close or a clean cli_close,
otherwise a safely classified Interrupted/Failed/Ended), a label
(title or display_name,
else the cleaned first user message, clamped hard so a whole prompt
never dumps) and a relative last-activity time. The section is fully
absent when there are no children and on /new; a child's own page is
a normal transcript that can carry its own children, so navigation
recurses naturally. It costs one bounded query per page (plus one
batched first-user-message query only when some child is untitled),
on the parent's own read-only profile DB.

Confidently linked runs from *other* profiles join that same section
(and leave the inbox) instead of posing as top-level conversations:
a delegate_research job's worker sessions link to the origin session
recorded in the job's request.json when their first user message is
exactly equal to one of the job's prompts/*.md inside that job's
request/status time window, and a terminal tool result carrying a
standalone "session_id: <id>" line links as a child of the session
that ran it whenever the id resolves to exactly one other discovered
profile DB. Both discovery passes read only durable artifacts, stay
bounded to the 24-hour window plus a small documented skew, and share
one briefly cached snapshot. Their failure modes split by profile:
worker profiles (researcher) hold the inbox closed — a researcher
session never has a top-level row whether or not a link resolved, and
one that cannot be confidently linked is absent everywhere rather
than guessed onto a parent — while every other link fails open: a
malformed job, a locked DB or an ambiguous id leaves that row
top-level, and direct human-facing chats (any profile, any source)
always keep their inbox rows. Each child row carries its own profile:
the link, the identity badge and the row metadata name the child's
profile, never the parent's.

The inbox is split into honest sections, still one dense messenger
list, and every open session renders above every closed one.
Open/closed is the partition the core listing already draws on the
projected tip: a conversation whose surfaced row carries ended_at is
closed — archived or not, an ended conversation never sits inside an
open-labeled section — and so is an archived one (mirrored from
Discord or closed locally); only ended_at NULL and unarchived is
open. The open buckets render first — Active (a live unexpired
session_turn_leases row for the conversation — the holder's
"turn=<continuation id>" parsed conservatively, conversation_id as
the fallback — or a composer reply this server is currently running
for the session; never for a conversation the tip already ended or
archived), then Open · unfinished (everything else — stale user- or
tool-ended transcripts are never mislabeled completed) and Open ·
completed (the newest active non-hidden event is a plain assistant
answer) — and Closed strictly last, however recent a closed
session is. Every closed row says why with an explicit Archived or
Ended chip and subdued styling, and the Closed block is a collapsed
disclosure so open work stays on top literally and visually.
Each section carries a stable id, a visible count badge and stays
newest-first inside itself; the search filter covers every section
and hides one with no visible children. A DB without the lease
table degrades to Completed/Incomplete classification with at most
one note.

The page is a dark messenger-style inbox rendered entirely server-side
(no external assets): one dense
full-width row per conversation, separated by hairlines — a circular
badge of the owning Hermes profile (its initial by default, or the
optional local avatar image described below),
the conversation name with a compact profile badge and the relative
time on the right,
a one-line last-message preview, and an optional tiny muted chip for
the session's last tool. A small inline script adds client-side row
filtering and swaps in a fresh copy of "/" every 10 s without a reload.

Participant avatars are optional and strictly local: an install that
wants pictures drops one PNG per identity beside that profile's state
DB — <profile home>/mission-control/avatar.png, and the main home's
mission-control/user.png for the person at the keyboard — and every
badge layers that image over its letter fallback (GET /avatar/
<profile> and GET /avatar-user serve exactly those fixed names,
re-resolved against the discovered mapping and the configured home
root on every request, never from URL input). With no files there is
nothing to fetch and nothing to break: a clean install renders pure
letter badges, a missing/unreadable/oversized/escaped file falls
back to the letter, and a broken image in the browser steps aside
client-side so the letter shows through.

Each row is one big link to GET /s/<profile>/<session_id>, a chat-style
transcript served from that profile's DB only (the profile
must be "default" — the main state.db — or a directory under
<HERMES_HOME>/profiles/, and the session id must match
^[A-Za-z0-9_.-]+$). The conversation renders oldest-first (ordered by
timestamp then id, every displayable row — no cap) on a
~900px centered canvas under one sticky header (back link, title,
profile pill, time range). User text is right-aligned in a blue-tinted
bubble (~72% wide) with a circular "You" badge on the right;
agent text is left-aligned on a neutral surface (~78%) with the owning
profile's badge on the left; each bubble carries
a subtle timestamp. The badges are letters on a colored disc, with
the optional local avatar image (see above) layered on top when that
profile has one — the letter always shows through as the fallback.
Runs of consecutive tool rows collapse into ONE
compact expandable group — "N tool calls" plus tool-name chips, opening
to a chronological list where each tool keeps its optional collapsed
details block holding substr(content,1,400). Grouping compresses the
drawing; the empty state shows only
when nothing at all is renderable. session_meta rows and
display_kind='hidden' rows are skipped, and user/assistant text is
Discord-envelope-stripped, HTML-escaped and pre-wrap (markdown is never
rendered). A Codex tool-call assistant row whose content is empty
narrates only in codex_message_items; exactly those rows also select a
substr-bounded slice of that JSON (never the whole blob) and recover
its visible commentary — parsed defensively, only assistant
"message" items' output_text/text blocks — so the narration renders as
a normal agent message and splits tool groups, while a row that already
has content stays the sole authority (never duplicated) and a carrier
with nothing recoverable still disappears. Malformed, truncated,
legacy, wrong-role or reasoning/tool JSON yields no text and never
breaks the page. substr(content,1,4000) caps every row in SQL, so giant
tool JSON never leaves the DB whole. A dependency-free inline script scrolls
near the newest message on load and floats a "Jump to latest" button
that appears only while away from the bottom; with JavaScript off the
button still works as a plain #latest anchor. Unknown profiles, ids
outside the pattern, unknown sessions and every other path get a themed
404.

Interactive layer (all same-origin, relative URLs, stdlib only):

- GET /s/<profile>/<id>/feed?after=<message_id> — JSON {messages,
  last_id, busy, subagents} of the session's rows newer than <after>
  (0 or omitted = the same full snapshot the transcript page
  renders), with the
  exact transcript display rules: session_meta/hidden/[SILENT] skipped,
  empty-content assistant carriers' Codex commentary recovered exactly
  like the page renders it, consecutive tools grouped. Each entry
  carries the same server-rendered
  <li> HTML the page itself uses (plus its plain text, so the client can
  match a stored user row against its optimistic twin), so appended rows
  can't drift from server-rendered ones; last_id is the cursor for the
  next poll. busy reports whether a composer reply is currently running
  for the session. subagents (a backwards-compatible addition) carries
  the inline sub-agent child rows AND the delegate dispatch cards as one
  keyed list — {count, ids, items:[{key, id, profile, started, state,
  rev, html}]} — re-shipped in full on every poll, so an open page
  reconciles both by key (insert at the chronological spot, update in
  place exactly once per real change, never duplicate, never reload)
  whether or not the parent's tool-call carrier has landed.
- The transcript page polls that feed every ~2 s (faster while a reply
  is in flight) and appends new user/agent bubbles and tool groups in
  place — no reload; it auto-scrolls only when already near the bottom.
  A sticky bottom composer (textarea + Send; Enter sends, Shift+Enter is
  a newline) POSTs /s/<profile>/<id>/reply (or /s/new on the blank
  /new page). Every send appends an optimistic user bubble at once and
  walks it through iMessage-style delivery ticks under the bubble —
  Sending (ONLY while the POST itself is unresolved), Sent (202
  accepted), Delivered (the feed echoed the stored user row), Read (the
  feed reports busy, or an answer landed) — and the server-rendered row
  replaces the twin once the DB has it, so a message is never doubled.
  A failed POST (or a failed /s/new launch) marks the bubble Failed,
  restores the text to the composer and re-enables it; 409 (reply or
  launch already running) only flashes a note. Send progress has two
  distinct halves: the ticks cover transport only, and from acceptance
  a separate accessible "Waiting for first response…" row holds the
  tail until the first assistant or tool output OF THAT TURN is
  persisted — scoped client-side to rows newer than the turn's own
  echoed user row (the feed cursor at acceptance, tightened on
  adoption), so a historical answer arriving through a lagging cursor
  can never satisfy a new turn. Once output exists the Live activity
  strip owns the tail instead. The typing dots appear only while the
  feed reports busy === true, never on /new, never merely because a
  send is waiting, and never beside the waiting row or the strip.
- POST /s/<profile>/<id>/reply — body application/json {"text": ...};
  validates the session exists in that profile DB (404), rejects
  empty text (400) and oversize bodies (413), and refuses a closed
  session (409). The turn itself is admitted synchronously as one run
  on the profile-scoped core API surface (POST /v1/runs, authorized
  with that profile's own API key): 202 only once the core has
  admitted the run — one in-flight turn per session, 409 while one is
  already running — and 503 when the core could not admit, which is
  an explicit failed send with the text restored, never a silent
  fallback, and nothing was created. The prompt travels only inside
  that one admission request's JSON body; a background poller then
  holds the session's busy lease until the run settles (a wedged run
  is stopped best-effort at a hard deadline, so it can never hold a
  session busy forever) and leaves the feed a one-shot canned note
  when the turn did not complete. The run's agent carries the gateway
  clarify callback, so a mid-turn question pauses as the clarify card
  below instead of being auto-answered. Core output never reaches any
  response — only these statuses do.
- POST /s/new — body {"text": ...}; validates the same way, then
  registers exactly one bounded background launch job and answers 202
  promptly with an opaque job id and its status URL — admission never
  blocks the response. A second POST while a launch is live gets 409:
  one at a time fails a double-submitted composer closed instead of
  admitting a duplicate run. GET /s/new/<job> serves {ok, job,
  status: starting|running|done|failed, session_id?, url?, error}
  from a bounded, thread-safe in-memory registry that holds only
  opaque state — never the prompt, never core output (parsed for the
  status word only, then dropped) — and prunes old terminal jobs
  (cap + age). The worker admits a fresh core run with no session id
  of its own: the core assigns the deterministic one, the admission
  202 echoes it, and the id is published on the status route the
  moment the session's row exists in the main DB; the client
  navigates to /s/default/<id> the moment it appears, without
  waiting for the run to finish (the fresh session is also registered
  busy, so the fresh page truthfully shows the live turn). A failed
  launch is terminal — never auto-retried — and reports only a canned
  reason word; a failure after the session row appeared additionally
  leaves the session's feed a one-shot note. On /new the composer
  stays locked after 202 (the waiting row showing) while the client
  polls the job with a hard bound; a terminal failure fails the
  optimistic message and restores the composer. GET /new serves the
  same dark chat chrome as a blank composer whose first send goes
  through /s/new.
- Clarify bridge: while the core API holds a pending clarify for a
  session (a gateway or /v1/runs agent paused on a question), the
  transcript page and every /feed poll also read the authenticated core
  GET /api/sessions/{id}/clarify (base CLARIFY_API_BASE, the repo
  default API server on loopback; the default profile's API key from
  the .env beside the main DB, a named profile's ONLY from its own
  .env; always through the /p/<profile> prefix; the key is never
  logged or echoed) and render it as an escaped Discord-style
  #clarify-card directly above the composer. Single choices submit on
  click, multi-select toggles then Submit (an optional UI-only Other
  contributes its typed text — never the label "Other"), and
  free-text questions get an input; while a card is active the normal
  composer is disabled with the placeholder "Answer the question
  above". Answers POST /s/<profile>/<id>/clarify through the same
  CSRF gate as every other mutation, which validates the session and
  payload locally, proxies the core clarify POST with the exact
  clarify_id the client holds, and answers only safe
  200/400/404/409/503 JSON — it never invokes /reply and never writes
  a user message. A new clarify_id replaces the card (selection
  resets), the same id preserves it, a failed answer flashes safely
  and re-polls, and any core API error leaves the page untouched (the
  feed simply omits the clarify field).
- The list page carries a "New chat" control linking to /new.

Live tool activity: a tool call appears the moment its assistant
tool_calls carrier is persisted and before its matching tool result
exists. The transcript page and every /feed poll compute a bounded
activity snapshot from the session's newest active rows (assistant
tool_calls JSON parsed defensively — malformed legacy JSON never
breaks the page — with result rows matched against the id /
call_id / response_item_id candidates) and render a "Live
activity" strip after the transcript rows: one row per unresolved
call with the tool name, a truthful state label and a bounded,
secret-redacted argument summary (raw tool_calls JSON is never
emitted). Completed tool history stays in its normal collapsed tool
group — the strip only ever represents the current turn's
in-flight state. When the turn is live (a valid lease or a running
composer job) but nothing is unresolved, the strip shows the
weakest truthful state — "Waiting for first response…" until the
turn's first assistant/tool output is persisted (the same words the
composer-side waiting row carries, so the handoff reads as one
continuous state; a historical answer before the turn's user row —
or, until hermes persists that row, before the composer turn's
acceptance time — never satisfies it), then Thinking after <tool> /
Processing tool results / Working — and the old generic typing row
never shows beside it. /feed returns the same
snapshot as a structured "activity" object {active, state,
pending_count, names, html} whose html is the exact server-rendered
strip, which the client swaps in (or removes) on every poll while
the feed cursor keeps its existing behavior.

Inline dispatch cards: the real spawn-shaped delegate tool call
(delegate_agent, delegate_task, delegate_claude_agent,
delegate_cursor_agent) is the chronological authority — one
Discord-style dispatch card renders at the call carrier's position
in the transcript (a batch call contributes one card per task, in
order), and exactly the matching generic delegate_* tool result row
disappears (control calls such as delegate_agent action
list/steer/stop are not dispatches and keep their ordinary rows).
A card links into a child transcript only when a real direct child
session or async delegation proves the route — same time window AND
agreeing goal text, matched one-to-one; a matched child renders AS
its card, never as a second standalone row, and an unmatched child
keeps its own inline row at its dispatch time. A
delegate_claude_agent card additionally resolves a durable spawn
receipt (tools/claude_run_receipts.py — written the moment the
Claude subprocess exists, keyed by the exact parent session id +
tool call id under that profile's own claude-runs tree) and, when
every check passes, the whole card opens that exact run's live
viewer page (a Watch affordance) while the run is still going and
after it completes; anything less than a fully validated receipt
fails closed to the same static card. Claude viewer links take
precedence over child-session links on Claude cards; non-Claude
cards and standalone child rows are unchanged. The initial render
and every /feed poll share the exact renderer and revision key, so
a static card upgrades in place the moment its receipt appears —
no duplicate cards, no reload. A floating "Next sub-agent" pill
steps through the inline child rows and dispatch cards by one
identity space.
"""

import argparse
import atexit
import glob
import importlib
import ipaddress
import html
import json
import os
import re
import secrets
import signal
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from plugins.mission_control.chat_format import (
    CHAT_DETAIL_CHARS,
    CHAT_TEXT_CHARS,
    CODEX_ITEMS_MAX_CHARS,
    CODEX_ITEM_MAX_BLOCKS,
    CODEX_ITEMS_MAX_DEPTH,
    CODEX_ITEMS_MAX_ITEMS,
    TOOL_CHIP_MAX,
    chat_items,
    chat_messages,
    clean_preview,
    clamp_label,
    codex_commentary_text,
    fmt_hhmm,
    fmt_rel,
    fmt_short,
    fmt_time,
    merge_children,
    merge_delegate_cards,
)
from plugins.mission_control.delegates import (
    CHILD_KEY_UNSAFE_RE,
    WAITING_FIRST_RESPONSE,
    DELEGATE_CALLS_CHAIN_SQL,
    DELEGATE_CALLS_CHARS,
    DELEGATE_CALLS_MAX,
    DELEGATE_MATCH_SKEW,
    DELEGATE_MATCH_WINDOW,
    DELEGATE_RESULTS_CHAIN_SQL,
    DELEGATE_RESULTS_MAX,
    DELEGATE_TOOL_LABELS,
    SUBAGENT_STATE_LABELS,
    _goals_agree,
    child_dom_key,
    child_revision,
    delegate_card_state,
    delegate_carrier_calls,
    delegate_dom_key,
    delegate_revision,
    delegate_spawn_calls,
    load_delegations,
    subagent_state,
    summarize_arguments,
)
from plugins.mission_control.discord_sync import (
    DISCORD_SYNC_INTERVAL_SECONDS,
    _discord_sync_stop,
    DISCORD_TIMEOUT_SECONDS,
    SNOWFLAKE_RE,
    discord_request,
    fetch_active_thread_ids,
)
from plugins.mission_control.http_util import (
    HOSTNAME_RE,
    LOOPBACK_HOSTS,
    MISDIRECTED_HTML,
    WILDCARD_BINDS,
    _default_trusted_hosts,
    _is_loopback,
    _local_interface_ips,
    _normalize_host,
    _server_trusted_hosts,
)
from plugins.mission_control.lineage import (
    LINEAGE_CACHE_SECONDS,
    LINEAGE_FIRST_USER_SQL,
    LINEAGE_PROMPT_MAX_CHARS,
    LINEAGE_SESSION_ROWS_SQL,
    LINEAGE_SKEW_SECONDS,
    LINEAGE_TERMINAL_SQL,
    LINEAGE_TOOL_JSON_CHARS,
    LINEAGE_WINDOW_SESSIONS_SQL,
    LINEAGE_WORKER_CANDIDATES_SQL,
    LINEAGE_WORKER_SOURCES,
    RESEARCH_JOBS_DIR,
    SESSION_ID_LINE_RE,
    _lineage_job_specs,
)
from plugins.mission_control.queries import (
    ACTIVITY_MAX_ROWS,
    WINDOW_SECONDS,
    compute_activity,
    ACTIVITY_SQL,
    COUNT_CHAIN_MISMATCH_SQL,
    CHAT_PAGE_SQL,
    CHAT_SESSION_SQL,
    COUNT_THREAD_MISMATCH_SQL,
    DISCORD_RECENT_THREADS_SQL,
    FEED_BACKFILL_CHAIN_SQL,
    FEED_BACKFILL_MAX,
    FEED_CATCHUP_MAX,
    FEED_LAST_ID_CHAIN_SQL,
    LAST_LINE_CHUNK,
    LAST_LINE_SQL,
    LEASES_LIVE_SQL,
    ROOT_TITLE_SQL,
    SESSION_CWD_SQL,
    SESSION_ID_RE,
    SESSION_STATE_SQL,
    SET_ARCHIVE_BY_CHAIN_SQL,
    SET_ARCHIVE_BY_THREAD_CHANGED_SQL,
    SET_ARCHIVE_BY_THREAD_SQL,
    SUBAGENT_LABEL_SQL,
    SUBAGENT_MAX_CHILDREN,
    SUBAGENTS_CHAIN_SQL,
    TURN_ID_RE,
    _chain_ids,
    classify_session,
    load_last_lines,
    load_last_tools,
    load_lease_ids,
    load_newest_events,
    mark_your_turn,
    parse_turn_id,
    session_lease_live,
)

# These two chain statements stay defined HERE (not in queries.py):
# the feed-barrier tests patch them as seams on this module, and the
# readers (load_chat/load_feed) must observe the patched objects.
from plugins.mission_control import queries as _queries
CHAT_PAGE_CHAIN_SQL = _queries.CHAT_PAGE_SQL.replace(
    "WHERE session_id = ?", "WHERE session_id IN ({placeholders})")
FEED_AFTER_CHAIN_SQL = _queries.FEED_AFTER_SQL.replace(
    "WHERE session_id = ?", "WHERE session_id IN ({placeholders})")

from plugins.mission_control.redaction import (
    ARGS_SUMMARY_CHARS,
    ARGS_VALUE_CHARS,
    REDACTED,
    SUBAGENT_LABEL_CHARS,
    DELEGATE_SPAWN_NAMES,
    TOOL_CALLS_MAX,
    parse_tool_calls,
    redact_secret_text,
    redact_secrets,
    sanitize_text,
)

from plugins.mission_control.ui_assets import (
    CHAT_SHELL,
    CLARIFY_CARD_CSS,
    LIVE_CSS,
    PAGE_SHELL,
    SHELL_CSS,
    SIDEBAR_JS,
    TYPING_CSS,
    TYPING_ROW,
    WAITING_CSS,
    WAITING_ROW,
    render_clarify_card,
)
from urllib.parse import parse_qs, quote, unquote, urlsplit

from hermes_constants import display_hermes_home, get_hermes_home
from hermes_state import SessionDB

# State/profile discovery always flows through get_hermes_home() so a
# custom HERMES_HOME (or profile override) is honored; tests and the CLI
# may re-point these module-level knobs at scratch databases.
_HERMES_HOME = get_hermes_home()
MAIN_DB = str(_HERMES_HOME / "state.db")
PROFILE_GLOB = str(_HERMES_HOME / "profiles" / "*" / "state.db")
# In-place swap of the inbox often enough that running -> completed
# transitions feel live (the feed already polls every 2 s in chats).
REFRESH_SECONDS = 10

# This surface has no server pager, so the core listing is asked for
# every row and the rolling closed-row window is applied after the
# global open/closed partition (load_sessions) — never inside the
# query, where it could strand an open conversation behind rows the
# window kept.
LIST_ALL_LIMIT = 1000000

# ---- inbox listing ----------------------------------------------------
# The inbox's session set and ordering are core-owned (load_sessions
# reads SessionDB.list_sessions_rich; see there for the projection,
# visibility and window rules). Title falls back to display_name, then
# the session id; sub-agent sessions never appear as inbox rows — they
# live inside their parent's conversation instead. The projected tip's
# ended_at and archived feed the Closed section together — the same
# open/closed split core's open_first ordering draws: an ended or
# archived session is Closed no matter what else is true, and only an
# unended, unarchived one stays open.


# Inbox section keys in display order. The ordering contract: every
# open session renders above every closed one — Your turn first (the
# open conversations whose next move is the human's: an explicit wait
# the core names in its batch waiting-status answer — an API-run
# clarify, a native gateway clarify prompt, a restart confirmation —
# or an idle chat resting on a plain assistant answer), then Active,
# Open · unfinished, Open · completed, and Closed strictly last — so
# a closed conversation can never jump ahead of an older open one
# however fresh its last activity is. Rows are bucketed by state
# before any rendering (this surface has no row cap or window to
# partition around), and each bucket keeps its own newest-first
# order. The open resting buckets are retitled "Open · …" so the
# headers read as one honest question: live, or open (and if open,
# done or not), with the closed tail collapsed by default. The
# ids/data-state values are unchanged stable hooks for the client
# filter; only order and titles moved.
# Active and Completed always render (when the page has any rows at
# all), Your turn, Incomplete and Closed only when they have members.
# Closed is the projected tip's ended_at or the archived flag
# (archived mirrors Discord or a local close) — it wins over every
# other signal, and every closed row says so itself with an Archived
# or Ended chip.
SECTION_ORDER = ("your_turn", "active", "incomplete", "completed", "closed")
SECTION_TITLES = {"active": "Active", "closed": "Closed",
                  "completed": "Open \N{MIDDLE DOT} completed",
                  "incomplete": "Open \N{MIDDLE DOT} unfinished",
                  "your_turn": "Your turn"}

# ---- chat transcript route (/s/<profile>/<session_id>) ---------------
CHAT_PATH_RE = re.compile(r"^/s/([^/]+)/([^/]+)$")

# Live-update and composer routes hanging off the transcript page:
# GET  /s/<profile>/<id>/feed?after=<message_id>  (JSON delta poll)
# POST /s/<profile>/<id>/reply                    (send one turn)
# POST /s/new                                     (start a session)
# GET  /s/new/<job_id>                            (launch status poll)
# GET  /new                                       (blank composer)
FEED_PATH_RE = re.compile(r"^/s/([^/]+)/([^/]+)/feed$")
REPLY_PATH_RE = re.compile(r"^/s/([^/]+)/([^/]+)/reply$")
# Opaque launch-job ids come from secrets.token_urlsafe(), so the
# character class is exactly its alphabet; anything else 404s before
# the registry is ever consulted.
NEW_JOB_PATH_RE = re.compile(r"^/s/new/([A-Za-z0-9_-]+)$")

# POST /s/<profile>/<id>/close and .../reopen: archive/unarchive the
# session — for a Discord thread session this patches the Discord
# thread first and only mirrors locally when Discord confirms.
ARCHIVE_PATH_RE = re.compile(r"^/s/([^/]+)/([^/]+)/(close|reopen)$")

# POST /s/<profile>/<id>/clarify: answer this session's pending
# clarify question through the core API (see the clarify bridge
# constants below).
CLARIFY_PATH_RE = re.compile(r"^/s/([^/]+)/([^/]+)/clarify$")

# ---- optional local avatar images ------------------------------------
# Every participant renders as a letter badge by default — no image
# assets ship, none are fetched, a clean install works untouched. An
# install that wants photos provides ONE optional PNG per identity,
# always through this fixed, profile-aware layout the user owns:
#
#   <profile home>/mission-control/avatar.png   that profile's agent
#   <main home>/mission-control/user.png        the person at the keys
#
# GET /avatar/<profile> and GET /avatar-user serve exactly those fixed
# filenames, re-resolved from the discovered DB mapping on every
# request — the URL contributes only a profile name that must already
# be a discovered profile, never a path. A file that is missing,
# unreadable, larger than AVATAR_MAX_BYTES, or resolves (through
# symlinks) outside the configured home root is simply not served and
# the letter badge stands alone — the same discovery boundary the
# databases obey.
AVATAR_DIR_NAME = "mission-control"
PROFILE_AVATAR_FILE = "avatar.png"
USER_AVATAR_FILE = "user.png"
AVATAR_PATH_RE = re.compile(r"^/avatar/([A-Za-z0-9_.-]+)$")
USER_AVATAR_PATH = "/avatar-user"
AVATAR_MAX_BYTES = 2 * 1024 * 1024
AVATAR_CACHE_CONTROL = "public, max-age=3600"
AVATAR_CONTENT_TYPE = "image/png"


# ---- archive ownership (user actions vs the background mirror) --------
# One rule: a user-confirmed close/reopen always wins over a background
# snapshot that was already in flight when the user acted. Every profile
# DB carries a monotonically increasing epoch; a user mutation bumps it
# (and writes) while holding _archive_epoch_lock, and the sync pass
# re-checks the epoch under the same lock right before it applies a
# fetched snapshot — a snapshot whose epoch moved is stale and is
# discarded whole, never partially applied. Because both writers hold
# the same lock across their transactional DB writes, the database is
# never left holding a mix of the two.
_archive_epoch_lock = threading.Lock()
_archive_epochs = {}


def _archive_epoch(db_path):
    """Current archive epoch for one profile DB (0 when never bumped)."""
    with _archive_epoch_lock:
        return _archive_epochs.get(db_path, 0)

# ---- core API bridge (clarify card + run transport) ------------------
# Two things live in the core API server: the pending-clarify card (a
# /v1/runs agent paused on a question) and the runs themselves. This
# server proxies clarify answers back and — for composer turns — admits
# runs through POST /v1/runs instead of spawning the oneshot CLI: the
# run's agent gets the gateway clarify callback, so a question the
# composer's turn asks actually pauses for the card instead of being
# auto-answered by the headless -q default. The only configuration is
# the base URL (the repo's own API server on loopback by default,
# overridable through the deployment's HERMES_API_SERVER_URL); the
# per-profile API key is read fresh from the .env beside the profile's
# DB (the default profile's from the .env beside the main DB, a named
# profile's ONLY from its own .env) and leaves this process solely as
# the value of one Authorization header — never logged, never echoed,
# never inside any error string. Upstream bodies are parsed but never
# emitted: errors report the HTTP status alone.
CLARIFY_API_BASE = (os.environ.get("HERMES_API_SERVER_URL")
                    or "http://127.0.0.1:8642")
# Short on purpose: the card rides the feed poll, so a wedged core must
# never pin a client's poll for long, and the card body is tiny.
CLARIFY_TIMEOUT_SECONDS = 4.0
# Run admission has to stay well under a browser-friendly request
# budget while still allowing the core its (prompt) 202 path; status
# and stop calls answer fast and keep the same bound.
RUNS_TIMEOUT_SECONDS = 10.0
# How often a live job polls its run's status while holding the busy
# lease. Sub-second so a finished turn releases the session promptly.
RUN_POLL_SECONDS = 0.5
# Pollable /v1/runs statuses that end a job's watch. "waiting_for_*"
# are deliberately absent: a paused question or approval must keep the
# lease held (the session is busy mid-turn, the card is answerable).
RUN_TERMINAL_STATES = ("completed", "failed", "cancelled", "interrupted")
CLARIFY_MAX_BODY_BYTES = 64 * 1024
# Presentation bounds mirrored from the core card contract (the core
# already enforces them; these re-bound whatever actually arrives so a
# malformed or hostile upstream can never crash or flood the page).
CLARIFY_MAX_CHOICES = 8
CLARIFY_MAX_QUESTION_CHARS = 2000
CLARIFY_MAX_CHOICE_CHARS = 500
CLARIFY_ID_MAX_CHARS = 128
# The batch waiting-status call runs on the inbox render path (every
# refresh), so it gets the tightest core deadline of all: one bounded
# GET per profile, and a wedged core must never stall the page.
WAITING_TIMEOUT_SECONDS = 2.0
# A response list can never legitimately exceed the card's choice bound
# plus its Other; anything larger is refused before ever proxying.
CLARIFY_MAX_RESPONSE_ITEMS = 16

# ---- composer / run plumbing ------------------------------------------
# A composer turn can legitimately run minutes with tools in the loop;
# the cap only exists so a wedged run can't hold a session "busy"
# forever. On the deadline the job stops the run best-effort and
# reports failure.
HERMES_TIMEOUT_SECONDS = 900

# Composer payloads: anything bigger than this is refused before it is
# read, and text past this many characters is refused after. Chat
# messages have no business approaching either.
MAX_BODY_BYTES = 64 * 1024
MAX_TEXT_CHARS = 32000

# ---- cross-site request forgery boundary --------------------------------
# Every state-changing route (reply, /s/new, close/reopen) rejects
# browser-simple cross-origin requests BEFORE the body is parsed or any
# state changes, using three header-only checks (see Handler.do_POST):
# an Origin header naming another host, a content type other than
# application/json (an HTML form can only produce the "simple" types),
# or a missing/wrong X-CSRF-Token — a non-simple header no form can
# carry and a cross-origin fetch cannot send without a preflight this
# server never grants. The token is one cryptographically random value
# per server process, created lazily on the first page render, emitted
# ONLY to pages this process serves (a <meta> tag), validated in
# constant time, and never logged or echoed back.
CSRF_HEADER = "X-CSRF-Token"
CSRF_META_NAME = "mission-control-csrf"
_csrf_token = None
_csrf_lock = threading.Lock()


def csrf_token():
    """This server process's CSRF token, minted on first use.

    Lazy so merely importing the module (tests, the CLI wiring) never
    spends entropy; per process so two servers never share a token.
    """
    global _csrf_token
    with _csrf_lock:
        if _csrf_token is None:
            _csrf_token = secrets.token_urlsafe(32)
        return _csrf_token


def csrf_meta_tag():
    """The <meta> element carrying the token into a served page."""
    return ('<meta name="%s" content="%s">'
            % (CSRF_META_NAME, html.escape(csrf_token(), quote=True)))

# The feed's poll cadence (client-side); the script polls faster while a
# reply is in flight so the answer lands promptly.
FEED_POLL_MS = 2000

# One in-flight composer turn per session: (profile, session_id) ->
# {"started": ts} — started being the turn's acceptance time, the floor
# that scopes the live strip's first-output detection to the accepted
# turn (a historical answer predating it can never satisfy the new
# turn). _job_notes holds a short failure note per session for the feed
# to deliver once (it never contains core output, just a canned line).
_jobs = {}
_job_notes = {}
_jobs_lock = threading.Lock()

# New-session launches serialize (one live run at a time): the HTTP
# handler never waits on this lock; the background worker holds it for
# its whole run and a concurrent POST is answered 409 instead. That
# one-at-a-time rule is what makes a double-submitted /s/new fail
# closed instead of admitting a second (duplicate) run.
_new_session_lock = threading.Lock()

# Transcript participants: the user side is the person at the keyboard;
# the assistant side renders as the owning profile's identity. Every
# participant is a letter badge derived from the label, with the
# optional local avatar image (see the avatar constants above) layered
# on top when the profile provides one — the letter is always the
# fallback, so a clean install renders identically.
USER_LABEL = "You"

# Identity for the main "default" profile (the main state.db).
DEFAULT_PROFILE_LABEL = "Hermes"


def _avatar_file(home, filename):
    """One trusted home directory + the fixed avatar filename -> the
    only path an avatar may ever be served from. Nothing here derives
    from request input."""
    return os.path.join(home, AVATAR_DIR_NAME, filename)


def _avatar_served_file(home, filename):
    """The avatar path for one trusted home when it may be served, else
    None — a plain regular file, within the size bound, resolving
    inside the configured home root (the same boundary discovery
    enforces for the databases)."""
    path = _avatar_file(home, filename)
    try:
        if not os.path.isfile(path):
            return None
        if os.path.getsize(path) > AVATAR_MAX_BYTES:
            return None
        if not _db_stays_in_home(path, _home_root()):
            return None
    except OSError:
        return None
    return path


def profile_avatar_url(profile):
    """The served avatar URL for one profile name, or "" (letter badge
    only). The profile must be one discover_dbs() serves; the file is
    its own optional mission-control/avatar.png."""
    home = profile_home(profile)
    if home is None:
        return ""
    if _avatar_served_file(home, PROFILE_AVATAR_FILE) is None:
        return ""
    return "/avatar/" + quote(profile, safe="")


def user_avatar_url():
    """The served avatar URL for the person at the keyboard, or "".

    One optional user.png under the main home's mission-control
    directory — the main home is exactly the root every served DB
    must live in, so the user avatar follows the same boundary."""
    home = os.path.dirname(os.path.abspath(MAIN_DB))
    if _avatar_served_file(home, USER_AVATAR_FILE) is None:
        return ""
    return USER_AVATAR_PATH


def avatar_img(url, label, size):
    """The escaped <img> that layers one avatar over its letter badge
    ("" when there is no avatar for this identity)."""
    if not url:
        return ""
    return ('<img class="av-img" src="%s" alt="%s" width="%d"'
            ' height="%d">' % (html.escape(url, quote=True),
                               html.escape(label, quote=True),
                               size, size))


def profile_identity(profile):
    """DB profile name -> its display identity dict {label, letter,
    avatar}.

    The label is a safe humanization of the profile name ("my_bot" ->
    "My Bot"); the main "default" profile renders as "Hermes". The
    letter is the badge initial taken from that label. avatar is the
    served URL of the profile's optional local image, "" when it has
    none — the letter badge renders either way.
    """
    name = str(profile or "").strip()
    if not name or name == "default":
        label = DEFAULT_PROFILE_LABEL
    else:
        label = " ".join(name.replace("_", " ").replace("-", " ")
                         .split()).title() or DEFAULT_PROFILE_LABEL
    return {"label": label, "letter": (label[:1] or "H").upper(),
            "avatar": profile_avatar_url(name or "default")}


def _home_root():
    """The resolved root every served database must live inside.

    That root is the directory holding MAIN_DB with symlinks resolved:
    MAIN_DB is <HERMES_HOME>/state.db by construction, and the tests
    and the CLI re-point these module knobs at a scratch home, so
    deriving the root here — at call time, from the knob — honors
    exactly the home this process is configured to serve, never a
    different one captured at import."""
    return os.path.realpath(os.path.dirname(os.path.abspath(MAIN_DB)))


def _contained(path, root):
    """True when a resolved path is root itself or lives under it."""
    prefix = root if root.endswith(os.sep) else root + os.sep
    return path == root or path.startswith(prefix)


def _canonical_db(path, root):
    """The canonical, contained database path for a candidate, or None.

    Every path component counts: a profile directory that is a symlink
    out of the home, or a state.db that is one, resolves outside root
    and is rejected here — before any connection is opened — so no
    read, write, spawn or token lookup can reach a database the home
    did not configure. The answer is the fully resolved real path (all
    symlinks resolved NOW), which is the only spelling every later
    connect, archive, transcript, spawn and mutation path may open. A
    candidate that cannot be resolved (missing, broken,
    permission-denied, or racing away mid-check) answers None instead
    of raising: discovery may only ever shrink."""
    try:
        real = os.path.realpath(path)
        if not os.path.isfile(real):
            return None
    except OSError:
        return None
    return real if _contained(real, root) else None


def _db_stays_in_home(path, root):
    """True when a candidate state.db fully resolves inside root.

    Containment question only, kept for the avatar boundary; the value
    every database surface uses is the canonical path _canonical_db()
    returns (see discover_dbs / _connect_db)."""
    return _canonical_db(path, root) is not None


def _db_file_identity(path):
    """(st_dev, st_ino) of the file at path, or None when it cannot be
    stated — the value compared at the connection boundary, so a path
    that still resolves inside the home but was swapped to a different
    file is told apart from the database discovery accepted."""
    try:
        st = os.stat(path)
        return (st.st_dev, st.st_ino)
    except OSError:
        return None


# Identity of every DB the most recent discovery pass accepted: canonical
# path -> (st_dev, st_ino). Replaced whole by each discover_dbs() pass and
# compared by _connect_db, so a check/open swap between discovery and the
# connection answers as an unavailable database instead of opening a
# different file.
_db_identities = {}


def _connect_db(path, write=False, timeout=2.0):
    """Open one discovered database after boundary revalidation.

    The canonical path is re-derived HERE, at the connection boundary —
    not reused from a check that ran earlier — and the file identity is
    compared with the last discovery pass both before the open and
    after it. A component or file swapped in between therefore cannot
    redirect SQLite outside the trusted Hermes home: an escaping swap
    fails containment, a same-path different-file swap fails identity,
    and the read-write flavor opens through mode=rw so a swapped-away
    target can never be *created* either. Raises sqlite3.OperationalError
    (the callers' existing degraded-answer path) when the candidate no
    longer checks out."""
    canonical = _canonical_db(path, _home_root())
    if canonical is None:
        raise sqlite3.OperationalError("database not available")
    # The identity the open must land on: the one discovery recorded, or
    # — for a caller connecting ahead of any discovery pass — the one
    # stated just before the open. Either way the post-open restatement
    # must still name the same file.
    expected = _db_identities.get(canonical)
    if expected is None:
        expected = _db_file_identity(canonical)
        if expected is None:
            raise sqlite3.OperationalError("database not available")
    elif _db_file_identity(canonical) != expected:
        raise sqlite3.OperationalError("database changed under us")
    if write:
        # mode=rw, never rwc: a write open may flip an existing database,
        # never conjure one somewhere a swap pointed at.
        con = sqlite3.connect("file:" + quote(canonical) + "?mode=rw",
                              uri=True, timeout=timeout)
    else:
        con = sqlite3.connect("file:" + quote(canonical) + "?mode=ro",
                              uri=True, timeout=timeout)
    try:
        if _db_file_identity(canonical) != expected:
            raise sqlite3.OperationalError("database changed under us")
    except Exception:
        con.close()
        raise
    return con


def discover_dbs():
    """Yield (canonical path, profile name) for the main DB then each
    profile DB.

    Every candidate is resolved to its canonical real path HERE and only
    that value is ever returned, so every later connect, archive,
    transcript, spawn and mutation path opens the validated file, not
    the original (possibly symlinked) spelling. A candidate that is
    missing or unreadable, or whose profile directory or state.db
    resolves outside _home_root(), is dropped before it is ever
    returned, and so is a path alias: a second spelling that resolves
    to a database already served (the main DB reached through a profile
    symlink, or two profile names for one file) is refused rather than
    double-served. Profile names come from the canonical directory, so
    an alias can never rename or shadow a served profile.

    The one exception is the main DB missing entirely: a path that
    still resolves inside the home stays discovered (exactly as before
    the canonicalization) so the listing reports it as a load-time
    note instead of silently vanishing — _connect_db() still refuses
    to open anything there, so a missing database can be named but
    never read or written."""
    root = _home_root()
    found = []
    identities = {}
    seen = set()
    canon = _canonical_db(MAIN_DB, root)
    if canon is None:
        try:
            real = os.path.realpath(MAIN_DB)
        except OSError:
            real = ""
        if real and _contained(real, root):
            canon = real
    if canon is not None:
        ident = _db_file_identity(canon)
        found.append((canon, "default"))
        seen.add(ident or canon)
        identities[canon] = ident
    for path in sorted(glob.glob(PROFILE_GLOB)):
        canon = _canonical_db(path, root)
        if canon is None:
            continue
        ident = _db_file_identity(canon)
        key = ident or canon
        if key in seen:
            continue  # an alias of a database already served
        seen.add(key)
        identities[canon] = ident
        found.append((canon, os.path.basename(os.path.dirname(canon))))
    _db_identities.clear()
    _db_identities.update(identities)
    return found


def profile_home(profile):
    """Trusted HERMES_HOME directory for one profile name, or None.

    The only source is the discovered DB mapping: the name must be one
    discover_dbs() returned, and the home is that entry's DB parent —
    the main home for "default", the profile's own directory (each
    profile is a full HERMES_HOME) for a named one. Nothing here ever
    builds a path from request input, so a crafted profile name can
    only ever resolve to a home this server actually discovered."""
    for db_path, name in discover_dbs():
        if name == profile:
            return os.path.dirname(os.path.abspath(db_path))
    return None


def load_sessions(now):
    """Return (rows, notes) across all DBs; each DB failure becomes a note.

    The listing itself is core-owned: every discovered DB is read
    through SessionDB.list_sessions_rich(open_first=True,
    order_by_last_active=True, include_archived=True,
    compact_rows=True), so compression-tip projection, pinned
    back-fill, branch/reset visibility and hidden and delegate
    filtering all follow the one definition Hermes core owns — the
    surfaced row for a compressed conversation is its live tip, never
    the always-ended root. The refresh is metadata-only, so the compact
    projection is requested: the large system_prompt and
    git_metadata_generation blobs this page never renders stay in the
    DB instead of crossing a million-row refresh. This function
    attaches only the presentation envelope: last-line/last-tool
    enrichment, lease/job Active marking and the Completed/Incomplete
    judgment, batched per DB exactly as before (never N+1), each
    degrading to a weaker classification plus at most one note when a
    table/column is missing.

    Rows merge across DBs with one deterministic global key: every
    open conversation before every closed one, canonical last-active
    order inside each partition, then stable (profile, session id)
    tie-breakers. The rolling 24h window bounds only conversations
    that have come to rest — projected tip ended, or archived: an open
    or pinned conversation stays listed however old it is, so a
    quiet-but-live chat can never fall out of the inbox, and a pin the
    window would drop stays reachable without landing below a closed
    row.
    """
    lo = now - WINDOW_SECONDS
    rows, notes = [], []
    for path, profile in discover_dbs():
        try:
            # tuning_pragmas=False — plain-SQLite defaults, no config
            # load, no ambient-home side effect (see _chain_ids).
            sdb = SessionDB(path, read_only=True, tuning_pragmas=False)
            try:
                rich = sdb.list_sessions_rich(
                    limit=LIST_ALL_LIMIT, order_by_last_active=True,
                    open_first=True, include_archived=True,
                    exclude_sources=("subagent",), compact_rows=True,
                )
            finally:
                sdb.close()
        except Exception as exc:
            # Core listing failed for this DB: one note, its rows absent.
            notes.append("%s: %s" % (profile, exc))
            continue
        db_rows = []
        for s in rich:
            last = s.get("last_active") or s.get("started_at") or 0.0
            rested = s.get("ended_at") is not None or bool(s.get("archived"))
            if rested and not bool(s.get("pinned")) \
                    and not (lo <= last <= now):
                continue
            db_rows.append({
                "id": s["id"],
                "source": s.get("source") or "",
                "title": s.get("title") or s.get("display_name")
                or s["id"],
                "last": last,
                "ended": s.get("ended_at"),
                "archived": bool(s.get("archived")),
                "profile": profile,
                "state": "incomplete",
                # Chain keys for search: the projected row is the tip, so
                # the root's id/title and every intermediate member id
                # ride along as extra search spellings.
                "root": s.get("_lineage_root_id") or "",
                "lineage": list(s.get("_lineage_ids") or []),
            })
        try:
            con = _connect_db(path)
        except sqlite3.Error as exc:
            notes.append("%s: %s" % (profile, exc))
            continue
        try:
            try:
                lines = load_last_lines(con, [r["id"] for r in db_rows])
            except sqlite3.Error as exc:
                # Sessions still list; the column just falls back to —.
                lines = {}
                notes.append("last-message data for %s (%s)"
                             % (profile, exc))
            try:
                tools = load_last_tools(con, [r["id"] for r in db_rows])
            except sqlite3.Error as exc:
                tools = {}
                notes.append("last-tool data for %s (%s)" % (profile, exc))
            lease_ids = set()
            try:
                lease_ids = load_lease_ids(con, now)
            except sqlite3.Error as exc:
                # No lease table (or unreadable): classification
                # degrades to Completed/Incomplete. One note, same
                # style as the preview fallbacks above.
                notes.append("turn leases for %s (%s)" % (profile, exc))
            newest = {}
            try:
                newest = load_newest_events(con,
                                            [r["id"] for r in db_rows])
            except sqlite3.Error as exc:
                newest = {}
                notes.append("completion state for %s (%s)" % (profile, exc))
            root_titles = {}
            roots = [r["root"] for r in db_rows if r["root"]]
            for start in range(0, len(roots), LAST_LINE_CHUNK):
                chunk = roots[start:start + LAST_LINE_CHUNK]
                root_titles.update(con.execute(
                    ROOT_TITLE_SQL.format(
                        placeholders=",".join("?" * len(chunk))), chunk))
            for r in db_rows:
                r["last_line_role"], r["last_line"] = \
                    lines.get(r["id"], ("", ""))
                r["last_tool"] = tools.get(r["id"], "")
                extra = [r["root"], root_titles.get(r["root"], "")]
                extra.extend(x for x in r["lineage"] if x != r["id"])
                r["search_extra"] = " ".join(x for x in extra if x)
                del r["root"], r["lineage"]
                # State precedence: the projected tip's ended_at or
                # archived flag -> closed first (the same split core's
                # open_first ordering draws), then a live lease/job ->
                # active, else completed/incomplete. An ended
                # conversation is closed at archived=0 too, so it can
                # never render under an open-labeled section.
                if r["archived"] or r["ended"] is not None:
                    r["state"] = "closed"
                elif r["id"] in lease_ids:
                    r["state"] = "active"
                else:
                    r["state"] = classify_session(r, newest)
            rows.extend(db_rows)
        finally:
            con.close()
    # Confidently linked cross-profile children leave every inbox
    # section: they render as Sub-agents under their parent instead.
    # Nothing else is ever hidden — not by profile, source or title —
    # so a worker-profile run the lineage pass could not resolve keeps
    # its row rather than being guessed onto a parent (fail open; its
    # own direct session page opens fine either way).
    linked = lineage_index(now)["child_keys"]
    if linked:
        rows = [r for r in rows
                if (r["profile"], r["id"]) not in linked]
    # Global merge, applied before any rendering: open rows strictly
    # before closed ones, canonical last-active order inside each
    # partition, stable (profile, session id) tie-breakers so identical
    # timestamps never shuffle between renders.
    rows.sort(key=lambda r: (r["state"] == "closed", -(r["last"] or 0.0),
                             r["profile"], str(r["id"])))
    return rows, notes


def mark_job_states(rows):
    """Mark inbox rows this server is currently replying in as active.

    The _jobs table is the second Active signal (a Mission Control
    composer reply running in this process); it wins over the
    DB-derived classification for the sessions it names — but never
    over a closed row (the projected tip's ended_at, or archived),
    which keeps a closed session Closed even while a reply started
    earlier is still draining.
    """
    if not _jobs or not rows:
        return
    with _jobs_lock:
        keys = set(_jobs)
    for r in rows:
        if (r["profile"], r["id"]) in keys and r["state"] != "closed":
            r["state"] = "active"


def load_chat(profile, session_id, dbs, busy_job=False, busy_since=None):
    """Load one conversation's header and transcript page from its DB.

    dbs maps profile name -> DB path (discover_dbs()). The transcript,
    the sub-agent section and the feed cursor span the conversation's
    whole canonical chain (_chain_ids), so a root bookmark, a persisted
    middle segment and the live tip all render the same page — row id
    interleaves the members in write order. Returns None when the
    session id isn't in that profile's DB — a session id is never
    looked up in any other DB. sqlite3.Error propagates so the caller
    can render a themed 500 (a locked DB is not "unknown session").
    The live-activity snapshot is computed here too, so the initial
    page render already carries the strip (busy_job is whether this
    server is currently running a composer reply for the session, and
    busy_since that turn's acceptance time — the floor scoping its
    first-output detection).

    last_id is a durable high-water cursor: the newest row id captured
    BEFORE the transcript rows are read, then raised to the newest id
    the page actually contains. A row that lands between the cursor
    capture and the row query is inside the page (and its id lifts the
    cursor past it), and anything later is replayed by the first
    after=last_id poll — every event appears exactly once, none is
    lost to the snapshot/delta seam.
    """
    con = _connect_db(dbs[profile])
    try:
        sess = con.execute(CHAT_SESSION_SQL, (session_id,)).fetchone()
        if sess is None:
            return None
        title, display_name, started, last, source, thread_id, \
            archived = sess
        chain = _chain_ids(dbs[profile], session_id)
        ph = ",".join("?" * len(chain))
        last_id = con.execute(
            FEED_LAST_ID_CHAIN_SQL.format(placeholders=ph),
            chain).fetchone()[0] or 0
        rows = con.execute(
            CHAT_PAGE_CHAIN_SQL.format(placeholders=ph), chain).fetchall()
        for row in rows:
            if row[3] > last_id:
                last_id = row[3]
        subagents = subagents_for(con, profile, chain)
        delegate = delegate_cards(con, profile, chain, subagents)
        activity = compute_activity(con, session_id, time.time(),
                                    busy_job, busy_since)
    finally:
        con.close()
    rows.reverse()  # fetched newest-first; display oldest-first
    return {
        "id": session_id,
        "title": title or display_name or session_id,
        "profile": profile,
        "started": started,
        "last": last,
        "source": source or "",
        "thread_id": thread_id,
        "archived": bool(archived),
        "rows": rows,
        "last_id": last_id,
        # Children a dispatch card absorbed leave the standalone list;
        # the cards render at their call carriers instead.
        "subagents": delegate["children"],
        "delegate_cards": delegate["cards"],
        "delegate_suppress": delegate["suppress"],
        "activity": activity,
    }


def load_subagents(con, profile, chain):
    """Direct sub-agent children of one conversation, oldest first.

    *chain* is the canonical chain id list; a bare session id is still
    accepted (it names that one segment), so every caller that held a
    single id keeps working. Children hang off any member of the
    canonical chain (a dispatch mid-conversation records that member's
    id as its parent), so the
    IN-list covers the whole conversation. Called on the conversation's
    own open connection, so the section always reads the same profile
    DB as the transcript around it: one bounded SUBAGENTS_CHAIN_SQL
    run, plus one batched SUBAGENT_LABEL_SQL run only when some child
    has no title/display_name to show (the usual case). Label falls
    back title -> display_name -> the cleaned first user message, so a
    row reads as the goal the child was dispatched for. Every child
    carries its profile (here always the parent's) and started_at so it
    can merge with cross-profile children. Every field is escaped later
    at render time.
    """
    if isinstance(chain, str):
        chain = [chain]
    children = []
    ph = ",".join("?" * len(chain))
    for sid, title, display_name, started, last, ended, end_reason \
            in con.execute(SUBAGENTS_CHAIN_SQL.format(placeholders=ph),
                           chain + [SUBAGENT_MAX_CHILDREN]):
        children.append({
            "id": sid,
            "profile": profile,
            "label": clamp_label(title or display_name or ""),
            "started": started or 0,
            "last": last,
            "ended": ended,
            "end_reason": end_reason or "",
        })
    blank = [c["id"] for c in children if not c["label"]]
    if blank:
        goals = {}
        for start in range(0, len(blank), LAST_LINE_CHUNK):
            chunk = blank[start:start + LAST_LINE_CHUNK]
            sql = SUBAGENT_LABEL_SQL.format(
                placeholders=",".join("?" * len(chunk)))
            goals.update(con.execute(sql, chunk))
        for c in children:
            if not c["label"]:
                c["label"] = clamp_label(goals.get(c["id"], ""))
    return children


# ---- cross-profile lineage (research jobs + terminal launches) ------
# A delegate_research job runs its lanes/synthesis in a *worker*
# profile DB, and a session can launch a whole other profile over the
# terminal tool; those runs are confident children of the session that
# dispatched them, so they belong in its Sub-agents section instead of
# the inbox. Both links are discovered from durable read-only artifacts
# (job request/status/prompt files, messages rows) — never guessed from
# source='cli' or profile != default. A prompt-and-window job match is
# inference, so it additionally requires the candidate session's own
# source to explicitly mark a non-human worker run
# (LINEAGE_WORKER_SOURCES); a terminal launch needs no such gate
# because the parent's own tool result recorded the child's session
# id. Every unresolved link fails open: malformed, locked, ambiguous,
# human-facing or merely unmatched stays an ordinary top-level row,
# never guessed onto a parent. A profile may exist purely to receive
# dispatched work, but that is a fact about the data, never evidence —
# hiding one of its sessions requires the same durable job proof as
# any other profile.


def _lineage_lookup(con_path, ids):
    """Which of `ids` exist non-hidden in one DB -> {id: full row}.

    One batched chunked query; None (not {}) when the DB could not be
    read, so callers can tell "known absent" from "unknowable"."""
    rows = {}
    ids = list(ids)
    try:
        con = _connect_db(con_path)
        try:
            for start in range(0, len(ids), LAST_LINE_CHUNK):
                chunk = ids[start:start + LAST_LINE_CHUNK]
                sql = LINEAGE_SESSION_ROWS_SQL.format(
                    placeholders=",".join("?" * len(chunk)))
                for row in con.execute(sql, chunk):
                    rows[row[0]] = row
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return rows


def _lineage_job_children(specs, dbs_by_profile):
    """Job specs matched against their worker DBs ->
    ({child key: {parent keys}}, {child key: row}).

    One windowed candidates query plus one batched first-user-message
    query per worker profile — never one query per child. A candidate
    links to every spec whose window contains its started_at and whose
    prompts byte-contain its first user message, and ONLY when the
    candidate's own source is an explicitly non-human worker source
    (LINEAGE_WORKER_SOURCES): a human-facing CLI/API/Discord/Telegram
    session keeps its inbox row even when its first prompt and start
    time match a job — prompt bytes plus a time window are inference,
    never proof a person did not type them. Multiple parents are
    resolved (or rejected) by the caller. A first user message whose
    original character length exceeds LINEAGE_PROMPT_MAX_CHARS is
    rejected before that comparison — its capped read is only a prefix,
    and a job prompt sitting exactly at the cap must not equal that
    prefix."""
    by_worker = {}
    for spec in specs:
        by_worker.setdefault(spec["worker"], []).append(spec)
    claims, rows = {}, {}
    for worker, wspecs in by_worker.items():
        path = dbs_by_profile.get(worker)
        if path is None:
            continue
        try:
            con = _connect_db(path)
            try:
                cands = con.execute(LINEAGE_WORKER_CANDIDATES_SQL, (
                    min(s["lo"] for s in wspecs),
                    max(s["hi"] for s in wspecs))).fetchall()
                ids = [r[0] for r in cands]
                firsts = {}
                for start in range(0, len(ids), LAST_LINE_CHUNK):
                    chunk = ids[start:start + LAST_LINE_CHUNK]
                    sql = LINEAGE_FIRST_USER_SQL.format(
                        chars=LINEAGE_PROMPT_MAX_CHARS,
                        placeholders=",".join("?" * len(chunk)))
                    for sid, text, src_chars in con.execute(sql, chunk):
                        firsts[sid] = (text, src_chars)
            finally:
                con.close()
        except sqlite3.Error:
            continue  # locked/unreadable worker DB -> its runs stay
                      # unlinked (they keep their top-level rows)
        for sid, title, display_name, started, last, ended, end_reason, \
                source in cands:
            if source not in LINEAGE_WORKER_SOURCES:
                continue  # human-facing/blank/unknown: never a child
            text, src_chars = firsts.get(sid, (None, 0))
            if not text:
                continue
            if src_chars > LINEAGE_PROMPT_MAX_CHARS:
                continue  # capped read is only a prefix, never a match
            for spec in wspecs:
                if spec["lo"] <= (started or 0) <= spec["hi"] \
                        and text in spec["prompts"]:
                    key = (worker, sid)
                    claims.setdefault(key, set()).add(spec["parent"])
                    rows[key] = (sid, title, display_name, started, last,
                                 ended, end_reason)
    return claims, rows


def _lineage_terminal_claims(dbs, lo, hi):
    """Standalone "session_id: <id>" lines in windowed terminal tool
    results -> {(child id): {(parent profile, parent id)}}.

    Only sessions with activity inside the read window are scanned;
    each result row is JSON-parsed defensively and capped before it is
    scanned, and only successful results (an integer exit_code of 0 —
    never a bool) are trusted for the ids their output carries.
    Repeated/compacted copies collapse through DISTINCT plus the sets.
    An unparseable or non-JSON row, or one with a missing, nonzero or
    boolean exit code, yields nothing."""
    claims = {}
    for path, profile in dbs:
        try:
            con = _connect_db(path)
            try:
                sids = [r[0] for r in con.execute(
                    LINEAGE_WINDOW_SESSIONS_SQL, (lo, hi))]
                for start in range(0, len(sids), LAST_LINE_CHUNK):
                    chunk = sids[start:start + LAST_LINE_CHUNK]
                    sql = LINEAGE_TERMINAL_SQL.format(
                        chars=LINEAGE_TOOL_JSON_CHARS,
                        placeholders=",".join("?" * len(chunk)))
                    for sid, raw in con.execute(sql, [lo, hi] + chunk):
                        try:
                            obj = json.loads(raw)
                        except (ValueError, TypeError):
                            continue
                        if not isinstance(obj, dict):
                            continue
                        exit_code = obj.get("exit_code")
                        if not isinstance(exit_code, int) \
                                or isinstance(exit_code, bool) \
                                or exit_code != 0:
                            continue  # missing/failed run -> fail open
                        output = obj.get("output")
                        if not isinstance(output, str):
                            continue
                        output = output.replace("\r\n", "\n") \
                                       .replace("\r", "\n")
                        for cid in SESSION_ID_LINE_RE.findall(output):
                            if cid != sid:  # never a self-link
                                claims.setdefault(cid, set()) \
                                      .add((profile, sid))
            finally:
                con.close()
        except sqlite3.Error:
            continue  # locked/unreadable DB -> fail open for its parents
    return claims


def build_lineage(now):
    """One bounded, defensive pass -> {"children": {parent key:
    [child, ...]}, "child_keys": set of (profile, id)}.

    Keys are (profile, session id) — the child's identity, so the same
    session id in two profiles is two different children and never
    collides. Two confident links are honoured — delegate_research jobs
    (worker sessions whose first user message exactly matches a job
    prompt inside that job's window AND whose own source explicitly
    marks a non-human worker run, claimed for the job's origin session
    once that session exists in its owner DB) and terminal launches (a
    standalone session_id line resolving to exactly one *other*
    discovered profile). A child claimed by more than one parent, or
    whose id does not resolve cleanly, is ambiguous and dropped —
    unlinked, so it keeps its inbox row rather than being guessed onto
    either parent."""
    dbs = discover_dbs()
    profiles = {p for _, p in dbs}
    dbs_by_profile = {p: path for path, p in dbs}

    # Research jobs: match worker sessions, then keep only claims whose
    # parent row really exists in the owner's own DB.
    job_claims, job_rows = _lineage_job_children(
        _lineage_job_specs(dbs, profiles, now), dbs_by_profile)
    parent_ids = {}
    for parents in job_claims.values():
        for profile, sid in parents:
            parent_ids.setdefault(profile, set()).add(sid)
    existing_parents = set()
    for profile, ids in parent_ids.items():
        path = dbs_by_profile.get(profile)
        if path is None:
            continue
        found = _lineage_lookup(path, ids) or {}
        existing_parents.update((profile, sid) for sid in found)
    for key, parents in job_claims.items():
        job_claims[key] = {p for p in parents if p in existing_parents}

    # Terminal launches: resolve each printed id in the discovered DBs.
    # An id held by no DB, or by more than one, is ambiguous, and one
    # unreadable DB makes the whole resolution unknowable — every case
    # fails open rather than mis-link.
    window_lo = now - WINDOW_SECONDS - LINEAGE_SKEW_SECONDS
    term_claims = _lineage_terminal_claims(dbs, window_lo,
                                           now + LINEAGE_SKEW_SECONDS)
    claims = {}   # child key -> {parent keys}, both sources merged
    for key, parents in job_claims.items():
        if parents:
            claims[key] = set(parents)
    child_rows = dict(job_rows)
    if term_claims:
        holders = {}   # child id -> {profile: row}
        for path, profile in dbs:
            found = _lineage_lookup(path, term_claims.keys())
            if found is None:
                holders = None
                break
            for sid, row in found.items():
                holders.setdefault(sid, {})[profile] = row
        if holders:
            for cid, parents in term_claims.items():
                found = holders.get(cid, {})
                if len(found) != 1:
                    continue  # unknown or ambiguous id -> fail open
                child_profile, row = next(iter(found.items()))
                if all(p[0] == child_profile for p in parents):
                    continue  # same-profile launch, never cross-profile
                key = (child_profile, cid)
                claims.setdefault(key, set()).update(parents)
                child_rows[key] = row

    children, child_keys = {}, set()
    for key, parents in claims.items():
        if len(parents) != 1:
            continue  # ambiguous parentage -> dropped (stays unlinked)
        row = child_rows.get(key)
        if row is None:
            continue
        sid, title, display_name, started, last, ended, end_reason = row
        children.setdefault(next(iter(parents)), []).append({
            "id": sid,
            "profile": key[0],
            "label": clamp_label(title or display_name or ""),
            "started": started or 0,
            "last": last,
            "ended": ended,
            "end_reason": end_reason or "",
        })
        child_keys.add(key)
    for kids in children.values():
        # Explicit stable order: started_at, then session id, then the
        # child's profile — two children of one parent can never share
        # all three, so the section renders identically every rebuild.
        kids.sort(key=lambda c: (c["started"] or 0, str(c["id"]),
                                 c["profile"]))
    return {"children": children, "child_keys": child_keys}


_lineage_cache = {"at": 0.0, "index": None}
_lineage_lock = threading.Lock()


def lineage_index(now):
    """The process-wide lineage snapshot, rebuilt at most once per
    LINEAGE_CACHE_SECONDS and shared by the inbox and every chat/feed
    render. Any unexpected build error degrades to an empty index —
    linked runs just lose their Sub-agents entries and reappear as
    top-level rows until the next rebuild."""
    with _lineage_lock:
        index = _lineage_cache["index"]
        if index is None \
                or now - _lineage_cache["at"] >= LINEAGE_CACHE_SECONDS:
            try:
                index = build_lineage(now)
            except Exception:
                index = {"children": {}, "child_keys": set()}
            _lineage_cache["at"] = now
            _lineage_cache["index"] = index
        return index


def subagents_for(con, profile, chain):
    """The children a conversation page and its feed polls show: the
    same-profile source='subagent' children of any chain member plus
    the confidently linked cross-profile children of the lineage
    index, merged into one oldest-first list whose rows each carry
    their own profile.

    The 50-child bound (SUBAGENT_MAX_CHILDREN) is applied AFTER the
    merge, over the combined same- and cross-profile list, with the
    same explicit tie-breaker build_lineage sorts by — so a parent
    with runaway dispatches renders a bounded section no matter which
    DBs its children landed in."""
    if isinstance(chain, str):
        chain = [chain]
    children = load_subagents(con, profile, chain)
    linked_index = lineage_index(time.time())["children"]
    linked = []
    for sid in chain:
        linked.extend(linked_index.get((profile, sid)) or [])
    children = children + linked
    children.sort(key=lambda c: (c.get("started") or 0, str(c["id"]),
                                 c.get("profile", "")))
    return children[:SUBAGENT_MAX_CHILDREN]


# The spawn-receipt validator (tools.claude_run_receipts — the same
# fail-closed reader the writer ships with) is an OPTIONAL capability:
# this module's static imports stay stdlib plus the core state helpers
# so the plugin remains standalone-loadable, so it is bound dynamically
# and cached once. Without it (or on any error) every Claude card keeps
# its static form — the viewer link degrades, never the page.
_RECEIPTS_MODULE = None
_RECEIPTS_LOADED = False


def _receipt_watch_url(home, sid, call_id):
    """resolve_watch_url(home, sid, call_id) from the tools layer, or
    None when that module is unavailable. Never raises."""
    global _RECEIPTS_MODULE, _RECEIPTS_LOADED
    if not _RECEIPTS_LOADED:
        _RECEIPTS_LOADED = True
        try:
            _RECEIPTS_MODULE = importlib.import_module(
                "tools.claude_run_receipts")
        except Exception:
            _RECEIPTS_MODULE = None
    if _RECEIPTS_MODULE is None:
        return None
    try:
        return _RECEIPTS_MODULE.resolve_watch_url(home, sid, call_id)
    except Exception:
        return None


def claude_card_watch_url(home, card):
    """The validated live-viewer URL for one Claude dispatch card, or "".

    `home` is the profile's own HERMES_HOME (profile_home — the only
    trusted home source, never request input; resolved once by the
    caller for the whole card set). Resolution is exact on every axis:
    the receipt must live in THAT home's claude-runs tree, keyed by the
    exact carrier session id + tool call id the card carries, and the
    fail-closed reader rejects anything malformed, oversized, mismatched,
    cross-profile, unsafe or missing its run log. A Claude card without
    a call id, the tools layer being unavailable, or any failure keeps
    the static form — a URL is never fabricated from tool arguments."""
    if card.get("tool") != "delegate_claude_agent":
        return ""
    call_id = str(card.get("call_id") or "")
    sid = str(card.get("sid") or "")
    if not call_id or not sid or not home:
        return ""
    return str(_receipt_watch_url(home, sid, call_id) or "")


def delegate_cards(con, profile, chain, children):
    """The dispatch cards one conversation renders at its delegate
    call carriers, plus the suppression/match fallout.

    Returns {"cards", "suppress", "children"}: cards in carrier
    discovery order (one per dispatched task, batch calls contributing
    several at one carrier; repeated delegate_claude_agent carriers of
    one (session, call id) pair fold into a single card tied to the
    latest carrier, because the redriven execution's receipt replaces
    the earlier one at the same path), suppress the set of tool_call
    ids whose generic delegate result row a card replaces, and children
    the sub-agent
    list minus every child a card absorbed (a matched child renders AS
    its dispatch card — linked, with the child's own state — never as a
    second standalone row). Matching is one-to-one and fail-open: a
    child/delegation pairs with a card only inside the time window AND
    with agreeing goal text, and only when exactly one candidate fits;
    anything weaker leaves the card truthful but unlinked and keeps the
    child its own row. A delegate_claude_agent card additionally
    resolves its spawn receipt's live-viewer URL (claude_card_watch_
    url), which takes precedence over any child link on that card."""
    if isinstance(chain, str):
        chain = [chain]
    carriers = con.execute(
        DELEGATE_CALLS_CHAIN_SQL.format(
            chars=DELEGATE_CALLS_CHARS,
            placeholders=",".join("?" * len(chain))),
        list(chain) + [DELEGATE_CALLS_MAX]).fetchall()
    result_times = {}
    for _res_id, _res_name, res_call_id, res_ts in con.execute(
            DELEGATE_RESULTS_CHAIN_SQL.format(
                placeholders=",".join("?" * len(chain))),
            list(chain) + [DELEGATE_RESULTS_MAX]):
        if res_call_id:
            result_times[str(res_call_id)] = res_ts
    delegations = load_delegations(con, chain)

    cards = []
    suppress = set()
    seen_keys = {}
    claude_carriers = {}
    for row_id, row_sid, ts, raw in carriers:
        try:
            carrier_ts = float(ts)
        except (TypeError, ValueError):
            continue
        for call in delegate_carrier_calls(raw):
            spawns = delegate_spawn_calls(call["name"], call["args"])
            if not spawns:
                continue
            call_id = ""
            for candidate in call["ids"]:
                if candidate:
                    call_id = candidate
                    break
            if call_id:
                suppress.add(call_id)
            base = call_id or "row%d" % row_id
            sid = str(row_sid or "")
            for idx, spawn in enumerate(spawns):
                # A redriven delegate_claude_agent call reuses its exact
                # (carrier session, canonical call id) pair, and the
                # second execution's spawn receipt atomically replaces
                # the first at the same path: the pair names ONE logical
                # dispatch. Repeated carriers therefore fold into a
                # single card that follows the LATEST carrier (its time,
                # label, model) — the one execution the receipt can ever
                # point at — instead of multiplying cards that would all
                # silently retarget to the newest run. Distinct sessions
                # sharing one call id, independent calls, id-less
                # carriers and every other delegate tool keep their
                # per-carrier cards.
                if call_id and spawn["tool"] == "delegate_claude_agent":
                    prior = claude_carriers.get((sid, call_id))
                    if prior is not None:
                        prior["ts"] = carrier_ts
                        prior["label"] = clamp_label(spawn["label"])
                        prior["model"] = spawn.get("model") or ""
                        continue
                key = delegate_dom_key(base, idx)
                # Distinct dispatches sharing one base key (one call id
                # echoed in two chain sessions, say) must not share a
                # DOM key: bump the suffix until it is unique.
                bump = seen_keys.get(key, 0)
                seen_keys[key] = bump + 1
                if bump:
                    key = "%s-%d" % (key, bump)
                card = {
                    "key": key,
                    "call_id": call_id,
                    "sid": sid,
                    "ts": carrier_ts,
                    "tool": spawn["tool"],
                    "label": clamp_label(spawn["label"]),
                    "model": spawn.get("model") or "",
                    "profile": profile,
                    "child": None,
                    "delegation": None,
                    "result_ts": (result_times.get(call_id)
                                  if call_id else None),
                }
                cards.append(card)
                if call_id and spawn["tool"] == "delegate_claude_agent":
                    claude_carriers[(sid, call_id)] = card

    # Delegation matching first (the durable dispatch record), one goal
    # slot per card, only inside the window with agreeing text.
    for card in cards:
        matches = []
        for d in delegations:
            if abs(d["dispatched"] - card["ts"]) > DELEGATE_MATCH_WINDOW:
                continue
            for gi, goal in enumerate(d["goals"]):
                if not d["claimed"][gi] and \
                        _goals_agree(goal, card["label"]):
                    matches.append((d, gi))
        chosen = {(id(d), gi) for d, gi in matches}
        if len(chosen) == 1:
            d, gi = matches[0]
            d["claimed"][gi] = True
            card["delegation"] = d
            if not card["model"] and d["model"]:
                card["model"] = d["model"]

    # Then direct/linked children: same window (with a small skew for
    # write ordering), same goal agreement, one-to-one, fail-open.
    remaining = list(children or [])
    for card in cards:
        matches = []
        for c in remaining:
            started = c.get("started") or 0
            try:
                started = float(started)
            except (TypeError, ValueError):
                continue
            if not (-DELEGATE_MATCH_SKEW
                    <= started - card["ts"] <= DELEGATE_MATCH_WINDOW):
                continue
            if _goals_agree(c.get("label"), card["label"]):
                matches.append(c)
        if len(matches) == 1:
            child = matches[0]
            remaining.remove(child)
            card["child"] = child
            card["profile"] = str(child.get("profile") or profile)

    # Last: the Claude viewer link (exact receipt, fail closed), which
    # the renderer and the revision key both consume — so a static card
    # upgrades in place the moment a late receipt lands. One home
    # lookup serves the whole set.
    home = profile_home(profile)
    for card in cards:
        card["watch_url"] = claude_card_watch_url(home, card)
        card["state"] = delegate_card_state(card)
    return {"cards": cards, "suppress": suppress, "children": remaining}


def _row_renders_text(row):
    """True when chat_messages would show this row as a text bubble.

    The delta-feed seam backfill walks rows backwards and must stop at
    exactly the rows that end a tool group; this mirrors chat_messages'
    drop rules ([SILENT], whitespace-only text, and empty assistant
    carriers with nothing recoverable are transparent — they never
    separate two tool rows that render as one group).
    """
    role = row[0]
    if role not in ("user", "assistant"):
        return False
    text = row[4] if isinstance(row[4], str) else ""
    if text == "[SILENT]":
        return False
    if clean_preview(text):
        return True
    if role == "assistant" and not text:
        codex = row[8] if len(row) > 8 else ""
        return bool(clean_preview(codex_commentary_text(codex)))
    return False


def load_feed(profile, session_id, dbs, after, busy_job=False,
              busy_since=None):
    """One feed poll: display items newer than `after`, plus the next
    cursor.

    after=0 means "full snapshot": every displayable row the transcript
    page renders, oldest-first, so a cold client could rebuild the whole
    conversation. Rows are filtered exactly like the page (session_meta,
    hidden and [SILENT] never survive chat_messages, and an
    empty-content assistant carrier recovers its Codex commentary the
    same way), consecutive tools arrive as one group, and last_id is the
    MAX row id — including skipped rows — unless an after>0 catch-up
    poll hit its LIMIT, in which case it stops at the newest row
    actually returned so nothing is jumped over. Row id is the one
    authoritative chronology everywhere (see CHAT_PAGE_SQL), so a
    non-monotonic timestamp can never reorder a delta against the page
    that came before it. Returns None when the session id isn't in that
    profile's DB. sqlite3.Error propagates for the caller to answer as
    a JSON 500.

    Delta polls keep maximal tool runs maximal: when the delta's oldest
    row is a tool, the run it belongs to may have started in an earlier
    poll, so the bounded FEED_BACKFILL_SQL window of immediately
    preceding rows is walked backwards (tools join, transparent rows
    are stepped over, any text-rendering row stops the walk) and the
    COMPLETE group is re-rendered with the first_id it has always had.
    The client replaces its older, shorter group element on the
    first_id match, so one run split across polls still renders as one
    group and never merges across intervening text.

    The activity snapshot is recomputed on every poll, independently of
    the cursor: a delta poll that returns no rows must still report a
    carrier that stays unresolved, so it reads its own bounded tail
    rather than the delta rows. busy_since (a composer turn's
    acceptance time) is that snapshot's turn floor, read under the
    same lock as busy so a job settling mid-poll can never be judged
    with half its state.

    The cursor is a durable high-water mark: the newest row id over
    the conversation's whole canonical chain, captured BEFORE the
    snapshot/delta rows are read (a timestamp or mutable activity
    field is never a cursor), then raised to the newest id the rows
    actually contain when the poll was not capped. A row that lands
    between the cursor capture and the row query is therefore inside
    the snapshot (its id lifts the cursor past it), and anything later
    is replayed by the next after=last_id poll — every event appears
    exactly once, none is lost to the snapshot/delta seam. A capped
    catch-up still stops at the newest row it actually sent.
    """
    con = _connect_db(dbs[profile])
    try:
        sess = con.execute(CHAT_SESSION_SQL, (session_id,)).fetchone()
        if sess is None:
            return None
        _title, _display_name, _started, _last, source, thread_id, \
            archived = sess
        chain = _chain_ids(dbs[profile], session_id)
        ph = ",".join("?" * len(chain))
        # Cursor first: the high-water mark predates the snapshot.
        tip = con.execute(
            FEED_LAST_ID_CHAIN_SQL.format(placeholders=ph), chain
        ).fetchone()[0] or 0
        if after > 0:
            rows = con.execute(
                FEED_AFTER_CHAIN_SQL.format(placeholders=ph),
                chain + [after, FEED_CATCHUP_MAX]
            ).fetchall()
            # Cursor math uses the delta alone — backfilled rows are
            # older than the cursor by construction and never affect
            # it. Not capped -> the cursor rises to the newest id the
            # poll actually saw (tip included, skipped rows too);
            # capped -> stop at what was sent.
            if len(rows) >= FEED_CATCHUP_MAX:
                last_id = max([r[3] for r in rows] + [after])
            else:
                last_id = max([tip] + [r[3] for r in rows])
            if rows and rows[0][0] == "tool":
                back = con.execute(
                    FEED_BACKFILL_CHAIN_SQL.format(placeholders=ph),
                    chain + [rows[0][3], FEED_BACKFILL_MAX]
                ).fetchall()
                prefix = []
                for row in back:  # newest -> oldest
                    if row[0] == "tool":
                        prefix.append(row)
                    elif _row_renders_text(row):
                        break
                prefix.reverse()
                rows = prefix + rows
        else:
            rows = con.execute(
                CHAT_PAGE_CHAIN_SQL.format(placeholders=ph), chain
            ).fetchall()
            rows.reverse()
            last_id = max([tip] + [r[3] for r in rows])
        subagents = subagents_for(con, profile, chain)
        # Dispatch cards recompute on every poll from their own bounded
        # delegate-only queries, independent of the message cursor — a
        # delta poll whose rows don't include the carrier still
        # suppresses the carrier's generic result row and still ships
        # the card (with its current state, and its viewer link the
        # moment a receipt lands) in the keyed payload.
        delegate = delegate_cards(con, profile, chain, subagents)
        activity = compute_activity(con, session_id, time.time(),
                                    busy_job, busy_since)
    finally:
        con.close()
    return {"items": chat_items(chat_messages(
                rows, suppress=delegate["suppress"])),
            "last_id": last_id,
            "subagents": delegate["children"],
            "delegate_cards": delegate["cards"],
            "activity": activity,
            "session_state": {
                "archived": bool(archived),
                "discord_thread": bool(source == "discord" and thread_id),
                "can_toggle": True,
            }}


def load_session_cwd(profile, session_id, dbs):
    """(exists, cwd, archived) for one session in its own profile DB;
    cwd may be None. The reply route refuses new turns while archived
    is set (and the page renders the working directory the session
    was rooted in)."""
    con = _connect_db(dbs[profile])
    try:
        row = con.execute(SESSION_CWD_SQL, (session_id,)).fetchone()
    finally:
        con.close()
    return (row is not None, row[0] if row is not None else None,
            bool(row[1]) if row is not None else False)


# ---- Discord API plumbing -------------------------------------------
# The token for a profile lives in the .env beside its state.db. It is
# read fresh from disk (never cached across passes, never logged) and
# only ever leaves the process as an in-memory Authorization header.
def load_env_value(env_path, name):
    """One KEY's value from a .env file, or None (no file, no such key,
    empty value). Parsed line-wise with split('=', 1) so '=' inside the
    value survives; surrounding quotes are stripped. The value is never
    written anywhere."""
    try:
        with open(env_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() != name:
                    continue
                value = value.strip().strip('"').strip("'").strip()
                return value or None
    except OSError:
        pass
    return None


def load_discord_token(db_path):
    """The DISCORD_BOT_TOKEN from the .env beside one profile's DB, or
    None. The value is never written anywhere."""
    return load_env_value(os.path.join(os.path.dirname(db_path), ".env"),
                          "DISCORD_BOT_TOKEN")


# ---- core-API clarify client -----------------------------------------
# Reads and answers the core's per-session clarify routes for the
# transcript page only. Fail-closed on every axis: no key means no
# call, an error never reaches the page as a crash, and nothing beyond
# the bounded card fields ever crosses in either direction.


def clarify_api_key(profile, dbs):
    """The API_SERVER_KEY authorized for this profile's clarify routes,
    or None. The default profile's key comes from the .env beside the
    main DB; a named profile's key comes ONLY from the .env beside its
    own state.db — never the main file, so a named profile without its
    own key simply has no key. The value never leaves this process
    except as one request's Authorization header."""
    if profile == "default":
        env_path = os.path.join(os.path.dirname(MAIN_DB), ".env")
    else:
        db_path = dbs.get(profile)
        if not db_path:
            return None
        env_path = os.path.join(os.path.dirname(db_path), ".env")
    return load_env_value(env_path, "API_SERVER_KEY")


def core_api_request(method, path, profile, dbs, payload=None,
                     timeout=None):
    """One authenticated core API call -> (status, obj, err).

    path is the route after the profile prefix (the caller quotes any
    ids it embeds): "/v1/runs", "/v1/runs/<run_id>",
    "/api/sessions/<sid>/clarify". err is None only on a 2xx whose body
    parsed (or was empty); otherwise it is a bounded safe string
    carrying the HTTP status or the failure class alone — never the
    key, never the upstream body (exception text is reduced to its
    class name so a URL can never leak either). The body read is capped
    at CLARIFY_MAX_BODY_BYTES + 1; larger is an error. The key appears
    only in the Authorization header of this one request object.
    """
    key = clarify_api_key(profile, dbs)
    if not key:
        return 0, None, "no API key configured"
    url = (CLARIFY_API_BASE.rstrip("/") + "/p/"
           + quote(profile, safe="") + path)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    raw = b""
    try:
        with urllib.request.urlopen(
                req,
                timeout=CLARIFY_TIMEOUT_SECONDS if timeout is None
                else timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            raw = resp.read(CLARIFY_MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code or 0
        try:
            raw = exc.read(CLARIFY_MAX_BODY_BYTES + 1)
        except OSError:
            raw = b""
    except Exception as exc:  # keep the page alive no matter what
        # Never include the exception text: it could echo the URL or
        # environment. The class name is enough — and this arm also
        # catches the http.client read errors (IncompleteRead and kin)
        # that are neither OSError nor ValueError.
        return 0, None, "request failed (%s)" % type(exc).__name__
    if len(raw) > CLARIFY_MAX_BODY_BYTES:
        return status, None, "response too large"
    obj = None
    if raw:
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            obj = None
    if status < 200 or status >= 300:
        return status, obj, "upstream HTTP %d" % status
    if raw and obj is None:
        return status, None, "unparseable response body"
    return status, obj, None


def clarify_request(method, profile, session_id, dbs, payload=None):
    """One authenticated core clarify call -> (status, obj, err).

    Thin path builder over core_api_request; same contract, same
    bounds, same fail-closed error strings.
    """
    return core_api_request(
        method,
        "/api/sessions/" + quote(session_id, safe="") + "/clarify",
        profile, dbs, payload)


def clarify_fetch_card(profile, session_id, dbs):
    """(card, err): the bounded pending clarify card for one session.

    card is None with err None when the core affirmatively reports no
    pending clarify ({"pending_clarify": null}); a dict {clarify_id,
    question, choices, multi_select} when one is pending; None with a
    safe err string on any failure. The upstream card is re-validated
    and re-bounded here — a malformed or hostile card is an error,
    never a crash and never rendered."""
    status, obj, err = clarify_request("GET", profile, session_id, dbs)
    if err is not None:
        return None, err
    if not isinstance(obj, dict):
        return None, "unexpected response shape"
    pending = obj.get("pending_clarify")
    if pending is None:
        return None, None
    if not isinstance(pending, dict):
        return None, "unexpected response shape"
    cid = pending.get("clarify_id")
    question = pending.get("question")
    if not isinstance(cid, str) or not cid.strip() \
            or len(cid) > CLARIFY_ID_MAX_CHARS:
        return None, "unexpected response shape"
    if not isinstance(question, str):
        return None, "unexpected response shape"
    choices_raw = pending.get("choices")
    choices = None
    if choices_raw is not None:
        if not isinstance(choices_raw, list):
            return None, "unexpected response shape"
        cleaned = []
        for choice in choices_raw[:CLARIFY_MAX_CHOICES]:
            if not isinstance(choice, str) or not choice.strip():
                return None, "unexpected response shape"
            cleaned.append(choice.strip()[:CLARIFY_MAX_CHOICE_CHARS])
        choices = cleaned
    return {
        "clarify_id": cid,
        "question": question.strip()[:CLARIFY_MAX_QUESTION_CHARS],
        "choices": choices,
        "multi_select": bool(pending.get("multi_select")),
    }, None


def valid_clarify_response(resp):
    """True when resp is exactly one of the two response shapes the
    core accepts: a non-empty string, or a non-empty list (bounded by
    CLARIFY_MAX_RESPONSE_ITEMS) of non-empty strings. Numbers, dicts,
    booleans, null and empty shapes are refused before proxying."""
    if isinstance(resp, str):
        return bool(resp.strip()) and len(resp) <= MAX_TEXT_CHARS
    if isinstance(resp, list) and resp \
            and len(resp) <= CLARIFY_MAX_RESPONSE_ITEMS:
        return all(isinstance(item, str) and item.strip()
                   and len(item) <= MAX_TEXT_CHARS for item in resp)
    return False


def feed_clarify(profile, session_id, dbs, archived):
    """The feed/poll shape of the pending clarify, or None on error.

    {active, id, html} — html being the exact escaped card markup the
    page renders. An archived session skips the upstream call entirely
    (its card would be unanswerable) and reports none. None (the caller
    omits the field) on any API error so an open page keeps its current
    card instead of flashing it away on a blip; {active: False} only
    ever means the core affirmatively reported no pending clarify."""
    if archived:
        return {"active": False, "id": "", "html": ""}
    card, err = clarify_fetch_card(profile, session_id, dbs)
    if err is not None:
        return None
    if card is None:
        return {"active": False, "id": "", "html": ""}
    return {"active": True, "id": card["clarify_id"],
            "html": render_clarify_card(card)}


def waiting_session_ids(dbs, profiles):
    """{profile: set(session ids)} the core says wait on the human.

    ONE bounded GET /api/sessions/waiting per profile per inbox
    refresh — the batch answer that feeds the Your turn section, so
    the request count scales with profiles, never with rows. The
    endpoint is read-only and profile-scoped; it names sessions only,
    so native gateway waits (a clarify prompt or restart confirmation
    armed by the gateway itself, not an API run) are included without
    this server ever seeing their questions.

    Fail closed on every axis: no key, an unreachable core, a non-2xx,
    or an unparseable/hostile body (a waiting value that is not a list
    included) simply omits the profile from the map — its rows keep
    the sections they already had, and no wait is ever invented from a
    guess."""
    waits = {}
    for profile in sorted(profiles):
        _status, obj, err = core_api_request(
            "GET", "/api/sessions/waiting", profile, dbs,
            timeout=WAITING_TIMEOUT_SECONDS)
        if err is not None or not isinstance(obj, dict):
            continue
        raw = obj.get("waiting")
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            # A non-list waiting value ({"waiting": 1} or true) is
            # malformed, not an empty wait list: fail closed like any
            # other hostile body rather than crash iterating it.
            continue
        ids = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            sid = item.get("session_id")
            if isinstance(sid, str) and sid.strip():
                ids.add(sid.strip())
        waits[profile] = ids
    return waits


def apply_discord_snapshot(db_path, active_ids, now):
    """Mirror one authoritative active-thread snapshot into one profile DB.

    Only sessions whose thread saw activity in the rolling 24h window are in
    scope (older history is never synced). Desired state: archived = false when
    the thread appears in the complete active-thread set, else true. Only
    changed rows are updated, in one transaction, and each thread's rows are
    read back for exact equality before the commit — a mismatch rolls
    everything back. Returns the number of changed rows."""
    lo = now - WINDOW_SECONDS
    con = _connect_db(db_path, write=True, timeout=5.0)
    try:
        tids = [r[0] for r in con.execute(
            DISCORD_RECENT_THREADS_SQL, (lo,))]
        changed = 0
        try:
            con.execute("BEGIN IMMEDIATE")
            for tid in tids:
                if not isinstance(tid, str) \
                        or not SNOWFLAKE_RE.fullmatch(tid):
                    continue
                desired = 0 if tid in active_ids else 1
                changed += con.execute(
                    SET_ARCHIVE_BY_THREAD_CHANGED_SQL,
                    (desired, tid, desired)).rowcount
            for tid in tids:
                if not isinstance(tid, str) \
                        or not SNOWFLAKE_RE.fullmatch(tid):
                    continue
                desired = 0 if tid in active_ids else 1
                mismatch = con.execute(
                    COUNT_THREAD_MISMATCH_SQL, (tid, desired)
                ).fetchone()[0]
                if mismatch:
                    raise sqlite3.Error("read-back mismatch")
            con.commit()
        except sqlite3.Error:
            con.rollback()
            raise
        return changed
    finally:
        con.close()


def discord_sync_once(now):
    """One sync pass over every profile that has a Discord token.

    A profile is mirrored only when its snapshot is authoritative
    (every guild and active-thread request succeeded with the expected
    shape); on any partial failure the profile is skipped whole — an
    absent thread is never inferred archived from incomplete data.
    Per-profile outcomes are one safe stderr line each (profile, status
    class, path class only — never bodies or headers).

    A snapshot is only a candidate: the profile's archive epoch is read
    before the fetch, and re-checked under _archive_epoch_lock before
    the snapshot is applied (also under the lock). If the user closed or
    reopened anything while the fetch was in flight, the epoch moved and
    the stale snapshot is discarded whole — the user-confirmed state
    wins by construction, and no transaction here can interleave with a
    user-mutation transaction on the same lock."""
    for db_path, profile in discover_dbs():
        token = load_discord_token(db_path)
        if not token:
            continue
        epoch = _archive_epoch(db_path)
        active, err = fetch_active_thread_ids(token)
        if err is not None:
            sys.stderr.write("discord-sync: %s skipped: %s\n"
                             % (profile, err))
            continue
        try:
            with _archive_epoch_lock:
                if _archive_epochs.get(db_path, 0) != epoch:
                    sys.stderr.write(
                        "discord-sync: %s snapshot superseded by a user "
                        "action; keeping user state\n" % profile)
                    continue
                changed = apply_discord_snapshot(db_path, active, now)
        except sqlite3.Error:
            sys.stderr.write("discord-sync: %s db write failed\n"
                             % profile)
            continue
        if changed:
            sys.stderr.write("discord-sync: %s mirrored (%d changed)\n"
                             % (profile, changed))


def discord_sync_loop():
    """The background mirror: a pass immediately, then every
    DISCORD_SYNC_INTERVAL_SECONDS until _discord_sync_stop is set. Failures
    never kill the thread and never raise — one bad pass just waits for the next
    interval (Discord failures are already logged by the pass itself)."""
    while not _discord_sync_stop.is_set():
        try:
            discord_sync_once(time.time())
        except Exception as exc:
            sys.stderr.write("discord-sync: pass failed (%s)\n"
                             % type(exc).__name__)
        _discord_sync_stop.wait(DISCORD_SYNC_INTERVAL_SECONDS)


def set_session_archived(profile, session_id, dbs, desired):
    """One close/reopen action -> (http_status, payload).

    Unknown profile/session ids are refused before anything runs. For
    a Discord thread session the thread is patched first and the local
    mirror happens only when Discord answers 200 with the requested
    thread_metadata.archived exactly (a 200 with the wrong state is a
    failure); the local write then flips every row on that thread in
    one transaction with an exact read-back. On any Discord or token
    failure before the PATCH confirms, nothing local changes and the
    payload says so. Once Discord HAS confirmed, a later local mirror
    failure can no longer claim "nothing was changed": the payload
    carries discord_changed/sync_pending and says the background sync
    will retry, without a local affected count. A non-Discord (or
    threadless) session flips every member of its canonical
    compression chain in one transaction — the listed conversation is
    its projected tip, so the root's row must follow the tip's — with
    an exact read-back over the same set; no Discord claim is made.
    Every local write bumps the profile's archive epoch and runs
    under _archive_epoch_lock, so a background snapshot fetched before
    the user acted can never overwrite this result. The payload always
    carries ok/archived/discord/thread_id/affected (or a bounded safe
    error)."""
    db_path = dbs[profile]
    try:
        con = _connect_db(db_path)
        try:
            row = con.execute(SESSION_STATE_SQL, (session_id,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return 500, {"ok": False, "error": "database error"}
    if row is None:
        return 404, {"ok": False, "error": "unknown session"}
    source, thread_id, _archived = row
    archived_now = 1 if desired else 0
    is_discord = (source == "discord" and isinstance(thread_id, str)
                  and SNOWFLAKE_RE.fullmatch(thread_id))
    base = {"archived": bool(desired), "discord": bool(is_discord),
            "thread_id": thread_id if is_discord else None}
    # The canonical chain the close/reopen acts on: for a non-Discord
    # conversation every compression member flips together (the listed
    # conversation is its projected tip, so closing the tip must close
    # the root's row too and vice versa). A Discord thread session
    # keeps flipping by thread_id — continuation rows already share it.
    chain = _chain_ids(db_path, session_id) if not is_discord \
        else [session_id]
    chain_ph = ",".join("?" * len(chain))
    if is_discord:
        token = load_discord_token(db_path)
        if not token:
            return 502, {"ok": False, "affected": 0, **base,
                         "error": "no Discord token for this profile; "
                                  "nothing was changed"}
        status, obj, err = discord_request(
            "PATCH", "/channels/%s" % thread_id, token,
            {"archived": bool(desired)})
        if err is not None or status != 200 or not isinstance(obj, dict):
            return 502, {"ok": False, "affected": 0, **base,
                         "error": "Discord refused the change (%s); "
                                  "nothing was changed"
                                  % (err or "unexpected response")}
        meta = obj.get("thread_metadata")
        confirmed = meta.get("archived") if isinstance(meta, dict) else None
        if not isinstance(confirmed, bool) or confirmed != bool(desired):
            return 502, {"ok": False, "affected": 0, **base,
                         "error": "Discord did not confirm the new "
                                  "state; nothing was changed"}
        try:
            with _archive_epoch_lock:
                # The user mutation owns this DB until the transaction
                # lands: bump the epoch first so any snapshot the
                # background sync already fetched for this profile is
                # stale from this instant on, then write under the same
                # lock so neither path can interleave.
                _archive_epochs[db_path] = \
                    _archive_epochs.get(db_path, 0) + 1
                conw = _connect_db(db_path, write=True, timeout=5.0)
                try:
                    conw.execute("BEGIN IMMEDIATE")
                    affected = conw.execute(
                        SET_ARCHIVE_BY_THREAD_SQL,
                        (archived_now, thread_id)).rowcount
                    mismatch = conw.execute(
                        COUNT_THREAD_MISMATCH_SQL,
                        (thread_id, archived_now)).fetchone()[0]
                    if mismatch:
                        raise sqlite3.Error("read-back mismatch")
                    conw.commit()
                except sqlite3.Error:
                    conw.rollback()
                    # Discord already confirmed the change — do NOT claim
                    # "nothing was changed". Say what happened honestly:
                    # Discord flipped, the local mirror did not, and the
                    # background sync will reconcile. affected is omitted:
                    # no local row count was durably written. The epoch
                    # stays bumped: the failed write left local state
                    # untouched and the user's confirmed Discord state
                    # must still win over any older snapshot.
                    return 500, {"ok": False, **base,
                                 "discord_changed": True,
                                 "sync_pending": True,
                                 "error": "Discord state changed but the "
                                          "local mirror failed; the "
                                          "background sync will retry"}
                finally:
                    conw.close()
        except sqlite3.Error:
            return 500, {"ok": False, **base,
                         "discord_changed": True,
                         "sync_pending": True,
                         "error": "Discord state changed but the local "
                                  "database is unavailable; the "
                                  "background sync will retry"}
        return 200, {"ok": True, "affected": affected, **base}
    try:
        with _archive_epoch_lock:
            # Same ownership rule as the Discord-confirmed branch: the
            # epoch bump plus the write happen together under the lock
            # so an in-flight snapshot can never land over this one.
            _archive_epochs[db_path] = \
                _archive_epochs.get(db_path, 0) + 1
            conw = _connect_db(db_path, write=True, timeout=5.0)
            try:
                conw.execute("BEGIN IMMEDIATE")
                affected = conw.execute(
                    SET_ARCHIVE_BY_CHAIN_SQL.format(
                        placeholders=chain_ph),
                    [archived_now] + chain
                ).rowcount
                mismatch = conw.execute(
                    COUNT_CHAIN_MISMATCH_SQL.format(
                        placeholders=chain_ph),
                    chain + [archived_now]
                ).fetchone()[0]
                if mismatch:
                    raise sqlite3.Error("read-back mismatch")
                conw.commit()
            except sqlite3.Error:
                conw.rollback()
                return 500, {"ok": False, "affected": 0, **base,
                             "error": "local update failed; nothing was "
                                      "changed"}
            finally:
                conw.close()
    except sqlite3.Error:
        return 500, {"ok": False, "affected": 0, **base,
                     "error": "database error; nothing was changed"}
    return 200, {"ok": True, "affected": affected, **base}


# ---- composer turn transport (core API runs) -------------------------
# Every composer turn — a reply on an existing session or a fresh
# launch from /new — runs as a /v1/runs run on the core API server,
# never as an oneshot CLI subprocess. Two things make that the only
# path: the run's agent carries the gateway clarify callback, so a
# mid-turn question registers in tools.clarify_gateway under the exact
# canonical session id and becomes the card this server can serve and
# answer (the CLI's -q callback auto-answers instead — the bug the
# review flagged), and the admission 202 names the session id
# deterministically, so navigation needs no DB-row correlation.
#
# Nothing about a turn ever touches argv: the prompt goes only into
# the body of the one admission request and the per-profile key only
# into its Authorization header (see core_api_request). Admission is
# synchronous with the POST that accepted the turn — an unavailable
# core surfaces as an explicit failed send with the text restored,
# never a silent fallback and never a duplicate run — while a
# background poller holds the session's busy lease until the run
# settles.


def session_row_exists(db_path, session_id):
    """True when the sessions table already has this row.

    Admission names the id deterministically, but the core writes the
    row when the agent starts executing, so a client that navigates
    the instant it sees the id would otherwise race a page the DB
    cannot serve yet. Bound by the same discovery rule as every other
    read: a DB symlinked outside the configured home is not opened."""
    try:
        con = _connect_db(db_path)
    except sqlite3.Error:
        return False
    try:
        row = con.execute(
            "SELECT 1 FROM sessions WHERE id = ? LIMIT 1",
            (session_id,)).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        con.close()


def admit_run(profile, dbs, session_id, text):
    """Admit exactly one core API run for a composer turn.

    (run_id, session_id, None) on a clean 202 — the session id being
    the one asked for, or when session_id is empty the fresh
    deterministic id the core assigned and echoed — and (None, None,
    reason) otherwise, reason a bounded safe word for the failure
    class ("unavailable", "refused", "malformed", "mismatch"). The
    prompt appears only inside this one request's JSON body; the reply
    statuses keep no trace of it."""
    payload = {"input": text}
    if session_id:
        payload["session_id"] = session_id
    status, obj, err = core_api_request(
        "POST", "/v1/runs", profile, dbs, payload,
        timeout=RUNS_TIMEOUT_SECONDS)
    if err is not None:
        return None, None, "unavailable"
    if status != 202 or not isinstance(obj, dict):
        return None, None, "refused"
    run_id = obj.get("run_id")
    sid = obj.get("session_id")
    if not isinstance(run_id, str) or not run_id.strip() \
            or not isinstance(sid, str) or not sid.strip():
        return None, None, "malformed"
    if session_id and sid != session_id:
        # The core must run the exact session the composer addressed;
        # anything else would silently fork the conversation.
        return None, None, "mismatch"
    return run_id, sid, None


def run_state(profile, dbs, run_id):
    """(state_word, err) for one admitted run.

    The state word is the pollable status verb alone ("running",
    "waiting_for_clarify", "completed", ...) — never a field that
    could carry prompt, transcript, or card content."""
    status, obj, err = core_api_request(
        "GET", "/v1/runs/" + quote(run_id, safe=""), profile, dbs,
        timeout=RUNS_TIMEOUT_SECONDS)
    if err is not None or not isinstance(obj, dict):
        return "", err or "unavailable"
    state = obj.get("status")
    return (state.strip() if isinstance(state, str) else ""), None


def stop_run(profile, dbs, run_id):
    """Best-effort stop of one run whose deadline was reached."""
    core_api_request(
        "POST", "/v1/runs/" + quote(run_id, safe="") + "/stop",
        profile, dbs, {}, timeout=RUNS_TIMEOUT_SECONDS)


def wait_run_settled(profile, dbs, run_id, on_session_row=None):
    """Poll one admitted run until it settles.

    Returns the settling word: "completed", "failed" (the run reported
    a terminal failure), or "timeout" (the deadline below was reached
    and the run was stopped best-effort — the same bound the oneshot
    CLI cap had, so a wedged run can never hold a session busy
    forever). on_session_row(), when given, is called every pass until
    it returns True; the fresh-launch worker uses it to publish the
    session id exactly when the row becomes servable. Never raises."""
    deadline = time.monotonic() + HERMES_TIMEOUT_SECONDS
    notified = False
    try:
        while True:
            state, _err = run_state(profile, dbs, run_id)
            if state in RUN_TERMINAL_STATES:
                return "completed" if state == "completed" else "failed"
            if on_session_row is not None and not notified \
                    and on_session_row():
                notified = True
            if time.monotonic() >= deadline:
                stop_run(profile, dbs, run_id)
                return "timeout"
            time.sleep(RUN_POLL_SECONDS)
    except Exception:
        return "failed"


def reply_worker(profile, session_id, run_id, dbs):
    """Hold the session's busy lease while the admitted run executes.

    Admission already happened synchronously in start_reply; this
    thread only watches the run to its terminal state (so the busy and
    typing semantics match the oneshot era, including a question that
    legitimately pauses the run mid-turn), releases the lease exactly
    once, and leaves the feed a one-shot canned note when the turn did
    not complete. Never retries, never admits a second run."""
    key = (profile, session_id)
    outcome = wait_run_settled(profile, dbs, run_id)
    with _jobs_lock:
        _jobs.pop(key, None)
        if outcome != "completed":
            _job_notes[key] = ("The reply run failed"
                               + (" (timed out)"
                                  if outcome == "timeout" else "")
                               + "; nothing was added to the session.")


def start_reply(profile, session_id, text, dbs):
    """Accept one composer reply: admit the core run, then hand the
    lease to the poller.

    Returns "started" (answer 202), "busy" (409 — a turn is already
    running for the session) or "unavailable" (the core API could not
    admit the run; nothing was created, so the client must surface a
    failed send and restore the text). The lease is taken before
    admission so a double submission cannot admit twice, and released
    here when admission fails — no state outlives a refused turn."""
    key = (profile, session_id)
    with _jobs_lock:
        if key in _jobs:
            return "busy"
        _jobs[key] = {"started": time.time()}
    run_id, _sid, reason = admit_run(profile, dbs, session_id, text)
    if run_id is None:
        with _jobs_lock:
            _jobs.pop(key, None)
        return "unavailable"
    threading.Thread(
        target=reply_worker, args=(profile, session_id, run_id, dbs),
        daemon=True).start()
    return "started"


def session_job_started(profile, session_id):
    """The acceptance time of the composer turn this server is currently
    running for the session, or None when none is — a pure peek
    (unlike session_job_state it never consumes a note), so a page
    render can carry the live strip and its turn floor without eating
    the one-shot failure note the feed still owes the client. None
    doubles as the busy answer: no started time, no live turn."""
    with _jobs_lock:
        job = _jobs.get((profile, session_id))
        return job.get("started") if job else None


def session_job_state(profile, session_id):
    """(busy, turn acceptance time, one-shot failure note) for the
    feed, under one lock — busy and the floor that scopes the turn's
    first-output detection are read atomically, so a job settling
    between two peeks can never be judged with half its state."""
    key = (profile, session_id)
    with _jobs_lock:
        job = _jobs.get(key)
        return (job is not None,
                job.get("started") if job else None,
                _job_notes.pop(key, None))


# ---- new-session launch jobs (POST /s/new) ---------------------------
# Acceptance is decoupled from the run: POST /s/new validates, starts
# exactly one background launch and answers 202 with an opaque job id;
# the run's progress is served by GET /s/new/<job>. The registry below
# is bounded and thread-safe and holds only opaque state — job id,
# state word, session id, a canned error string, timestamps — never
# the prompt (that goes only into the admission request's body) and
# never core output (parsed for status words only, then dropped).
NEW_JOB_STARTING = "starting"   # accepted; admission not yet complete
NEW_JOB_RUNNING = "running"     # admitted; the run is executing
NEW_JOB_DONE = "done"           # the run completed
NEW_JOB_FAILED = "failed"       # admission failed, or the run failed
NEW_JOB_LIVE_STATES = (NEW_JOB_STARTING, NEW_JOB_RUNNING)
# Terminal jobs are pruned oldest-first past the cap and dropped once
# they have been finished this long; a live job is never dropped (and
# at most one is ever live — see start_new_session).
NEW_JOBS_MAX = 64
NEW_JOB_TTL_SECONDS = 15 * 60
_new_jobs = {}
_new_jobs_lock = threading.Lock()


def _new_job_error(reason):
    """Canned failure line for a launch job: reason word only — never
    core output, never the prompt, nothing secret-shaped."""
    if reason == "timeout":
        return "the launch timed out"
    if reason == "unavailable":
        return "the agent gateway could not be reached"
    return "the launch failed"


def _prune_new_jobs_locked(now):
    """Bound the registry. Caller holds _new_jobs_lock.

    Terminal jobs past NEW_JOB_TTL_SECONDS go first, then the oldest
    terminal ones until the registry is under NEW_JOBS_MAX. Live jobs
    are never pruned here; the one-launch-at-a-time rule already bounds
    them to a single entry."""
    stale = sorted(
        (job.get("finished") or job.get("created") or now, job_id)
        for job_id, job in _new_jobs.items()
        if job["state"] not in NEW_JOB_LIVE_STATES)
    for stamp, job_id in stale:
        if now - stamp < NEW_JOB_TTL_SECONDS:
            break
        _new_jobs.pop(job_id, None)
    while len(_new_jobs) > NEW_JOBS_MAX:
        oldest = min(
            ((job.get("finished") or job.get("created") or now, job_id)
             for job_id, job in _new_jobs.items()
             if job["state"] not in NEW_JOB_LIVE_STATES),
            default=None)
        if oldest is None:
            break  # all live; serialization keeps this at one anyway
        _new_jobs.pop(oldest[1], None)


def new_job_payload(job_id):
    """The status route's view of one job, or None when unknown/pruned.

    {ok, job, status, session_id, url, error} and nothing else: opaque
    words and ids plus the canned error string — no prompt, no output,
    no secrets, because the registry never held any."""
    with _new_jobs_lock:
        job = _new_jobs.get(job_id)
        if job is None:
            return None
        sid = job.get("session_id")
        return {
            "ok": True,
            "job": job_id,
            "status": job["state"],
            "session_id": sid,
            "url": ("/s/default/" + quote(sid, safe="")
                    if isinstance(sid, str) and sid else None),
            "error": job.get("error") or "",
        }


def new_session_worker(job_id, text):
    """One background launch behind POST /s/new.

    Under the launch lock (one at a time, so a duplicate POST can never
    admit a second run): admit a fresh core run with no session id —
    the core assigns the deterministic one and the 202 echoes it —
    publish that id to the job the moment the session's row exists in
    the main DB (the status route can then send the client to
    /s/default/<id> before the run finishes, exactly when the page can
    actually be served), register the busy lease for the fresh session
    with the launch's own acceptance time as the turn floor, then
    settle the job exactly once — done when the run completed, failed
    otherwise. Never retries."""
    reason = "unavailable"
    sid = None
    with _new_session_lock:
        with _new_jobs_lock:
            job = _new_jobs.get(job_id)
            if job is None or job["state"] != NEW_JOB_STARTING:
                return  # pruned or unknown: run nothing
            job["state"] = NEW_JOB_RUNNING
        dbs = {name: db_path for db_path, name in discover_dbs()}
        run_id, sid, why = admit_run("default", dbs, "", text)
        if run_id is None:
            sid = None
            reason = why or "unavailable"
        else:
            with _new_jobs_lock:
                job = _new_jobs.get(job_id)
                created = (job or {}).get("created") or time.time()

            def _publish_when_row_exists():
                if not session_row_exists(MAIN_DB, sid):
                    return False
                with _new_jobs_lock:
                    job = _new_jobs.get(job_id)
                    if job is not None and not job.get("session_id"):
                        job["session_id"] = sid
                # The busy registration carries the job's acceptance
                # time, not this discovery moment: that stamp is the
                # floor that scopes first-output detection to the
                # launched turn, and rows written between acceptance
                # and discovery are that turn's own.
                with _jobs_lock:
                    if ("default", sid) not in _jobs:
                        _jobs[("default", sid)] = {"started": created,
                                                   "launch": job_id}
                return True

            outcome = wait_run_settled(
                "default", dbs, run_id,
                on_session_row=_publish_when_row_exists)
            reason = None if outcome == "completed" else outcome
    with _new_jobs_lock:
        job = _new_jobs.get(job_id)
        resolved = (job or {}).get("session_id")
        if resolved is None and sid and session_row_exists(MAIN_DB, sid):
            # A run that settled before a poll ever observed the row (a
            # fast failure after the agent already wrote it) still owns
            # that row: publish it so a client can navigate and read the
            # failure note — the end-of-run diff the oneshot era used.
            resolved = sid
            if job is not None:
                job["session_id"] = sid
        if job is not None:
            job["finished"] = time.time()
            if reason is None and resolved:
                job["state"] = NEW_JOB_DONE
                job["error"] = ""
            else:
                job["state"] = NEW_JOB_FAILED
                job["error"] = _new_job_error(reason or "failed")
            _prune_new_jobs_locked(time.time())
    if resolved:
        # Release the busy registration the publisher made (a plain
        # reply could not have taken the key meanwhile — it would have
        # been refused 409), and on failure leave the session page a
        # one-shot note, exactly like a failed reply run.
        key = ("default", resolved)
        with _jobs_lock:
            _jobs.pop(key, None)
            if reason is not None:
                _job_notes[key] = ("The new-session run failed"
                                   + (" (timed out)"
                                      if reason == "timeout" else "")
                                   + "; the session may be incomplete.")


def start_new_session(text):
    """Accept one new-session launch: (job_id, None) or (None, "busy").

    Registers an opaque job and starts exactly one background worker.
    While any launch is still live a second call is refused — one at a
    time is what fails a double-submitted composer closed instead of
    admitting a duplicate run. The prompt text goes only to the worker
    (and from there into the admission request's body), never into the
    registry or any response."""
    with _new_jobs_lock:
        for job in _new_jobs.values():
            if job["state"] in NEW_JOB_LIVE_STATES:
                return None, "busy"
        job_id = secrets.token_urlsafe(12)
        _new_jobs[job_id] = {
            "state": NEW_JOB_STARTING,
            "session_id": None,
            "error": "",
            "created": time.time(),
            "finished": None,
        }
        _prune_new_jobs_locked(time.time())
    threading.Thread(target=new_session_worker, args=(job_id, text),
                     daemon=True).start()
    return job_id, None


# ---- Discord-style app shell -----------------------------------------
# Both pages (the inbox and every transcript) are ONE full-viewport
# application shell modeled on Discord's current dark desktop UI: a
# narrow left rail with the real profile filters (All plus one letter
# badge per discovered profile, with a live-presence dot when
# a profile has an Active session), a 240px conversation sidebar
# (server-style header, search, New chat, the honest Active / Closed /
# Open sections, and a user panel carrying the sync state), and a main
# panel on the layered charcoal surface (#1e1f22 / #2b2d31 / #313338,
# blurple #5865f2). On / the main panel is a select-a-chat splash; on
# /s/<profile>/<id> it is the transcript. Under 900px the shell becomes
# a single pane: the rail collapses into a chip row inside the sidebar,
# the inbox shows the sidebar only, and a transcript shows only the
# chat with a working back link.


# Profiles pinned in the left rail, in order; an "All" filter sits above
# them. Every rail item is a real filter control (in-place on the inbox,
# a link to the filtered inbox from chat pages) — nothing decorative.
# The list is whatever profile DBs exist right now (the main "default"
# DB always first, then each profile directory alphabetically), so the
# rail never names a profile this install does not have.
def rail_profiles():
    """Profile keys for the left rail and filter chips, in rail order."""
    return [profile for _path, profile in discover_dbs()]


def render_rail(active_profile, presence):
    """The narrow left rail: "All" plus one item per known profile.

    active_profile is "" for All, a profile key for one filter, or None
    for "no selection" (a chat whose profile isn't in the rail).
    presence is the set of profiles with an Active session — those
    badges get the green live-presence dot.
    """
    esc = html.escape
    items = []
    items.append(
        '<a class="rail-item%s" href="/" data-profile-filter=""'
        ' title="All conversations" aria-label="All conversations">'
        '<span class="rail-ico rail-all" aria-hidden="true">All</span>'
        '</a>\n' % (" is-selected" if active_profile == "" else ""))
    items.append('<span class="rail-sep" aria-hidden="true"></span>\n')
    for key in rail_profiles():
        ident = profile_identity(key)
        sel = " is-selected" if active_profile == key else ""
        pres = ""
        if key in presence:
            pres = ('<i class="pres" role="img" aria-label="live session"'
                    ' title="a session is live"></i>')
        items.append(
            '<a class="rail-item%s" href="/?profile=%s"'
            ' data-profile-filter="%s" title="%s" aria-label="%s chats">'
            '<span class="rail-ico">'
            '<span aria-hidden="true">%s</span>%s</span>%s</a>\n'
            % (sel, esc(key), esc(key), esc(ident["label"]),
               esc(ident["label"]), esc(ident["letter"]),
               avatar_img(ident["avatar"], ident["label"], 48), pres))
    return '<nav class="rail" aria-label="Profiles">\n%s</nav>\n' % \
        "".join(items)


def render_conv_row(now, r, selected=None):
    """One conversation row for the sidebar, shared by the inbox and
    every transcript page.

    Discord DM-list shape: the owning profile's circular avatar (with a
    green presence badge while the session is Active), the title with a
    small relative time on the right, and one preview line — owning
    profile label, last message, optional last-tool chip. Closed rows
    carry the chip saying why — Archived, or Ended when only the tip's
    ended_at closed them. The pre-lowered search blob rides on
    data-q, the owning profile on data-profile (the rail filter), the
    section on data-state.
    """
    esc = html.escape
    ident = profile_identity(r["profile"])
    # The search blob resolves the conversation, not just the surfaced
    # row: the root's id and title ride alongside the tip's (plus every
    # intermediate member id), so searching either end of a compressed
    # chain — or a bookmarked middle segment — finds the one entry the
    # listing projects it to.
    blob = " ".join([str(r["id"]), str(r["title"]), r["profile"],
                     ident["label"], str(r["source"]),
                     r["last_line"].replace("\n", " "),
                     r["last_tool"],
                     r.get("search_extra", "")]).lower()
    title = str(r["title"])
    if r["last_line"]:
        preview_text = esc(r["last_line"])
        body_title = esc(r["last_line"])
        no_line = ""
    else:
        preview_text = "&mdash;"
        body_title = ""
        no_line = " no-line"
    tool_chip = ""
    if r["last_tool"]:
        tool_chip = ('<span class="conv-tool" title="last tool: %s">%s'
                     '</span>'
                     % (esc(r["last_tool"]), esc(r["last_tool"])))
    # Closed rows say so in words, and say which kind of closed: a
    # compact chip beside the title, closed rows only — never on any
    # other state. Archived for the archive flag, Ended for a
    # conversation the tip's ended_at closed while still unarchived.
    closed_chip = ""
    if r["state"] == "closed":
        if r["archived"]:
            closed_chip = ('<span class="conv-archived"'
                           ' title="archived conversation">'
                           'Archived</span>')
        else:
            closed_chip = ('<span class="conv-archived"'
                           ' title="ended conversation (not archived)">'
                           'Ended</span>')
    pres = ""
    if r["state"] == "active":
        pres = ('<i class="pres" role="img" aria-label="live session"'
                ' title="a session is live"></i>')
    sel = ""
    if selected is not None and \
            selected == (r["profile"], str(r["id"])):
        sel = " is-selected"
    url = "/s/%s/%s" % (quote(r["profile"], safe=""),
                        quote(str(r["id"]), safe=""))
    return (
        '<article class="conv%s" data-q="%s" data-state="%s"'
        ' data-profile="%s">'
        '<a class="conv-link" href="%s">'
        '<span class="avatar" title="profile: %s">'
        '<span aria-hidden="true">%s</span>%s%s</span>'
        '<div class="conv-main">'
        '<div class="conv-top">'
        '<h2 class="conv-title" title="%s">%s</h2>'
        '%s'
        '<span class="conv-when" data-ts="%.6f" title="%s">'
        '<span class="rel">%s</span></span>'
        '</div>'
        '<div class="conv-preview-row" title="%s">'
        '<p class="conv-preview%s"><span class="conv-prof">%s</span>'
        ' &middot; %s</p>%s</div>'
        '</div></a></article>\n'
        % (sel, esc(blob), esc(r["state"]), esc(r["profile"]),
           esc(url), esc(ident["label"]),
           esc(ident["letter"]),
           avatar_img(ident["avatar"], ident["label"], 32),
           pres,
           esc("%s \N{BULLET} %s" % (title, ident["label"])), esc(title),
           closed_chip,
           r["last"], esc(fmt_time(r["last"])),
           esc(fmt_rel(now, r["last"])),
           body_title, no_line, esc(ident["label"]), preview_text,
           tool_chip))


def render_conv_sections(now, rows, selected=None):
    """Rows -> the honest sections (Active / Open · unfinished /
    Open · completed / Closed), newest-first inside each, with count
    badges.

    Every open section renders before Closed: the buckets are filled
    from the state each row already carries before the first section
    is emitted, so a closed session (the tip's ended_at, or archived)
    never precedes an open one however newer its last activity is.
    Active and Completed always render
    (stable hooks + count badges), Incomplete and Closed only when
    they have members. Closed is a
    native <details> disclosure with no "open" attribute in the served
    HTML — first visit (and every no-JS reload) lands collapsed, like a
    collapsed Discord category; the client script restores the user's
    saved choice and temporarily expands it when a search matches
    archived rows. `selected` is the (profile, session id) pair of the
    open transcript, if any — its row gets the selected fill.
    """
    if not rows:
        return ""
    # A chat page for a closed session keeps its selected row visible:
    # the disclosure ships open (and pinned via data-keep so the client
    # script won't snap it shut) — the selected channel is never hidden.
    selected_state = None
    if selected is not None:
        for r in rows:
            if selected == (r["profile"], str(r["id"])):
                selected_state = r["state"]
                break
    convs = [render_conv_row(now, r, selected) for r in rows]
    buckets = {key: [] for key in SECTION_ORDER}
    for conv_html, r in zip(convs, rows):
        key = r["state"] if r["state"] in buckets else "incomplete"
        buckets[key].append(conv_html)
    sections = []
    for key in SECTION_ORDER:
        items = buckets[key]
        if key in ("your_turn", "incomplete", "closed") and not items:
            continue
        dot = ('<span class="sec-dot" aria-hidden="true"></span>'
               if key == "active" else "")
        if key == "closed":
            # Same stable hooks and head/count semantics as the plain
            # sections; only Closed is a disclosure, and its rows stay
            # ordinary DOM (hidden by the UA while collapsed, clickable
            # when expanded). data-keep pins the open state against the
            # client script's saved-choice restore (selected chat only).
            keep = " open data-keep=\"1\"" if selected_state == "closed" \
                else ""
            sections.append(
                '<details class="convsec" id="sec-%s" '
                'data-section="%s"%s>\n'
                '  <summary class="convsec-head">'
                '<span class="convsec-caret" aria-hidden="true">'
                '</span><h2 class="convsec-title">%s</h2>'
                '<span class="sec-count" data-count="%d">%d'
                '</span></summary>\n%s</details>\n'
                % (key, key, keep, SECTION_TITLES[key],
                   len(items), len(items), "".join(items)))
        else:
            sections.append(
                '<section class="convsec" id="sec-%s" data-section="%s">\n'
                '  <h2 class="convsec-head">%s<span class="convsec-title">'
                '%s</span><span class="sec-count" data-count="%d">%d'
                '</span></h2>\n%s</section>\n'
                % (key, key, dot, SECTION_TITLES[key],
                   len(items), len(items), "".join(items)))
    return "".join(sections)


def render_sidebar(now, rows, notes, selected=None, active_profile="",
                   refresh_seconds=None, user_status=""):
    """The rail + conversation sidebar block shared by every page.

    The sidebar carries the server-style header, the search (with the
    "/" shortcut hint), the mobile profile-filter chips, New chat, the
    skip notes, the honest sections, and the bottom user panel (the
    user's letter badge plus, on the inbox, the live/stale sync state
    and as-of time). refresh_seconds set marks the inbox variant: it
    adds the
    hidden #total hook, the sync hook ids and the no-JS reload note the
    auto-refresh script swaps in place. Chat pages get a static count
    and `user_status` in the panel instead.
    """
    esc = html.escape
    if notes:
        note_items = "".join(
            "<li><strong>Skipped</strong>%s</li>\n" % esc(" " + n)
            for n in notes)
        notes_block = '<ul class="notes" id="notes">%s</ul>' % note_items
    else:
        notes_block = '<ul class="notes" id="notes" hidden></ul>'

    if rows:
        no_rows_hidden = " hidden"
        no_rows_msg = ""
    elif notes:
        no_rows_hidden = ""
        no_rows_msg = ("No conversations in the last 24 hours from the "
                       "databases that were readable.")
    else:
        no_rows_hidden = ""
        no_rows_msg = "No conversations in the last 24 hours."

    sections = render_conv_sections(now, rows, selected)
    presence = set(r["profile"] for r in rows if r["state"] == "active")
    rail = render_rail(active_profile, presence)

    # The rail collapsed into chips (mobile): same filters, same
    # data-profile-filter hooks, same selected state.
    chips = ['<a class="chip-filter%s" href="/" data-profile-filter="">All'
             '</a>' % (" is-selected" if active_profile == "" else "")]
    for key in rail_profiles():
        ident = profile_identity(key)
        sel = " is-selected" if active_profile == key else ""
        chips.append(
            '<a class="chip-filter%s" href="/?profile=%s"'
            ' data-profile-filter="%s">%s</a>'
            % (sel, esc(key), esc(key), esc(ident["label"])))

    if refresh_seconds is not None:
        hooks = ('<span id="total" data-total="%d" data-now="%.6f" hidden>'
                 '</span>\n' % (len(rows), now))
        noscript = ("<noscript><span class=\"noscript-note\">JavaScript is "
                    "off: the filter is inactive and the page reloads "
                    "every %d&thinsp;s.</span></noscript>"
                    % refresh_seconds)
        as_of = datetime.fromtimestamp(now).strftime("%H:%M:%S")
        status = (
            '<span class="sync" id="sync"><span class="dot"'
            ' aria-hidden="true"></span><span class="label"'
            ' id="sync-label">live</span></span>'
            '<span class="as-of">&middot; as of <time id="as-of">%s'
            '</time></span>' % esc(as_of))
    else:
        hooks = ""
        noscript = ("<noscript><span class=\"noscript-note\">JavaScript is "
                    "off: the filter is inactive.</span></noscript>")
        status = '<span class="as-of">%s</span>' % esc(user_status)

    return (
        "%s"
        '<nav class="sidebar" aria-label="Conversations">\n'
        '  <header class="sb-head">'
        '<h1 class="sb-title">Mission Control</h1></header>\n'
        '  <div class="sb-tools">\n'
        '    <div class="search">'
        '<input type="search" id="filter"'
        ' placeholder="Search conversations&hellip;"'
        ' aria-label="Search conversations" autocomplete="off"'
        ' spellcheck="false"><kbd aria-hidden="true">/</kbd></div>\n'
        '    <div class="rail-chips" role="group"'
        ' aria-label="Profile filter">%s</div>\n'
        '    <div class="sb-meta">'
        '<a class="new-chat" href="/new"'
        ' title="Start a new Hermes session">+ New chat</a>'
        '<span class="shown" id="shown">%d shown</span>%s</div>\n'
        '  </div>\n'
        '  <div class="sb-scroll">\n'
        '%s%s'
        '    <div class="state" id="no-rows"%s>\n'
        '      <p style="margin:0">%s</p>\n'
        '    </div>\n'
        '    <div class="convs" id="rows">\n'
        '%s'
        '      <div class="state state-slim" id="no-match" hidden>'
        'No conversations match <span id="no-match-q"></span></div>\n'
        '    </div>\n'
        '  </div>\n'
        '  <footer class="sb-user">'
        '<span class="avatar">'
        '<span aria-hidden="true">U</span>%s</span>'
        '<span class="sb-user-meta">'
        '<span class="sb-user-name">You</span>'
        '<span class="sb-user-status">%s</span>'
        '</span></footer>\n'
        '</nav>\n'
        % (rail, "".join(chips), len(rows), noscript,
           hooks, notes_block, no_rows_hidden, esc(no_rows_msg),
           sections, avatar_img(user_avatar_url(), "You", 32), status))


def render(now, rows, notes, active_profile=""):
    """The inbox page: the Discord-style shell with the conversation
    sidebar on the left and a select-a-chat splash in the main panel.

    Rows/sections/notes are built by the shared sidebar helpers (the
    transcript pages render the same sidebar); active_profile is the
    rail filter from ?profile= ("" = All).
    """
    esc = html.escape
    generated = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")

    return PAGE_SHELL.substitute(
        shell_css=SHELL_CSS,
        sidebar_js=SIDEBAR_JS,
        csrf_meta=csrf_meta_tag(),
        refresh_seconds=REFRESH_SECONDS,
        sidebar=render_sidebar(now, rows, notes,
                               active_profile=active_profile,
                               refresh_seconds=REFRESH_SECONDS),
        generated=esc(generated),
    )


def render_tool_group(tools):
    """One run of consecutive tool rows -> one compact expandable strip.

    The collapsed summary counts the run ("3 tool calls") next to
    tool-name chips (a name per distinct tool, "name xN" when repeated);
    opening it lists the tools chronologically, each with its optional
    400-char detail disclosure. Styled flat and slim on purpose — a
    bookkeeping row, never a chat bubble. The <li> carries the run's
    oldest row id as data-first-id: the client-side seam merge matches
    it to replace (not append) when a later poll re-delivers the same
    maximal group grown at its new end.
    """
    esc = html.escape
    n = len(tools)
    label = "%d tool call%s" % (n, "" if n == 1 else "s")

    counts = {}
    for t in tools:  # insertion order = chronological order
        counts[t["tool"]] = counts.get(t["tool"], 0) + 1
    chips = []
    for name, c in counts.items():
        shown = name if c == 1 else "%s ×%d" % (name, c)
        chips.append('<span class="chip" title="%s">%s</span>'
                     % (esc(shown), esc(shown)))
    if len(chips) > TOOL_CHIP_MAX:
        chips = (chips[:TOOL_CHIP_MAX]
                 + ['<span class="chip chip-more">+%d more</span>'
                    % (len(counts) - TOOL_CHIP_MAX)])

    first_s, last_s = fmt_short(tools[0]["ts"]), fmt_short(tools[-1]["ts"])
    span = first_s if first_s == last_s else \
        first_s + " \N{EN DASH} " + last_s
    span_full = fmt_time(tools[0]["ts"]) + " \N{EN DASH} " + \
        fmt_time(tools[-1]["ts"])

    rows = []
    for t in tools:
        detail = ""
        if t["detail"]:
            detail = ('<details class="tool-detail"><summary>details'
                      '</summary><pre>%s</pre></details>' % esc(t["detail"]))
        rows.append(
            '<li class="tg-item"><span class="chip">%s</span>'
            '<span class="tg-time" title="%s">%s</span>%s</li>\n'
            % (esc(t["tool"]), esc(fmt_time(t["ts"])),
               esc(fmt_short(t["ts"])), detail))

    # data-ts anchors the group at its OLDEST row's time — the position
    # the run occupies in the flat timeline — so a late-discovered
    # dispatch card can be placed against it during feed reconciliation;
    # a card whose time falls inside the span is not split client-side
    # (the next full render shows the true split).
    return (
        '<li class="tool-group" data-first-id="%d" data-ts="%.3f">'
        '<details class="tg">'
        '<summary class="tg-sum">'
        '<span class="tg-count">%s</span>'
        '<span class="tg-chips">%s</span>'
        '<span class="tg-when" title="%s">%s</span>'
        '</summary><ol class="tg-list">%s</ol>'
        '</details></li>\n'
        % (tools[0]["id"], tools[0]["ts"], esc(label), "".join(chips),
           esc(span_full), esc(span), "".join(rows)))


def render_chat_text(it, cont="", identity=None):
    """One text message -> its <li> HTML in Discord's flat group shape.

    A group starts with the 40px letter badge, the author name and a
    timestamp; a same-sender follow-up (`cont`) repeats none of them —
    just a gutter timestamp that appears on row hover. Rendered here
    once so the transcript page and the feed append the exact same
    markup (feed rows arrive solo, so they take the default). `identity`
    is the owning profile's resolved identity dict (or a profile name to
    resolve); agent messages carry that profile's badge and name, user
    messages always carry You.
    """
    esc = html.escape
    if identity is None:
        identity = profile_identity("default")
    elif isinstance(identity, str):
        identity = profile_identity(identity)
    side = "from-user" if it["role"] == "user" else "from-agent"
    if side == "from-user":
        av_letter = "U"
        av_title = "user message"
        author = USER_LABEL
        av_img = avatar_img(user_avatar_url(), USER_LABEL, 40)
    else:
        av_letter = identity["letter"]
        av_title = "%s (assistant)" % identity["label"]
        author = identity["label"]
        av_img = avatar_img(identity["avatar"], identity["label"], 40)
    if cont:
        # Continuation: no avatar, no author — a small timestamp in the
        # gutter, revealed while the row is hovered (CSS).
        return (
            '<li class="msg %s%s" data-ts="%.3f">'
            '<span class="msg-gutter">'
            '<span class="mtime" title="%s">%s</span></span>'
            '<div class="msg-body"><p class="text">%s</p></div></li>\n'
            % (side, cont, it["ts"], esc(fmt_time(it["ts"])),
               esc(fmt_hhmm(it["ts"])), esc(it["text"])))
    # Letter badge with the optional avatar image layered on top; when
    # the file is missing the img never renders, and when it fails
    # mid-load the error listener hides it — the letter always shows.
    # data-ts is the row's message time on both shapes: the client-side
    # dispatch-card reconciliation places a late-discovered card against
    # it (rows without data-ts — the optimistic bubble, the typing and
    # waiting tails — still count as newest).
    return (
        '<li class="msg %s" data-ts="%.3f">'
        '<span class="avatar" title="%s">'
        '<span aria-hidden="true">%s</span>%s</span>'
        '<div class="msg-body">'
        '<div class="msg-head"><span class="msg-author">%s</span>'
        '<span class="mtime" title="%s">%s</span></div>'
        '<p class="text">%s</p></div></li>\n'
        % (side, it["ts"], esc(av_title),
           esc(av_letter), av_img, esc(author), esc(fmt_time(it["ts"])),
           esc(fmt_short(it["ts"])), esc(it["text"])))


def render_chat_item(it, identity=None):
    """One display item (text bubble or tool group) -> its <li> HTML —
    the single shape both the transcript page and the /feed entries
    carry, so appended rows can never drift from server-rendered ones.
    `identity` (profile name or resolved identity dict) selects the
    agent badge; tool groups ignore it."""
    if it["kind"] == "tools":
        return render_tool_group(it["items"])
    return render_chat_text(it, identity=identity)


def render_child_item(now, parent_profile, c):
    """One child -> its inline timeline <li>: a slim Discord-style
    status card that sits in the transcript at the child's dispatch
    time, not a chat bubble and not part of any tool group.

    The card is one clickable link carrying the status dot, a "Sub-agent"
    tag, the goal label clamped hard, and the child's own profile
    identity, end state and relative last-activity time; it opens the
    child's own transcript (/s/<child profile>/<child id>). data-ts is
    the stored dispatch time (merge_children sorted on it), data-child
    the reconciliation key the feed polls diff on, and data-rev the
    cheap change detector. Every child-controlled string — label, ids,
    end reason — is html.escape()d here, the one place the row is
    built; malformed child dicts degrade to their id and never raise."""
    esc = html.escape
    state = subagent_state(c.get("ended"), c.get("end_reason"))
    sid = str(c.get("id") or "")
    label = clamp_label(str(c.get("label") or sid or "sub-agent"))
    cprofile = str(c.get("profile") or parent_profile or "default")
    ident = profile_identity(cprofile)
    url = "/s/%s/%s" % (quote(cprofile, safe=""), quote(sid, safe=""))
    key = child_dom_key(cprofile, sid)
    tip = sid
    reason = str(c.get("end_reason") or "").strip()
    if reason:
        tip = "%s \N{BULLET} ended: %s" % (
            tip, reason[:SUBAGENT_LABEL_CHARS])
    meta = "%s \N{BULLET} %s" % (ident["label"],
                                 SUBAGENT_STATE_LABELS[state])
    last = c.get("last")
    if last:
        try:
            meta = "%s \N{BULLET} %s" % (meta, fmt_rel(now, float(last)))
        except (TypeError, ValueError):
            pass
    try:
        ts = float(c.get("started") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    return (
        '<li class="msg subagent-item" id="sa-%s" data-child="%s"'
        ' data-ts="%.3f" data-state="%s" data-rev="%s">'
        '<a class="sa-card" href="%s" title="%s">'
        '<span class="sa-dot sa-%s" aria-hidden="true"></span>'
        '<span class="sa-body">'
        '<span class="sa-tag">Sub-agent</span>'
        '<span class="sa-label">%s</span>'
        '<span class="sa-meta">%s</span>'
        '</span><span class="sa-go" aria-hidden="true">Open</span>'
        '</a></li>\n'
        % (esc(key), esc(key), ts, esc(state),
           esc(child_revision(c, state)),
           esc(url), esc(tip), state, esc(label), esc(meta)))


def child_feed_payload(now, parent_profile, children, cards=None):
    """The per-child object every /feed poll carries:
    {count, ids, items} where each item is {key, id, profile, started,
    state, rev, html} — key (the row's data-child) is the
    reconciliation key, started places a brand-new child at its
    chronological position, state and rev let an existing row update in
    place exactly once per real change, and html is the exact
    server-rendered <li> the page itself uses. Children ride every poll
    as a full list (never a delta over the message cursor), so a client
    reconciles by key whether or not the parent's tool-call carrier has
    landed — and a malformed child row is skipped, never fatal.

    Dispatch cards (the `cards` argument) append to the same list in
    the same shape: one keyed identity space covers both inline child
    rows and delegate dispatch cards, so the client's single
    reconciliation path — and the Next sub-agent stepper — handles both
    without knowing the difference."""
    items = []
    for c in children or []:
        if not isinstance(c, dict):
            continue
        try:
            sid = str(c.get("id") or "")
            if not sid:
                continue
            cprofile = str(c.get("profile") or parent_profile or "default")
            started = float(c.get("started") or 0)
            state = subagent_state(c.get("ended"), c.get("end_reason"))
        except (TypeError, ValueError):
            continue
        items.append({
            "key": child_dom_key(cprofile, sid),
            "id": sid,
            "profile": cprofile,
            "started": started,
            "state": state,
            "rev": child_revision(c, state),
            "html": render_child_item(now, parent_profile, c),
        })
    items.extend(delegate_feed_items(now, parent_profile, cards))
    return {"count": len(items), "ids": [i["id"] for i in items],
            "items": items}


def render_delegate_card(now, parent_profile, card):
    """One dispatch card -> its inline timeline <li>: a Discord-style
    embed at the delegate call carrier's position, where the generic
    delegate_* tool row used to be — a status dot, a "Sub-agent
    dispatch" tag with the exact tool name as a mono chip, the clamped
    goal label, and a meta line (owning profile, agent kind, model when
    known, state, relative activity).

    A delegate_claude_agent card with a validated spawn receipt is the
    whole-card anchor to that exact run's live Claude viewer page
    (target=_blank — the viewer is another surface, not a transcript
    route), and the viewer link takes PRECEDENCE over any child link:
    it is the one route that shows the run itself, it exists even while
    the tool result row is still absent, and it stays equally valid
    after the run completes. Otherwise, when the dispatch matched a
    real child session, the card is a link into that child's own
    transcript (the URL is built from the matched child's validated
    profile/id only — never from payload text); with no safe route at
    all the card renders the same truthful embed as a plain span, with
    no href and no Open affordance. data-child is the reconciliation
    key the feed polls diff on (shared with inline child rows, so Next
    sub-agent steps through both by one identity space), data-ts the
    carrier time merge_delegate_cards sorted on, data-rev the cheap
    change detector — the page and every /feed poll build this exact
    markup through this one function, so a static card upgrades in
    place the moment its receipt appears and never duplicates. Every
    payload-controlled string is html.escape()d here, the one place the
    row is built; malformed cards degrade to a generic label and never
    raise."""
    esc = html.escape
    state = str(card.get("state") or "running")
    if state not in SUBAGENT_STATE_LABELS:
        state = "running"
    key = str(card.get("key") or delegate_dom_key("call", 0))
    tool = str(card.get("tool") or "delegate_agent")
    tool_label = DELEGATE_TOOL_LABELS.get(tool, "Sub-agent")
    label = clamp_label(str(card.get("label") or "")) \
        or "Sub-agent dispatch"
    cprofile = str(card.get("profile") or parent_profile or "default")
    ident = profile_identity(cprofile)
    meta = "%s \N{BULLET} %s" % (ident["label"], tool_label)
    model = str(card.get("model") or "").strip()
    if model:
        meta = "%s \N{BULLET} %s" % (meta, model[:SUBAGENT_LABEL_CHARS])
    meta = "%s \N{BULLET} %s" % (meta, SUBAGENT_STATE_LABELS[state])
    child = card.get("child")
    last = child.get("last") if isinstance(child, dict) else None
    if not last and isinstance(card.get("delegation"), dict):
        d = card["delegation"]
        last = d.get("completed") or d.get("dispatched")
    if not last:
        last = card.get("result_ts")
    if last:
        try:
            meta = "%s \N{BULLET} %s" % (meta, fmt_rel(now, float(last)))
        except (TypeError, ValueError):
            pass
    try:
        ts = float(card.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    # The viewer URL only ever arrives here through the data layer's
    # validated receipt lookup (claude_card_watch_url); the scheme
    # re-check is belt-and-braces so a future caller cannot turn a
    # relative or javascript: string into a link.
    watch = str(card.get("watch_url") or "")
    if not watch.lower().startswith(("http://", "https://")):
        watch = ""
    tip = tool
    if isinstance(child, dict) and child.get("id"):
        tip = "%s \N{BULLET} %s" % (tool, str(child.get("id")))
    rev = delegate_revision(card, state)
    head = (
        '<li class="msg delegate-item" id="sa-%s" data-child="%s"'
        ' data-ts="%.3f" data-state="%s" data-rev="%s">'
        % (esc(key), esc(key), ts, esc(state), esc(rev)))
    body = (
        '<span class="sa-dot sa-%s" aria-hidden="true"></span>'
        '<span class="sa-body">'
        '<span class="sa-tag">Sub-agent dispatch'
        ' <span class="dlg-tool">%s</span></span>'
        '<span class="sa-label">%s</span>'
        '<span class="sa-meta">%s</span>'
        '</span>' % (state, esc(tool), esc(label), esc(meta)))
    if watch:
        return ('%s<a class="sa-card dlg-card" href="%s"'
                ' target="_blank" rel="noopener"'
                ' title="Watch this run in the Claude live viewer">'
                '%s<span class="sa-go" aria-hidden="true">Watch</span>'
                '</a></li>\n'
                % (head, esc(watch), body))
    if isinstance(child, dict) and child.get("id"):
        url = "/s/%s/%s" % (quote(cprofile, safe=""),
                            quote(str(child.get("id")), safe=""))
        return ('%s<a class="sa-card dlg-card" href="%s" title="%s">'
                '%s<span class="sa-go" aria-hidden="true">Open</span>'
                '</a></li>\n'
                % (head, esc(url), esc(tip), body))
    return ('%s<span class="sa-card dlg-card dlg-static" title="%s">'
            '%s</span></li>\n' % (head, esc(tip), body))


def delegate_feed_items(now, parent_profile, cards):
    """The dispatch-card half of the keyed /feed payload: one item per
    card, same {key, id, profile, started, state, rev, html} shape the
    child items carry, so the client reconciles both kinds through the
    one keyed path — insert at the carrier's chronological spot, update
    a state change in place by rev, never duplicate. started is the
    carrier time (the chronological authority), id the linked child's
    session id or the call id otherwise. A malformed card is skipped,
    never fatal."""
    items = []
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        try:
            key = str(card.get("key") or "")
            if not key:
                continue
            started = float(card.get("ts") or 0)
            state = str(card.get("state") or "running")
        except (TypeError, ValueError):
            continue
        child = card.get("child")
        items.append({
            "key": key,
            "id": (str(child.get("id"))
                   if isinstance(child, dict) and child.get("id")
                   else str(card.get("call_id") or key)),
            "profile": str(card.get("profile")
                           or parent_profile or "default"),
            "started": started,
            "state": state,
            "rev": delegate_revision(card, state),
            "html": render_delegate_card(now, parent_profile, card),
        })
    return items


def next_subagent_pill(dom_key):
    """The pill anchored at an already-computed row key — used when the
    timeline's first keyed row is a dispatch card rather than a
    standalone child. The key is server-folded to the safe id charset
    before it ever reaches an attribute."""
    safe = CHILD_KEY_UNSAFE_RE.sub("_", str(dom_key or ""))
    return (
        '<a class="next-subagent" id="next-subagent" href="#sa-%s">'
        '<span aria-hidden="true">&darr;</span> Next sub-agent\n'
        '  </a>\n'
        % html.escape(safe))


def render_activity(act):
    """The Live activity strip for a conversation page ("" when there is
    nothing truthful to show).

    One row per unresolved tool call — tool name, its state label, and
    the bounded redacted argument summary — or, when the turn is live
    but nothing is unresolved, the single weakest truthful state. The
    feed ships this exact markup as activity.html, so a poll-swap can
    never drift from the server-rendered page. Completed tool history
    is not duplicated here: it stays in its collapsed tool group.
    """
    esc = html.escape
    if not act:
        return ""
    rows = []
    for p in act.get("pending") or []:
        args = ""
        if p.get("args"):
            args = ('<span class="la-args" title="%s">%s</span>'
                    % (esc(p["args"]), esc(p["args"])))
        rows.append(
            '<li class="la-row">'
            '<span class="chip la-tool">%s</span>'
            '<span class="la-state">%s</span>%s</li>\n'
            % (esc(p["name"]), esc(p["state"]), args))
    if not rows:
        state = act.get("state") or ""
        if not state:
            return ""
        rows.append('<li class="la-row la-only">'
                    '<span class="la-state">%s</span></li>\n'
                    % esc(state))
    return (
        '<div class="live-activity" id="live-activity" role="status"'
        ' aria-live="polite" data-active="%s">\n'
        '  <h2 class="la-head"><span class="la-dot" aria-hidden="true">'
        '</span>Live activity</h2>\n'
        '  <ul class="la-list">\n%s  </ul>\n'
        '</div>\n'
        % ("true" if act.get("active") else "false", "".join(rows)))


def render_chat(chat, inbox_rows=None, inbox_notes=None):
    """The /s/<profile>/<session_id> page: a chat-style transcript.

    Discord flat message groups in the shell's main panel: 40px avatar,
    friendly author name and timestamp on group starts, same-sender
    follow-ups with neither; tool runs as compact left-accented embeds
    (render_tool_group). The empty state shows only when the final
    renderable list is empty. Every displayable row renders — there is
    no newest-N window. The sidebar (inbox_rows/inbox_notes, best-effort
    loaded by the handler) is the same conversation list the inbox
    shows, with this session selected.
    """
    esc = html.escape
    now = time.time()
    # Children merge into the flat chronological list first, then each
    # delegate dispatch card at its call carrier's own position, so tool
    # grouping runs AROUND both: either one dispatched between two tool
    # rows splits what would be one collapsed group. The generic
    # delegate result rows a card replaces are already gone (the
    # suppress set came out of the same delegate pass that matched the
    # cards), and a child a card absorbed is not in `children` — it
    # renders AS the card, never as a second standalone row.
    children = chat.get("subagents") or []
    cards = chat.get("delegate_cards") or []
    items = chat_items(merge_delegate_cards(
        merge_children(
            chat_messages(chat["rows"],
                          suppress=chat.get("delegate_suppress") or ()),
            children),
        cards))
    # The owning profile's identity drives the header pill label, every
    # agent bubble's badge and the typing row — user bubbles stay You.
    ident = profile_identity(chat["profile"])

    parts = []
    prev_side = None
    first_keyed = None
    for it in items:
        if it["kind"] == "text":
            side = "from-user" if it["role"] == "user" else "from-agent"
            # a run of messages from one sender keeps its own rhythm
            cont = " cont" if side == prev_side else ""
            prev_side = side
            parts.append(render_chat_text(it, cont, ident))
        elif it["kind"] == "child":
            # an inline child is neither sender: it breaks a run, so the
            # next bubble shows its full header again
            prev_side = None
            if first_keyed is None:
                first_keyed = child_dom_key(
                    str(it["c"].get("profile") or chat["profile"]),
                    str(it["c"].get("id") or ""))
            parts.append(render_child_item(now, chat["profile"], it["c"]))
        elif it["kind"] == "delegate":
            # a dispatch card breaks a sender run the same way
            prev_side = None
            if first_keyed is None:
                first_keyed = str(it["card"].get("key") or "")
            parts.append(
                render_delegate_card(now, chat["profile"], it["card"]))
        else:
            prev_side = None
            parts.append(render_tool_group(it["items"]))

    when = fmt_short(chat["started"]) + " \N{EN DASH} " + \
        fmt_short(chat["last"])
    when_full = fmt_time(chat["started"]) + " \N{EN DASH} " + \
        fmt_time(chat["last"])

    # Close/Reopen chrome: one compact header button (its data-action
    # and label mirror the current state; the client flips them via
    # applySessionState), and the closed banner, rendered always but
    # hidden while the session is open so a poll can unhide it in
    # place. A closed session's composer renders disabled with the
    # closed placeholder.
    archived = bool(chat.get("archived"))
    session_toggle = (
        '<button class="session-toggle" id="session-toggle" type="button" '
        'data-action="%s" aria-pressed="%s" title="%s">%s</button>\n'
        % ("reopen" if archived else "close",
           "true" if archived else "false",
           esc("Reopen this session (unarchives the Discord thread)"
               if archived else
               "Close this session (archives the Discord thread)"),
           "Reopen" if archived else "Close"))
    closed_banner = (
        '<div class="closed-banner" id="closed-banner" role="status"%s>'
        'Session closed</div>\n' % ("" if archived else " hidden"))
    # The pending clarify card (already-escaped HTML, "" when none):
    # while one is active the composer renders disabled with the
    # clarify placeholder — the question above is the only way to answer.
    clarify_html = chat.get("clarify_card") or ""
    card_active = bool(clarify_html)
    if archived:
        placeholder = "This session is closed."
    elif card_active:
        placeholder = "Answer the question above"
    else:
        placeholder = "Message %s\N{HORIZONTAL ELLIPSIS}" % ident["label"]

    return CHAT_SHELL.substitute(
        mode="chat",
        shell_css=SHELL_CSS,
        sidebar_js=SIDEBAR_JS,
        csrf_meta=csrf_meta_tag(),
        # The same rail + conversation sidebar the inbox renders, with
        # this session's row selected and its profile lit in the rail.
        sidebar=render_sidebar(
            now, inbox_rows or [], inbox_notes or [],
            selected=(chat["profile"], str(chat["id"])),
            active_profile=(chat["profile"]
                            if chat["profile"] in rail_profiles()
                            else None),
            user_status="Viewing %s" % ident["label"]),
        title=esc(str(chat["title"])),
        full_title=esc(str(chat["title"])),
        # The header topic shows the friendly label (never the raw
        # "default"); the raw profile stays on data-profile.
        profile=esc(ident["label"]),
        profile_attr=esc(chat["profile"]),
        session_attr=esc(str(chat["id"])),
        when=esc(when),
        when_title=esc(when_full),
        session_toggle=session_toggle,
        closed_banner=closed_banner,
        archived_state="1" if archived else "0",
        composer_disabled=" disabled" if (archived or card_active) else "",
        # The floating Next sub-agent control exists only on a page
        # that already has inline child rows or dispatch cards (a page
        # with neither must show nothing); with JavaScript off it is a
        # plain anchor to the first keyed timeline row (child or card,
        # whichever the transcript reaches first), and the client
        # builds it on demand the moment polling delivers one.
        next_subagent=(
            next_subagent_pill(first_keyed) if first_keyed else ""),
        # The live strip is part of the initial render (the same
        # snapshot the feed recomputes); "" when the turn has nothing
        # truthful to show.
        live_activity=render_activity(chat.get("activity")),
        # Rendered only when nothing survived filtering: a session with
        # any message or tool group never even carries the string.
        empty_state="" if items else (
            '<div class="state" id="empty-state">\n'
            '    <h2 class="empty-title">Nothing here yet</h2>\n'
            '    <p style="margin:0">No displayable messages in this '
            'session.</p>\n  </div>\n'),
        rows="".join(parts),
        poll_ms=FEED_POLL_MS,
        last_id=chat["last_id"],
        # The pending clarify card above the composer, escaped HTML
        # straight from the core card ("" when none is pending).
        clarify_card=clarify_html,
        clarify_css=CLARIFY_CARD_CSS,
        avatar_user_attr=esc(user_avatar_url()),
        live_css=LIVE_CSS,
        typing_css=TYPING_CSS,
        waiting_css=WAITING_CSS,
        typing_row=TYPING_ROW % (esc(ident["label"]),
                                 esc(ident["letter"]),
                                 avatar_img(ident["avatar"],
                                            ident["label"], 40),
                                 esc(ident["label"]),
                                 esc(ident["label"])),
        waiting_row=WAITING_ROW % (esc(ident["label"]),
                                   esc(ident["letter"]),
                                   avatar_img(ident["avatar"],
                                              ident["label"], 40),
                                   esc(ident["label"])),
        typing_selector='"#typing-row"',
        composer_placeholder=esc(placeholder),
    )


def render_new(inbox_rows=None, inbox_notes=None):
    """GET /new: the chat chrome as a blank composer (mode "new").

    No session exists yet, so there is nothing to poll and nothing to
    render — the inline script sends the first message to POST /s/new,
    gets its fast 202, shows the waiting row while it polls the launch
    job's status, and navigates to /s/default/<id> the moment the
    correlated session id is published (without waiting for the run
    to finish); a terminal job failure fails the optimistic message and
    restores the composer. The typing row and the live activity strip
    are deliberately absent (markup, CSS and selector alike): a blank
    chat must never look like the assistant is typing or a tool is
    running.
    The waiting row is the one tail element /new does carry — it marks
    a genuinely accepted send, never phantom work. The subagent section
    is absent too — no session means no children. Nor does /new carry
    the Close/Reopen toggle or the closed banner: there is no session
    to close."""
    esc = html.escape
    # /new is always the default profile: the rail shows its filter
    # selected, data-profile keeps the raw name.
    new_ident = profile_identity("default")
    return CHAT_SHELL.substitute(
        mode="new",
        shell_css=SHELL_CSS,
        sidebar_js=SIDEBAR_JS,
        csrf_meta=csrf_meta_tag(),
        sidebar=render_sidebar(time.time(), inbox_rows or [],
                               inbox_notes or [],
                               active_profile="default",
                               user_status="New chat"),
        title="New chat",
        full_title="Start a new Hermes session",
        profile=esc(new_ident["label"]),
        profile_attr="default",
        session_attr="",
        when="not started yet",
        when_title="The session begins with your first message",
        session_toggle="",
        closed_banner="",
        archived_state="0",
        composer_disabled="",
        next_subagent="",
        # No session yet: no live strip either (and no typing row) — a
        # blank chat must never look like something is running.
        live_activity="",
        empty_state=('<div class="state" id="empty-state">\n'
                     '    <h2 class="empty-title">New chat</h2>\n'
                     '    <p style="margin:0">This is the beginning of a '
                     'new Hermes session — send a message below to start '
                     'it.</p>\n  </div>\n'),
        rows="",
        poll_ms=FEED_POLL_MS,
        last_id=0,
        # Nor a clarify card: no session means nothing can be asking.
        clarify_card="",
        clarify_css="",
        avatar_user_attr=esc(user_avatar_url()),
        live_css="",
        typing_css="",
        # The waiting row rides /new too: after the launch is accepted
        # (202) it holds the tail while the client polls the job, until
        # navigation (or a terminal failure) ends the page.
        waiting_css=WAITING_CSS,
        typing_row="",
        waiting_row=WAITING_ROW % (esc(new_ident["label"]),
                                   esc(new_ident["letter"]),
                                   avatar_img(new_ident["avatar"],
                                              new_ident["label"], 40),
                                   esc(new_ident["label"])),
        typing_selector='""',
        composer_placeholder=esc(
            "Say something to start a new session\N{HORIZONTAL ELLIPSIS}"),
    )


def error_page(exc):
    """Themed 500 page; same tokens, minimal chrome."""
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n<meta name=\"color-scheme\" content=\"dark\">\n"
        "<link rel=\"icon\" href=\"data:,\">\n"
        "<title>Mission Control &mdash; error</title>\n"
        "<style>\n"
        "body { margin: 0; background: #313338; color: #dbdee1; font-family: system-ui,\n"
        "  ui-sans-serif, -apple-system, \"Segoe UI\", sans-serif; font-size: 14px; }\n"
        ".panel { max-width: 560px; margin: 18vh auto 0; padding: 22px 26px;\n"
        "  background: #2b2d31; border-left: 4px solid #f0b232; border-radius: 4px; }\n"
        "h1 { margin: 0 0 6px; font-size: 16px; color: #f2f3f5; }\n"
        "p { margin: 0; color: #949ba4; font-size: 13px; }\n"
        "</style>\n</head>\n<body>\n"
        "<div class=\"panel\">\n<h1>Failed to load sessions</h1>\n"
        "<p>%s</p>\n</div>\n</body>\n</html>\n"
        % html.escape("%s: %s" % (type(exc).__name__, exc)))


def not_found_page(detail):
    """Themed 404 page; the error page's dark chrome plus a way back."""
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n<meta name=\"color-scheme\" content=\"dark\">\n"
        "<link rel=\"icon\" href=\"data:,\">\n"
        "<title>Mission Control &mdash; not found</title>\n"
        "<style>\n"
        "body { margin: 0; background: #313338; color: #dbdee1; font-family: system-ui,\n"
        "  ui-sans-serif, -apple-system, \"Segoe UI\", sans-serif; font-size: 14px; }\n"
        ".panel { max-width: 560px; margin: 18vh auto 0; padding: 22px 26px;\n"
        "  background: #2b2d31; border-left: 4px solid #5865f2; border-radius: 4px; }\n"
        "h1 { margin: 0 0 6px; font-size: 16px; color: #f2f3f5; }\n"
        "p { margin: 0 0 14px; color: #949ba4; font-size: 13px; }\n"
        "a.back { display: inline-block; height: 30px; padding: 0 12px;\n"
        "  line-height: 28px; border-radius: 4px; text-decoration: none;\n"
        "  color: #fff; font-size: 13px; font-weight: 600;\n"
        "  background: #5865f2; }\n"
        "a.back:hover, a.back:focus-visible { outline: none;\n"
        "  background: #4752c4; }\n"
        "</style>\n</head>\n<body>\n"
        "<div class=\"panel\">\n<h1>Not found</h1>\n"
        "<p>%s</p>\n"
        "<a class=\"back\" href=\"/\">&larr; All sessions</a>\n"
        "</div>\n</body>\n</html>\n"
        % html.escape(detail))


class Handler(BaseHTTPRequestHandler):
    server_version = "HermesSessions/1.0"

    def _send_page(self, status, body):
        """Send one fully-buffered HTML response (never a partial page)."""
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self, detail):
        self._send_page(404, not_found_page(detail).encode("utf-8"))

    def _send_json(self, status, obj):
        """Send one buffered JSON response (the feed and composer APIs)."""
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_avatar(self, path):
        """Serve one fixed avatar PNG (never any other file).

        The path arrives already resolved by the avatar URL helpers —
        a fixed filename inside a trusted home, re-checked here (size
        cap included) so a file that changed on disk since its URL was
        rendered can neither grow the response without bound nor
        escape: anything but a serveable PNG is the themed 404, and
        the page's letter badge shows instead.
        """
        try:
            with open(path, "rb") as fh:
                body = fh.read(AVATAR_MAX_BYTES + 1)
        except OSError:
            self._not_found("There is no avatar at this address.")
            return
        if len(body) > AVATAR_MAX_BYTES:
            self._not_found("There is no avatar at this address.")
            return
        self.send_response(200)
        self.send_header("Content-Type", AVATAR_CONTENT_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", AVATAR_CACHE_CONTROL)
        self.end_headers()
        self.wfile.write(body)

    def _read_body_text(self):
        """Read one small composer body -> (error_status, text).

        error_status is None when the body parsed, else the HTTP status
        to answer with (411/413/400). The body must be application/json
        (enforced earlier by the CSRF gate; re-checked here so direct
        callers inherit the same rule) holding an object with a string
        "text". MAX_BODY_BYTES caps the read before it starts.
        """
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            return 411, ""
        try:
            length = int(raw_len)
        except ValueError:
            return 400, ""
        if length <= 0:
            return 400, ""
        if length > MAX_BODY_BYTES:
            return 413, ""
        try:
            raw = self.rfile.read(length)
        except OSError:
            return 400, ""
        if len(raw) != length:
            return 400, ""
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0]
        if ctype.strip().lower() != "application/json":
            return 415, ""
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return 400, ""
        if not isinstance(obj, dict) \
                or not isinstance(obj.get("text"), str):
            return 400, ""
        return None, obj["text"]

    def _composer_text(self):
        """(error_status, message_text) for a composer POST: the body
        read, then stripped and length-checked. 400 covers empty,
        413 oversize."""
        err, text = self._read_body_text()
        if err is not None:
            return err, ""
        text = text.strip()
        if not text:
            return 400, ""
        if len(text) > MAX_TEXT_CHARS:
            return 413, ""
        return None, text

    def _host_allowed(self):
        """True when the request's Host header names this server.

        The Host is normalized (_normalize_host) and must be a member
        of the trusted set derived from the configured bind address
        (_server_trusted_hosts; see --trusted-host). Forwarded /
        X-Forwarded-* are never consulted: without an explicitly
        trusted proxy those headers are client-controlled."""
        host = _normalize_host(self.headers.get("Host"))
        return host is not None and \
            host in _server_trusted_hosts(self.server)

    def _origin_allowed(self, value):
        """True when an Origin/Referer URL names exactly this server.

        Two accepted shapes, both requiring the URL's host to be in the
        trusted Host set (see --trusted-host): the direct-access shape
        — scheme http and the exact port this server bound (an absent
        port means the scheme default 80) — and the trusted-proxy
        shape — scheme https on the default public port (443 or
        absent), for a deployment whose TLS terminator forwards to this
        HTTP socket under a host the operator explicitly trusted.
        Anything else — another scheme, a non-default https port, an
        untrusted host, a malformed or credential-bearing URL — is
        refused. Forwarded / X-Forwarded-* headers are never consulted
        to make this decision: without an explicitly trusted proxy
        those headers are client-controlled."""
        try:
            parts = urlsplit(value)
            port = parts.port  # None when absent; ValueError on garbage
        except ValueError:
            return False
        if parts.scheme not in ("http", "https"):
            return False
        host = _normalize_host(parts.netloc)
        if host is None or \
                host not in _server_trusted_hosts(self.server):
            return False
        try:
            bound = self.server.server_address[1]
        except (AttributeError, IndexError):
            bound = None
        if parts.scheme == "https":
            # Trusted reverse proxy: the browser's public origin is the
            # TLS terminator's (host the operator listed), never this
            # backend socket's address or port.
            return port in (443, None)
        return port == bound if bound is not None else port in (80, None)

    def do_GET(self):
        # Host first: every HTML page this server emits carries the
        # CSRF token, so a Host this server was not configured to serve
        # is refused (421 Misdirected Request) before any page — or any
        # JSON — is emitted. A DNS-rebinding origin therefore cannot
        # even read a token to post back, however well it matches its
        # own Origin header.
        if not self._host_allowed():
            self.close_connection = True
            self._send_page(421, MISDIRECTED_HTML)
            return
        # Only the path picks the route; a query string never does
        # (the feed reads its cursor out of parts.query itself).
        parts = urlsplit(self.path)
        path = parts.path
        if path == "/":
            # One server "now" per request defines the whole 24h window.
            now = time.time()
            # The rail filter rides in as ?profile=<name> (chat pages
            # link back to the filtered inbox this way); unknown values
            # fall back to All.
            prof = parse_qs(parts.query).get("profile", [""])[-1]
            if prof not in rail_profiles():
                prof = ""
            try:
                rows, notes = load_sessions(now)
                mark_job_states(rows)  # replies running here are Active
                # waiting on the human, not us: one bounded batch call
                # per profile, never a probe per session
                mark_your_turn(rows, waiting_session_ids(
                    {name: db_path for db_path, name in discover_dbs()},
                    {r["profile"] for r in rows}))
                body = render(now, rows, notes, prof).encode("utf-8")
            except Exception as exc:  # keep the server alive no matter what
                self._send_page(500, error_page(exc).encode("utf-8"))
                return
            self._send_page(200, body)
            return

        # GET /new: the blank composer that starts a session via
        # POST /s/new.
        if path == "/new":
            self._send_page(200, render_new(*self._inbox()).encode("utf-8"))
            return

        # GET /avatar/<profile> and GET /avatar-user: the optional local
        # avatar PNGs, by their fixed filenames inside a home this server
        # already trusts. Anything else — an unknown profile, a missing,
        # swapped, or oversized file — is the themed 404; the letter
        # badge the img covers is the fallback everywhere.
        if path == USER_AVATAR_PATH:
            home = os.path.dirname(os.path.abspath(MAIN_DB))
            served = _avatar_served_file(home, USER_AVATAR_FILE)
            if served is None:
                self._not_found("There is no avatar at this address.")
            else:
                self._send_avatar(served)
            return
        m = AVATAR_PATH_RE.match(path)
        if m is not None:
            home = profile_home(unquote(m.group(1)))
            served = (_avatar_served_file(home, PROFILE_AVATAR_FILE)
                      if home is not None else None)
            if served is None:
                self._not_found("There is no avatar at this address.")
            else:
                self._send_avatar(served)
            return

        # GET /s/<profile>/<id>/feed?after=<message_id>: the transcript
        # page's JSON delta poll.
        m = FEED_PATH_RE.match(path)
        if m is not None:
            self._get_feed(unquote(m.group(1)), unquote(m.group(2)),
                           parts.query)
            return

        # GET /s/new/<job_id>: the /new composer's launch-status poll.
        # Checked before CHAT_PATH_RE, which would otherwise read the
        # same path as profile "new" (a profile that can never exist).
        m = NEW_JOB_PATH_RE.match(path)
        if m is not None:
            self._get_new_job(m.group(1))
            return

        m = CHAT_PATH_RE.match(path)
        if m is None:
            self._not_found("There is no page at this address.")
            return
        # Card hrefs are built with quote(), so undo that here. Both
        # captures are then validated before touching a DB: the profile
        # must be one discover_dbs() actually serves, the session id
        # plain safe characters (fullmatch — $ alone would let a
        # percent-decoded trailing newline slip past the class).
        profile = unquote(m.group(1))
        session_id = unquote(m.group(2))
        dbs = {name: db_path for db_path, name in discover_dbs()}
        if profile not in dbs or not SESSION_ID_RE.fullmatch(session_id):
            self._not_found("Unknown profile or session id.")
            return
        try:
            # The busy peek lets the initial render carry the live strip
            # for a reply this server is running — with the turn's
            # acceptance time as its first-output floor — without
            # consuming the one-shot failure note the feed still owes
            # the client.
            started = session_job_started(profile, session_id)
            chat = load_chat(profile, session_id, dbs,
                             started is not None, started)
        except sqlite3.Error as exc:
            # A locked or broken DB is a server error, not "unknown session".
            self._send_page(500, error_page(exc).encode("utf-8"))
            return
        if chat is None:
            self._not_found("Unknown profile or session id.")
            return
        # The pending clarify card rides the initial render too (""
        # when none is pending, the core is unreachable, or the session
        # is closed — never a failed page).
        cl = feed_clarify(profile, session_id, dbs, chat["archived"])
        chat["clarify_card"] = cl["html"] if cl else ""
        self._send_page(200, render_chat(chat, *self._inbox())
                        .encode("utf-8"))

    def _inbox(self):
        """(rows, notes) for the conversation sidebar on chat pages.

        Best effort: a transcript must never fail because the inbox
        list did, so any error degrades to an empty sidebar.
        """
        try:
            rows, notes = load_sessions(time.time())
            mark_job_states(rows)
            mark_your_turn(rows, waiting_session_ids(
                {name: db_path for db_path, name in discover_dbs()},
                {r["profile"] for r in rows}))
            return rows, notes
        except Exception:
            return [], []

    def _get_feed(self, profile, session_id, query):
        """GET /s/<profile>/<id>/feed — {messages, last_id, busy,
        activity, subagents, note?}.

        `after` is the last messages.id the client has already drawn
        (0 or omitted = the full snapshot the page renders). Each
        message entry carries the same server-rendered <li> HTML the
        page uses; last_id is the next cursor. busy is true while a
        composer reply is running here OR a valid turn lease exists.
        activity is the recomputed live strip snapshot: {active,
        state, pending_count, names, html}, html being the exact
        markup the page renders ("" when there is nothing to show). A
        finished job's failure note rides along exactly once. clarify
        (backwards-compatible addition, omitted on any core API error
        so an open page keeps its current card) is the pending clarify
        card {active, id, html} from the authenticated core GET.
        """
        dbs = {name: db_path for db_path, name in discover_dbs()}
        if profile not in dbs or not SESSION_ID_RE.fullmatch(session_id):
            self._send_json(404, {"ok": False,
                                  "error": "unknown profile or session"})
            return
        vals = parse_qs(query).get("after", ["0"])
        try:
            after = int(vals[-1])
        except (IndexError, ValueError):
            self._send_json(400, {"ok": False, "error": "bad after"})
            return
        if after < 0:
            after = 0
        busy, started, note = session_job_state(profile, session_id)
        try:
            feed = load_feed(profile, session_id, dbs, after, busy,
                             started)
        except sqlite3.Error:
            self._send_json(500, {"ok": False, "error": "database error"})
            return
        if feed is None:
            self._send_json(404, {"ok": False, "error": "unknown session"})
            return
        act = feed["activity"]
        # Feed-appended agent bubbles carry the same letter badge the
        # initial render used: the identity of the profile this session
        # lives in.
        feed_ident = profile_identity(profile)
        payload = {
            "ok": True,
            "messages": [
                {"id": it["id"], "kind": it["kind"],
                 "role": it.get("role", ""),
                 # the plain text ("" for tool groups) lets the client
                 # match a stored user row against its optimistic twin
                 "text": it.get("text", ""),
                 # tools groups carry the run's oldest row id: the seam
                 # identity for the client-side merge that keeps one
                 # maximal group whole when its rows span two polls
                 **({"first_id": it["first_id"]}
                    if it["kind"] == "tools" else {}),
                 "html": render_chat_item(it, feed_ident)}
                for it in feed["items"]],
            "last_id": feed["last_id"],
            # busy: a composer job here, or the durable lease Hermes
            # itself holds on the turn — either one means work is live.
            "busy": bool(busy or act.get("lease")),
            # The live tool-activity snapshot, recomputed on its own
            # every poll: {active, state, pending_count, names, html}.
            # Backwards-compatible addition: older clients ignore it;
            # the strip html is the exact markup the page renders.
            "activity": {
                "active": bool(act.get("active")),
                "state": act.get("state", ""),
                "pending_count": int(act.get("pending_count", 0)),
                "names": list(act.get("names", [])),
                "html": render_activity(act),
            },
            # Inline child rows and delegate dispatch cards, re-rendered
            # on every poll as one keyed list ({count, ids, items}) so
            # an open page discovers newly dispatched work — and a
            # static Claude card's viewer link — without a reload.
            # Backwards-compatible addition: older clients just ignore
            # it. Each item's html is the exact <li> markup the page
            # renders through the same renderers, key/rev drive the
            # in-place reconciliation.
            "subagents": child_feed_payload(
                time.time(), profile, feed["subagents"],
                feed.get("delegate_cards")),
            # The session's archive state rides every poll, so a Discord
            # archive/unarchive (mirrored by the sync) disables/enables
            # the composer and flips the toggle on an open page without a
            # reload. Backwards-compatible addition.
            "session_state": feed["session_state"],
        }
        # The pending clarify card, as {active, id, html}: active false
        # removes a visible card, a new id replaces it (selection
        # resets), the same id preserves it. Omitted entirely whenever
        # the core clarify GET fails, so a blip never flashes a card
        # away — and never fails the poll.
        clarify = feed_clarify(profile, session_id, dbs,
                               feed["session_state"]["archived"])
        if clarify is not None:
            payload["clarify"] = clarify
        if note:
            payload["note"] = note
        self._send_json(200, payload)

    def _csrf_gate(self):
        """Header-only forgery check for every state-changing route.

        Returns None when the request may proceed, else the HTTP status
        to answer with (403/415/421). Runs before any POST handler parses a
        body, touches SQLite or calls Discord, so a browser-simple
        cross-origin request can never mutate anything:

        - The Host header itself must name this server (the
          DNS-rebinding case: an origin whose Host AND Origin both name
          the attacker used to sail through an origin==host
          comparison). Untrusted Host -> 421.
        - Origin (when the client sends one) must name exactly this
          server — trusted host, and either the direct-access shape
          (http scheme plus the port this server actually bound) or the
          trusted-proxy shape (https scheme on the default public
          port, for a TLS terminator the operator listed with
          --trusted-host) — never merely echo the request's own Host,
          and never anything a Forwarded / X-Forwarded-* header claims.
          A Referer, when Origin is absent but Referer rides along, is
          held to the same rule.
        - The content type must be application/json exactly. An HTML
          form can only send the "simple" types, so this alone stops
          every forged form post.
        - X-CSRF-Token must equal this process's token, in constant
          time. A non-simple header forces a CORS preflight for
          cross-origin fetches — which this server never approves — and
          forms cannot set it at all.

        The token value is never included in the response or the log.
        """
        if not self._host_allowed():
            return 421
        origin = self.headers.get("Origin")
        if origin:
            if not self._origin_allowed(origin):
                return 403
        else:
            # Absent Origin is a non-browser client; a Referer, when
            # one rides along, still names where the request came from
            # and is held to the same rule.
            referer = self.headers.get("Referer")
            if referer and not self._origin_allowed(referer):
                return 403
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0]
        if ctype.strip().lower() != "application/json":
            return 415
        sent = self.headers.get(CSRF_HEADER) or ""
        if not secrets.compare_digest(sent, csrf_token()):
            return 403
        return None

    def do_POST(self):
        # The forgery gate runs before anything else: no route below may
        # parse a body or mutate state until the request proved it came
        # from a page this process served.
        refused = self._csrf_gate()
        if refused is not None:
            # A rejected request's body was never read; do not let a
            # half-consumed connection be reused.
            self.close_connection = True
            self._send_json(refused, {"ok": False,
                                      "error": "request refused"})
            return
        # Only the path picks the route; a query string never does.
        path = urlsplit(self.path).path
        if path == "/s/new":
            self._post_new()
            return
        m = REPLY_PATH_RE.match(path)
        if m is not None:
            self._post_reply(unquote(m.group(1)), unquote(m.group(2)))
            return
        m = CLARIFY_PATH_RE.match(path)
        if m is not None:
            self._post_clarify(unquote(m.group(1)), unquote(m.group(2)))
            return
        m = ARCHIVE_PATH_RE.match(path)
        if m is not None:
            self._post_archive(unquote(m.group(1)), unquote(m.group(2)),
                               m.group(3))
            return
        self._send_json(404, {"ok": False, "error": "no such endpoint"})

    def _post_archive(self, profile, session_id, action):
        """POST /s/<profile>/<id>/(close|reopen) — archive/unarchive.

        The body is ignored but still drained (the client sends an empty
        JSON object so the content-type rule holds uniformly). The heavy
        lifting — Discord verify-then-mirror for thread sessions,
        local-only flip otherwise — lives in set_session_archived; this
        wrapper only validates the profile/session pair and ships the
        JSON. Never runs on an unknown profile or session."""
        dbs = {name: db_path for db_path, name in discover_dbs()}
        if profile not in dbs or not SESSION_ID_RE.fullmatch(session_id):
            self.close_connection = True
            self._send_json(404, {"ok": False,
                                  "error": "unknown profile or session"})
            return
        err, _text = self._read_body_text()
        if err is not None and err != 400:
            # 400 ("{}" has no "text" key) is fine here — the body is
            # ignored by design; only size/shape failures matter, and
            # the read itself has already drained the connection.
            self.close_connection = True
            self._send_json(err, {"ok": False, "error": "bad request body"})
            return
        status, payload = set_session_archived(
            profile, session_id, dbs, action == "close")
        self._send_json(status, payload)

    def _post_reply(self, profile, session_id):
        """POST /s/<profile>/<id>/reply — accept one composer turn.

        202 {ok: true} once the core API has admitted the run; 409 while
        one is already running or the session is closed, 400/413 for
        empty or oversize text, 404 for an unknown profile/session, and
        503 when the core API could not admit the turn — which is an
        explicit failed send (the client restores the text), never a
        silent fallback to a CLI run. Core output never reaches the
        response — only these statuses do.
        """
        dbs = {name: db_path for db_path, name in discover_dbs()}
        if profile not in dbs or not SESSION_ID_RE.fullmatch(session_id):
            self.close_connection = True
            self._send_json(404, {"ok": False,
                                  "error": "unknown profile or session"})
            return
        err, text = self._composer_text()
        if err is not None:
            self._send_json(err, {"ok": False, "error": "bad request body"})
            return
        try:
            exists, _cwd, archived = load_session_cwd(profile, session_id,
                                                      dbs)
        except sqlite3.Error:
            self._send_json(500, {"ok": False, "error": "database error"})
            return
        if not exists:
            self._send_json(404, {"ok": False, "error": "unknown session"})
            return
        # A closed session refuses new turns even when a stale client
        # bypasses the disabled composer.
        if archived:
            self._send_json(409, {"ok": False,
                                  "error": "the session is closed"})
            return
        outcome = start_reply(profile, session_id, text, dbs)
        if outcome == "busy":
            self._send_json(409, {"ok": False,
                                  "error": "a reply is already running"})
            return
        if outcome != "started":
            # Admission failed synchronously: nothing ran, nothing was
            # written, and the client must show a failed send with the
            # text restored — there is no auto-answer fallback path.
            self._send_json(503, {"ok": False,
                                  "error": "agent gateway unavailable"})
            return
        self._send_json(202, {"ok": True})

    def _post_new(self):
        """POST /s/new — accept one fresh launch, answer 202 at once.

        Validation is synchronous (411/413/400 for a bad body).
        Acceptance registers exactly one bounded background job and
        returns the opaque job id plus its status URL; 409 while
        another launch is still live (one at a time fails a duplicate
        POST closed instead of admitting a second run). The session id
        — assigned deterministically by the core admission and served
        by GET /s/new/<job> once the row exists — is the only thing the
        client ever learns; never the prompt, never core output.
        """
        err, text = self._composer_text()
        if err is not None:
            self._send_json(err, {"ok": False, "error": "bad request body"})
            return
        job_id, _why = start_new_session(text)
        if job_id is None:
            self._send_json(409, {"ok": False,
                                  "error": "a new session is already "
                                           "starting"})
            return
        self._send_json(202, {"ok": True, "job": job_id,
                              "status_url": "/s/new/" + job_id})

    def _clarify_body(self):
        """(error, clarify_id, response) for a clarify POST: a bounded
        JSON object {"clarify_id": 1-128-char string, "response": a
        non-empty string or a short list of non-empty strings}. error
        is None only for a fully valid payload; every malformed body is
        one canned 400, so nothing beyond the safe status set — and
        never an upstream or echo detail — ever answers."""
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            return "length", "", None
        try:
            length = int(raw_len)
        except ValueError:
            return "length", "", None
        if length <= 0 or length > MAX_BODY_BYTES:
            return "length", "", None
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0]
        if ctype.strip().lower() != "application/json":
            return "ctype", "", None
        try:
            raw = self.rfile.read(length)
        except OSError:
            return "read", "", None
        if len(raw) != length:
            return "read", "", None
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return "json", "", None
        if not isinstance(obj, dict):
            return "json", "", None
        clarify_id = obj.get("clarify_id")
        if not isinstance(clarify_id, str):
            return "clarify_id", "", None
        clarify_id = clarify_id.strip()
        if not clarify_id or len(clarify_id) > CLARIFY_ID_MAX_CHARS:
            return "clarify_id", "", None
        response = obj.get("response")
        if not valid_clarify_response(response):
            return "response", "", None
        if isinstance(response, str):
            response = response.strip()
        else:
            response = [item.strip() for item in response]
        return None, clarify_id, response

    def _post_clarify(self, profile, session_id):
        """POST /s/<profile>/<id>/clarify — answer the pending clarify.

        Validates the profile/session pair, the body shape, the
        session's existence and open state locally, then proxies the
        core API's clarify POST carrying exactly the clarify_id the
        client holds. Only 200/400/404/409/503 JSON ever answers, every
        error a canned safe string (the upstream body is never echoed);
        nothing here touches /reply or writes any user message.
        """
        dbs = {name: db_path for db_path, name in discover_dbs()}
        if profile not in dbs or not SESSION_ID_RE.fullmatch(session_id):
            self.close_connection = True
            self._send_json(404, {"ok": False,
                                  "error": "unknown profile or session"})
            return
        err, clarify_id, response = self._clarify_body()
        if err is not None:
            self._send_json(400, {"ok": False, "error": "bad request body"})
            return
        try:
            exists, _cwd, archived = load_session_cwd(profile, session_id,
                                                      dbs)
        except sqlite3.Error:
            self._send_json(503, {"ok": False,
                                  "error": "session lookup failed"})
            return
        if not exists:
            self._send_json(404, {"ok": False, "error": "unknown session"})
            return
        # A closed session refuses clarify answers even when a stale
        # client still shows the card.
        if archived:
            self._send_json(409, {"ok": False,
                                  "error": "the session is closed"})
            return
        status, _obj, err = clarify_request(
            "POST", profile, session_id, dbs,
            {"clarify_id": clarify_id, "response": response})
        if err is None and status == 200:
            self._send_json(200, {"ok": True, "resolved": True,
                                  "clarify_id": clarify_id})
        elif status == 400:
            self._send_json(400, {"ok": False,
                                  "error": "invalid clarify response"})
        elif status == 404:
            self._send_json(404, {"ok": False,
                                  "error": "no pending clarify"})
        elif status == 409:
            self._send_json(409, {"ok": False,
                                  "error": "clarify not pending"})
        else:
            # Any other upstream verdict — auth failure, 5xx, timeout,
            # unparseable — reads as an availability problem from here.
            self._send_json(503, {"ok": False,
                                  "error": "clarify upstream unavailable"})

    def _get_new_job(self, job_id):
        """GET /s/new/<job> — the bounded status object for one launch.

        {ok, job, status: starting|running|done|failed, session_id?,
        url?, error} and nothing else: the registry never held the
        prompt or any core output, so there is nothing secret to
        leak. session_id is published as soon as the session's row
        exists in the main DB — while status is still running — so the
        client can navigate to /s/default/<id> the moment that page is
        servable, without waiting for the run. 404 for an unknown or
        pruned job id; the client treats that as terminal and never
        retries the launch."""
        payload = new_job_payload(job_id)
        if payload is None:
            self._send_json(404, {"ok": False, "error": "unknown job"})
            return
        self._send_json(200, payload)

    def do_HEAD(self):
        self.do_GET()

    def log_message(self, fmt, *args):
        # Request line only; never log query strings or bodies.
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="hermes mission_control serve",
        description="Mission Control: web view of Hermes sessions active "
                    "in the last 24 hours, reading "
                    "%s." % display_hermes_home())
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to bind (default 127.0.0.1, loopback "
                         "only). Binding a non-loopback address is "
                         "explicit, UNAUTHENTICATED, and only safe on a "
                         "trusted private network")
    ap.add_argument("--port", type=int, default=9136)
    ap.add_argument("--trusted-host", action="append", default=[],
                    metavar="HOST",
                    help="additional Host header value this server "
                         "answers for (repeatable). By default only the "
                         "bind address itself is trusted — plus, for a "
                         "loopback or wildcard bind, the local "
                         "machine's own addresses — and requests whose "
                         "Host names anything else are refused with 421. "
                         "Forwarded/X-Forwarded-* headers are never "
                         "trusted.")
    ap.add_argument("--no-discord-sync", action="store_true",
                    help="disable the background Discord archive sync "
                         "(for proof servers against synthetic data)")
    args = ap.parse_args(argv)
    # Backstop for the sync thread too: any exit path sets its stop
    # event (the finally below joins it).
    atexit.register(_discord_sync_stop.set)

    # SIGTERM stops the loop the same way Ctrl-C does, so the finally
    # block (and atexit backstop) can shut the sync thread down.
    # Windows has no deliverable SIGTERM; Ctrl-C still stops the loop.
    if hasattr(signal, "SIGTERM"):
        def _stop(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, _stop)

    # From here on everything runs inside the stop-guard: a SIGTERM (or
    # Ctrl-C) that lands mid-startup — say, between the flushed startup
    # line and the serve loop — is caught here, not traced out of main.
    sync_thread = None
    httpd = None
    try:
        # A non-loopback bind is a deliberate act: say plainly what it
        # exposes. The server has NO authentication — anyone who can
        # reach the address can read every session and send replies.
        if not _is_loopback(args.host):
            print("WARNING: binding %s — Mission Control has no built-in "
                  "authentication. Anyone who can reach this address can "
                  "read the session transcript and send messages. Keep it "
                  "on a trusted private network or put an authenticating "
                  "proxy in front." % args.host, flush=True)

        # The Discord -> DB archive mirror: exactly one daemon thread,
        # its first pass immediately, then every
        # DISCORD_SYNC_INTERVAL_SECONDS until the stop event. Proof
        # servers opt out with --no-discord-sync.
        if not args.no_discord_sync:
            sync_thread = threading.Thread(
                target=discord_sync_loop, name="discord-sync", daemon=True)
            sync_thread.start()

        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
        # Pin the Host-trust set for the whole serve lifetime: the
        # bind-derived defaults plus every explicit --trusted-host
        # entry (refused spellings are dropped with a warning, since a
        # typo there would silently keep the request refused).
        trusted = _default_trusted_hosts(args.host)
        for extra in args.trusted_host:
            name = _normalize_host(extra)
            if name is None or name in WILDCARD_BINDS:
                print("WARNING: ignoring unparseable --trusted-host %r"
                      % (extra,), flush=True)
            elif name not in trusted:
                trusted.add(name)
                print("serving Host %s in addition to the bind address"
                      % (name,), flush=True)
        httpd.trusted_hosts = trusted
        # Report the port actually bound: --port 0 asks the OS for a
        # free port, which is how a test (or anything else that must not
        # race for a fixed number) gets one — server_address carries the
        # real value back after the bind.
        bound_port = httpd.server_address[1]
        print("serving on http://%s:%d/ (home: %s, profiles: %s%s)"
              % (args.host, bound_port, display_hermes_home(),
                 ", ".join(p for _, p in discover_dbs()),
                 "" if sync_thread else ", discord-sync off"), flush=True)
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _discord_sync_stop.set()
        if sync_thread is not None:
            sync_thread.join(timeout=DISCORD_TIMEOUT_SECONDS + 2)
        if httpd is not None:
            httpd.server_close()


if __name__ == "__main__":
    main()
