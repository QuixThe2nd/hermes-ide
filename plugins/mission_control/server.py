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
  the direct-children state — count, ids, and the exact section HTML —
  which the client swaps in on every poll so an open page discovers
  newly dispatched children without a reload.
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
"""

import argparse
import atexit
import glob
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
from string import Template
from urllib.parse import parse_qs, quote, unquote, urlsplit

from hermes_constants import display_hermes_home, get_hermes_home
from hermes_state import SessionDB

WINDOW_SECONDS = 24 * 3600
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

# Last message per session: the newest messages row with real text —
# role 'user' or 'assistant' (session_meta and tool rows are never
# candidates), non-empty content that isn't the "[SILENT]" marker —
# ordered by timestamp then id (id breaks timestamp ties). substr()
# runs inside the subquery, so only the first 800 characters ever
# leave the DB; tool JSON and full bodies are never selected.
# idx_messages_session (session_id, timestamp) serves the IN-list lookup.
LAST_LINE_SQL = """
SELECT session_id, role, timestamp, id, preview FROM (
  SELECT session_id, role, timestamp, id,
         substr(content, 1, 800) AS preview,
         ROW_NUMBER() OVER (
           PARTITION BY session_id ORDER BY timestamp DESC, id DESC
         ) AS rn
  FROM messages
  WHERE role IN ('user', 'assistant')
    AND IFNULL(content, '') != ''
    AND content != '[SILENT]'
    AND session_id IN ({placeholders})
) WHERE rn = 1
"""

# Last tool per session: the newest messages row with role 'tool' and
# a non-empty tool_name, same ROW_NUMBER ordering as last message.
# Only session_id/tool_name/timestamp/id/role are ever selected — the
# tool JSON in content/tool_calls never leaves the DB.
LAST_TOOL_SQL = """
SELECT session_id, tool_name FROM (
  SELECT session_id, tool_name, timestamp, id, role,
         ROW_NUMBER() OVER (
           PARTITION BY session_id ORDER BY timestamp DESC, id DESC
         ) AS rn
  FROM messages
  WHERE role = 'tool'
    AND IFNULL(tool_name, '') != ''
    AND session_id IN ({placeholders})
) WHERE rn = 1
"""

# IN-lists are chunked to stay under the 999 host-parameter limit of
# older SQLite builds; a 24h page fits one chunk, so this stays a single
# extra query per DB rather than one per session.
LAST_LINE_CHUNK = 900

# ---- inbox classification (Active / Completed / Incomplete) ---------
# Hermes turn leases: one row per conversation root, the live
# continuation session encoded in the holder as "...:turn=<id>:...".
# Only expires_at > now counts as live; a DB without the table simply
# degrades to Completed/Incomplete classification (plus one note).
LEASES_LIVE_SQL = """
SELECT conversation_id, holder FROM session_turn_leases
WHERE expires_at > ?
"""

# holder looks like
# "pid=2253327:turn=20260901_163739_4fef22:20260901_163739_4fef22:91d8fec2:platform=discord"
# — the id right after "turn=" is the live continuation session. Parsed
# conservatively (full session-id character class) and only then;
# conversation_id is the fallback when no turn id parses.
TURN_ID_RE = re.compile(r"(?:^|:)turn=([A-Za-z0-9_.-]+)")

# Newest active, non-hidden, non-session_meta event per session — the
# Completed signal. All comparisons happen inside SQL on the full
# content (the [SILENT] test must be exact), so only tiny boolean
# flags ever leave the DB. Chunked like the preview queries.
NEWEST_EVENT_SQL = """
SELECT session_id, role, has_content, has_tools, silent FROM (
  SELECT session_id, role,
         IFNULL(content, '') != '' AS has_content,
         IFNULL(tool_calls, '') != '' AS has_tools,
         IFNULL(content, '') = '[SILENT]' AS silent,
         ROW_NUMBER() OVER (
           PARTITION BY session_id ORDER BY timestamp DESC, id DESC
         ) AS rn
  FROM messages
  WHERE IFNULL(active, 1) = 1
    AND role != 'session_meta'
    AND IFNULL(display_kind, '') != 'hidden'
    AND session_id IN ({placeholders})
) WHERE rn = 1
"""

# Inbox section keys in display order. The ordering contract: every
# open session renders above every closed one — Active first, then
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
# all), Incomplete and Closed only when they have members. Closed is
# the projected tip's ended_at or the archived flag (archived mirrors
# Discord or a local close) — it wins over every other signal, and
# every closed row says so itself with an Archived or Ended chip.
SECTION_ORDER = ("active", "incomplete", "completed", "closed")
SECTION_TITLES = {"active": "Active", "closed": "Closed",
                  "completed": "Open \N{MIDDLE DOT} completed",
                  "incomplete": "Open \N{MIDDLE DOT} unfinished"}

# ---- chat transcript route (/s/<profile>/<session_id>) ---------------
# The profile must be one discover_dbs() actually serves (so it maps to
# exactly one DB), and the session id must be plain safe characters —
# no traversal, no quotes, no wildcards worth worrying about.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
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

# ---- Discord archive sync ------------------------------------------
# Everything the background mirror and the close/reopen actions need.
# The token only ever lives in memory as an Authorization header value
# (never logged, never served); snowflake thread ids are validated as
# plain digits before one is ever interpolated into a URL.
DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_USER_AGENT = "DiscordBot (https://github.com/hermes-agent, 1.0)"
DISCORD_TIMEOUT_SECONDS = 10
DISCORD_MAX_BODY_BYTES = 1024 * 1024
# At least this long between any two Discord API requests, enforced
# through one shared lock by every caller.
DISCORD_MIN_REQUEST_GAP = 0.25
DISCORD_SYNC_INTERVAL_SECONDS = 30
SNOWFLAKE_RE = re.compile(r"^[0-9]{1,25}$")
# Anything token-shaped that shows up inside a Discord error payload is
# masked before the (bounded) message may be returned to a client.
TOKENISH_RE = re.compile(r"[A-Za-z0-9_-]{40,}")

_discord_lock = threading.Lock()
_discord_last_request = 0.0
_discord_cooldown_until = 0.0
_discord_sync_stop = threading.Event()

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
RUN_FAILED_STATES = ("failed", "cancelled", "interrupted")
CLARIFY_MAX_BODY_BYTES = 64 * 1024
# Presentation bounds mirrored from the core card contract (the core
# already enforces them; these re-bound whatever actually arrives so a
# malformed or hostile upstream can never crash or flood the page).
CLARIFY_MAX_CHOICES = 8
CLARIFY_MAX_QUESTION_CHARS = 2000
CLARIFY_MAX_CHOICE_CHARS = 500
CLARIFY_ID_MAX_CHARS = 128
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

# Display caps: the SQL-level substr keeps any single row at 4000 chars
# (an 80 KB tool result never leaves the DB whole) and a tool bubble
# shows at most 400 characters of detail. CHAT_TEXT_CHARS is the Python
# side of that same 4000-char transcript text cap — the clamp applied to
# commentary recovered from codex_message_items, whose SQL projection
# is bounded to the same size (the clamp guards rows arriving by any
# other path).
CHAT_DETAIL_CHARS = 400
CHAT_TEXT_CHARS = 4000
# The codex_message_items source bound. The SQL projections above and
# codex_commentary_text() must agree on this number: SQL selects the
# value only when its character length is within it, and the parser
# rejects anything longer, so an oversized blob can never be sliced
# into an accepted truncated prefix from either side.
CODEX_ITEMS_MAX_CHARS = 4000

# Distinct tool-name chips in a collapsed group's summary line; past
# this a "+N more" chip keeps mixed runs to one readable row.
TOOL_CHIP_MAX = 6

# One session's header: everything the chat page shows except messages.
# source/thread_id/archived drive the Close/Reopen toggle, the closed
# banner and the disabled composer.
CHAT_SESSION_SQL = """
SELECT title, display_name, started_at, last_activity_at,
       source, thread_id, archived
FROM sessions
WHERE id = ?
"""

# The transcript page itself: every displayable row, newest-first in
# SQL, reversed in Python into display order. Row id is the one
# authoritative chronology — AUTOINCREMENT insertion order, unique and
# monotone, immune to the non-monotonic timestamps a tool result or a
# clock skew can write — so the page, the full feed and every delta
# poll all agree on one order and the id cursor can never skip or
# reorder. There is no row cap, a 1700-event session renders all 1700.
# substr() caps content at 4000 characters before it leaves the DB;
# tool JSON is never selected whole.
# The lifecycle columns (assistant tool_calls carrier, tool result id,
# finish_reason) feed tool-call/activity matching; tool_calls is
# substr-capped too, so arguments never leave the DB unbounded.
# The trailing column is the Codex commentary fallback: a Codex
# tool-call assistant row carries its narration only in
# codex_message_items while content stays '', so exactly those rows
# also select that JSON for chat_messages to parse defensively — but
# ONLY whole and only within CODEX_ITEMS_MAX_CHARS (length() counts
# characters for TEXT): an oversized value selects '' so SQL can never
# hand the parser a truncated prefix, even when the first 4000
# characters happen to parse as valid JSON followed by padding. Every
# other row — user, tool, or an assistant row with content — selects ''
# there, so content stays the sole authority and is never duplicated.
CHAT_PAGE_SQL = """
SELECT role, tool_name, timestamp, id, substr(content, 1, 4000),
       substr(IFNULL(tool_calls, ''), 1, 2000), tool_call_id,
       finish_reason,
       CASE WHEN role = 'assistant' AND IFNULL(content, '') = ''
             AND length(IFNULL(codex_message_items, ''))
                 BETWEEN 1 AND 4000
            THEN codex_message_items ELSE '' END
FROM messages
WHERE session_id = ?
  AND role != 'session_meta'
  AND IFNULL(display_kind, '') != 'hidden'
