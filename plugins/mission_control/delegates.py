"""Delegate/sub-agent dispatch cards and their defensive parsers for
mission-control: the carrier-scanning JSON parsers, delegation loading,
card state/dom keys/revisions, argument summaries and activity labels.

Moved byte-identical from server.py. delegate_cards() itself stays in
the facade (it resolves profile homes and spawn-receipt watch URLs
against the module-seam globals).
"""

import json
import re
import sqlite3

from plugins.mission_control.redaction import (
    ARGS_SUMMARY_CHARS,
    DELEGATE_SPAWN_NAMES,
    ARGS_VALUE_CHARS,
    TOOL_CALLS_MAX,
    clamp_label,
    parse_tool_calls,
    redact_secrets,
    redact_secret_text,
    sanitize_text,
)
# delegate_agent actions that manage existing work instead of
# dispatching it; they never produce cards.
DELEGATE_CONTROL_ACTIONS = frozenset(("list", "steer", "stop"))

# The assistant carriers that may hold delegate calls, oldest first —
# over the conversation's whole canonical chain (a dispatch
# mid-conversation hangs off whichever member made the call). Bounded
# twice: LIKE selects only delegate-bearing rows, LIMIT caps even a
# pathological session, and the tool_calls slice is sized so a real
# batch call's task list usually parses whole (truncation past it is
# still handled — the carrier recovery below reads the surviving
# prefix, just with less label detail).
DELEGATE_CALLS_CHAIN_SQL = """
SELECT id, session_id, timestamp,
       substr(IFNULL(tool_calls, ''), 1, {chars})
FROM messages
WHERE session_id IN ({placeholders})
  AND role = 'assistant'
  AND tool_calls LIKE '%delegate%'
ORDER BY timestamp ASC, id ASC
LIMIT ?
"""
DELEGATE_CALLS_MAX = 40
DELEGATE_CALLS_CHARS = 16384

# The delegate tool result rows: their call ids drive result-row
# suppression and the no-lineage state fallback (a returned synchronous
# call reads Done). Bounded by the same per-conversation delegate count.
DELEGATE_RESULTS_CHAIN_SQL = """
SELECT id, tool_name, tool_call_id, timestamp
FROM messages
WHERE session_id IN ({placeholders})
  AND role = 'tool'
  AND tool_name IN ('delegate_agent', 'delegate_task',
                    'delegate_claude_agent', 'delegate_cursor_agent')
ORDER BY id ASC
LIMIT ?
"""
DELEGATE_RESULTS_MAX = 120

# Async delegations dispatched by this conversation, oldest first — the
# durable record a background delegate_agent spawn leaves behind (the
# parent is whichever chain member made the call). The task_json slice
# stays bounded; a missing table (older DBs, fixtures) degrades to no
# delegation matches, never an error.
DELEGATIONS_CHAIN_SQL = """
SELECT delegation_id, state, dispatched_at, completed_at,
       substr(IFNULL(task_json, ''), 1, {chars})
FROM async_delegations
WHERE parent_session_id IN ({placeholders})
ORDER BY dispatched_at ASC, delegation_id ASC
LIMIT ?
"""
DELEGATIONS_MAX = 50
DELEGATIONS_TASK_CHARS = 4000

# Matching windows: a child session or async delegation belongs to a
# dispatch only when it starts within this many seconds of the carrier
# AND its goal text agrees with the card's label. Both conditions are
# required — time alone or text alone never fabricates a link.
DELEGATE_MATCH_WINDOW = 120.0
DELEGATE_MATCH_SKEW = 10.0
# How much shared normalized text counts as "the same goal": short
# enough to be cheap, long enough that two different tasks from one
# batch practically never collide.
DELEGATE_LABEL_MATCH_CHARS = 32

# Friendly per-tool labels for the card's meta line.
DELEGATE_TOOL_LABELS = {
    "delegate_agent": "Sub-agent",
    "delegate_task": "Sub-agent",
    "delegate_claude_agent": "Claude agent",
    "delegate_cursor_agent": "Cursor agent",
}
DELEGATION_STATE_KEYS = {
    "completed": "done",
    "complete": "done",
    "done": "done",
    "failed": "failed",
    "error": "failed",
    "timed_out": "failed",
    "timeout": "failed",
    "cancelled": "interrupted",
    "canceled": "interrupted",
    "stopped": "interrupted",
}


