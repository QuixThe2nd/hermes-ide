"""Cross-profile lineage discovery for mission-control: research-job
artifact readers and the job-spec matching that links worker sessions
back to their origin session.

Moved byte-identical from server.py. The DB-touching passes
(_lineage_lookup, _lineage_job_children, _lineage_terminal_claims,
build_lineage, lineage_index) stay in the facade: they connect through
_connect_db/discover_dbs, which read the module-seam globals.
"""

import json
import os
import re

from plugins.mission_control.queries import SESSION_ID_RE, WINDOW_SECONDS
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
    "kanban",           # historical kanban board workers
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
