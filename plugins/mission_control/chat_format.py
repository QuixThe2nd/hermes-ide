"""Codex commentary parsing, chat message shaping and the shared
time formatters for mission-control.

Moved byte-identical from server.py.
"""

import json
import re
from datetime import datetime

from plugins.mission_control.redaction import (
    DELEGATE_SPAWN_NAMES,
    clean_preview,
    clamp_label,
    redact_secret_text,
    sanitize_text,
)


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


def chat_messages(rows, suppress=()):
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

    `suppress` holds the tool_call ids of delegate dispatches that
    rendered as inline cards: exactly those generic delegate_* tool
    result rows drop out here (initial page, full snapshot and delta
    feed alike), while every unrelated tool row — delegate control
    calls included, which are never in the set — passes through
    unchanged.
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
            if suppress and tool_name in DELEGATE_SPAWN_NAMES:
                call_id = row[6] if len(row) > 6 else None
                if call_id and str(call_id) in suppress:
                    continue
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


def merge_children(msgs, children):
    """The flat chronological message list with one {"kind": "child"}
    card per sub-agent merged in at that child's dispatch time.

    Merging happens BEFORE tool grouping, on purpose: a child dispatched
    between two tool rows is its own timeline event — it splits what
    would otherwise be one collapsed tool group, and several children
    from one dispatch stay individually addressable rows instead of
    disappearing into a group. Ordering key is the child's stored
    sessions.started_at (the chronological dispatch time) against each
    message's timestamp; the 0/1 tag places a child after the messages
    of an identical timestamp (a dispatch follows the carrier that
    triggered it) while the preceding stable sort keeps both input
    orders (messages by (timestamp, id), children by (started, id)).
    """
    if not children:
        return msgs
    keyed = [((m["ts"], 0), m) for m in msgs]
    keyed += [(((c.get("started") or 0), 1), {"kind": "child", "c": c})
              for c in children]
    keyed.sort(key=lambda pair: pair[0])
    return [m for _key, m in keyed]


def merge_delegate_cards(msgs, cards):
    """The message list with one {"kind": "delegate"} item per dispatch
    card merged in at its call carrier's timestamp — the spawn-shaped
    delegate tool call is the chronological authority, so the card
    lands exactly where the carrier sits in the visible timeline (the
    carrier itself renders nothing when it has no text). The (ts, 1)
    tag places a card after any same-timestamp message bubble (a
    carrier WITH text keeps its bubble and the card follows it), and
    the stable sort keeps a batch's several cards in task order. Runs
    before tool grouping like merge_children, so a card also splits
    what would be one collapsed tool group."""
    if not cards:
        return msgs
    # merge_children runs first, so the list may already carry child
    # items, whose timestamp lives on the child record itself.
    def item_ts(m):
        if m["kind"] == "child":
            return (m["c"].get("started") or 0, 0)
        return (m.get("ts") or 0, 0)
    keyed = [(item_ts(m), m) for m in msgs]
    keyed += [((c["ts"], 1), {"kind": "delegate", "card": c})
              for c in cards]
    keyed.sort(key=lambda pair: pair[0])
    return [m for _key, m in keyed]


def chat_items(msgs):
    """Renderable messages -> the final list the page draws.

    Every maximal run of consecutive tool messages collapses into ONE
    {"kind": "tools", "items": [...]} group; text messages and inline
    child/dispatch cards pass through unchanged (a card between two
    tool rows therefore splits them into two groups — the merge helpers
    placed it there on purpose). Grouping runs after chat_messages()
    has dropped empty
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