def _decode_json_string(text, start):
    """text[start] must be '\"' -> (value, next_index), or (partial,
    None) when the string runs off the end of a truncated JSON slice.
    Escapes are honored, so a premature quote inside the value never
    ends it early; anything malformed yields ("", None)."""
    if start >= len(text) or text[start] != '"':
        return "", None
    out = []
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == '"':
            return "".join(out), i + 1
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "u" and i + 5 < len(text):
                try:
                    out.append(chr(int(text[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    return "".join(out), None
            out.append({"n": "\n", "t": "\t", "r": "\r",
                        "b": "\b", "f": "\f"}.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out), None


def _scan_string_field(text, key, start=0):
    """Best-effort value of one JSON string field inside a possibly
    truncated object slice: the decoded string at the first "key": "…"
    at or after `start`, or "" when the key or its string value never
    appears. Used only when whole-document parsing already failed."""
    needle = '"%s"' % key
    pos = text.find(needle, start)
    if pos < 0:
        return ""
    i = pos + len(needle)
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    if i >= len(text) or text[i] != ":":
        return ""
    i += 1
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    value, _end = _decode_json_string(text, i)
    return value


def _task_label(obj):
    """One parsed task/dispatch argument dict -> its best goal text:
    goal, then task, then context (the batch shape carries all three;
    the goal is the dispatch's own words, context is scenery)."""
    if not isinstance(obj, dict):
        return ""
    for key in ("goal", "task", "context", "message", "prompt"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def delegate_spawn_calls(name, args_raw):
    """One delegate call -> its dispatch descriptors, [] for control
    calls and non-spawn shapes. Each descriptor is {tool, label, model}
    with the raw (unclamped, unescaped) goal text as label; the caller
    clamps and escapes. Never raises: truncated or legacy argument JSON
    yields whatever partial tasks are still recoverable.

    Batch shapes (delegate_agent/delegate_task with a tasks list) yield
    one descriptor per recovered task; Claude/Cursor calls always yield
    exactly one (they take a single task and have no control actions).
    A delegate_agent whose action reads list/steer/stop — parsed whole
    or recovered from a truncated slice — yields nothing, and one with
    no discernible spawn content yields nothing either (its generic
    tool row simply stays)."""
    if name not in DELEGATE_SPAWN_NAMES:
        return []
    args_raw = args_raw if isinstance(args_raw, str) else ""
    obj = None
    if args_raw.strip():
        try:
            obj = json.loads(args_raw)
        except (ValueError, TypeError):
            obj = None
    if name in ("delegate_claude_agent", "delegate_cursor_agent"):
        label = _task_label(obj)
        if not label:
            label = _scan_string_field(args_raw, "task") or \
                _scan_string_field(args_raw, "goal")
        model = obj.get("model") if isinstance(obj, dict) else None
        return [{"tool": name, "label": label,
                 "model": model if isinstance(model, str) else ""}]
    # delegate_agent / delegate_task: control calls first, from the
    # parsed object or from a partial scan of a truncated slice.
    action = ""
    if isinstance(obj, dict):
        value = obj.get("action")
        action = value.strip().lower() if isinstance(value, str) else ""
    elif args_raw:
        value = _scan_string_field(args_raw, "action")
        action = value.strip().lower()
    if action in DELEGATE_CONTROL_ACTIONS:
        return []
    tasks = None
    if isinstance(obj, dict):
        raw_tasks = obj.get("tasks")
        if isinstance(raw_tasks, list):
            tasks = raw_tasks
    if tasks is None and obj is None and '"tasks"' in args_raw:
        # Truncated batch: recover each complete task object still
        # visible in the slice, and a partial trailing one's label.
        tasks = _recover_task_prefix(args_raw)
    if tasks:
        out = []
        for task in tasks[:TOOL_CALLS_MAX]:
            label = _task_label(task)
            model = task.get("model") if isinstance(task, dict) else None
            out.append({"tool": name, "label": label,
                        "model": model if isinstance(model, str)
                        else ""})
        if out:
            return out
    label = _task_label(obj)
    if not label and obj is None:
        label = _scan_string_field(args_raw, "goal") or \
            _scan_string_field(args_raw, "task") or \
            _scan_string_field(args_raw, "context")
    if not label:
        # No discernible spawn content: not card-worthy (an empty or
        # control-ish call keeps its ordinary tool row).
        return []
    model = obj.get("model") if isinstance(obj, dict) else None
    return [{"tool": name, "label": label,
             "model": model if isinstance(model, str) else ""}]


# A delegate tool name as a JSON string value — the anchor the carrier
# recovery scans for when the outer tool_calls array is truncated past
# whole-document parsing.
DELEGATE_NAME_RE = re.compile(
    r'"(delegate_agent|delegate_task|delegate_claude_agent|'
    r'delegate_cursor_agent)"')
DELEGATE_NAME_KEY_RE = re.compile(r'"name"\s*:\s*$')
DELEGATE_CALL_ID_RE = re.compile(r'"call_id"\s*:\s*$')
DELEGATE_ID_KEY_RE = re.compile(r'"id"\s*:\s*$')
DELEGATE_ARGS_KEY_RE = re.compile(r'"arguments"\s*:\s*$')


def delegate_carrier_calls(raw):
    """One bounded carrier tool_calls slice -> its delegate call
    descriptors [{name, args, ids}], tolerating truncation.

    The whole-document parser (parse_tool_calls) wins whenever the
    slice is complete; a truncated slice — a long batch call cut by the
    SQL cap takes the entire outer array down with it — falls back to
    scanning for delegate name values directly: each one's call id is
    recovered from the id/call_id key just before it, its arguments
    string decoded from just after it (partial when the slice ends
    mid-string). Escaped content inside argument strings never matches
    the anchors (their quotes are backslash-escaped there), and a name
    string that is not a "name" value (say, one echoing inside an
    argument) is rejected by its preceding key context."""
    calls = [c for c in parse_tool_calls(raw)
             if c["name"] in DELEGATE_SPAWN_NAMES]
    if not isinstance(raw, str):
        return []
    if calls and raw.rstrip().endswith("]"):
        return calls
    out = []
    for m in DELEGATE_NAME_RE.finditer(raw):
        name = m.group(1)
        if not DELEGATE_NAME_KEY_RE.search(
                raw[max(0, m.start() - 16):m.start()]):
            continue
        # The id keys sit at the call object's head, before the name;
        # scan a bounded window backwards for call_id, then id.
        head = raw[max(0, m.start() - 2000):m.start()]
        ids = []
        for key_re in (DELEGATE_CALL_ID_RE, DELEGATE_ID_KEY_RE):
            found = None
            for km in key_re.finditer(head):
                found = km
            if found is not None:
                value, _end = _decode_json_string(head, found.end())
                if value:
                    ids = [value]
                    break
        args = ""
        tail = raw[m.end():m.end() + 400]
        am = DELEGATE_ARGS_KEY_RE.search(tail)
        if am is not None:
            args, _end = _decode_json_string(raw, m.end() + am.end())
        out.append({"name": name, "args": args or "", "ids": ids})
    return out


def _recover_task_prefix(args_raw):
    """Truncated batch arguments -> the task objects still recoverable
    from the slice: every balanced {...} inside the tasks array parsed
    on its own, plus one partial trailing task reduced to its scanned
    label. Anything malformed yields []."""
    pos = args_raw.find('"tasks"')
    if pos < 0:
        return []
    start = args_raw.find("[", pos)
    if start < 0:
        return []
    tasks = []
    i = start + 1
    while i < len(args_raw) and len(tasks) < TOOL_CALLS_MAX:
        while i < len(args_raw) and args_raw[i] in " \t\r\n,":
            i += 1
        if i >= len(args_raw) or args_raw[i] == "]":
            break
        if args_raw[i] != "{":
            break
        depth = 0
        j = i
        end = None
        while j < len(args_raw):
            ch = args_raw[j]
            if ch == '"':
                _v, nxt = _decode_json_string(args_raw, j)
                if nxt is None:
                    break
                j = nxt
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
            j += 1
        if end is None:
            # Partial trailing task: keep its scanned goal text only.
            label = ""
            for key in ("goal", "task", "context"):
                label = _scan_string_field(args_raw, key, i)
                if label:
                    break
            if label:
                tasks.append({"goal": label})
            break
        try:
            task = json.loads(args_raw[i:end])
        except (ValueError, TypeError):
            task = None
        if isinstance(task, dict):
            tasks.append(task)
        i = end
    return tasks


def load_delegations(con, chain):
    """This conversation's async delegation records -> [{id, state,
    dispatched, completed, goals, model}], oldest first. The table is
    absent on older DBs and fixtures: that simply means no delegation
    can match, never an error. Each row's task_json is parsed
    defensively; its goals list (falling back to the single goal) is
    what cards match against, truncated slice or not."""
    out = []
    try:
        rows = con.execute(
            DELEGATIONS_CHAIN_SQL.format(
                chars=DELEGATIONS_TASK_CHARS,
                placeholders=",".join("?" * len(chain))),
            list(chain) + [DELEGATIONS_MAX]).fetchall()
    except sqlite3.Error:
        return out
    for did, state, dispatched, completed, task_json in rows:
        goals = []
        model = ""
        obj = None
        if isinstance(task_json, str) and task_json.strip():
            try:
                obj = json.loads(task_json)
            except (ValueError, TypeError):
                obj = None
        if isinstance(obj, dict):
            raw_goals = obj.get("goals")
            if isinstance(raw_goals, list):
                goals = [g for g in raw_goals if isinstance(g, str)]
            if not goals:
                goal = obj.get("goal")
                if isinstance(goal, str) and goal.strip():
                    goals = [goal]
            raw_model = obj.get("model")
            if isinstance(raw_model, str):
                model = raw_model
        if not goals:
            goal = _scan_string_field(
                task_json if isinstance(task_json, str) else "", "goal")
            if goal:
                goals = [goal]
        try:
            dispatched_ts = float(dispatched)
        except (TypeError, ValueError):
            continue
        try:
            completed_ts = float(completed) \
                if completed is not None else None
        except (TypeError, ValueError):
            completed_ts = None
        out.append({"id": str(did or ""),
                    "state": str(state or "").strip().lower(),
                    "dispatched": dispatched_ts,
                    "completed": completed_ts,
                    "goals": goals, "model": model,
                    "claimed": [False] * len(goals)})
    return out


def _norm_goal(text):
    """Goal text -> its match form: whitespace-collapsed, case-folded,
    capped — the same normalization both sides of a match get."""
    return " ".join(str(text or "").split()).casefold()[
        :DELEGATE_LABEL_MATCH_CHARS * 4]


def _goals_agree(a, b):
    """Whether two goal texts name the same task: equal or one a
    prefix of the other (labels clamp at different points) once
    normalized, with a floor so empty/trivial strings never agree."""
    na, nb = _norm_goal(a), _norm_goal(b)
    if len(na) < DELEGATE_LABEL_MATCH_CHARS // 2 \
            or len(nb) < DELEGATE_LABEL_MATCH_CHARS // 2:
        return False
    short = na[:DELEGATE_LABEL_MATCH_CHARS]
    other = nb[:DELEGATE_LABEL_MATCH_CHARS]
    return short == other or na.startswith(nb) or nb.startswith(na)


def delegate_card_state(card):
    """One card's truthful state key: the matched child session's own
    classification when there is one, else the matched delegation's
    stored state, else the weakest call-lifecycle fact — Running until
    the (synchronous) call's result row exists, Done once it does."""
    child = card.get("child")
    if child is not None:
        return subagent_state(child.get("ended"), child.get("end_reason"))
    delegation = card.get("delegation")
    if delegation is not None:
        return DELEGATION_STATE_KEYS.get(delegation["state"], "running") \
            if delegation["state"] else "running"
    return "done" if card.get("result_ts") is not None else "running"


def delegate_dom_key(call_base, idx):
    """One stable DOM key for a dispatch card: the call id (or carrier
    row fallback) plus the task index inside its batch, folded to the
    same safe charset child keys use. Names the row's id, its
    data-child attribute, the feed item's key and the Next sub-agent
    anchor's target — one spelling everywhere."""
    return "delegate-%s-%d" % (
        CHILD_KEY_UNSAFE_RE.sub("_", str(call_base or "call")), idx)


def delegate_revision(card, state):
    """Everything visible on one dispatch card folded into one cheap
    string — the page bakes it into data-rev, the feed ships it as rev,
    so a poll only rebuilds a card whose observable content (state,
    label, link, model, activity, viewer URL) actually moved. The
    viewer URL riding here is what upgrades a static Claude card in
    place exactly once, the moment its receipt appears."""
    child = card.get("child") or {}
    return "\N{BULLET}".join((
        str(card.get("call_id") or ""), state,
        str(card.get("label") or ""), str(card.get("model") or ""),
        str(card.get("tool") or ""),
        str(card.get("watch_url") or ""),
        str(child.get("id") or ""), str(child.get("profile") or ""),
        str(child.get("last") or ""),
        str((card.get("delegation") or {}).get("state") or ""),
        str(card.get("result_ts") or "")))


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


# Folded to a safe id/key charset: profile and session ids are already
# [A-Za-z0-9_.-] by the route and DB, but the key is also baked into
# HTML id attributes and data- keys, so anything stranger is folded
# rather than trusted.
CHILD_KEY_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def child_dom_key(profile, sid):
    """One stable DOM key for a child row: "<profile>-<session id>"
    with unsafe characters folded to "_". Names the row's id ("sa-<key>")
    and data-child attribute on the page, the feed item's key, and the
    Next sub-agent anchor's target — one spelling everywhere."""
    return "%s-%s" % (
        CHILD_KEY_UNSAFE_RE.sub("_", str(profile or "profile")),
        CHILD_KEY_UNSAFE_RE.sub("_", str(sid or "session")))


def child_revision(c, state):
    """Everything visible on one child row, folded into one cheap
    string. The page bakes it into data-rev and the feed ships it as
    rev, so a poll only rebuilds a row whose observable content (state,
    label, profile, activity, end reason) actually moved — the relative
    last-activity time rides along through `last`, whose bucket changes
    exactly when the rendered words do."""
    return "\N{BULLET}".join((
        str(c.get("id") or ""), str(c.get("profile") or ""), state,
        str(c.get("label") or ""), str(c.get("last") or ""),
        str(c.get("end_reason") or "")))