ORDER BY id DESC
"""

# Newest row id in a session — the feed cursor. MAX over every row
# (including the ones display rules skip) so filtered rows still advance
# the cursor instead of being re-scanned on every poll.
FEED_LAST_ID_SQL = """
SELECT MAX(id) FROM messages WHERE session_id = ?
"""

# Rows newer than the cursor, oldest-first by the same authoritative
# id order as the page, same display filters, lifecycle columns and
# Codex commentary fallback column as the transcript page (the
# empty-content assistant carrier recovers its narration identically
# in a delta poll). LIMIT keeps one pathological catch-up bounded;
# when the limit bites, the cursor stops at the newest row actually
# returned so nothing is silently skipped.
FEED_AFTER_SQL = """
SELECT role, tool_name, timestamp, id, substr(content, 1, 4000),
       substr(IFNULL(tool_calls, ''), 1, 2000), tool_call_id,
       finish_reason,
       CASE WHEN role = 'assistant' AND IFNULL(content, '') = ''
             AND length(IFNULL(codex_message_items, ''))
                 BETWEEN 1 AND 4000
            THEN codex_message_items ELSE '' END
FROM messages
WHERE session_id = ?
  AND id > ?
  AND role != 'session_meta'
  AND IFNULL(display_kind, '') != 'hidden'
ORDER BY id ASC
LIMIT ?
"""

# Delta-feed group seam: when a delta's oldest row is a tool result,
# the collapsed group it belongs to may have started in an earlier
# poll. These are the rows immediately older than the delta — same
# projection and filters, newest-first — so load_feed can rebuild the
# COMPLETE maximal tool group (see FEED_BACKFILL_MAX) instead of
# letting one run render as two adjacent groups across polls.
FEED_BACKFILL_SQL = """
SELECT role, tool_name, timestamp, id, substr(content, 1, 4000),
       substr(IFNULL(tool_calls, ''), 1, 2000), tool_call_id,
       finish_reason,
       CASE WHEN role = 'assistant' AND IFNULL(content, '') = ''
             AND length(IFNULL(codex_message_items, ''))
                 BETWEEN 1 AND 4000
            THEN codex_message_items ELSE '' END
FROM messages
WHERE session_id = ?
  AND id < ?
  AND role != 'session_meta'
  AND IFNULL(display_kind, '') != 'hidden'
ORDER BY id DESC
LIMIT ?
"""
# Bound on the seam rebuild. A run of consecutive tool rows longer
# than this, split exactly at a poll boundary, is re-rendered from
# this many rows back; the client-side merge then no longer
# recognizes the older element and appends instead of replacing (two
# adjacent groups, correct order, no lost rows — a reload shows the
# full run). One bounded backfill, never a page-wide rescan.
FEED_BACKFILL_MAX = 200

# The LIMIT above bounds only one after>0 catch-up poll — never the
# transcript itself. When it bites, the cursor stops at the newest row
# actually returned, so the next poll picks up the rest.
FEED_CATCHUP_MAX = 300

# The live-activity snapshot: the session's newest active rows with
# exactly the fields lifecycle matching needs — assistant tool_calls
# carriers (substr-capped), tool result ids, finish_reason — and only a
# 200-char slice of content, which the strip never shows anyway. It is
# recomputed from this bounded tail on its own, independent of the feed
# cursor, so a pending call stays pending on every poll until its
# result row lands (a delta poll alone would forget the carrier).
ACTIVITY_SQL = """
SELECT role, tool_name, tool_call_id, finish_reason,
       substr(IFNULL(content, ''), 1, 200),
       substr(IFNULL(tool_calls, ''), 1, 2000), id, timestamp
FROM messages
WHERE session_id = ?
  AND IFNULL(active, 1) = 1
  AND role != 'session_meta'
  AND IFNULL(display_kind, '') != 'hidden'
ORDER BY timestamp DESC, id DESC
LIMIT ?
"""
ACTIVITY_MAX_ROWS = 60

# Defensive parsing bounds: one carrier rarely holds more than a
# handful of calls, and summaries are display-only.
TOOL_CALLS_MAX = 12
ARGS_SUMMARY_CHARS = 160
ARGS_VALUE_CHARS = 48

# Session row lookup for the reply route: existence plus the recorded
# working directory (the resume runs there when it still exists) and
# the archived flag (a closed session refuses new turns).
SESSION_CWD_SQL = """
SELECT cwd, archived FROM sessions WHERE id = ?
"""

# Close/reopen and Discord sync writes. The close/reopen action for a
# Discord session flips every row on the thread (continuation sessions
# share it) in one transaction; the sync only ever touches rows whose
# thread had activity in the rolling 24h window.
SESSION_STATE_SQL = """
SELECT source, thread_id, archived FROM sessions WHERE id = ?
"""
SET_ARCHIVE_BY_THREAD_SQL = """
UPDATE sessions SET archived = ?
WHERE source = 'discord' AND thread_id = ?
"""
SET_ARCHIVE_BY_THREAD_CHANGED_SQL = """
UPDATE sessions SET archived = ?
WHERE source = 'discord' AND thread_id = ? AND archived != ?
"""
SET_ARCHIVE_BY_ID_SQL = """
UPDATE sessions SET archived = ? WHERE id = ?
"""
COUNT_THREAD_MISMATCH_SQL = """
SELECT COUNT(*) FROM sessions
WHERE source = 'discord' AND thread_id = ? AND archived != ?
"""
COUNT_ID_MISMATCH_SQL = """
SELECT COUNT(*) FROM sessions WHERE id = ? AND archived != ?
"""
DISCORD_RECENT_THREADS_SQL = """
SELECT DISTINCT thread_id FROM sessions
WHERE source = 'discord'
  AND thread_id IS NOT NULL
  AND thread_id != ''
  AND last_activity_at >= ?
"""

# ---- sub-agent section (/s/<profile>/<id>) ---------------------------
# A sub-agent session is one stored with source='subagent'; its parent
# is parent_session_id. The source test alone decides membership —
# Discord continuation sessions share the parent column and must never
# be treated as children. Direct children only: opening a child runs
# the same query for its own id, which makes the navigation recursive
# without any recursion in this file.

# Direct children of one session, oldest first. LIMIT bounds the query
# (a runaway parent can never flood the page); idx_sessions_parent
# serves the parent lookup. Runs on the same profile DB as the parent.
# started_at lets the caller merge these with cross-profile children
# (which live in another DB) into one oldest-first list.
SUBAGENTS_SQL = """
SELECT id, title, display_name, started_at, last_activity_at,
       ended_at, end_reason
FROM sessions
WHERE parent_session_id = ?
  AND source = 'subagent'
  AND hidden = 0
ORDER BY started_at ASC, id ASC
LIMIT ?
"""

SUBAGENT_MAX_CHILDREN = 50

# Goal-label fallback: sub-agent rows usually have no title or
# display_name, so a child's row label comes from its first non-empty
# user message (the goal it was dispatched for). One batched query for
# every blank-titled child on the page, chunked past the host-parameter
# limit exactly like the inbox previews — never one query per child.
# substr() keeps the read bounded: only the first 400 characters of the
# prompt ever leave the DB.
SUBAGENT_LABEL_SQL = """
SELECT session_id, preview FROM (
  SELECT session_id, substr(content, 1, 400) AS preview,
         ROW_NUMBER() OVER (
           PARTITION BY session_id ORDER BY timestamp ASC, id ASC
         ) AS rn
  FROM messages
  WHERE role = 'user'
    AND IFNULL(content, '') != ''
    AND content != '[SILENT]'
    AND session_id IN ({placeholders})
) WHERE rn = 1
"""

# Belt-and-braces over the SQL substr: the Python clamp keeps any one
# row's markup small no matter where the label came from.
SUBAGENT_LABEL_CHARS = 200

# ---- canonical-chain variants ----------------------------------------
# A conversation is the whole compression lineage Hermes core resolves
# (root first, tip last — see _chain_ids), not the one row a URL names.
# These are the transcript-page, feed, cursor, sub-agent and archive
# statements above with only the session filter widened from "= ?" to
# "IN (...)" over the chain members, so a root bookmark and the live
# tip render the same transcript, the same sub-agent section, advance
# the same cursor, and close/reopen flips the same set of rows. Row id
# stays the one chronology: chain members' messages interleave by the
# global AUTOINCREMENT id, which is exactly the order they were written.
CHAT_PAGE_CHAIN_SQL = CHAT_PAGE_SQL.replace(
    "WHERE session_id = ?", "WHERE session_id IN ({placeholders})")
FEED_LAST_ID_CHAIN_SQL = FEED_LAST_ID_SQL.replace(
    "WHERE session_id = ?", "WHERE session_id IN ({placeholders})")
FEED_AFTER_CHAIN_SQL = FEED_AFTER_SQL.replace(
    "WHERE session_id = ?", "WHERE session_id IN ({placeholders})")
FEED_BACKFILL_CHAIN_SQL = FEED_BACKFILL_SQL.replace(
    "WHERE session_id = ?", "WHERE session_id IN ({placeholders})")
SUBAGENTS_CHAIN_SQL = SUBAGENTS_SQL.replace(
    "WHERE parent_session_id = ?", "WHERE parent_session_id IN ({placeholders})")

# Close/reopen for a non-Discord conversation flips every chain member
# in one transaction (a Discord thread session keeps flipping by
# thread_id, which continuation rows already share); the mismatch
# read-back covers the same set.
SET_ARCHIVE_BY_CHAIN_SQL = SET_ARCHIVE_BY_ID_SQL.replace(
    "WHERE id = ?", "WHERE id IN ({placeholders})")
COUNT_CHAIN_MISMATCH_SQL = """
SELECT COUNT(*) FROM sessions
WHERE id IN ({placeholders}) AND archived != ?
"""

# Root id -> display title for projected (tip) rows: search resolves a
# root's title and id to the conversation its tip surfaces, so both
# spellings ride the client search blob (render_conv_row's data-q).
ROOT_TITLE_SQL = """
SELECT id, COALESCE(NULLIF(title, ''), NULLIF(display_name, ''), id)
FROM sessions WHERE id IN ({placeholders})
"""

# Discord envelopes wrap the real text in bookkeeping: a leading
# "[Triggering message id: …]" block (the handle for reply/react/pin)
# and a "[username]" sender prefix. Previews show the message itself.
TRIGGERING_BLOCK_RE = re.compile(r"^\s*\[Triggering message id:[^\]]*\]\s*")
SENDER_PREFIX_RE = re.compile(r"^\[[^\]\s]+\]\s+")
BLANK_RUN_RE = re.compile(r"(?:[ \t]*\n){3,}")

# Lone UTF-16 surrogates can reach Python strings only through escaped
# JSON ("\ud800") — SQLite TEXT and utf-8 decoding cannot produce one.
# They are unencodable: any response write (utf-8) would raise
# UnicodeEncodeError, so every display-bound string replaces them with
# U+FFFD. Properly paired astral characters are single code points in a
# Python str and never match.
LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def sanitize_text(text):
    """One string -> the same string with lone surrogates replaced by
    U+FFFD, so nothing downstream (HTML escape, JSON body, utf-8 write)
    can raise UnicodeEncodeError."""
    if not text:
        return text
    if not LONE_SURROGATE_RE.search(text):
        return text
    return LONE_SURROGATE_RE.sub("\N{REPLACEMENT CHARACTER}", text)


def clean_preview(text):
    """Raw message text -> display preview: envelope stripped, extra
    blank lines collapsed, ends trimmed, lone surrogates replaced.
    Newlines survive (the preview cell renders pre-wrap)."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = TRIGGERING_BLOCK_RE.sub("", text, count=1)
    text = SENDER_PREFIX_RE.sub("", text, count=1)
    text = BLANK_RUN_RE.sub("\n\n", text)
    return sanitize_text(text).strip()


