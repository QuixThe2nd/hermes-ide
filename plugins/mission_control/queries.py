"""SQL constants and pure per-connection query helpers for
mission-control: last-line/tool projections, lease lookups, newest-event
classification, chat/feed SQL and the chain variants.

Moved byte-identical from server.py. The *_CHAIN_SQL constants the
tests patch as seams (CHAT_PAGE_CHAIN_SQL, FEED_AFTER_CHAIN_SQL) stay
defined in the server facade, derived from these base statements.
"""

import re
import sqlite3

from hermes_state import SessionDB

from plugins.mission_control.redaction import (
    clean_preview,
    clamp_label,
)

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


# Safe session-id characters: plain safe characters only — no
# traversal, no quotes, no wildcards worth worrying about. Shared by
# the lease parser here, the lineage matcher and the facade's route
# validation.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


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


def mark_your_turn(rows, waits):
    """Move the open conversations waiting on the human into Your turn.

    Membership is exactly the two ways a chat stops being the agent's
    turn while staying open (closed still wins here — the projected
    tip's ended_at or the archived flag, exactly as load_sessions
    decided; those rows are never touched): an explicit wait the core
    names in the batch waiting-status answer — an API-run clarify, a
    native gateway clarify prompt, or a restart confirmation, all one
    surface to this server — or an idle chat whose newest active event
    is a plain assistant answer (classify_session's completed rule,
    so those rows move over instead of resting mislabeled). An
    explicit wait outranks a live lease or composer job AND needs
    neither: whatever is parked on the human's answer, the row is the
    human's to move, lease or no lease.

    ``waits`` is the waiting_session_ids(dbs, profiles) map of one
    refresh. Fail closed: a profile absent from the map (no key,
    unreachable core, error) promotes nothing — its rows keep the
    sections they already had. Idle rows still classify from the
    newest-event tuple with no per-session HTTP at all.
    """
    for r in rows:
        if r["state"] == "closed":
            continue
        waiting = waits.get(r["profile"])
        if waiting is not None and r["id"] in waiting:
            r["state"] = "your_turn"
        elif r["state"] == "completed":
            r["state"] = "your_turn"


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