def clamp_label(text):
    """Raw text -> one-line sub-agent row label: cleaned with the same
    helpers as previews, all whitespace collapsed, capped hard so a
    whole prompt can never dump into the section (CSS clamps the visual
    to two lines on top of this)."""
    text = " ".join(clean_preview(text).split())
    if len(text) > SUBAGENT_LABEL_CHARS:
        text = text[:SUBAGENT_LABEL_CHARS - 1].rstrip() \
            + "\N{HORIZONTAL ELLIPSIS}"
    return text


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


def _chain_ids(db_path, session_id):
    """Ids of the conversation *session_id* belongs to, root first.

    The definition of "one conversation" is core-owned
    (SessionDB.get_session_lineage_ids — the same chain the resume
    readers count), so a compression root, any middle segment and the
    live tip all resolve to the same list and every transcript, feed,
    cursor and archive path below acts on the whole conversation. The
    caller supplies the discovered canonical DB path; an unreadable DB
    or an unknown id degrades to [session_id] — the page then renders
    exactly the one segment it always could, never a wrong chain."""
    try:
        # tuning_pragmas=False: this server serves explicit discovered
        # paths, so its connections take plain-SQLite defaults and never
        # load config.yaml — the config read would materialize the
        # ambient HERMES_HOME, which is not this server's to touch.
        sdb = SessionDB(db_path, read_only=True, tuning_pragmas=False)
    except Exception:
        return [session_id]
    try:
        return sdb.get_session_lineage_ids(session_id) or [session_id]
    except Exception:
        return [session_id]
    finally:
        sdb.close()


def load_last_lines(con, session_ids):
    """Map session id -> (role, cleaned preview) of its newest text row.

    One batched query for all the ids (chunked only past the
    host-parameter limit), so a page load never becomes N+1.
    """
    lines = {}
    for start in range(0, len(session_ids), LAST_LINE_CHUNK):
        chunk = session_ids[start:start + LAST_LINE_CHUNK]
        sql = LAST_LINE_SQL.format(placeholders=",".join("?" * len(chunk)))
        for sid, role, _ts, _id, preview in con.execute(sql, chunk):
            lines[sid] = (role, clean_preview(preview))
    return lines


def load_last_tools(con, session_ids):
    """Map session id -> tool_name of its newest tool row, batched like
    load_last_lines (one query per chunk, never per session)."""
    tools = {}
    for start in range(0, len(session_ids), LAST_LINE_CHUNK):
        chunk = session_ids[start:start + LAST_LINE_CHUNK]
        sql = LAST_TOOL_SQL.format(placeholders=",".join("?" * len(chunk)))
        for sid, tool_name in con.execute(sql, chunk):
            tools[sid] = tool_name
    return tools


def parse_turn_id(holder):
    """Lease holder -> the live continuation session id, or None.

    The holder carries "...:turn=<session_id>:..."; only a value that
    fully matches the session-id character class is trusted, so a
    mangled or foreign holder just yields None (and the caller falls
    back to the lease's conversation_id).
    """
    m = TURN_ID_RE.search(holder or "")
    if m and SESSION_ID_RE.fullmatch(m.group(1)):
        return m.group(1)
    return None


def lease_session_id(conversation_id, holder):
    """One lease -> the session id it makes Active: the parsed turn id
    when there is one, else the conversation root itself (and only when
    that even looks like a session id)."""
    turn = parse_turn_id(holder)
    if turn:
        return turn
    if conversation_id and SESSION_ID_RE.fullmatch(conversation_id):
        return conversation_id
    return None


def load_lease_ids(con, now):
    """Set of session ids with a live unexpired turn lease in this DB.

    One bounded query over the (small) lease table; sqlite3.Error
    propagates so a DB without the table degrades to plain
    Completed/Incomplete classification with a single note.
    """
    ids = set()
    for conversation_id, holder in con.execute(LEASES_LIVE_SQL, (now,)):
        sid = lease_session_id(conversation_id, holder)
        if sid:
            ids.add(sid)
    return ids


def load_newest_events(con, session_ids):
    """Map session id -> (role, has_content, has_tools, silent) of its
    newest active, non-hidden, non-session_meta event — the Completed
    signal. Batched and chunked exactly like the preview queries;
    sqlite3.Error propagates so an ancient schema degrades to Open ·
    unfinished for every open row (closed rows never consult this)
    with a note."""
    newest = {}
    for start in range(0, len(session_ids), LAST_LINE_CHUNK):
        chunk = session_ids[start:start + LAST_LINE_CHUNK]
        sql = NEWEST_EVENT_SQL.format(
            placeholders=",".join("?" * len(chunk)))
        for sid, role, has_content, has_tools, silent in con.execute(
                sql, chunk):
            newest[sid] = (role, bool(has_content), bool(has_tools),
                           bool(silent))
    return newest


def classify_session(row, newest):
    """Inbox section for one open session row: completed / incomplete.

    Active and Closed are decided by the caller (leases + this
    server's running jobs; the projected tip's ended_at or archived),
    never here — a row this sees is already known to be open. Completed
    is honest by construction: the newest active event is an assistant
    answer — real text, no tool_calls carrier, not [SILENT]. A
    transcript that merely ends on a user message or a tool result
    stays incomplete instead of being mislabeled.
    """
    role, has_content, has_tools, silent = newest.get(
        row["id"], ("", False, False, False))
    if (role == "assistant" and has_content and not has_tools
            and not silent):
        return "completed"
    return "incomplete"


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
        "subagents": subagents,
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

# Job artifacts live under each owner home: <home>/research_jobs/rj_*.
RESEARCH_JOBS_DIR = "research_jobs"
# A worker session matches a job only when its first user message is
# byte-equal to one of that job's prompts/*.md AND its started_at sits
# inside [created_at - skew, max(updated_at, completed_at) + skew].
# The skew absorbs runner/status write lag and stays far under the gap
# between distinct jobs, so identical prompt bytes (which do recur
# across jobs) can never cross-match a session into the wrong family.
LINEAGE_SKEW_SECONDS = 60
# Reads stay bounded: a job JSON file past this size is not a job file
# and is skipped, a prompt file longer than this many characters could
# never equal the equally capped first-user DB read anyway (oversize
# prompts are skipped, never truncated), and only this prefix of a
# terminal tool result is ever scanned for the launcher's line. The
# prompt cap and every check against it count UTF-8 characters — the
# same units as the substr() on the DB side — so a multibyte prompt is
# judged by its true length.
LINEAGE_JOB_JSON_BYTES = 65536
# NOTE: real synthesis and correction prompts routinely run to tens of
# thousands of characters. An earlier 8192-character cap silently
# skipped every one of them, so those worker sessions leaked into the
# top-level inbox — keep this cap comfortably above the largest prompt
# the runner writes.
LINEAGE_PROMPT_MAX_CHARS = 64 * 1024
LINEAGE_TOOL_JSON_CHARS = 4000
# The whole index is rebuilt at most this often: the feed polls every
# 2 s and must not rescan every windowed terminal row on every poll,
# so callers share one briefly cached snapshot instead.
LINEAGE_CACHE_SECONDS = 10.0

# A terminal tool result is JSON whose "output" string may carry the
# launched session as a standalone line — matched only as a whole line
# in the session-id character class, never as a substring.
SESSION_ID_LINE_RE = re.compile(
    r"(?m)^[ \t]*session_id:[ \t]*([A-Za-z0-9_.-]+)[ \t]*$")

# Sessions of one DB with activity inside the lineage read window —
# the parents whose terminal tool rows are worth scanning (plus skew,
# the documented small allowance on top of the 24h product window).
LINEAGE_WINDOW_SESSIONS_SQL = """
SELECT id FROM sessions
WHERE hidden = 0
  AND last_activity_at >= ?
  AND last_activity_at <= ?
"""

# Terminal tool results of those sessions, each row capped at
# LINEAGE_TOOL_JSON_CHARS before it leaves the DB; DISTINCT collapses
# repeated copies and the caller's set collapses the rest.
# idx_messages_session serves the IN-list lookup.
LINEAGE_TERMINAL_SQL = """
SELECT DISTINCT session_id, substr(content, 1, {chars})
FROM messages
WHERE role = 'tool'
  AND tool_name = 'terminal'
  AND timestamp >= ?
  AND timestamp <= ?
  AND session_id IN ({placeholders})
"""

# Worker-DB candidates for job matching: sessions started inside the
# union of that profile's job windows (bounded even when a stale job
# widens the range — the per-job window still decides every match).
# The candidate's own source rides along: a job match is inference
# (prompt bytes + time window against a durable job artifact), so it
# may only ever claim a session whose source EXPLICITLY marks a
# non-human worker run. A human-facing session — CLI, API, Discord,
# Telegram, the mission-control composer itself — keeps its inbox row
# even when its first prompt and start time happen to match a job.
LINEAGE_WORKER_CANDIDATES_SQL = """
SELECT id, title, display_name, started_at, last_activity_at,
       ended_at, end_reason, IFNULL(source, '')
FROM sessions
WHERE hidden = 0
  AND started_at >= ?
  AND started_at <= ?
"""

# The explicit non-human worker sources a job match may claim. Anything
# else — the human-facing surfaces, a blank source, or an unknown tag —
# fails open: the session stays a top-level inbox row rather than being
# guessed onto a parent. Terminal-launch links need no source gate;
# they rest on the parent's own recorded session_id line, not on
# inference. "research-worker" is the deep_research runner's tag for
# its lane/synthesis runs (the canonical -p spawn contract); runs
# spawned before that tagging are 'cli' and honestly stay unlinked.
LINEAGE_WORKER_SOURCES = frozenset((
    "subagent",         # delegate_tool children
    "tool",             # sessions launched by a tool integration
    "kanban",           # kanban board workers
    "research-worker",  # deep_research lane/synthesis runs
))

# The exact first user message of the candidates — the prompt text a
# job dispatched — capped at LINEAGE_PROMPT_MAX_CHARS (the same cap a
# prompt file must fit under, so exact equality is decided on equal
# ground), batched and chunked past the host-parameter limit. The same
# ROW_NUMBER-selected row also yields length(content), the original
# character length before that cap: a message longer than the cap would
# otherwise come back as its truncated prefix, which a job prompt
# sitting exactly at the cap could falsely equal, so the caller rejects
# any first message whose original length exceeds the cap before its
# text is ever compared.
LINEAGE_FIRST_USER_SQL = """
SELECT session_id, preview, src_chars FROM (
  SELECT session_id, substr(content, 1, {chars}) AS preview,
         length(content) AS src_chars,
         ROW_NUMBER() OVER (
           PARTITION BY session_id ORDER BY timestamp ASC, id ASC
         ) AS rn
  FROM messages
  WHERE role = 'user'
    AND IFNULL(content, '') != ''
    AND content != '[SILENT]'
    AND session_id IN ({placeholders})
) WHERE rn = 1
"""

# Which of a set of session ids exist (non-hidden) in one DB — the
# existence check for job parents, and the resolver for terminal child
# ids. Batched and chunked like every other IN-list here.
LINEAGE_SESSION_ROWS_SQL = """
SELECT id, title, display_name, started_at, last_activity_at,
       ended_at, end_reason
FROM sessions
WHERE hidden = 0
  AND id IN ({placeholders})
"""


def _read_json_capped(path):
    """One small JSON file -> the parsed object, or None on any error.

    Real job artifacts are a few hundred bytes; anything missing,
    unreadable, undecodable, empty or past LINEAGE_JOB_JSON_BYTES is
    None, and the caller skips the job (its worker runs then stay
    unlinked)."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(LINEAGE_JOB_JSON_BYTES + 1)
    except OSError:
        return None
    if not raw or len(raw) > LINEAGE_JOB_JSON_BYTES:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _read_prompt(path):
    """One prompts/*.md -> its exact text, or None (missing/oversize).

    The read stops at cap+1 characters, so even a huge file is read
    boundedly, and any file longer than the cap is skipped rather than
    truncated — its equality against the equally capped first-user DB
    read could only ever be a prefix match. The DB side applies the
    mirror-image rule: a first message whose original length exceeds
    the cap is rejected before comparison (see
    LINEAGE_FIRST_USER_SQL). Every length here counts UTF-8
    characters, the same units as the substr() cap and the length()
    read on the DB side, so a multibyte prompt is judged by its true
    length. An empty prompt matches nothing either."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read(LINEAGE_PROMPT_MAX_CHARS + 1)
    except (OSError, UnicodeDecodeError):
        return None
    if not text or len(text) > LINEAGE_PROMPT_MAX_CHARS:
        return None
    return text


def _lineage_job_specs(dbs, profiles, now):
    """Durable delegate_research jobs -> one spec per confident job.

    A spec is {parent, worker, lo, hi, prompts}: the (profile, session
    id) the job runs for, the worker profile it dispatches to, its
    request/status time window padded with the documented skew, and
    its exact prompt texts. A job is skipped — its worker sessions
    then stay unlinked and keep their top-level rows — when its
    artifacts are malformed, its origin.hermes_home is not the home it
    was found under, its worker is not a discovered *other* profile,
    or its window lies entirely outside the product window. Every
    field is validated BEFORE any set or dict use (a worker_profile
    that is not a non-empty string is skipped, never allowed to reach
    the profiles set membership test), and one malformed job only
    skips itself: the rest of the pass keeps its links."""
    home_profile = {os.path.realpath(os.path.dirname(path)): p
                    for path, p in dbs}
    horizon = now - WINDOW_SECONDS - LINEAGE_SKEW_SECONDS
    specs = []
    for db_path, owner in dbs:
        base = os.path.join(os.path.dirname(db_path), RESEARCH_JOBS_DIR)
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            if not name.startswith("rj_"):
                continue
            try:
                spec = _lineage_job_spec(
                    os.path.join(base, name), owner, home_profile,
                    profiles, horizon)
            except Exception:
                continue  # one malformed job never discards the others
            if spec is not None:
                specs.append(spec)
    return specs


def _lineage_job_spec(jdir, owner, home_profile, profiles, horizon):
    """One job directory -> its spec, or None when it may not link.

    All the defensive validation lives here so a single bad artifact
    (unparseable JSON, a non-string worker_profile, an origin home that
    is not the home the job was found under) skips exactly this job."""
    req = _read_json_capped(os.path.join(jdir, "request.json"))
    status = _read_json_capped(os.path.join(jdir, "status.json"))
    if not isinstance(req, dict) or not isinstance(status, dict):
        return None
    origin = req.get("origin")
    if not isinstance(origin, dict):
        return None
    parent_id = origin.get("session_id")
    origin_home = origin.get("hermes_home")
    worker = req.get("worker_profile")
    created = req.get("created_at")
    if not (isinstance(parent_id, str)
            and SESSION_ID_RE.fullmatch(parent_id)):
        return None
    if not (isinstance(worker, str) and worker
            and worker in profiles and worker != owner):
        return None
    try:
        home = (os.path.realpath(origin_home)
                if isinstance(origin_home, str) else None)
    except (OSError, ValueError):
        home = None
    if home_profile.get(home) != owner:
        return None
    if not (isinstance(created, (int, float)) and created > 0):
        return None
    ends = [created]
    for key in ("updated_at", "completed_at"):
        val = status.get(key)
        if isinstance(val, (int, float)) and val > 0:
            ends.append(val)
    lo = created - LINEAGE_SKEW_SECONDS
    hi = max(ends) + LINEAGE_SKEW_SECONDS
    if hi < horizon:
        return None
    prompts = set()
    pdir = os.path.join(jdir, "prompts")
    try:
        pnames = sorted(os.listdir(pdir))
    except OSError:
        pnames = []
    for pname in pnames:
        text = _read_prompt(os.path.join(pdir, pname))
        if text:
            prompts.add(text)
    if not prompts:
        return None
    return {"parent": (owner, parent_id), "worker": worker,
            "lo": lo, "hi": hi, "prompts": prompts}


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


# ---- live tool activity (/s/<profile>/<id> + /feed) ------------------
# A tool call must be visible after its assistant tool_calls carrier is
# persisted and before its matching tool result row exists. Everything
# below is defensive by design: carriers are legacy JSON of several
# Hermes generations, so nothing here may ever raise into a page or a
# feed poll, and nothing unbounded may ever reach the response.

# Keys whose values are redacted at any depth of a parsed argument
# object (matched as substrings of the lowercased key, so
# "github_token" or "PASSWORD" both redact), plus the same words in
# textual "key=value" / "key: value" forms inside string values. Each
# keyword's own underscore is [_-]? in the patterns, so the hyphenated
# spelling ("x-api-key", "access-key") is the same keyword.
SECRET_KEY_WORDS = ("password", "passwd", "passphrase", "secret", "token",
                    "api_key", "apikey", "access_key", "auth", "authorization",
                    "cookie", "credential")
SECRET_KEY_RE = re.compile(
    "(?i)(?:%s)" % "|".join(w.replace("_", "[_-]?")
                            for w in SECRET_KEY_WORDS))
# The same words as a name's required SUFFIX — exactly the judgment the
# assignment pass below applies to a raw name, factored out so a
# percent-decoded parameter name is judged by the same rule.
SECRET_KEY_SUFFIX_RE = re.compile(
    "(?i)(?:%s)$" % "|".join(w.replace("_", "[_-]?")
                             for w in SECRET_KEY_WORDS))
REDACTED = "[REDACTED]"

# The one bounded redaction boundary every UI-exposed tool argument and
# tool-result detail passes through, in order (each pattern's output is
# opaque to the ones after it):
#
# 1. Authorization header values — the keyword plus its whole
#    credential, scheme word included in the match so "Authorization:
#    Bearer <token>" can never leave "<token>" (or mask only "Bearer")
#    behind. Handles quoting and the common delimiter characters.
# 2. Standalone bearer tokens with no keyword in front of them.
# 3. Credential-bearing URLs and DB URIs — scheme://user:pass@… keeps
#    the scheme and host shape, replaces the whole userinfo.
# 4. Key/value secret assignments, quoted or bare — the FULL value is
#    replaced (a quoted phrase like password: "hunter two" is one
#    match, not a masked first word plus a leaked remainder).
# 5. Percent-encoded parameter names — "Api%5FKey=…",
#    "access%2Dtoken=…", "%41pi%5fkey=…" — spell the keyword with
#    escapes the raw-name pass cannot see, so a name is judged again
#    after exactly one safe percent-decoding pass of the NAME ONLY;
#    the value is replaced whole, still exactly as written.
AUTH_VALUE_RE = re.compile(
    r"(?i)\b(authorization|proxy[-_]authorization)\b(\s*[=:]\s*)"
    r"(?:bearer|basic|digest|token|oauth|negotiate|hmac|mutual)[ \t]+"
    r"[^\s,;\"'<>)]+"
    r"|\b(authorization|proxy[-_]authorization)\b(\s*[=:]\s*)"
    r"[^\s,;\"'<>)]+")
BEARER_TOKEN_RE = re.compile(
    r"(?i)\b(bearer)[ \t]+[A-Za-z0-9._~+/\-=]{8,}")
URL_USERINFO_RE = re.compile(
    r"(?i)\b(?:[a-z][a-z0-9+.\-]*)://(?:[^\s/@:\[\"']+)?(?::[^\s/@\[\"']*)?@")
# Compound names count too ("github_token: …", "x-api-key: …",
# "SESSION_SECRET=…"), so the keyword may carry a run of identifier
# characters — underscores AND hyphens. The value may not be the
# redaction marker itself (keeps the pass idempotent and a following
# quote alive).
SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_-]*(?:%s))[\"']?([ \t]*[:=][ \t]*)"
    r"(?!\[REDACTED\])(\"[^\"]*\"|'[^']*'|[^\s,;&>]+)"
    % "|".join(w.replace("_", "[_-]?") for w in SECRET_KEY_WORDS))

# A parameter NAME that carries at least one percent escape, its "="
# and the value forms the assignment pass would have consumed — quoted
# phrase or bare run, exactly the same value charset, so pass 5's
# replacements match pass 4's shape. Only percent-bearing names ever
# match, so ordinary text ("status=open", "a=b", prose) is not even a
# candidate here, and a clean name like "redirect_uri" never blankets
# the nested parameters its value may hold.
ENCODED_PARAM_RE = re.compile(
    r"(?<![A-Za-z0-9_.~%-])([A-Za-z0-9_.~%-]*%[A-Za-z0-9_.~%-]*)="
    r"(\"[^\"]*\"|'[^']*'|[^\s,;&>]+)")


def _param_name_is_sensitive(name):
    """True when a URL/query/form parameter name is secret-bearing.

    The raw name counts exactly when the assignment pass above would
    have taken it (a secret keyword as the name's suffix). Otherwise
    the name gets ONE safe percent-decoding pass — the name only,
    never any value — and is judged again by the same suffix rule, so
    "Api%5FKey", "access%2Dtoken" and "%41pi%5fkey" all count while
    "redirect%5Furi" and "token%5Fcount" do not. Malformed escapes
    fail closed: whatever still holds an escape (or an undecodable
    byte) after that one pass is judged by the broader substring test
    AND by the most lenient further reading — bad escapes simply
    dropped, "%%5Fapi%%5Fkey" reading as "__api_key" — so a name that
    MIGHT decode to a keyword under any decoder redacts. Even a
    decoder error counts as sensitive. Never raises.
    """
    if SECRET_KEY_SUFFIX_RE.search(name):
        return True
    try:
        decoded = unquote(name)
    except Exception:
        return True
    if SECRET_KEY_SUFFIX_RE.search(decoded):
        return True
    if "%" in decoded or "\N{REPLACEMENT CHARACTER}" in decoded:
        if SECRET_KEY_RE.search(decoded):
            return True
        lenient = decoded.replace("%", "").replace(
            "\N{REPLACEMENT CHARACTER}", "")
        return bool(SECRET_KEY_SUFFIX_RE.search(lenient))
    return False


def _redact_encoded_params(text, depth=0):
    """Mask the values of percent-encoded sensitive parameter names.

    Pass 5 of the redaction boundary: each candidate name=value pair is
    judged by _param_name_is_sensitive(); a sensitive name keeps its
    encoded spelling and loses its whole value — quoted forms included
    — to REDACTED, with nothing decoded, normalized or reordered in the
    original text. A benign encoded name's value may itself carry
    nested parameters (a redirect target URL), so that value is
    re-scanned on its own, strictly shorter each level and capped at a
    few levels, instead of being blanket-consumed and hiding them.
    """
    if depth >= 4 or "%" not in text:
        return text
    out, last = [], 0
    for m in ENCODED_PARAM_RE.finditer(text):
        value = m.group(2)
        if _param_name_is_sensitive(m.group(1)):
            fixed = REDACTED
        else:
            fixed = _redact_encoded_params(value, depth + 1)
        if fixed == value:
            continue
        out.append(text[last:m.start(2)])
        out.append(fixed)
        last = m.end()
    if not out:
        return text
    out.append(text[last:])
    return "".join(out)


def redact_secret_text(text):
    """One string -> the same string with credential-shaped content
    masked, value-complete rather than first-token-only.

    Runs on every string that can reach a tool argument summary, a tool
    detail block, or a Discord error line: Authorization headers
    (keyword + scheme + full credential), bare bearer tokens,
    user:password URL/DB-URI userinfo, password/token/api-key style
    assignments with their full quoted or bare value, and the same
    assignments hiding behind a percent-encoded parameter name. Useful
    non-secret text (paths, commands, ordinary words) passes through.
    """
    if not text:
        return text

    def _auth_sub(m):
        # group layout alternates (keyword, sep) between the two arms
        keyword = m.group(1) or m.group(3)
        sep = m.group(2) or m.group(4)
        # The scheme word and the credential go together — keeping the
        # scheme would only hand a later pass a bare "Bearer <masked>"
        # fragment. One replacement, whole value.
        return keyword + sep + REDACTED

    text = AUTH_VALUE_RE.sub(_auth_sub, text)
    text = BEARER_TOKEN_RE.sub(
        lambda m: m.group(1) + " " + REDACTED, text)
    text = URL_USERINFO_RE.sub(
        lambda m: m.group(0).split("://", 1)[0] + "://" + REDACTED + "@",
        text)
    text = SECRET_ASSIGN_RE.sub(
        lambda m: m.group(1) + m.group(2) + REDACTED, text)
    text = _redact_encoded_params(text)
    return text


def redact_secrets(value):
    """Recursively redact secret-bearing keys of a parsed JSON value;
    lists and nested dicts are walked, everything else passes through."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = str(k)
            if SECRET_KEY_RE.search(key):
                out[key] = REDACTED
            else:
                out[key] = redact_secrets(v)
        return out
    if isinstance(value, list):
        return [redact_secrets(v) for v in value]
    if isinstance(value, str):
        return redact_secret_text(value)
    return value


def parse_tool_calls(raw):
    """Bounded tool_calls JSON -> [{name, args, ids}], never raising.

    Accepts both the OpenAI-ish shape ({"function": {"name",
    "arguments"}}) and flatter legacy shapes ({"name", "arguments"}).
    arguments may be a JSON string or an already-parsed object; ids
    collects every non-empty id-ish field a tool result could echo
    back (id, call_id, response_item_id). Malformed input yields [] —
    the page and the feed simply see no pending calls from it.
    """
    if not raw or not isinstance(raw, str):
        return []
    try:
        arr = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(arr, list):
        return []
    calls = []
    for c in arr[:TOOL_CALLS_MAX]:
        if not isinstance(c, dict):
            continue
        fn = c.get("function")
        name = ""
        args_raw = ""
        if isinstance(fn, dict):
            name = fn.get("name") or c.get("name") or ""
            a = fn.get("arguments", c.get("arguments"))
        else:
            name = c.get("name") or ""
            a = c.get("arguments")
        if isinstance(a, str):
            args_raw = a
        elif a is None:
            args_raw = ""
        else:
            try:
                args_raw = json.dumps(a)
            except (TypeError, ValueError):
                args_raw = ""
        if not isinstance(name, str):
            name = ""
        ids = []
        for key in ("id", "call_id", "response_item_id"):
            v = c.get(key)
            if isinstance(v, str) and v:
                ids.append(v)
        calls.append({"name": name, "args": args_raw, "ids": ids})
    return calls


def summarize_arguments(raw):
    """One call's raw arguments JSON -> a bounded, useful, redacted
    one-line summary ("workdir=/x task=Fix the…" style).

    Never emits the raw JSON: values are redacted (keyed and textual
    forms), trimmed, collapsed and hard-capped, so a whole prompt or a
    credential can never dump into the strip. Unparseable input falls
    back to the redacted, collapsed, capped text itself.
    """
    if not raw or not isinstance(raw, str):
        return ""
    obj = None
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        obj = None
    parts = []
    if isinstance(obj, dict):
        for k, v in list(redact_secrets(obj).items())[:6]:
            key = " ".join(str(k).split())
            if not key:
                continue
            if isinstance(v, dict):
                val = "{\N{HORIZONTAL ELLIPSIS}}"
            elif isinstance(v, list):
                val = "[\N{HORIZONTAL ELLIPSIS}]"
            elif v is None:
                val = "null"
            elif isinstance(v, bool):
                val = "true" if v else "false"
            elif isinstance(v, (int, float)):
                val = str(v)
            else:
                val = " ".join(str(v).split())
                if len(val) > ARGS_VALUE_CHARS:
                    val = val[:ARGS_VALUE_CHARS - 1].rstrip() \
                        + "\N{HORIZONTAL ELLIPSIS}"
            parts.append("%s=%s" % (key, val))
    text = " ".join(parts) if parts else " ".join(raw.split())
    text = sanitize_text(redact_secret_text(text))
    if len(text) > ARGS_SUMMARY_CHARS:
        text = text[:ARGS_SUMMARY_CHARS - 1].rstrip() \
            + "\N{HORIZONTAL ELLIPSIS}"
    return text


def activity_state_label(name, args_raw):
    """One unresolved call -> the truthful state label for the strip."""
    if name == "clarify":
        return "Waiting for your input"
    if name == "restart":
        return "Waiting for restart confirmation"
    if name == "process":
        action = None
        try:
            obj = json.loads(args_raw or "{}")
            if isinstance(obj, dict):
                action = obj.get("action")
        except (ValueError, TypeError):
            action = None
        if action == "wait":
            return "Waiting for process"
        return "Running process"
    if name in ("delegate_claude_agent", "delegate_cursor_agent"):
        return "Waiting for coding agent"
    if name == "delegate_task":
        return "Dispatching subagent"
    return "Running %s" % (name or "tool")


# The pre-first-output state of a live turn: shown from acceptance
# until the first assistant or tool row of THIS turn is persisted. The
# client's composer-side waiting row carries the same words, so the
# handoff between the two views reads as one continuous state.
WAITING_FIRST_RESPONSE = "Waiting for first response\N{HORIZONTAL ELLIPSIS}"


def session_lease_live(con, session_id, now):
    """Whether this session holds a live unexpired turn lease — either
    as the lease's parsed turn id (the usual continuation case) or as
    the conversation root itself. Never raises: a missing lease table
    just means not live."""
    try:
        for conversation_id, holder in con.execute(
                LEASES_LIVE_SQL, (now,)):
            if lease_session_id(conversation_id, holder) == session_id:
                return True
            # the root itself is also a live signal when it is the
            # session being viewed and the holder names a continuation
            if conversation_id == session_id:
                return True
    except sqlite3.Error:
        return False
    return False


def compute_activity(con, session_id, now, busy_job=False, busy_since=None):
    """One bounded activity snapshot for a session, never raising.

    Walks the newest ACTIVITY_MAX_ROWS active rows chronologically:
    every assistant tool_calls carrier after the last user message is
    the current turn, and each of its calls stays pending until a tool
    row echoes one of its id candidates back. The turn is scoped by two
    cuts, each keeping interrupted older turns from looming as
    "running" forever: rows after the last user message, and — for a
    composer turn this server accepted at busy_since — rows newer than
    that acceptance. The second cut is what makes a newly accepted
    turn truthfully pre-first-output even before hermes has persisted
    the turn's own user row: a historical answer or carrier predating
    the acceptance can neither satisfy the wait nor pose as the turn's
    pending work. Tool results — wherever they sit in the window —
    resolve their carriers. With no pending call but a live turn (valid
    lease or a composer job in this server), the weakest truthful
    state is derived from the turn's newest row instead. The snapshot
    is recomputed on its own, independent of the feed cursor.
    """
    act = {"active": bool(busy_job), "lease": False, "state": "",
           "pending": [], "pending_count": 0, "names": []}
    try:
        rows = con.execute(
            ACTIVITY_SQL, (session_id, ACTIVITY_MAX_ROWS)).fetchall()
    except sqlite3.Error:
        rows = []
    rows.reverse()  # fetched newest-first; walk chronologically
    lease = session_lease_live(con, session_id, now)
    act["lease"] = lease
    act["active"] = bool(busy_job) or lease

    # The accepted turn's own rows. Without a floor (a lease-driven
    # turn, or a plain snapshot) every row is in scope; with one, only
    # rows strictly newer than the acceptance — the turn's user row,
    # once hermes persists it, is newer too, so it re-tightens the
    # scope exactly as the client-side cursor does.
    turn = rows if busy_since is None else \
        [r for r in rows if (r[7] or 0) > busy_since]

    result_ids = set()
    last_user = -1
    for row in rows:
        if row[0] == "tool":
            call_id = row[2]
            if isinstance(call_id, str) and call_id:
                result_ids.add(call_id)
    for i, row in enumerate(turn):
        if row[0] == "user":
            last_user = i

    pending, seen = [], set()
    for row in turn[last_user + 1:]:
        if row[0] != "assistant":
            continue
        tool_calls = row[5]
        if not isinstance(tool_calls, str) or not tool_calls:
            continue
        for call in parse_tool_calls(tool_calls):
            key = call["ids"][0] if call["ids"] else call["name"]
            if not key or key in seen:
                continue
            if any(cid in result_ids for cid in call["ids"]):
                continue  # its result row exists — completed history
            seen.add(key)
            pending.append({
                "name": call["name"] or "tool",
                "args": summarize_arguments(call["args"]),
                "state": activity_state_label(call["name"],
                                              call["args"]),
            })

    if pending:
        act["pending"] = pending
        act["pending_count"] = len(pending)
        act["names"] = [p["name"] for p in pending]
        act["state"] = pending[-1]["state"]
        return act

    if act["active"]:
        newest = turn[-1] if turn else None
        if newest is None or newest[0] == "user":
            # Pre-first-output of the live turn: no assistant or tool
            # row has landed after the turn's floor — its own user row
            # when one exists, else the acceptance a composer job
            # recorded. A historical answer sits before that floor and
            # can never satisfy the new turn — the same cut already
            # scoped pending calls above.
            act["state"] = WAITING_FIRST_RESPONSE
        elif newest[0] == "tool":
            act["state"] = "Thinking after %s" % (newest[1] or "tool")
        elif newest[0] == "assistant":
            tool_calls = newest[5] if isinstance(newest[5], str) else ""
            calls = parse_tool_calls(tool_calls) if tool_calls else []
            if tool_calls and calls and all(
                    any(cid in result_ids for cid in c["ids"])
                    for c in calls):
                act["state"] = "Processing tool results"
            else:
                act["state"] = "Working"
        else:
            act["state"] = "Working"
    return act


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
        activity = compute_activity(con, session_id, time.time(),
                                    busy_job, busy_since)
    finally:
        con.close()
    return {"items": chat_items(chat_messages(rows)), "last_id": last_id,
            "subagents": subagents, "activity": activity,
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


def _discord_wait_turn():
    """Enforce the shared request pacing: at least
    DISCORD_MIN_REQUEST_GAP since the last request anywhere in this
    process, plus any 429 retry_after cooldown. Caller must hold
    _discord_lock; updates the last-request stamp on exit."""
    global _discord_last_request
    now = time.monotonic()
    wait = max(_discord_last_request + DISCORD_MIN_REQUEST_GAP,
               _discord_cooldown_until) - now
    if wait > 0:
        time.sleep(wait)
    _discord_last_request = time.monotonic()


def _discord_note_response(status, obj):
    """Fold a response's rate-limit signal into the shared cooldown.
    A 429's retry_after silences every caller for that long — the sync
    never hot-loops against a limited bucket."""
    global _discord_cooldown_until
    if status != 429 or not isinstance(obj, dict):
        return
    try:
        retry_after = float(obj.get("retry_after") or 0)
    except (TypeError, ValueError):
        retry_after = 0
    # Honor Discord's retry_after in full — long-lived buckets (hours)
    # are real — but keep a defensive ceiling so a corrupt or hostile
    # value can't silence the sync forever: one day.
    retry_after = min(max(retry_after, 1.0), 86400.0)
    until = time.monotonic() + retry_after
    if until > _discord_cooldown_until:
        _discord_cooldown_until = until


def discord_error_string(status, obj):
    """Bounded, safe error text for a failed Discord call: the HTTP
    status plus Discord's numeric code/message, truncated, with
    token-shaped runs masked. Request headers and bodies never appear
    here, so this string is safe to return to a client or log."""
    detail = ""
    if isinstance(obj, dict):
        code = obj.get("code")
        message = obj.get("message")
        if isinstance(message, str) and message:
            detail = " " + " ".join(message.split())
            if isinstance(code, int):
                detail = " (code %d)%s" % (code, detail)
        elif isinstance(code, int):
            detail = " (code %d)" % code
    text = "HTTP %d%s" % (status, detail)
    text = redact_secret_text(TOKENISH_RE.sub(REDACTED, text))
    return text[:160]


def discord_request(method, path, token, payload=None):
    """One Discord API call -> (status, parsed_body, error).

    error is None on success (2xx); otherwise a bounded safe string
    (discord_error_string) and status may be 0 when the request never
    reached Discord. The body read is capped at DISCORD_MAX_BODY_BYTES
    +1; larger is an error. All requests serialize through _discord_lock
    with shared pacing/cooldown. The token appears only in the
    Authorization header of this one request object."""
    url = DISCORD_API_BASE + path
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bot " + token)
    req.add_header("User-Agent", DISCORD_USER_AGENT)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with _discord_lock:
        _discord_wait_turn()
        raw = b""
        try:
            with urllib.request.urlopen(
                    req, timeout=DISCORD_TIMEOUT_SECONDS) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                raw = resp.read(DISCORD_MAX_BODY_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code or 0
            try:
                raw = exc.read(DISCORD_MAX_BODY_BYTES + 1)
            except OSError:
                raw = b""
        except (OSError, ValueError) as exc:
            # Never include the exception text: it could echo the URL or
            # environment. The class name is enough.
            return 0, None, "request failed (%s)" % type(exc).__name__
        if len(raw) > DISCORD_MAX_BODY_BYTES:
            obj = None
        elif raw:
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                obj = None
        else:
            obj = None
        _discord_note_response(status, obj)
    if len(raw) > DISCORD_MAX_BODY_BYTES:
        return status, None, "response too large (HTTP %d)" % status
    if raw and obj is None:
        return status, None, "unparseable response body (HTTP %d)" % status
    if status < 200 or status >= 300:
        return status, obj, discord_error_string(status, obj)
    return status, obj, None


def fetch_active_thread_ids(token):
    """(active_id_set, error) from Discord: every active thread across
    every guild the bot is in. All-or-nothing — any failed request or
    unexpected shape yields (None, safe_error), and the caller must
    then not treat absence as archived. Requests run sequentially
    (each one paced by the shared lock inside discord_request)."""
    status, guilds, err = discord_request("GET", "/users/@me/guilds", token)
    if err is not None:
        return None, "guilds: %s" % err
    if not isinstance(guilds, list):
        return None, "guilds: unexpected shape (HTTP %d)" % status
    active = set()
    for guild in guilds:
        if not isinstance(guild, dict):
            return None, "guilds: unexpected entry"
        gid = guild.get("id")
        if not isinstance(gid, str) or not SNOWFLAKE_RE.fullmatch(gid):
            return None, "guilds: unexpected id"
        status, obj, err = discord_request(
            "GET", "/guilds/%s/threads/active" % gid, token)
        if err is not None:
            return None, "active threads: %s" % err
        if not isinstance(obj, dict) or not isinstance(obj.get("threads"),
                                                      list):
            return None, "active threads: unexpected shape (HTTP %d)" \
                % status
        for thread in obj["threads"]:
            if not isinstance(thread, dict):
                return None, "active threads: unexpected entry"
            tid = thread.get("id")
            if not isinstance(tid, str) or not SNOWFLAKE_RE.fullmatch(tid):
                return None, "active threads: unexpected id"
            active.add(tid)
    return active, None


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


# Structural bounds for codex_message_items parsing, all enforced by
# codex_commentary_text itself so direct callers get the same contract
# the SQL projection gives the page and feed. The persisted production
# shape is a small list of flat message items (item dict -> content
# list -> block dict -> text string, depth 4); everything past these
# caps is rejected whole.
CODEX_ITEMS_MAX_ITEMS = 32   # top-level message items in one value
CODEX_ITEM_MAX_BLOCKS = 32   # content blocks in one message item
CODEX_ITEMS_MAX_DEPTH = 6    # JSON nesting depth (legitimate max: 4)


def _codex_depth_ok(data):
    """True when the parsed JSON nests no deeper than the cap.

    Iterative on purpose — no recursion that a deep blob could turn
    into a RecursionError, and a hard node budget (32k) so even a
    wide-but-shallow blob costs bounded work. Depth counts container
    levels: the top-level value is 0, its members 1, and so on.
    """
    seen = 0
    stack = [(data, 0)]
    while stack:
        node, depth = stack.pop()
        seen += 1
        if seen > 32000 or depth > CODEX_ITEMS_MAX_DEPTH:
            return False
        if isinstance(node, dict):
            stack.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list):
            stack.extend((v, depth + 1) for v in node)
    return True


def codex_commentary_text(raw):
    """Bounded codex_message_items JSON -> visible assistant commentary.

    A Codex tool-call assistant row keeps content='' and stores its
    narration in codex_message_items instead; this recovers that visible
    text — and only that. The persisted shape (agent/codex_responses_
    adapter.py) is message items whose type is "message" and role
    "assistant", carrying a normalized "status" always and a "phase"
    exactly when the run wrote one. Visible narration is ONLY the
    complete commentary phase: status must be "completed" (never
    "in_progress" or "incomplete") and phase exactly "commentary" —
    phase-less items, "analysis", "reasoning", "final",
    "final_answer", failed and cancelled variants all yield "". Only
    content blocks typed output_text/text contribute, each solely
    through its string "text" field; reasoning items, function calls
    and their arguments, tool results, bare-string content and
    arbitrary nested strings match none of those shapes, so they can
    never surface.

    Input is bounded before parsing: anything longer than
    CODEX_ITEMS_MAX_CHARS is rejected whole (never sliced into an
    accepted truncated prefix), as is anything structurally past the
    item-count, block-count or nesting-depth caps. Malformed JSON
    (and JSON deep enough that json.loads itself raises) yields ""
    and never propagates. Recovered text is sanitized of lone
    surrogates and clamped to CHAT_TEXT_CHARS, the same cap content
    itself gets in SQL.
    """
    if not isinstance(raw, str) or not raw:
        return ""
    if len(raw) > CODEX_ITEMS_MAX_CHARS:
        return ""
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return ""
    if not _codex_depth_ok(data):
        return ""
    if isinstance(data, dict):
        items = (data,)
    elif isinstance(data, list):
        if len(data) > CODEX_ITEMS_MAX_ITEMS:
            return ""
        items = data
    else:
        return ""
    texts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        if item.get("status") != "completed":
            continue
        if item.get("phase") != "commentary":
            continue
        blocks = item.get("content")
        if not isinstance(blocks, list) \
                or len(blocks) > CODEX_ITEM_MAX_BLOCKS:
            continue
        parts = [b["text"] for b in blocks
                 if isinstance(b, dict)
                 and b.get("type") in ("output_text", "text")
                 and isinstance(b.get("text"), str)]
        if parts:
            texts.append("\n".join(parts))
    if not texts:
        return ""
    return sanitize_text("\n\n".join(texts))[:CHAT_TEXT_CHARS]


def chat_messages(rows):
    """Transcript rows -> renderable messages.

    Rows with nothing to show are dropped: the '[SILENT]' marker (same
    rule the list page uses for previews) and assistant rows that end up
    with no text at all. Tool rows keep the tool_name plus a short
    detail slice of the already substr-capped content. Every kept
    message carries its DB row id — the feed's cursor and per-entry id.

    An assistant row whose content is empty is the Codex commentary
    shape: its narration sits in codex_message_items, so exactly then
    (never for a row that already has content, which stays the sole
    authority) the trailing fallback column is parsed defensively and
    its recovered text renders as a normal agent message. A carrier
    with nothing recoverable still disappears, so truly consecutive
    tool rows keep collapsing into one group.

    Rows may carry the lifecycle columns (tool_calls, tool_call_id,
    finish_reason) and the Codex commentary fallback after the display
    fields; display rules ignore the lifecycle ones, and the activity
    snapshot is what actually consumes them.
    """
    out = []
    for row in rows:
        role, tool_name, ts, row_id, content = row[:5]
        text = content if isinstance(content, str) else ""
        if role in ("user", "assistant"):
            if text == "[SILENT]":
                continue
            body = clean_preview(text)
            if not body and role == "assistant" and not text:
                # Codex commentary fallback, column 8: only an
                # assistant row with empty content reaches here.
                codex = row[8] if len(row) > 8 else ""
                body = clean_preview(codex_commentary_text(codex))
            if body:
                out.append({"kind": "text", "role": role, "ts": ts,
                            "id": row_id, "text": body})
        elif role == "tool":
            # Tool-result details are UI-exposed tool output: the same
            # redaction boundary as argument summaries, applied BEFORE
            # the display slice so a credential can never survive at
            # the tail of the clamp, and sanitized so a lone surrogate
            # cannot kill the page encoding.
            detail = sanitize_text(redact_secret_text(text))
            out.append({"kind": "tool", "ts": ts, "id": row_id,
                        "tool": tool_name or "tool",
                        "detail": detail[:CHAT_DETAIL_CHARS].strip()})
    return out


def chat_items(msgs):
    """Renderable messages -> the final list the page draws.

    Every maximal run of consecutive tool messages collapses into ONE
    {"kind": "tools", "items": [...]} group; text messages pass through
    unchanged. Grouping runs after chat_messages() has dropped empty
    rows, so tools separated only by an empty assistant carrier merge
    into the same group. The empty case is [] and only []. A group's id
    is its newest row's id (items are chronological), so the feed cursor
    advances past the whole group, and first_id is its oldest row's id —
    the seam identity a delta poll's client-side merge matches (see
    load_feed) so one run split across polls still renders as one
    group.
    """
    out = []
    for m in msgs:
        if m["kind"] == "tool":
            if out and out[-1]["kind"] == "tools":
                out[-1]["items"].append(m)
                out[-1]["id"] = m["id"]
            else:
                out.append({"kind": "tools", "items": [m],
                            "id": m["id"], "first_id": m["id"]})
        else:
            out.append(m)
    return out


def fmt_time(ts):
    """Server-local time, ISO-shaped."""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def fmt_short(ts):
    """Compact date-time for message headers and header time ranges."""
    return datetime.fromtimestamp(ts).strftime("%d %b %H:%M")


def fmt_hhmm(ts):
    """Hour:minute only — the hover gutter of a continuation message."""
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def fmt_rel(now, ts):
    """Short relative age; kept in step with the client-side version."""
    s = int(now - ts)
    if s < 15:
        return "just now"
    if s < 60:
        return "%ds ago" % s
    if s < 3600:
        return "%dm ago" % (s // 60)
    if s < 86400:
        return "%dh ago" % (s // 3600)
    return "%dd ago" % (s // 86400)


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

/* ---- subagent children (embed) ---------------------------------------- */
.subagents {
  margin: 12px 48px 0 72px; padding: 8px 0 6px;
  background: var(--embed); border-radius: 4px;
  border-left: 4px solid #4e5058;
}
.sa-head {
  margin: 0; padding: 2px 12px 7px;
  font-size: 11px; font-weight: 700; letter-spacing: 0.04em;
  text-transform: uppercase; color: var(--muted);
}
.sa-count {
  margin-left: 4px; font-weight: 600; color: var(--ink-2);
  font-variant-numeric: tabular-nums;
}
.sa-list { list-style: none; margin: 0; padding: 0; display: grid; }
.sa-link {
  display: flex; align-items: flex-start; gap: 9px;
  padding: 5px 12px; text-decoration: none; color: inherit;
}
.sa-link:hover { background: rgba(78,80,88,0.30); }
.sa-link:focus-visible { outline: 2px solid var(--blurple); outline-offset: -2px; }
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
.sa-label {
  display: -webkit-box; -webkit-box-orient: vertical;
  -webkit-line-clamp: 2; line-clamp: 2; overflow: hidden;
  font-size: 13px; line-height: 1.4; color: var(--ink-2);
}
.sa-meta {
  display: block; margin-top: 1px; font-size: 11px;
  color: var(--faint); font-variant-numeric: tabular-nums;
}

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
$closed_banner$subagents
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
</main>

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

  // The direct-children section rides every poll: swap it in place so
  // a child dispatched while the page is open appears without a
  // reload. It lives above the transcript, so replacing it never moves
  // the newest message and never touches the typing row or the cursor.
  function applySubagents(sub) {
    if (!sub || typeof sub !== "object") return;
    var html = sub.html || "";
    var current = document.getElementById("subagents");
    if (!html) {
      if (current && current.parentNode) {
        current.parentNode.removeChild(current);
      }
      return;
    }
    if (current) {
      current.insertAdjacentHTML("beforebegin", html);
      if (current.parentNode) current.parentNode.removeChild(current);
    } else if (closedBanner) {
      closedBanner.insertAdjacentHTML("afterend", html);
    } else if (pad) {
      pad.insertAdjacentHTML("afterbegin", html);
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

  // Land near the newest message, then sync the button with reality.
  if (scroller) scroller.scrollTop = scroller.scrollHeight;
  autosize();
  syncComposerVar();
  updateJump();
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
        if key in ("incomplete", "closed") and not items:
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

    return (
        '<li class="tool-group" data-first-id="%d"><details class="tg">'
        '<summary class="tg-sum">'
        '<span class="tg-count">%s</span>'
        '<span class="tg-chips">%s</span>'
        '<span class="tg-when" title="%s">%s</span>'
        '</summary><ol class="tg-list">%s</ol>'
        '</details></li>\n'
        % (tools[0]["id"], esc(label), "".join(chips), esc(span_full),
           esc(span), "".join(rows)))


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
            '<li class="msg %s%s">'
            '<span class="msg-gutter">'
            '<span class="mtime" title="%s">%s</span></span>'
            '<div class="msg-body"><p class="text">%s</p></div></li>\n'
            % (side, cont, esc(fmt_time(it["ts"])),
               esc(fmt_hhmm(it["ts"])), esc(it["text"])))
    # Letter badge with the optional avatar image layered on top; when
    # the file is missing the img never renders, and when it fails
    # mid-load the error listener hides it — the letter always shows.
    return (
        '<li class="msg %s">'
        '<span class="avatar" title="%s">'
        '<span aria-hidden="true">%s</span>%s</span>'
        '<div class="msg-body">'
        '<div class="msg-head"><span class="msg-author">%s</span>'
        '<span class="mtime" title="%s">%s</span></div>'
        '<p class="text">%s</p></div></li>\n'
        % (side, esc(av_title),
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


# Compact factual labels for a child's end state; the raw end_reason
# rides along in the row's title tooltip instead of the row itself.
SUBAGENT_STATE_LABELS = {
    "running": "Running",
    "done": "Done",
    "interrupted": "Interrupted",
    "failed": "Failed",
    "ended": "Ended",
}


def subagent_state(ended_at, end_reason):
    """(state key) for one child from its stored end fields. agent_close
    and a clean cli_close are the two clean finishes (Done) — a worker
    CLI run that closed on its own did finish; still-open children are
    Running; everything else is classified weakly but safely — the
    label never claims more than the reason suggests, and unknown
    reasons just say Ended."""
    if ended_at is None:
        return "running"
    reason = (end_reason or "").strip().lower()
    if reason in ("agent_close", "cli_close"):
        return "done"
    if any(k in reason for k in ("interrupt", "cancel", "suspend")):
        return "interrupted"
    if any(k in reason for k in ("error", "fail", "timeout", "crash",
                                 "abort")):
        return "failed"
    return "ended"


def render_subagents(now, profile, children):
    """The direct-children section for a conversation page ("" when
    there are none, so the page carries no trace of it).

    A nested task list inside the chat — not chat bubbles, not an ops
    table: one slim clickable row per child with a status dot, the goal
    label clamped to two lines, and the child's profile identity, end
    state and relative last-activity time. Every child carries its own
    profile, so a cross-profile child links into its own profile's DB
    and names its own persona — the parent's profile is only the
    fallback. The feed ships this exact markup on every poll, which is
    how an open page discovers newly dispatched children."""
    esc = html.escape
    if not children:
        return ""  # nothing at all — no heading, no empty box
    rows = []
    for c in children:
        state = subagent_state(c["ended"], c["end_reason"])
        label = str(c["label"] or c["id"])
        cprofile = c.get("profile") or profile
        ident = profile_identity(cprofile)
        url = "/s/%s/%s" % (quote(cprofile, safe=""),
                            quote(str(c["id"]), safe=""))
        tip = str(c["id"])
        if c["end_reason"]:
            tip = "%s \N{BULLET} ended: %s" % (tip, c["end_reason"])
        meta = "%s \N{BULLET} %s" % (ident["label"],
                                     SUBAGENT_STATE_LABELS[state])
        if c["last"]:
            meta = "%s \N{BULLET} %s" % (meta, fmt_rel(now, c["last"]))
        rows.append(
            '<li class="sa-item">'
            '<a class="sa-link" href="%s" title="%s">'
            '<span class="sa-dot sa-%s" aria-hidden="true"></span>'
            '<span class="sa-body">'
            '<span class="sa-label">%s</span>'
            '<span class="sa-meta">%s</span>'
            '</span></a></li>\n'
            % (esc(url), esc(tip), state, esc(label), esc(meta)))
    return (
        '<section class="subagents" id="subagents">\n'
        '  <h2 class="sa-head">Sub-agents'
        ' <span class="sa-count">%d</span></h2>\n'
        '  <ul class="sa-list">\n%s  </ul>\n'
        '</section>\n'
        % (len(children), "".join(rows)))


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
    items = chat_items(chat_messages(chat["rows"]))
    # The owning profile's identity drives the header pill label, every
    # agent bubble's badge and the typing row — user bubbles stay You.
    ident = profile_identity(chat["profile"])

    parts = []
    prev_side = None
    for it in items:
        if it["kind"] == "text":
            side = "from-user" if it["role"] == "user" else "from-agent"
            # a run of messages from one sender keeps its own rhythm
            cont = " cont" if side == prev_side else ""
            prev_side = side
            parts.append(render_chat_text(it, cont, ident))
        else:
            prev_side = None
            parts.append(render_tool_group(it["items"]))

    when = fmt_short(chat["started"]) + " \N{EN DASH} " + \
        fmt_short(chat["last"])
    when_full = fmt_time(chat["started"]) + " \N{EN DASH} " + \
        fmt_time(chat["last"])
    now = time.time()

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
        # Direct subagent children sit just under the header; ""
        # (nothing at all) when the session has none.
        subagents=render_subagents(now, chat["profile"],
                                   chat["subagents"]),
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
        subagents="",
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
            # Direct subagent children, re-rendered on every poll so an
            # open page discovers newly dispatched children without a
            # reload. Backwards-compatible addition: older clients just
            # ignore it. ids carry the structured state; html is the
            # exact section markup the page renders ("" when none).
            "subagents": {
                "count": len(feed["subagents"]),
                "ids": [c["id"] for c in feed["subagents"]],
                "html": render_subagents(time.time(), profile,
                                         feed["subagents"]),
            },
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


def _is_loopback(host):
    """True when host binds a loopback interface only (this machine).

    An empty host is a wildcard bind (every interface), NOT loopback —
    only the explicit names count: "localhost", 127.0.0.0/8, ::1."""
    name = str(host or "").strip()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name.strip("[]")).is_loopback
    except ValueError:
        return False  # a hostname, wildcard, or non-loopback address


# ---- trusted Host and origin binding ---------------------------------
# DNS-rebinding defense: a request's own Host header proves nothing,
# because the connection arriving on our socket may be an attacker
# domain rebound to our address. Every HTML page this server emits
# carries the CSRF token and every state-changing route accepts it, so
# both token-bearing pages and POSTs are refused unless the Host names
# an address this server deliberately serves. The default trusted set
# is derived from the configured bind address; Forwarded /
# X-Forwarded-* are never consulted — without an explicitly trusted
# proxy those headers are client-controlled, and this server has no
# proxy-trust mechanism at all.

LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
WILDCARD_BINDS = ("", "*", "::", "0.0.0.0")
# One hostname label: letters/digits/hyphen/underscore, no leading or
# trailing hyphen; dots join labels. Anything else a Host header might
# carry is refused before it can match anything.
HOSTNAME_RE = re.compile(
    r"^[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?"
    r"(?:\.[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?)*$")

# The themed 421 page: Host refused before anything token-bearing or
# state-changing is considered.
MISDIRECTED_HTML = (
    "<!DOCTYPE html>\n<html>\n<head>\n<meta charset=\"utf-8\">\n"
    "<title>Misdirected request</title>\n</head>\n<body>\n"
    "<h1>This address is not served here.</h1>\n"
    "<p>The request named a host this server was not configured to "
    "serve.</p>\n</body>\n</html>\n").encode("utf-8")


def _normalize_host(value):
    """Host header / bind spelling -> one comparable host string, or
    None.

    Strips the port (an IPv6 literal only in its bracketed form, per
    RFC 7230 — a bare unbracketed one is refused), lowercases, drops
    one trailing dot, and canonicalizes IP literals through
    ipaddress so ::1, [::1] and 0:0:0:0:0:0:0:1 all compare equal.
    None means the value is not a host this parser accepts — garbage,
    whitespace, delimiters — and can never match a trusted entry."""
    if not isinstance(value, str):
        return None
    host = value.strip()
    if not host:
        return None
    if host.startswith("["):
        end = host.find("]")
        if end == -1:
            return None
        name = host[1:end]
        rest = host[end + 1:]
        if rest and not rest.startswith(":"):
            return None
    else:
        host_part, sep, port_part = host.partition(":")
        if sep:
            # An explicit port: exactly one, digits only. Anything else
            # where the port belongs — including a second colon, i.e. a
            # bare unbracketed IPv6 literal — is refused rather than
            # silently truncated to its first segment.
            if ":" in port_part or not port_part.isdigit():
                return None
        name = host_part
    name = name.strip().rstrip(".").lower()
    if not name or any(ch.isspace() or ch in '/\\?#@,%"' for ch in name):
        return None
    try:
        return str(ipaddress.ip_address(name))
    except ValueError:
        return name if HOSTNAME_RE.fullmatch(name) else None


def _local_interface_ips():
    """Best-effort set of this machine's own interface addresses.

    Used only to size the default trusted set for a wildcard bind —
    which genuinely answers on every one of them. Any failure (no
    resolver, odd platform) yields an empty set: the trusted set stays
    smaller and access uses a name that is listed."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            raw = info[4][0].split("%", 1)[0]
            try:
                ips.add(str(ipaddress.ip_address(raw)))
            except ValueError:
                pass
    except OSError:
        pass
    return ips


def _default_trusted_hosts(bind_host):
    """Trusted Host set for one configured bind address.

    A loopback bind (the default) trusts the loopback spellings —
    localhost, 127.0.0.1, ::1 — since any of them may be how the user
    reaches the socket and none can name another machine. A wildcard
    bind answers on every interface, so it also trusts this machine's
    own interface addresses. An explicit non-loopback address trusts
    exactly itself: binding a LAN IP is a deliberate act, and reaching
    that socket under any other name needs an explicit --trusted-host
    entry."""
    name = _normalize_host(bind_host)
    if name is None or name in WILDCARD_BINDS:
        return set(LOOPBACK_HOSTS) | _local_interface_ips()
    if name == "localhost":
        return set(LOOPBACK_HOSTS)
    try:
        if ipaddress.ip_address(name).is_loopback:
            return set(LOOPBACK_HOSTS) | {name}
    except ValueError:
        pass
    return {name}


def _server_trusted_hosts(server):
    """The trusted Host set in force on one HTTP server instance.

    main() pins the CLI-derived set (bind-derived defaults plus any
    explicit --trusted-host entries) onto the instance; a
    directly-constructed server — as the tests build — derives the set
    from its own bound address, cached on the instance after the first
    ask so a request never re-runs interface enumeration."""
    pinned = getattr(server, "trusted_hosts", None)
    if pinned is not None:
        return pinned
    cached = getattr(server, "_trusted_hosts_cache", None)
    if cached is None:
        try:
            bind = server.server_address[0]
        except (AttributeError, IndexError):
            bind = "127.0.0.1"
        cached = _default_trusted_hosts(bind)
        try:
            server._trusted_hosts_cache = cached
        except Exception:
            pass
    return cached


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
