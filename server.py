#!/usr/bin/env python3
"""Live, dark-themed dashboard for the Hermes usage-proxy SQLite ledger.

Single file, Python stdlib only (http.server + sqlite3 + json):

* the ledger is opened read-only per request (``mode=ro``): the dashboard can
  never block the proxy's writers and can never modify the ledger;
* ``GET /`` serves the single-page dark dashboard.  Its JavaScript polls
  ``/api/summary``, ``/api/timeseries`` and ``/api/events`` every 5 s and
  updates the stat cards, the per-harness bars, the canvas charts (tokens
  per hour, stacked by harness, and the model-usage donut) and the event
  table in place — no
  full page reloads.  The first paint is
  server-rendered from the same data, so the page is meaningful even with
  JavaScript disabled (the chart then shows as an accessible data table);
* every SQL statement is a fully static literal; request-supplied values are
  only ever bound ``?`` parameters, never spliced into the SQL text;
* any database failure (missing file, locked, corrupt) degrades to a soft
  error payload at HTTP 200, so the poll loop never crashes;
* nothing is written to stdout while serving (access logs are suppressed;
  the startup banner and real errors go to stderr).

Launch flags are unchanged — see ``usage-proxy-webui.service``.
"""

import argparse
import html
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

SYDNEY = ZoneInfo("Australia/Sydney")
UNATTRIBUTED = "unattributed"
DEFAULT_HOST = "192.168.30.20"
DEFAULT_PORT = 9136
DEFAULT_DB = "/root/.hermes/usage-proxy/usage.sqlite"

POLL_SECONDS = 5
FETCH_TIMEOUT_MS = 4500
DASHBOARD_EVENTS = 50       # rows shown in the recent-events table
API_EVENTS_DEFAULT = 200
API_EVENTS_MAX = 1000
HOURS = 24
DAYS_7D = 7

# Muted harness colours, assigned to callers by name hash (see
# harness_color_idx) so the same harness always lands on the same hue in the
# bars, the chips and the chart — on both the server and the browser.
HARNESS_COLOR_COUNT = 6

# Model-usage donut: the top MODEL_TOP_N models by 24 h tokens, the remainder
# folded into an "other" bucket.  Slice colour follows token rank (index i of
# MODEL_COLORS), so neighbours never repeat and "other" always draws the
# neutral grey.  The six hues are the harness family with the lightness
# stepped into the dark band (hue held), ordered so every neighbouring pair —
# including the wrap onto "other" — clears the CVD and normal-vision floors
# on the card surface (validator: worst adjacent OKLab dE 11.5 protan/deutan,
# 16.5 normal).
MODEL_TOP_N = 6
MODEL_COLORS = ("#bd8714", "#d46c8b", "#5b8def", "#2ea79a", "#9a7be0", "#65a46c")
MODEL_OTHER_COLOR = "#66738a"   # same neutral the page uses for unattributed
DONUT_SIZE = 180                # square canvas, CSS px (device-pixel scaled in JS)

# Outcome badge: green when usage is final, red for auth/rate-limit
# rejections, grey for everything else.
RATE_LIMIT_CODES = (401, 429)
TONE_GOOD = "good"
TONE_CRIT = "crit"
TONE_NONE = "none"

# ---------------------------------------------------------------------------
# SQL — every statement below is a fully static literal.  Request-supplied
# values are only ever passed as bound "?" parameters (the *_SINCE variants).
# Nothing here is built by concatenation or f-string.
# ---------------------------------------------------------------------------

SQL_WINDOW_ALL = """
    SELECT COUNT(*)                          AS requests,
           COALESCE(SUM(total_tokens), 0)    AS tokens,
           COALESCE(SUM(prompt_tokens), 0)   AS input_tokens,
           COALESCE(SUM(completion_tokens), 0) AS output_tokens,
           COALESCE(SUM(cached_tokens), 0)   AS cached_tokens,
           MIN(ts)                           AS first_ts
    FROM usage_events
"""
SQL_WINDOW_SINCE = """
    SELECT COUNT(*)                          AS requests,
           COALESCE(SUM(total_tokens), 0)    AS tokens,
           COALESCE(SUM(prompt_tokens), 0)   AS input_tokens,
           COALESCE(SUM(completion_tokens), 0) AS output_tokens,
           COALESCE(SUM(cached_tokens), 0)   AS cached_tokens,
           MIN(ts)                           AS first_ts
    FROM usage_events
    WHERE ts >= ?
"""

SQL_CALLER_ALL = """
    SELECT caller, COUNT(*) AS requests, COALESCE(SUM(total_tokens), 0) AS tokens
    FROM usage_events
    GROUP BY caller
    ORDER BY tokens DESC, requests DESC, caller ASC
"""
SQL_CALLER_SINCE = """
    SELECT caller, COUNT(*) AS requests, COALESCE(SUM(total_tokens), 0) AS tokens
    FROM usage_events
    WHERE ts >= ?
    GROUP BY caller
    ORDER BY tokens DESC, requests DESC, caller ASC
"""

SQL_ROUTE_ALL = """
    SELECT path, COUNT(*) AS requests, COALESCE(SUM(total_tokens), 0) AS tokens
    FROM usage_events
    GROUP BY path
    ORDER BY tokens DESC, requests DESC, path ASC
"""
SQL_ROUTE_SINCE = """
    SELECT path, COUNT(*) AS requests, COALESCE(SUM(total_tokens), 0) AS tokens
    FROM usage_events
    WHERE ts >= ?
    GROUP BY path
    ORDER BY tokens DESC, requests DESC, path ASC
"""

SQL_MODEL_ALL = """
    SELECT model, COUNT(*) AS requests, COALESCE(SUM(total_tokens), 0) AS tokens
    FROM usage_events
    GROUP BY model
    ORDER BY tokens DESC, requests DESC, model ASC
"""
SQL_MODEL_SINCE = """
    SELECT model, COUNT(*) AS requests, COALESCE(SUM(total_tokens), 0) AS tokens
    FROM usage_events
    WHERE ts >= ?
    GROUP BY model
    ORDER BY tokens DESC, requests DESC, model ASC
"""

# Every ts is a UTC ISO-8601 string written by the proxy, so a plain
# strftime bucket on the raw text is the UTC hour key ("YYYY-MM-DDTHH").
# The buckets themselves are labelled in Sydney time (see hour_buckets).
# caller+model are grouped too, so each hour folds into the per-harness /
# per-model "series" that stacks the chart columns (see query_timeseries).
SQL_PER_HOUR = """
    SELECT strftime('%Y-%m-%dT%H', ts) AS hour_key,
           caller,
           model,
           COUNT(*)                    AS requests,
           SUM(total_tokens)           AS tokens
    FROM usage_events
    WHERE ts >= ?
    GROUP BY hour_key, caller, model
"""

SQL_EVENTS = """
    SELECT id, ts, upstream, model, path, status_code, latency_ms,
           prompt_tokens, completion_tokens, cached_tokens, reasoning_tokens,
           cache_creation_tokens, total_tokens, outcome, usage_complete, caller
    FROM usage_events
    ORDER BY id DESC
    LIMIT ?
"""

_BREAKDOWN_SQL = {
    "caller": (SQL_CALLER_ALL, SQL_CALLER_SINCE),
    "route": (SQL_ROUTE_ALL, SQL_ROUTE_SINCE),
    "model": (SQL_MODEL_ALL, SQL_MODEL_SINCE),
}


# --------------------------------------------------------------------------
# Database access (read-only, short-lived connections)
# --------------------------------------------------------------------------

def open_db_readonly(path: str) -> sqlite3.Connection:
    """Open SQLite in read-only URI mode and fail fast on lock contention."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.2)
    conn.execute("PRAGMA busy_timeout = 200")
    return conn


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def cutoff_iso(hours: float) -> str:
    """UTC ISO cutoff in the same format the proxy writes into ``ts``."""
    return (utc_now() - timedelta(hours=hours)).isoformat(timespec="milliseconds")


def to_sydney_datetime(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(SYDNEY)
    except (TypeError, ValueError):
        return None


def to_sydney(ts: str | None) -> str:
    local = to_sydney_datetime(ts)
    return local.strftime("%b %d %H:%M:%S") if local else "—"


def caller_label(caller: Any) -> str:
    """Traffic with no recorded caller cannot be attributed to a harness."""
    return UNATTRIBUTED if not caller else str(caller)


def harness_color_idx(name: str) -> int:
    """djb2 — mirrored exactly in the browser JS so colours agree."""
    value = 5381
    for ch in name:
        value = (value * 33 + ord(ch)) % 2147483647
    return value % HARNESS_COLOR_COUNT


def harness_class_name(name: str | None) -> str:
    if not name or name == UNATTRIBUTED:
        return "h-unattr"
    return "h" + str(harness_color_idx(name))


def _pick(sql_all: str, sql_since: str, since_ts: str | None) -> tuple[str, tuple[Any, ...]]:
    """Choose the static statement for this window and bind its parameter."""
    if since_ts is None:
        return sql_all, ()
    return sql_since, (since_ts,)


def query_window(conn: sqlite3.Connection, since_ts: str | None) -> dict[str, Any]:
    """Request/token totals (input, output, cached) for one window."""
    sql, params = _pick(SQL_WINDOW_ALL, SQL_WINDOW_SINCE, since_ts)
    requests, tokens, input_t, output_t, cached_t, first_ts = conn.execute(sql, params).fetchone()
    return {
        "requests": requests or 0,
        "tokens": tokens or 0,
        "input_tokens": input_t or 0,
        "output_tokens": output_t or 0,
        "cached_tokens": cached_t or 0,
        "first_ts": first_ts,
    }


def query_breakdown(
    conn: sqlite3.Connection,
    key: str,
    since_ts: str | None,
) -> list[dict[str, Any]]:
    sql_all, sql_since = _BREAKDOWN_SQL[key]
    sql, params = _pick(sql_all, sql_since, since_ts)
    rows = conn.execute(sql, params).fetchall()
    if key == "caller":
        return [
            {
                "caller": caller_label(r[0]),
                "unattributed": not r[0],
                "requests": r[1] or 0,
                "total_tokens": r[2] or 0,
            }
            for r in rows
        ]
    if key == "route":
        return [
            {"route": r[0] if r[0] else "—", "requests": r[1] or 0, "total_tokens": r[2] or 0}
            for r in rows
        ]
    return [
        {"model": r[0] if r[0] is not None else "(null)", "requests": r[1] or 0, "total_tokens": r[2] or 0}
        for r in rows
    ]


def by_model_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-model rows shaped for /api/summary's ``by_model`` (24 h window).

    Events with no recorded model cannot be attributed, so they collapse into
    one ``unknown`` slice rather than vanishing from the total.
    """
    return [
        {
            "model": "unknown" if not r["model"] or r["model"] == "(null)" else r["model"],
            "tokens": int(r["total_tokens"] or 0),
            "requests": int(r["requests"] or 0),
        }
        for r in rows
    ]


def hour_buckets() -> list[dict[str, Any]]:
    """24 empty hourly buckets (UTC) ending with the current, just-started hour."""
    current = utc_now().replace(minute=0, second=0, microsecond=0)
    buckets = []
    for i in range(HOURS - 1, -1, -1):
        start = current - timedelta(hours=i)
        local = start.astimezone(SYDNEY)
        buckets.append(
            {
                "hour_bucket": start.isoformat(),
                "label_sydney": local.strftime("%H:%M"),
                "day_sydney": local.strftime("%a") if local.hour == 0 else None,
                "requests": 0,
                "tokens": 0,
                # per-(harness, model) token groups behind the hour total —
                # the stacked segments of the chart column (query_timeseries)
                "series": [],
                # The first bucket is truncated by the rolling cutoff and the
                # last is still in progress — both drawn at half strength.
                "partial": i in (0, HOURS - 1),
            }
        )
    return buckets


def query_timeseries(conn: sqlite3.Connection, since_ts: str) -> list[dict[str, Any]]:
    """Tokens, requests and per-harness/model groups per hour (this is
    /api/timeseries).

    Rows arrive grouped by hour × caller × model and fold into the 24
    buckets.  Each bucket's ``series`` aggregates tokens per (caller,
    model) within the hour — sorted tokens desc, zero-token groups
    dropped — while ``requests``/``tokens`` stay the plain hour totals.
    """
    rows = conn.execute(SQL_PER_HOUR, (since_ts,)).fetchall()
    groups_by_key: dict[str, dict[tuple[str, str], int]] = {}
    requests_by_key: dict[str, int] = {}
    for hour_key, caller, model, requests, tokens in rows:
        groups = groups_by_key.setdefault(hour_key, {})
        pair = (caller_label(caller), str(model) if model else "unknown")
        groups[pair] = groups.get(pair, 0) + (tokens or 0)
        requests_by_key[hour_key] = requests_by_key.get(hour_key, 0) + (requests or 0)

    buckets = hour_buckets()
    for bucket in buckets:
        key = bucket["hour_bucket"][:13]
        series = [
            {"caller": caller, "model": model, "tokens": tokens}
            for (caller, model), tokens in groups_by_key.get(key, {}).items()
            if tokens > 0
        ]
        series.sort(key=lambda s: (-s["tokens"], s["caller"], s["model"]))
        bucket["requests"] = requests_by_key.get(key, 0)
        bucket["tokens"] = sum(s["tokens"] for s in series)
        bucket["series"] = series
    return buckets


def _event_row(r: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": r[0],
        "ts": r[1],
        "ts_sydney": to_sydney(r[1]),
        "upstream": r[2],
        "model": r[3],
        "path": r[4],
        "route": r[4],
        "status_code": r[5],
        "latency_ms": r[6],
        "prompt_tokens": r[7],
        "completion_tokens": r[8],
        "cached_tokens": r[9],
        "reasoning_tokens": r[10],
        "cache_creation_tokens": r[11],
        "total_tokens": r[12],
        "outcome": r[13],
        "usage_complete": r[14],
        "caller": caller_label(r[15]),
        "unattributed": not r[15],
    }


def query_events(conn: sqlite3.Connection, limit: int = API_EVENTS_DEFAULT) -> list[dict[str, Any]]:
    rows = conn.execute(SQL_EVENTS, (limit,)).fetchall()
    return [_event_row(r) for r in rows]


def badge_parts(e: dict[str, Any]) -> tuple[str, str]:
    """Outcome badge: final = green, 401/429 = red, everything else grey."""
    status = e.get("status_code")
    if status in RATE_LIMIT_CODES:
        return TONE_CRIT, str(status)
    if e.get("usage_complete") == "final":
        return TONE_GOOD, "final"
    label = e.get("outcome")
    if not label:
        label = str(status) if status is not None else "—"
    return TONE_NONE, label


def empty_window() -> dict[str, Any]:
    return {"requests": 0, "tokens": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}


def error_snapshot(message: str) -> dict[str, Any]:
    """A payload shaped like a real snapshot, but flagged as failed."""
    return {
        "error": message,
        "generated_at_utc": utc_now().isoformat(timespec="seconds"),
        "generated_at_sydney": to_sydney(utc_now().isoformat(timespec="seconds")),
        "cutoff_24h_utc": cutoff_iso(HOURS),
        "total": {"requests": 0, "tokens": 0},
        "total_requests": 0,
        "total_tokens": 0,
        "first_event_day": None,
        "last_24h": {**empty_window(), "per_route": [], "per_caller": []},
        "last_7d": {"requests": 0, "tokens": 0},
        "all_time": {**empty_window(), "per_model": [], "per_route": []},
        "by_model": [],
        "per_caller": [],
        "per_caller_24h": [],
        "per_route": [],
        "per_hour": hour_buckets(),
        "events": [],
    }


def fetch_snapshot(db_path: str, event_limit: int = API_EVENTS_DEFAULT) -> dict[str, Any]:
    """Everything one dashboard refresh needs, or a soft-error snapshot.

    ``event_limit=0`` skips the events query entirely (used by /api/summary).
    """
    try:
        conn = open_db_readonly(db_path)
    except (sqlite3.Error, OSError) as exc:
        return error_snapshot(f"cannot open ledger read-only: {exc}")

    try:
        cutoff_24h = cutoff_iso(HOURS)
        cutoff_7d = cutoff_iso(HOURS * DAYS_7D)
        last_24h = query_window(conn, cutoff_24h)
        last_24h["per_route"] = query_breakdown(conn, "route", cutoff_24h)
        last_24h["per_caller"] = query_breakdown(conn, "caller", cutoff_24h)
        last_7d = query_window(conn, cutoff_7d)
        all_time = query_window(conn, None)
        per_hour = query_timeseries(conn, cutoff_24h)
        events = query_events(conn, event_limit) if event_limit > 0 else []
        by_model = by_model_rows(query_breakdown(conn, "model", cutoff_24h))
        caller_rows = query_breakdown(conn, "caller", None)
        route_rows = query_breakdown(conn, "route", None)
        model_rows = query_breakdown(conn, "model", None)
    except (sqlite3.Error, OSError) as exc:
        return error_snapshot(f"ledger query failed: {exc}")
    finally:
        conn.close()

    first_event_day = None
    if all_time["first_ts"]:
        local = to_sydney_datetime(all_time["first_ts"])
        first_event_day = local.strftime("%Y-%m-%d") if local else None

    return {
        "error": None,
        "generated_at_utc": utc_now().isoformat(timespec="seconds"),
        "generated_at_sydney": to_sydney(utc_now().isoformat(timespec="seconds")),
        "cutoff_24h_utc": cutoff_24h,
        "total": {"requests": all_time["requests"], "tokens": all_time["tokens"]},
        "total_requests": all_time["requests"],
        "total_tokens": all_time["tokens"],
        "first_event_day": first_event_day,
        "last_24h": last_24h,
        "last_7d": {"requests": last_7d["requests"], "tokens": last_7d["tokens"]},
        "all_time": {
            "requests": all_time["requests"],
            "tokens": all_time["tokens"],
            "input_tokens": all_time["input_tokens"],
            "output_tokens": all_time["output_tokens"],
            "cached_tokens": all_time["cached_tokens"],
            "per_model": model_rows,
            "per_route": route_rows,
        },
        "by_model": by_model,
        "per_caller": caller_rows,
        "per_caller_24h": last_24h["per_caller"],
        "per_route": route_rows,
        "per_hour": per_hour,
        "events": events,
    }


# --------------------------------------------------------------------------
# Formatting helpers (server-side mirrors of the client JS)
# --------------------------------------------------------------------------

def fmt_int(value: Any) -> str:
    if value is None:
        return "—"
    return f"{int(value):,}"


def fmt_compact(value: Any) -> str:
    n = float(value or 0)
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if abs(n) >= divisor:
            return f"{n / divisor:.1f}".rstrip("0").rstrip(".") + suffix
    return str(int(n))


def fmt_stat(value: Any) -> str:
    n = int(value or 0)
    return f"{n:,}" if abs(n) < 10_000 else fmt_compact(n)


def fmt_opt(value: Any) -> str:
    """Table numeral; unknown totals (usage missing) read as a dash, not 0."""
    return "—" if value is None else fmt_stat(value)


def fmt_avg(tokens: Any, requests: Any) -> str:
    if not requests:
        return "no requests"
    return f"avg {fmt_compact(float(tokens) / requests)} / request"


def fmt_cached_hint(cached: Any) -> str:
    return f"incl. {fmt_compact(cached)} cached" if cached else "prompt tokens"


def bucket_label(bucket: dict[str, Any]) -> str:
    day = bucket.get("day_sydney")
    return (day + " " if day else "") + (bucket.get("label_sydney") or "")


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else "—"))


# --------------------------------------------------------------------------
# HTML fragments
# --------------------------------------------------------------------------

def stat_card(value_id: str, hint_id: str, label: str, value: Any, hint: str) -> str:
    return (
        '<div class="card stat">'
        f'<div class="label">{esc(label)}</div>'
        f'<div class="value" id="{value_id}">{esc(fmt_stat(value))}</div>'
        f'<div class="hint" id="{hint_id}">{esc(hint)}</div>'
        "</div>"
    )


def render_cards(snapshot: dict[str, Any]) -> str:
    last_24h = snapshot.get("last_24h") or {}
    req = last_24h.get("requests") or 0
    return "".join(
        [
            stat_card("c-req-24h", "h-req-24h", "Requests · 24 h", last_24h.get("requests"), "rolling window"),
            stat_card("c-tok-24h", "h-tok-24h", "Total tokens · 24 h", last_24h.get("tokens"), fmt_avg(last_24h.get("tokens"), req)),
            stat_card("c-in-24h", "h-in-24h", "Input tokens · 24 h", last_24h.get("input_tokens"), fmt_cached_hint(last_24h.get("cached_tokens"))),
            stat_card("c-out-24h", "h-out-24h", "Output tokens · 24 h", last_24h.get("output_tokens"), fmt_avg(last_24h.get("output_tokens"), req)),
        ]
    )


def harness_table_body(rows: list[dict[str, Any]]) -> str:
    """One row per harness: chip, requests, tokens and a share-of-max bar."""
    if not rows:
        return '<tbody id="harness-body"><tr><td colspan="4" class="muted">No requests in the last 24 h</td></tr></tbody>'
    peak = max((float(r.get("total_tokens") or 0) for r in rows), default=0.0)
    out = []
    for r in rows:
        tokens = float(r.get("total_tokens") or 0)
        tr_class = "h-unattr" if r.get("unattributed") else harness_class_name(r.get("caller"))
        fill = ""
        if peak > 0 and tokens > 0:
            width = max(1.5, tokens / peak * 100)
            fill = f'<div class="bar-fill" style="width:{width:.1f}%"></div>'
        out.append(
            f'<tr class="{tr_class}">'
            f'<td><span class="chip">{esc(r.get("caller") or UNATTRIBUTED)}</span></td>'
            f'<td class="num">{fmt_int(r.get("requests"))}</td>'
            f'<td class="num">{esc(fmt_stat(tokens))}</td>'
            f'<td class="bar-cell"><div class="bar-track" aria-hidden="true">{fill}</div></td>'
            "</tr>"
        )
    return '<tbody id="harness-body">' + "".join(out) + "</tbody>"


def events_table_body(events: list[dict[str, Any]]) -> str:
    if not events:
        return '<tr><td colspan="8" class="muted">No events yet</td></tr>'
    out = []
    for e in events:
        tone, label = badge_parts(e)
        row_cls = ' class="row-crit"' if tone == TONE_CRIT else ""
        chip_cls = "h-unattr" if e.get("unattributed") else harness_class_name(e.get("caller"))
        out.append(
            f"<tr{row_cls}>"
            f'<td class="num" title="{esc(e.get("ts"))}">{esc(e.get("ts_sydney"))}</td>'
            f'<td><span class="chip {chip_cls}">{esc(e.get("caller") or UNATTRIBUTED)}</span></td>'
            f"<td>{esc(e.get('model'))}</td>"
            f"<td>{esc(e.get('route'))}</td>"
            f'<td class="num">{esc(fmt_opt(e.get("prompt_tokens")))}</td>'
            f'<td class="num">{esc(fmt_opt(e.get("completion_tokens")))}</td>'
            f'<td class="num total">{esc(fmt_opt(e.get("total_tokens")))}</td>'
            f'<td><span class="badge"><span class="dot-s tone-{tone}"></span><span>{esc(label)}</span></span></td>'
            "</tr>"
        )
    return "".join(out)


def bucket_series_lines(bucket: dict[str, Any]) -> list[str]:
    """Per-harness token totals with per-model detail, e.g.
    ``claude 12.3k (modelA 8.1k · modelB 4.2k)`` — one line per harness.

    The text twin of the canvas stacking (and of the browser-side
    ``seriesLines``) for the sr-only chart table.
    """
    by_caller: dict[str, dict[str, int]] = {}
    for s in bucket.get("series") or []:
        tokens = int(s.get("tokens") or 0)
        if tokens <= 0:
            continue
        models = by_caller.setdefault(s.get("caller") or UNATTRIBUTED, {})
        name = s.get("model") or "unknown"
        models[name] = models.get(name, 0) + tokens
    lines = []
    for caller, models in sorted(by_caller.items(), key=lambda kv: (-sum(kv[1].values()), kv[0])):
        detail = " · ".join(
            f"{m} {fmt_compact(t)}" for m, t in sorted(models.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        lines.append(f"{caller} {fmt_compact(sum(models.values()))} ({detail})")
    return lines


def chart_data_table(buckets: list[dict[str, Any]]) -> str:
    """Screen-reader table twin of the stacked canvas chart."""
    rows = []
    for b in buckets:
        cell = "".join(f"<div>{esc(line)}</div>" for line in bucket_series_lines(b)) or "—"
        rows.append(
            "<tr>"
            f"<td>{esc(bucket_label(b))}</td>"
            f'<td class="num">{fmt_int(b.get("requests"))}</td>'
            f'<td class="num">{fmt_int(b.get("tokens"))}</td>'
            f"<td>{cell}</td>"
            "</tr>"
        )
    return (
        '<table class="sr-only"><caption>Tokens per hour, last 24 hours (Australia/Sydney)</caption>'
        '<thead><tr><th scope="col">Hour</th><th scope="col">Requests</th><th scope="col">Tokens</th>'
        '<th scope="col">Per-harness tokens (per model)</th></tr></thead>'
        f'<tbody id="chart-table-body">{"".join(rows)}</tbody></table>'
    )


def donut_slices(by_model: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Donut slices: top MODEL_TOP_N models by tokens, remainder as "other".

    Rows without token accounting (total_tokens NULL) draw no slice, so an
    all-zero window degrades to the muted empty state instead of a dead ring.
    """
    rows = [r for r in (by_model or []) if (r.get("tokens") or 0) > 0]
    rows.sort(key=lambda r: (-r["tokens"], r["model"]))
    slices = [{"model": r["model"], "tokens": r["tokens"], "requests": r["requests"]} for r in rows[:MODEL_TOP_N]]
    if len(rows) > MODEL_TOP_N:
        rest = rows[MODEL_TOP_N:]
        slices.append(
            {
                "model": "other",
                "tokens": sum(r["tokens"] for r in rest),
                "requests": sum(r["requests"] for r in rest),
            }
        )
    return slices


def slice_color(index: int) -> str:
    return MODEL_COLORS[index] if index < len(MODEL_COLORS) else MODEL_OTHER_COLOR


def fmt_pct(part: int, total: int) -> str:
    if total <= 0 or part <= 0:
        return "0%"
    share = part / total * 100
    return ("<1" if share < 1 else str(round(share))) + "%"


def model_legend_html(slices: list[dict[str, Any]]) -> str:
    """Legend body: swatch, model, tokens, share — colour never carries it alone."""
    total = sum(s["tokens"] for s in slices)
    items = "".join(
        "<li>"
        f'<span class="swatch" style="background:{slice_color(i)}"></span>'
        f'<span class="name">{esc(s["model"])}</span>'
        f'<span class="num">{esc(fmt_stat(s["tokens"]))}</span>'
        f'<span class="pct">{esc(fmt_pct(s["tokens"], total))}</span>'
        "</li>"
        for i, s in enumerate(slices)
    )
    return f'<ul class="legend" id="model-legend">{items}</ul>'


def donut_aria(slices: list[dict[str, Any]]) -> str:
    total = sum(s["tokens"] for s in slices)
    breakdown = ", ".join(f"{s['model']} {fmt_pct(s['tokens'], total)}" for s in slices)
    return f"Donut chart of token share by model over the last 24 hours. {breakdown}"


def model_panel_html(by_model: list[dict[str, Any]] | None) -> str:
    """First-paint twin of the browser-rendered donut panel."""
    slices = donut_slices(by_model)
    aria = donut_aria(slices) if slices else "Donut chart of token share by model over the last 24 hours. No usage."
    return (
        '<section class="card" aria-label="Model usage">'
        '<div class="card-head"><h2>Model usage</h2>'
        '<span class="win">last 24 h &middot; by total tokens &middot; top '
        + str(MODEL_TOP_N)
        + " + other</span></div>"
        '<div class="donut-row" id="donut-row"'
        + ("" if slices else " hidden")
        + "><div class=\"donut-wrap\" id=\"donut-wrap\" tabindex=\"0\" role=\"group\" aria-label=\""
        + esc(aria)
        + '">'
        f'<canvas id="donut" width="{DONUT_SIZE}" height="{DONUT_SIZE}"></canvas>'
        '<div class="tooltip" id="donut-tip" hidden></div>'
        "</div>"
        + model_legend_html(slices)
        + '</div><p class="muted donut-empty" id="donut-empty"'
        + (" hidden" if slices else "")
        + ">no usage in the last 24h</p>"
        "</section>"
    )


# --------------------------------------------------------------------------
# Styling — dark, near-black, one accent hue, system font stack, no external
# assets of any kind (no CDN links, no webfonts).
# --------------------------------------------------------------------------

CSS = """
:root {
  color-scheme: dark;
  --bg: #0a0d12;
  --surface: #11151c;
  --surface-2: #171c25;
  --border: rgba(255, 255, 255, 0.07);
  --border-strong: rgba(255, 255, 255, 0.15);
  --grid: #1f2531;
  --text: #e8edf4;
  --text-2: #a7b2c3;
  --muted: #6d7889;
  --accent: #3987e5;
  --accent-bright: #6aa6ee;
  --good: #2ea043;
  --bad: #e5534b;
  --stale: #d29922;
  --unattr: #8b95a6;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --mono: ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, "Liberation Mono", monospace;
}
* { box-sizing: border-box; }
/* author display values must not defeat the hidden attribute (donut-row) */
[hidden] { display: none !important; }
html, body { margin: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--sans);
  font-size: 15px;
  line-height: 1.45;
  -webkit-font-smoothing: antialiased;
}

.wrap { max-width: 1400px; margin: 0 auto; padding: 26px 28px 56px; }

.topbar { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; margin-bottom: 18px; }
h1 { margin: 0; font-size: 1.32rem; font-weight: 650; letter-spacing: -0.015em; }
h1 .accent { color: var(--accent-bright); }
.subtitle { margin: 3px 0 0; color: var(--text-2); font-size: 0.83rem; }

.live {
  display: inline-flex; align-items: center; gap: 9px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 999px;
  padding: 6px 14px; font-size: 0.78rem; color: var(--text-2); white-space: nowrap;
  font-family: var(--mono); font-variant-numeric: tabular-nums;
}
.live .dot { flex: none; width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
.live.ok .dot { background: var(--good); box-shadow: 0 0 0 3px rgba(46, 160, 67, 0.18); }
.live.error .dot { background: var(--bad); box-shadow: 0 0 0 3px rgba(229, 83, 75, 0.2); }
.live.stale .dot { background: var(--stale); box-shadow: 0 0 0 3px rgba(210, 153, 34, 0.18); }
.live .sep { color: var(--muted); }
.live .updated { color: var(--text); }
@media (prefers-reduced-motion: no-preference) {
  .live.ok .dot, .live.stale .dot { animation: pulse 2.4s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: 0.4; } }
}

.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 12px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 15px 17px; min-width: 0; }
.card h2 { margin: 0; font-size: 0.92rem; font-weight: 600; }
.card-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
.card-meta { display: flex; gap: 14px; flex-wrap: wrap; }
.win { color: var(--muted); font-size: 0.73rem; }

/* harness/model segmented toggle in the hourly chart card head */
.chart-mode { display: inline-flex; gap: 2px; padding: 2px; background: rgba(255, 255, 255, 0.04); border: 1px solid var(--border); border-radius: 7px; }
.chart-mode .mode-btn {
  appearance: none; border: 0; padding: 3px 10px; border-radius: 5px;
  background: transparent; color: var(--muted); cursor: pointer;
  font-family: var(--mono); font-size: 0.66rem; font-weight: 600;
  letter-spacing: 0.05em; line-height: 1.5;
}
.chart-mode .mode-btn:hover { color: var(--text-2); }
.chart-mode .mode-btn:focus-visible { box-shadow: 0 0 0 2px var(--accent); outline: none; }
.chart-mode .mode-btn.active { background: rgba(57, 135, 229, 0.28); color: var(--text); }
.stat .label { color: var(--muted); font-size: 0.71rem; font-weight: 600; letter-spacing: 0.07em; text-transform: uppercase; }
.stat .value { font-family: var(--mono); font-size: 1.78rem; font-weight: 600; letter-spacing: -0.02em; line-height: 1.15; margin-top: 9px; font-variant-numeric: tabular-nums; }
.stat .hint { color: var(--muted); font-size: 0.74rem; margin-top: 7px; font-variant-numeric: tabular-nums; }

.mid { display: grid; grid-template-columns: minmax(0, 1fr); gap: 12px; margin-bottom: 12px; }
@media (min-width: 1080px) { .mid { grid-template-columns: minmax(0, 1.9fr) minmax(330px, 1fr); align-items: start; } }
.side { display: grid; gap: 12px; min-width: 0; }

.error-card { display: none; margin-bottom: 12px; border-color: rgba(229, 83, 75, 0.45); background: rgba(229, 83, 75, 0.07); }
.error-card.show { display: block; }
.error-card h2 { color: #f1a1a1; }
.error-card p { margin: 6px 0 0; color: var(--text-2); font-size: 0.82rem; overflow-wrap: anywhere; }

.scroll-x { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 0.84rem; }
th {
  text-align: left; color: var(--muted); font-size: 0.68rem; font-weight: 600;
  letter-spacing: 0.06em; text-transform: uppercase; padding: 6px 10px;
  border-bottom: 1px solid var(--border-strong); white-space: nowrap;
}
td { padding: 7px 10px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover td { background: var(--surface-2); }
th.num, td.num { text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums; }
td.total { font-weight: 600; }
td.bar-cell { width: 34%; min-width: 96px; }
th:first-child, td:first-child { padding-left: 2px; }
th:last-child, td:last-child { padding-right: 2px; }

.events th { position: sticky; top: 0; background: var(--surface); border-bottom: none; box-shadow: inset 0 -1px 0 var(--border-strong); z-index: 2; }
tr.row-crit td { background: rgba(229, 83, 75, 0.10); }
tr.row-crit:hover td { background: rgba(229, 83, 75, 0.16); }
tr.row-crit td:first-child { box-shadow: inset 2px 0 0 var(--bad); }

.muted { color: var(--muted); }

/* harness palette — muted, one hue per caller, applied via these classes */
.h0 { --hc: #5b8def; }
.h1 { --hc: #3fb0a3; }
.h2 { --hc: #9a7be0; }
.h3 { --hc: #d9a13b; }
.h4 { --hc: #d9708f; }
.h5 { --hc: #7fae83; }
.h-unattr { --hc: #66738a; }
.chip { display: inline-flex; align-items: center; gap: 7px; color: var(--text-2); }
.chip::before { content: ""; flex: none; width: 8px; height: 8px; border-radius: 50%; background: var(--hc, var(--muted)); }
.h-unattr .chip { color: var(--unattr); font-style: italic; }

.bar-track { height: 6px; background: rgba(255, 255, 255, 0.05); border-radius: 3px; overflow: hidden; }
.bar-fill { height: 100%; min-width: 3px; background: var(--hc, var(--accent)); border-radius: 0 3px 3px 0; }

.chart-wrap { position: relative; outline: none; border-radius: 8px; }
.chart-wrap:focus-visible { box-shadow: 0 0 0 2px var(--accent); }
.chart-wrap canvas { display: block; width: 100%; }

/* model-usage donut: ring on the left, legend beside it, wraps below on narrow cards */
.donut-row { display: flex; align-items: center; gap: 18px; flex-wrap: wrap; }
.donut-wrap { position: relative; flex: none; outline: none; border-radius: 8px; }
.donut-wrap:focus-visible { box-shadow: 0 0 0 2px var(--accent); }
.donut-wrap canvas { display: block; }
.donut-empty { margin: 4px 0 2px; font-size: 0.82rem; }
.legend { list-style: none; margin: 0; padding: 0; flex: 1 1 160px; min-width: 160px; }
.legend li { display: flex; align-items: center; gap: 8px; padding: 4px 0; font-size: 0.8rem; border-bottom: 1px solid var(--grid); }
.legend li:last-child { border-bottom: none; }
.legend .swatch { flex: none; width: 9px; height: 9px; border-radius: 3px; }
.legend .name { color: var(--text-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.legend .num { margin-left: auto; font-family: var(--mono); font-variant-numeric: tabular-nums; }
.legend .pct { color: var(--muted); min-width: 3.2em; text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums; }

.tooltip {
  position: absolute; z-index: 5; transform: translate(-50%, 0);
  background: rgba(9, 12, 17, 0.96); border: 1px solid var(--border-strong); border-radius: 8px;
  padding: 7px 10px; font-size: 0.78rem; pointer-events: none;
  box-shadow: 0 10px 26px rgba(0, 0, 0, 0.45); white-space: nowrap;
}
.tooltip .tv { font-weight: 650; font-family: var(--mono); font-variant-numeric: tabular-nums; }
.tooltip .tl { color: var(--text-2); margin-top: 2px; }
/* harness swatch inside a chart-tooltip line — colour set from JS */
.tooltip .tl-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; background: var(--muted); }

.badge { display: inline-flex; align-items: center; gap: 7px; color: var(--text-2); }
.dot-s { flex: none; width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
.tone-good { background: var(--good); }
.tone-crit { background: var(--bad); }
.tone-none { background: #66738a; }

footer { margin-top: 20px; color: var(--muted); font-size: 0.75rem; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }

@media (max-width: 600px) {
  body { font-size: 14px; }
  .wrap { padding: 18px 12px 44px; }
  h1 { font-size: 1.12rem; }
  .stat .value { font-size: 1.5rem; }
  .card { padding: 13px 14px; }
  th, td { padding: 6px 8px; }
}
"""


# --------------------------------------------------------------------------
# Client layer — polls /api/summary, /api/timeseries and /api/events every
# POLL_SECONDS and re-renders cards / harness bars / canvas charts (hourly
# columns, model donut) / event table in place.  All DB-derived strings are
# inserted with textContent, never innerHTML.
# --------------------------------------------------------------------------

JS = r"""
(function () {
  'use strict';

  var POLL_MS = __POLL_MS__;
  var FETCH_TIMEOUT_MS = __FETCH_TIMEOUT_MS__;
  var DASHBOARD_EVENTS = __DASHBOARD_EVENTS__;
  var N_COLORS = __HARNESS_COLOR_COUNT__;
  var UNATTR = 'unattributed';
  var RATE_LIMIT_CODES = [401, 429];
  var CH = { H: 260, padL: 50, padR: 12, padT: 24, padB: 26, barMax: 30 };
  var C = {
    bar: '#3987e5', barHot: '#6aa6ee', barPartial: 'rgba(57, 135, 229, 0.45)',
    grid: '#1f2531', axis: '#2b3342', text: '#6d7889', textStrong: '#a7b2c3',
    peak: '#e8edf4'
  };

  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function setText(id, text) { var node = $(id); if (node) node.textContent = text; }

  function fmtCompact(value) {
    var n = Number(value) || 0;
    var steps = [[1e9, 'B'], [1e6, 'M'], [1e3, 'k']];
    for (var i = 0; i < steps.length; i++) {
      if (Math.abs(n) >= steps[i][0]) {
        return (n / steps[i][0]).toFixed(1).replace(/\.0$/, '') + steps[i][1];
      }
    }
    return String(Math.round(n));
  }

  function fmtInt(value) {
    if (value === null || value === undefined) return '—';
    return Number(value).toLocaleString('en-US');
  }

  function fmtStat(value) {
    var n = Number(value) || 0;
    return Math.abs(n) < 10000 ? fmtInt(n) : fmtCompact(n);
  }

  function fmtOpt(value) { return (value === null || value === undefined) ? '—' : fmtStat(value); }

  function fmtAvg(tokens, requests) {
    if (!requests) return 'no requests';
    return 'avg ' + fmtCompact(tokens / requests) + ' / request';
  }

  function fmtCachedHint(cached) {
    return (Number(cached) || 0) > 0 ? 'incl. ' + fmtCompact(cached) + ' cached' : 'prompt tokens';
  }

  /* same djb2 as the server's harness_color_idx, so chip colours agree */
  function djb2(name) {
    var h = 5381;
    for (var i = 0; i < name.length; i++) h = (h * 33 + name.charCodeAt(i)) % 2147483647;
    return h;
  }

  function harnessClass(caller) {
    if (!caller || caller === UNATTR) return 'h-unattr';
    return 'h' + (djb2(caller) % N_COLORS);
  }

  /* hex mirror of the .h0…h5/.h-unattr CSS palette — a canvas cannot read
     CSS custom properties, so the chart resolves harnessClass() to hex here */
  var HARNESS_HEX = {
    h0: '#5b8def', h1: '#3fb0a3', h2: '#9a7be0',
    h3: '#d9a13b', h4: '#d9708f', h5: '#7fae83', 'h-unattr': '#66738a'
  };

  function harnessHex(caller) {
    return HARNESS_HEX[harnessClass(caller)] || HARNESS_HEX['h-unattr'];
  }

  /* hex mirror of the server's MODEL_COLORS + MODEL_OTHER_COLOR — the chart's
     per-model mode hashes into these instead of the harness palette */
  var MODEL_HEXES = ['#bd8714', '#d46c8b', '#5b8def', '#2ea79a', '#9a7be0', '#65a46c', '#66738a'];

  function hexToRgba(hex, alpha) {
    var n = parseInt(hex.slice(1), 16);
    return 'rgba(' + (n >> 16 & 255) + ',' + (n >> 8 & 255) + ',' + (n & 255) + ',' + alpha + ')';
  }

  function bucketLabel(b) {
    if (!b) return '';
    return (b.day_sydney ? b.day_sydney + ' ' : '') + (b.label_sydney || '');
  }

  var sydFmt = null, sydTime = null;
  try {
    sydFmt = new Intl.DateTimeFormat('en-US', {
      timeZone: 'Australia/Sydney', month: 'short', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
    });
    sydTime = new Intl.DateTimeFormat('en-AU', {
      timeZone: 'Australia/Sydney', hour: '2-digit', minute: '2-digit',
      second: '2-digit', hourCycle: 'h23'
    });
  } catch (e) { /* no ICU tz data: server-provided strings are used instead */ }

  function fmtSydneyTime(ms) {
    if (!sydTime) return new Date(ms).toISOString().slice(11, 19) + 'Z';
    return sydTime.format(new Date(ms));
  }

  /* ---- stat cards ---- */

  function renderCards(s) {
    var l24 = s.last_24h || {};
    var req = Number(l24.requests) || 0;
    setText('c-req-24h', fmtStat(l24.requests));
    setText('h-req-24h', 'rolling window');
    setText('c-tok-24h', fmtStat(l24.tokens));
    setText('h-tok-24h', fmtAvg(Number(l24.tokens) || 0, req));
    setText('c-in-24h', fmtStat(l24.input_tokens));
    setText('h-in-24h', fmtCachedHint(l24.cached_tokens));
    setText('c-out-24h', fmtStat(l24.output_tokens));
    setText('h-out-24h', fmtAvg(Number(l24.output_tokens) || 0, req));
  }

  /* ---- per-harness share bars ---- */

  function renderHarness(rows) {
    var tbody = $('harness-body');
    if (!tbody) return;
    tbody.textContent = '';
    if (!rows || !rows.length) {
      var emptyRow = el('tr');
      var cell = el('td', 'muted', 'No requests in the last 24 h');
      cell.colSpan = 4;
      emptyRow.appendChild(cell);
      tbody.appendChild(emptyRow);
      return;
    }
    var max = 0;
    rows.forEach(function (r) { max = Math.max(max, Number(r.total_tokens) || 0); });
    rows.forEach(function (r) {
      var tokens = Number(r.total_tokens) || 0;
      var tr = el('tr', r.unattributed ? 'h-unattr' : harnessClass(r.caller));
      var tdLabel = el('td');
      tdLabel.appendChild(el('span', 'chip', r.caller || UNATTR));
      tr.appendChild(tdLabel);
      tr.appendChild(el('td', 'num', fmtInt(r.requests)));
      tr.appendChild(el('td', 'num', fmtStat(tokens)));
      var tdBar = el('td', 'bar-cell');
      var track = el('div', 'bar-track');
      track.setAttribute('aria-hidden', 'true');
      if (max > 0 && tokens > 0) {
        var fill = el('div', 'bar-fill');
        fill.style.width = Math.max(1.5, (tokens / max) * 100) + '%';
        track.appendChild(fill);
      }
      tdBar.appendChild(track);
      tr.appendChild(tdBar);
      tbody.appendChild(tr);
    });
  }

  /* ---- model-usage donut (24 h, top 6 + other) ---- */

  var DN = {
    size: __DONUT_SIZE__, ring: 26,
    /* rank-order slice colours — server twin: MODEL_COLORS / MODEL_OTHER_COLOR */
    colors: ['#bd8714', '#d46c8b', '#5b8def', '#2ea79a', '#9a7be0', '#65a46c'],
    other: '#66738a', surface: '#11151c',
    text: '#a7b2c3', muted: '#6d7889', bright: '#e8edf4'
  };
  var donutGeom = null;      /* {slices, total, cx, cy, rIn, rOut, start} for hit tests */
  var donutHover = -1;
  var lastByModel = [];

  function donutSlices(byModel) {
    var rows = (byModel || []).filter(function (r) { return (Number(r.tokens) || 0) > 0; });
    rows.sort(function (a, b) { return (Number(b.tokens) || 0) - (Number(a.tokens) || 0) || String(a.model).localeCompare(String(b.model)); });
    var slices = rows.slice(0, DN.colors.length).map(function (r) {
      return { model: String(r.model), tokens: Number(r.tokens) || 0, requests: Number(r.requests) || 0 };
    });
    if (rows.length > DN.colors.length) {
      var rest = rows.slice(DN.colors.length);
      slices.push({
        model: 'other',
        tokens: rest.reduce(function (a, r) { return a + (Number(r.tokens) || 0); }, 0),
        requests: rest.reduce(function (a, r) { return a + (Number(r.requests) || 0); }, 0)
      });
    }
    return slices;
  }

  function sliceColor(i) { return i < DN.colors.length ? DN.colors[i] : DN.other; }

  function pctLabel(part, total) {
    if (!total || part <= 0) return '0%';
    var share = (part / total) * 100;
    return (share < 1 ? '<1' : String(Math.round(share))) + '%';
  }

  function renderDonut(byModel) {
    lastByModel = byModel || [];
    var canvas = $('donut'), wrap = $('donut-wrap');
    if (!canvas || !wrap) return;
    var slices = donutSlices(lastByModel);
    var row = $('donut-row'), empty = $('donut-empty');
    if (row) row.hidden = !slices.length;
    if (empty) empty.hidden = !!slices.length;
    if (!slices.length) { donutGeom = null; return; }

    var total = slices.reduce(function (a, s) { return a + s.tokens; }, 0);
    var dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(DN.size * dpr);
    canvas.height = Math.round(DN.size * dpr);
    canvas.style.width = DN.size + 'px';
    canvas.style.height = DN.size + 'px';
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, DN.size, DN.size);

    var cx = DN.size / 2, cy = DN.size / 2;
    var rOut = DN.size / 2 - 4, rIn = rOut - DN.ring;
    var mono = 'ui-monospace, Menlo, Consolas, monospace';
    var a0 = -Math.PI / 2;

    slices.forEach(function (s, i) {
      var ang = total > 0 ? (s.tokens / total) * Math.PI * 2 : 0;
      ctx.beginPath();
      ctx.moveTo(cx + Math.cos(a0) * rIn, cy + Math.sin(a0) * rIn);
      ctx.arc(cx, cy, rOut + (i === donutHover ? 3 : 0), a0, a0 + ang);
      ctx.arc(cx, cy, rIn, a0 + ang, a0, true);
      ctx.closePath();
      ctx.fillStyle = sliceColor(i);
      ctx.fill();
      /* 2 px surface ring = the gap between neighbouring slices */
      ctx.strokeStyle = DN.surface;
      ctx.lineWidth = 2;
      ctx.stroke();
      a0 += ang;
    });

    /* the hole carries the window total */
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = DN.bright;
    ctx.font = '600 15px ' + mono;
    ctx.fillText(fmtCompact(total), cx, cy - 8);
    ctx.fillStyle = DN.muted;
    ctx.font = '10px ' + mono;
    ctx.fillText('tokens · 24 h', cx, cy + 10);

    donutGeom = { slices: slices, total: total, cx: cx, cy: cy, rIn: rIn, rOut: rOut, start: -Math.PI / 2 };
    canvas.setAttribute('aria-label', 'Token share by model, last 24 h: ' +
      slices.map(function (s) { return s.model + ' ' + pctLabel(s.tokens, total); }).join(', '));

    var legend = $('model-legend');
    if (legend) {
      legend.textContent = '';
      slices.forEach(function (s, i) {
        var li = el('li');
        var sw = el('span', 'swatch');
        sw.style.background = sliceColor(i);
        li.appendChild(sw);
        li.appendChild(el('span', 'name', s.model));
        li.appendChild(el('span', 'num', fmtStat(s.tokens)));
        li.appendChild(el('span', 'pct', pctLabel(s.tokens, total)));
        legend.appendChild(li);
      });
    }
  }

  function donutSliceAt(x, y) {
    if (!donutGeom || !donutGeom.total) return -1;
    var g = donutGeom, dx = x - g.cx, dy = y - g.cy;
    var r = Math.hypot(dx, dy);
    if (r < g.rIn - 2 || r > g.rOut + 5) return -1;
    var rel = Math.atan2(dy, dx) - g.start;
    while (rel < 0) rel += Math.PI * 2;
    var acc = 0;
    for (var i = 0; i < g.slices.length; i++) {
      acc += (g.slices[i].tokens / g.total) * Math.PI * 2;
      if (rel <= acc) return i;
    }
    return -1;
  }

  function showDonutHover(i, px, py) {
    var g = donutGeom, tip = $('donut-tip'), wrap = $('donut-wrap');
    if (!g || !tip || !wrap || !g.slices[i]) return;
    donutHover = i;
    renderDonut(lastByModel);
    var s = g.slices[i];
    tip.textContent = '';
    tip.appendChild(el('div', 'tv', fmtCompact(s.tokens) + ' tokens'));
    tip.appendChild(el('div', 'tl',
      s.model + ' · ' + pctLabel(s.tokens, g.total) + ' · ' + fmtInt(s.requests) + ' req'));
    tip.hidden = false;
    tip.style.left = Math.max(tip.offsetWidth / 2 + 2,
      Math.min(wrap.clientWidth - tip.offsetWidth / 2 - 2, px)) + 'px';
    tip.style.top = Math.max(2, py - tip.offsetHeight - 10) + 'px';
  }

  function hideDonutHover() {
    if (donutHover < 0) { var tip = $('donut-tip'); if (tip) tip.hidden = true; return; }
    donutHover = -1;
    var tip2 = $('donut-tip');
    if (tip2) tip2.hidden = true;
    renderDonut(lastByModel);
  }

  function wireDonut() {
    var canvas = $('donut'), wrap = $('donut-wrap');
    if (!canvas || !wrap) return;
    canvas.addEventListener('pointermove', function (ev) {
      var rect = canvas.getBoundingClientRect();
      var i = donutSliceAt(ev.clientX - rect.left, ev.clientY - rect.top);
      if (i !== donutHover) showDonutHover(i, ev.clientX - rect.left, ev.clientY - rect.top);
    });
    canvas.addEventListener('pointerleave', hideDonutHover);
    wrap.addEventListener('keydown', function (ev) {
      var n = donutGeom ? donutGeom.slices.length : 0;
      if (!n) return;
      if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') {
        var next = donutHover < 0 ? 0 : (donutHover + (ev.key === 'ArrowRight' ? 1 : -1) + n) % n;
        var g = donutGeom, acc = -Math.PI / 2;
        for (var k = 0; k < next; k++) acc += (g.slices[k].tokens / g.total) * Math.PI * 2;
        var mid = acc + (g.slices[next].tokens / g.total) * Math.PI;
        showDonutHover(next, g.cx + Math.cos(mid) * (g.rOut + 3), g.cy + Math.sin(mid) * (g.rOut + 3));
        ev.preventDefault();
      } else if (ev.key === 'Escape') { hideDonutHover(); }
    });
  }

  /* ---- 24 h column chart on a canvas ---- */

  function niceStep(rough) {
    if (!(rough > 0) || !isFinite(rough)) return 1;
    var mag = Math.pow(10, Math.floor(Math.log10(rough)));
    var factors = [1, 2, 2.5, 5, 10];
    for (var i = 0; i < factors.length; i++) {
      if (rough <= factors[i] * mag) return factors[i] * mag;
    }
    return 10 * mag;
  }

  var chartGeom = null;
  var hoverIdx = -1;
  var chartMode = 'harness';   /* breakdown dimension: 'harness' | 'model' */

  function barTopPath(ctx, x, y, w, h) {
    var r = Math.min(3, w / 2, h);
    var x2 = x + w, yb = y + h;
    ctx.beginPath();
    ctx.moveTo(x, yb);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.lineTo(x2 - r, y);
    ctx.quadraticCurveTo(x2, y, x2, y + r);
    ctx.lineTo(x2, yb);
    ctx.closePath();
    ctx.fill();
  }

  /* one stacked-segment entry per member of the active breakdown dimension
     present in the bucket, name ascending — the fixed bottom-to-top stack
     order, independent of segment size */
  function bucketSegments(b) {
    var byKey = {};
    (b && b.series ? b.series : []).forEach(function (s) {
      var t = Number(s.tokens) || 0;
      if (t <= 0) return;
      var k = chartMode === 'model' ? (s.model || 'unknown') : (s.caller || UNATTR);
      byKey[k] = (byKey[k] || 0) + t;
    });
    return Object.keys(byKey)
      .map(function (k) { return { name: k, tokens: byKey[k] }; })
      .sort(function (a, k) { return a.name < k.name ? -1 : a.name > k.name ? 1 : 0; });
  }

  /* segment colours for one bar: the harness palette in harness mode; in
     model mode a stable djb2 hash of the model name into MODEL_HEXES, where
     a slot already claimed by an earlier (alphabetical) model in this bar is
     advanced +1 so stacked neighbours stay distinguishable */
  function segmentHexes(segs) {
    var out = {};
    if (chartMode !== 'model') {
      segs.forEach(function (s) { out[s.name] = harnessHex(s.name); });
      return out;
    }
    var taken = [];
    segs.forEach(function (s) {
      var idx = djb2(s.name) % MODEL_HEXES.length;
      for (var bump = 0; taken[idx] && bump < MODEL_HEXES.length; bump++) {
        idx = (idx + 1) % MODEL_HEXES.length;
      }
      taken[idx] = true;
      out[s.name] = MODEL_HEXES[idx];
    });
    return out;
  }

  /* "caller 12.3k (modelA 8.1k · modelB 4.2k)" per harness — textContent
     twin of the server's bucket_series_lines for the sr-only chart table */
  function seriesLines(b) {
    var byCaller = {};
    (b && b.series ? b.series : []).forEach(function (s) {
      var t = Number(s.tokens) || 0;
      if (t <= 0) return;
      var c = s.caller || UNATTR;
      var models = byCaller[c] || (byCaller[c] = {});
      var m = s.model || 'unknown';
      models[m] = (models[m] || 0) + t;
    });
    return Object.keys(byCaller)
      .sort(function (a, c) {
        return modelsTotal(byCaller[c]) - modelsTotal(byCaller[a]) || (a < c ? -1 : a > c ? 1 : 0);
      })
      .map(function (c) {
        var models = byCaller[c];
        var detail = Object.keys(models)
          .sort(function (a, m) { return models[m] - models[a] || (a < m ? -1 : a > m ? 1 : 0); })
          .map(function (m) { return m + ' ' + fmtCompact(models[m]); })
          .join(' · ');
        return c + ' ' + fmtCompact(modelsTotal(models)) + ' (' + detail + ')';
      });
  }

  function modelsTotal(models) {
    var t = 0;
    for (var m in models) t += models[m];
    return t;
  }

  function renderChart(buckets) {
    var canvas = $('chart'), wrap = $('chart-wrap');
    if (!canvas || !wrap || !buckets) return;
    var n = buckets.length;
    var cssW = Math.max(320, wrap.clientWidth || 800);
    var dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(CH.H * dpr);
    canvas.style.width = cssW + 'px';
    canvas.style.height = CH.H + 'px';
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, CH.H);

    var padL = CH.padL, padT = CH.padT;
    var plotW = cssW - padL - CH.padR;
    var plotH = CH.H - padT - CH.padB;
    var baseY = padT + plotH;
    var band = n ? plotW / n : plotW;
    var barW = Math.max(2, Math.min(CH.barMax, band - 6));

    var tokens = buckets.map(function (b) { return Number(b.tokens) || 0; });
    var peak = 0;
    tokens.forEach(function (t) { if (t > peak) peak = t; });

    /* y gridlines + tick labels */
    ctx.font = '11px ' + 'ui-monospace, Menlo, Consolas, monospace';
    ctx.textBaseline = 'middle';
    if (peak > 0) {
      ctx.strokeStyle = C.grid;
      ctx.fillStyle = C.text;
      ctx.lineWidth = 1;
      var step = niceStep(peak / 4);
      for (var tick = step; tick <= peak; tick += step) {
        var gy = Math.round(baseY - (tick / peak) * plotH) + 0.5;
        ctx.beginPath();
        ctx.moveTo(padL, gy);
        ctx.lineTo(cssW - CH.padR, gy);
        ctx.stroke();
        ctx.textAlign = 'right';
        ctx.fillText(fmtCompact(tick), padL - 8, gy);
      }
    }

    /* baseline */
    ctx.strokeStyle = C.axis;
    ctx.beginPath();
    ctx.moveTo(padL, baseY + 0.5);
    ctx.lineTo(cssW - CH.padR, baseY + 0.5);
    ctx.stroke();

    /* columns + x axis (Sydney time: day name at midnight, time every 4 h) */
    var peakIdx = tokens.indexOf(peak);
    buckets.forEach(function (b, i) {
      var v = tokens[i];
      if (v > 0) {
        var h = (v / peak) * plotH;
        var x = padL + i * band + (band - barW) / 2;
        var segs = bucketSegments(b);
        if (segs.length) {
          /* stacked segments of the active dimension, alphabetical from the
             base up; only the topmost keeps the rounded top, lower ones butt
             squarely */
          var hexes = segmentHexes(segs);
          var y = baseY;
          segs.forEach(function (s, si) {
            var sh = (s.tokens / v) * h;
            var color = hexes[s.name];
            ctx.fillStyle = b.partial ? hexToRgba(color, 0.45) : color;
            if (si === segs.length - 1) {
              barTopPath(ctx, x, y - sh, barW, sh);
            } else {
              /* +0.5 px overlap hides the hairline seam under the segment above */
              ctx.fillRect(x, y - sh, barW, sh + 0.5);
            }
            y -= sh;
          });
          if (i === hoverIdx) {
            /* barHot treatment for a stacked bar: one translucent lift pass
               over the whole column lightens every segment at once */
            ctx.fillStyle = 'rgba(255, 255, 255, 0.22)';
            barTopPath(ctx, x, baseY - h, barW, h);
          }
        } else {
          /* legacy shape (no series): one solid column exactly as before */
          ctx.fillStyle = i === hoverIdx ? C.barHot : (b.partial ? C.barPartial : C.bar);
          barTopPath(ctx, x, baseY - h, barW, h);
        }
      }
      var centre = padL + i * band + band / 2;
      var hour = parseInt(b.label_sydney, 10) || 0;
      if (b.day_sydney || hour % 4 === 0) {
        ctx.strokeStyle = C.axis;
        ctx.beginPath();
        ctx.moveTo(Math.round(centre) + 0.5, baseY);
        ctx.lineTo(Math.round(centre) + 0.5, baseY + 4);
        ctx.stroke();
        ctx.textAlign = 'center';
        ctx.fillStyle = b.day_sydney ? C.textStrong : C.text;
        ctx.font = (b.day_sydney ? '600 ' : '') + '11px ui-monospace, Menlo, Consolas, monospace';
        ctx.fillText(b.day_sydney || b.label_sydney, centre, baseY + 16);
        ctx.font = '11px ui-monospace, Menlo, Consolas, monospace';
      }
    });

    /* one selective direct label — the peak */
    if (peak > 0 && peakIdx >= 0) {
      ctx.fillStyle = C.peak;
      ctx.textAlign = 'center';
      ctx.font = '600 11px ui-monospace, Menlo, Consolas, monospace';
      ctx.fillText(fmtCompact(peak), padL + peakIdx * band + band / 2, Math.max(8, padT - 10));
    }

    chartGeom = { cssW: cssW, n: n, band: band, padL: padL, peak: peak, plotH: plotH, baseY: baseY, buckets: buckets, tokens: tokens };
    if (hoverIdx >= n) hoverIdx = -1;
    setText('chart-peak', peak > 0
      ? 'peak ' + fmtCompact(peak) + ' · ' + bucketLabel(buckets[peakIdx])
      : 'no traffic in the last 24 h');
    renderChartTable(buckets);
  }

  function renderChartTable(buckets) {
    var tbody = $('chart-table-body');
    if (!tbody) return;
    tbody.textContent = '';
    buckets.forEach(function (b) {
      var tr = el('tr');
      tr.appendChild(el('td', '', bucketLabel(b)));
      tr.appendChild(el('td', 'num', fmtInt(b.requests)));
      tr.appendChild(el('td', 'num', fmtInt(b.tokens)));
      var td = el('td');
      var lines = seriesLines(b);
      if (lines.length) {
        lines.forEach(function (l) { td.appendChild(el('div', '', l)); });
      } else {
        td.textContent = '—';
      }
      tr.appendChild(td);
      tbody.appendChild(tr);
    });
  }

  function showChartHover(i) {
    if (!chartGeom || !chartGeom.n) return;
    var b = chartGeom.buckets[i];
    if (!b) return;
    hoverIdx = i;
    renderChart(buckets());
    var tip = $('chart-tip');
    tip.textContent = '';
    tip.appendChild(el('div', 'tv', fmtCompact(chartGeom.tokens[i]) + ' tokens'));
    tip.appendChild(el('div', 'tl',
      bucketLabel(b) + ' · ' + fmtInt(b.requests) + ' req' + (b.partial ? ' · partial' : '')));
    /* one line per member of the active dimension present in that hour,
       dot coloured like its segment */
    var segs = bucketSegments(b);
    var hexes = segmentHexes(segs);
    segs.forEach(function (seg) {
      var line = el('div', 'tl');
      var dot = el('span', 'tl-dot');
      dot.style.background = hexes[seg.name];
      line.appendChild(dot);
      line.appendChild(document.createTextNode(seg.name + ' · ' + fmtCompact(seg.tokens)));
      tip.appendChild(line);
    });
    tip.hidden = false;
    var centre = chartGeom.padL + i * chartGeom.band + chartGeom.band / 2;
    var h = chartGeom.tokens[i] > 0 ? (chartGeom.tokens[i] / chartGeom.peak) * chartGeom.plotH : 0;
    tip.style.left = Math.max(tip.offsetWidth / 2 + 4,
      Math.min(chartGeom.cssW - tip.offsetWidth / 2 - 4, centre)) + 'px';
    tip.style.top = Math.max(2, chartGeom.baseY - h - tip.offsetHeight - 8) + 'px';
  }

  function hideChartHover() {
    hoverIdx = -1;
    var tip = $('chart-tip');
    if (tip) tip.hidden = true;
    if (chartGeom) renderChart(buckets());
  }

  /* switch the hourly chart's breakdown dimension; the poll re-render reads
     chartMode on every tick, so the choice survives without a reload */
  function setChartMode(mode) {
    if (mode !== 'harness' && mode !== 'model') return;
    chartMode = mode;
    [['mode-harness', 'harness'], ['mode-model', 'model']].forEach(function (p) {
      var btn = $(p[0]);
      if (!btn) return;
      btn.classList.toggle('active', mode === p[1]);
      btn.setAttribute('aria-pressed', mode === p[1] ? 'true' : 'false');
    });
    var wrap = $('chart-wrap');
    if (wrap) wrap.setAttribute('aria-label',
      'Column chart of tokens per hour over the last 24 hours, ' +
      (mode === 'model' ? 'broken down by model' : 'broken down by harness') +
      '. Use the left and right arrow keys to read values.');
    hoverIdx = -1;
    var tip = $('chart-tip');
    if (tip) tip.hidden = true;
    renderChart(buckets());
  }

  var lastSeries = null;
  function buckets() { return lastSeries || bootBuckets; }

  function wireChart() {
    var canvas = $('chart'), wrap = $('chart-wrap');
    if (!canvas || !wrap) return;
    var modeHarness = $('mode-harness'), modeModel = $('mode-model');
    if (modeHarness) modeHarness.addEventListener('click', function () { setChartMode('harness'); });
    if (modeModel) modeModel.addEventListener('click', function () { setChartMode('model'); });
    canvas.addEventListener('pointermove', function (ev) {
      if (!chartGeom || !chartGeom.n) return;
      var rect = canvas.getBoundingClientRect();
      var i = Math.floor((ev.clientX - rect.left - chartGeom.padL) / chartGeom.band);
      i = Math.max(0, Math.min(chartGeom.n - 1, i));
      if (i !== hoverIdx) showChartHover(i);
    });
    canvas.addEventListener('pointerleave', hideChartHover);
    wrap.addEventListener('keydown', function (ev) {
      var n = chartGeom ? chartGeom.n : 0;
      if (!n) return;
      if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') {
        var next = hoverIdx < 0 ? n - 1 : Math.max(0, Math.min(n - 1, hoverIdx + (ev.key === 'ArrowRight' ? 1 : -1)));
        showChartHover(next);
        ev.preventDefault();
      } else if (ev.key === 'Escape') { hideChartHover(); }
    });
  }

  /* ---- recent events ---- */

  function badgeTone(e) {
    var status = (e.status_code === null || e.status_code === undefined) ? null : Number(e.status_code);
    if (status !== null && RATE_LIMIT_CODES.indexOf(status) !== -1) return ['crit', String(status)];
    if (e.usage_complete === 'final') return ['good', 'final'];
    return ['none', e.outcome || (status === null ? '—' : String(status))];
  }

  function renderEvents(events) {
    var tbody = $('events-body');
    if (!tbody) return;
    tbody.textContent = '';
    if (!events || !events.length) {
      var tr = el('tr');
      var cell = el('td', 'muted', 'No events yet');
      cell.colSpan = 8;
      tr.appendChild(cell);
      tbody.appendChild(tr);
      return;
    }
    events.forEach(function (e) {
      var toneLabel = badgeTone(e);
      var tr = el('tr', toneLabel[0] === 'crit' ? 'row-crit' : '');

      var tdTime = el('td', 'num', e.ts_sydney || '—');
      tdTime.title = e.ts || '';
      tr.appendChild(tdTime);

      var tdCaller = el('td');
      tdCaller.appendChild(el('span', 'chip ' + (e.unattributed ? 'h-unattr' : harnessClass(e.caller)), e.caller || UNATTR));
      tr.appendChild(tdCaller);

      tr.appendChild(el('td', '', e.model || '—'));
      tr.appendChild(el('td', '', e.route || e.path || '—'));
      tr.appendChild(el('td', 'num', fmtOpt(e.prompt_tokens)));
      tr.appendChild(el('td', 'num', fmtOpt(e.completion_tokens)));
      tr.appendChild(el('td', 'num total', fmtOpt(e.total_tokens)));

      var tdBadge = el('td');
      var badge = el('span', 'badge');
      badge.appendChild(el('span', 'dot-s tone-' + toneLabel[0]));
      badge.appendChild(el('span', '', toneLabel[1]));
      tdBadge.appendChild(badge);
      tr.appendChild(tdBadge);
      tbody.appendChild(tr);
    });
  }

  /* ---- error card, live pill, polling ---- */

  function showError(message) {
    $('error-card').classList.add('show');
    $('error-msg').textContent = message;
  }

  function hideError() {
    $('error-card').classList.remove('show');
  }

  var lastOkAt = null;

  function setLive(state) {
    var live = $('live');
    live.classList.remove('ok', 'stale', 'error');
    if (state !== 'connecting') live.classList.add(state);
  }

  function tick() {
    var live = $('live');
    if (live.classList.contains('error')) { setText('tick', 'retrying…'); return; }
    if (lastOkAt === null) { setText('tick', 'connecting…'); return; }
    var age = Math.max(0, Math.round((Date.now() - lastOkAt) / 1000));
    setText('tick', 'live');
    if (age >= POLL_MS / 1000 + 4) setLive('stale');
  }

  async function fetchJson(url) {
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, FETCH_TIMEOUT_MS);
    try {
      var res = await fetch(url, { cache: 'no-store', signal: ctrl.signal });
      return await res.json();
    } finally {
      clearTimeout(timer);
    }
  }

  var inFlight = false;

  async function poll() {
    if (inFlight) return;
    inFlight = true;
    try {
      var results = await Promise.all([
        fetchJson('/api/summary'),
        fetchJson('/api/timeseries'),
        fetchJson('/api/events?limit=' + DASHBOARD_EVENTS),
      ]);
      var summary = results[0], series = results[1], events = results[2];

      var errors = [];
      if (summary && summary.error) errors.push(summary.error);
      if (series && series.error) errors.push(series.error);
      if (events && events.error) errors.push(events.error);
      if (errors.length) showError(errors.join(' · '));
      else hideError();

      /* Refetch keeps the frame: previous renders hold until new data lands. */
      if (summary && !summary.error) renderSummary(summary);
      if (Array.isArray(series)) { lastSeries = series; renderChart(series); }
      if (Array.isArray(events)) renderEvents(events);

      if (!errors.length) {
        lastOkAt = Date.now();
        setLive('ok');
        setText('updated', fmtSydneyTime(lastOkAt));
      } else {
        setLive('error');
      }
    } catch (err) {
      showError('fetch failed: ' + err);
      setLive('error');
    } finally {
      inFlight = false;
      tick();
    }
  }

  function renderSummary(summary) {
    renderCards(summary);
    renderHarness(summary.per_caller_24h && summary.per_caller_24h.length ? summary.per_caller_24h : summary.per_caller);
    renderDonut(summary.by_model);
  }

  var bootBuckets = [];
  var boot = {};
  try { boot = JSON.parse($('bootstrap').textContent); } catch (e) { boot = {}; }

  if (boot.error) showError(boot.error);
  renderSummary(boot);
  bootBuckets = boot.per_hour || [];
  lastSeries = bootBuckets;
  renderChart(bootBuckets);
  renderEvents(boot.events || []);
  wireChart();
  wireDonut();
  tick();

  var resizeTimer = null;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      var tip = $('chart-tip');
      if (tip) tip.hidden = true;
      hoverIdx = -1;
      renderChart(buckets());
    }, 150);
  });

  /* refresh immediately when the tab becomes visible again */
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) poll();
  });

  setInterval(poll, POLL_MS);
  setInterval(tick, 1000);
})();
"""


# --------------------------------------------------------------------------
# HTML rendering (initial paint; the browser re-renders from JSON afterwards)
# --------------------------------------------------------------------------

FAVICON = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E"
    "%3Crect width='16' height='16' rx='3' fill='%230a0d12'/%3E"
    "%3Cpath d='M3.5 11.5v-4M7 11.5v-7M10.5 11.5v-3M13.5 11.5v-5' stroke='%233987e5' "
    "stroke-width='1.8' stroke-linecap='round'/%3E%3C/svg%3E"
)


def render_page(snapshot: dict[str, Any]) -> bytes:
    per_hour = snapshot.get("per_hour") or hour_buckets()
    events = snapshot.get("events") or []
    harness_rows = snapshot.get("per_caller_24h")
    if not harness_rows:
        harness_rows = snapshot.get("per_caller") or []

    if snapshot.get("error"):
        error_class = " error-card show"
        error_msg = esc(snapshot["error"])
    else:
        error_class = ""
        error_msg = "The usage ledger is temporarily unavailable; the page will keep retrying."

    tokens = [float(b.get("tokens") or 0) for b in per_hour]
    peak = max(tokens) if tokens else 0.0
    peak_note = (
        "peak " + fmt_compact(peak) + " · " + bucket_label(per_hour[tokens.index(peak)])
        if peak > 0
        else "no traffic in the last 24 h"
    )

    bootstrap = json.dumps(snapshot, separators=(",", ":")).replace("</", "<\\/")

    js = (
        JS
        .replace("__POLL_MS__", str(POLL_SECONDS * 1000))
        .replace("__FETCH_TIMEOUT_MS__", str(FETCH_TIMEOUT_MS))
        .replace("__DASHBOARD_EVENTS__", str(DASHBOARD_EVENTS))
        .replace("__HARNESS_COLOR_COUNT__", str(HARNESS_COLOR_COUNT))
        .replace("__DONUT_SIZE__", str(DONUT_SIZE))
    )

    page = (
        """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Harness Usage — Live</title>
<link rel="icon" href=\""""
        + FAVICON
        + """\">
<style>"""
        + CSS
        + """</style>
</head>
<body>
<div class="wrap">

<header class="topbar">
  <div>
    <h1>Harness <span class="accent">Usage</span></h1>
    <p class="subtitle">Live LLM token usage &middot; read-only view of the SQLite ledger &middot; times in Australia/Sydney</p>
  </div>
  <div class="live" id="live" role="status">
    <span class="dot" aria-hidden="true"></span>
    <span id="tick">connecting&hellip;</span>
    <span class="sep" aria-hidden="true">&middot;</span>
    <span class="updated" id="updated">&mdash;</span>
  </div>
</header>

<div class="card error-card"""
        + error_class
        + """\" id="error-card">
  <h2>Ledger unavailable</h2>
  <p id="error-msg">"""
        + error_msg
        + """</p>
</div>

<section class="cards" aria-label="Last 24 hours">"""
        + render_cards(snapshot)
        + """</section>

<div class="mid">
<section class="card chart-card" aria-label="Tokens per hour">
  <div class="card-head">
    <h2>Tokens per hour</h2>
    <div class="chart-mode" role="group" aria-label="Chart breakdown dimension">
      <button type="button" class="mode-btn active" id="mode-harness" aria-pressed="true">harness</button>
      <button type="button" class="mode-btn" id="mode-model" aria-pressed="false">model</button>
    </div>
    <div class="card-meta">
      <span class="win" id="chart-peak">"""
        + esc(peak_note)
        + """</span>
      <span class="win">last 24 h &middot; 1 h buckets &middot; axis in Sydney time</span>
    </div>
  </div>
  <div class="chart-wrap" id="chart-wrap" tabindex="0" role="group" aria-label="Column chart of tokens per hour over the last 24 hours, broken down by harness. Use the left and right arrow keys to read values.">
    <canvas id="chart" width="800" height="260"></canvas>
    <div class="tooltip" id="chart-tip" hidden></div>
  </div>
  """
        + chart_data_table(per_hour)
        + """
</section>

<div class="side">
<section class="card" aria-label="Per-harness usage">
  <div class="card-head"><h2>Per-harness usage</h2><span class="win">last 24 h &middot; bar = share of top harness</span></div>
  <div class="scroll-x">
  <table>
    <thead><tr><th scope="col">Harness</th><th scope="col" class="num">Requests</th><th scope="col" class="num">Tokens</th><th scope="col"><span class="sr-only">Share of tokens</span></th></tr></thead>
    """
        + harness_table_body(harness_rows)
        + """
  </table>
  </div>
</section>

"""
        + model_panel_html(snapshot.get("by_model"))
        + """
</div>
</div>

<section class="card" aria-label="Recent events">
  <div class="card-head">
    <h2>Recent events</h2>
    <span class="win">last """
        + str(len(events))
        + """ &middot; newest first &middot; <span class="dot-s tone-good"></span> final &middot; <span class="dot-s tone-crit"></span> 401/429 &middot; <span class="dot-s tone-none"></span> other</span>
  </div>
  <div class="scroll-x">
  <table class="events">
    <thead>
      <tr>
        <th scope="col">Time</th><th scope="col">Harness</th><th scope="col">Model</th><th scope="col">Route</th>
        <th scope="col" class="num">In</th><th scope="col" class="num">Out</th><th scope="col" class="num">Total</th><th scope="col">Outcome</th>
      </tr>
    </thead>
    <tbody id="events-body">"""
        + events_table_body(events)
        + """</tbody>
  </table>
  </div>
</section>

<footer>
  Polls /api/summary, /api/timeseries and /api/events every """
        + str(POLL_SECONDS)
        + """ s &middot; SQLite opened read-only (mode=ro) &middot; LAN only, no authentication.
  <noscript>Live updates need JavaScript &mdash; showing the snapshot from page load.</noscript>
</footer>
</div>

<script type="application/json" id="bootstrap">"""
        + bootstrap
        + """</script>
<script>"""
        + js
        + """</script>
</body>
</html>"""
    )
    return page.encode("utf-8")


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

class UsageProxyHandler(BaseHTTPRequestHandler):
    db_path: str = DEFAULT_DB
    server_version = "usage-proxy-webui/2"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        """Access logs are suppressed — a LAN dashboard must not spam stdout."""

    def log_error(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _events_body(self, parsed) -> tuple[int, str, bytes]:
        qs = parse_qs(parsed.query)
        limit = API_EVENTS_DEFAULT
        if "limit" in qs:
            try:
                limit = min(max(1, int(qs["limit"][0])), API_EVENTS_MAX)
            except (ValueError, IndexError):
                return 400, "text/plain; charset=utf-8", b"invalid limit\n"

        conn: sqlite3.Connection | None = None
        try:
            conn = open_db_readonly(self.db_path)
            events = query_events(conn, limit)
        except (sqlite3.Error, OSError) as exc:
            # Soft failure: HTTP 200 with an error payload so pollers keep polling.
            body: Any = {"error": f"ledger unavailable: {exc}"}
            return 200, "application/json; charset=utf-8", json.dumps(body, indent=2).encode("utf-8")
        finally:
            if conn is not None:
                conn.close()

        if "envelope" in qs and qs["envelope"][0] == "object":
            payload: Any = {"events": events, "limit": limit}  # pre-redesign shape
        else:
            payload = events
        return 200, "application/json; charset=utf-8", json.dumps(payload, indent=2).encode("utf-8")

    def _timeseries_body(self) -> tuple[int, str, bytes]:
        conn: sqlite3.Connection | None = None
        try:
            conn = open_db_readonly(self.db_path)
            buckets = query_timeseries(conn, cutoff_iso(HOURS))
        except (sqlite3.Error, OSError) as exc:
            body: Any = {"error": f"ledger unavailable: {exc}"}
            return 200, "application/json; charset=utf-8", json.dumps(body, indent=2).encode("utf-8")
        finally:
            if conn is not None:
                conn.close()
        return 200, "application/json; charset=utf-8", json.dumps(buckets, indent=2).encode("utf-8")

    def _body_for(self, parsed) -> tuple[int, str, bytes]:
        route = parsed.path
        if route == "/":
            # Always render the shell at HTTP 200 — even when the ledger is
            # unreachable — so the browser keeps a page that can keep polling.
            snapshot = fetch_snapshot(self.db_path, DASHBOARD_EVENTS)
            return 200, "text/html; charset=utf-8", render_page(snapshot)
        if route == "/api/summary":
            snapshot = fetch_snapshot(self.db_path, event_limit=0)
            payload = {k: v for k, v in snapshot.items() if k != "events"}
            return 200, "application/json; charset=utf-8", json.dumps(payload, indent=2).encode("utf-8")
        if route == "/api/timeseries":
            return self._timeseries_body()
        if route == "/api/events":
            return self._events_body(parsed)
        return 404, "text/plain; charset=utf-8", b"not found\n"

    def do_GET(self) -> None:
        try:
            status, content_type, body = self._body_for(urlparse(self.path))
        except Exception as exc:  # a handler must never kill the keep-alive connection
            self.log_error("request failed: %r", exc)
            try:
                self._send(500, "application/json; charset=utf-8", b'{"error": "internal error"}\n')
            except OSError:
                pass
            return
        try:
            self._send(status, content_type, body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client hung up mid-response; nothing to do

    def do_HEAD(self) -> None:
        try:
            status, content_type, body = self._body_for(urlparse(self.path))
        except Exception as exc:
            self.log_error("request failed: %r", exc)
            try:
                self.send_error(500)
            except OSError:
                pass
            return
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()


def main() -> None:
    parser = argparse.ArgumentParser(description="Live read-only usage-proxy ledger dashboard")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Bind host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Bind port (default: {DEFAULT_PORT})")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite ledger path, opened read-only (default: {DEFAULT_DB})")
    args = parser.parse_args()

    UsageProxyHandler.db_path = args.db
    server = ThreadingHTTPServer((args.host, args.port), UsageProxyHandler)
    server.daemon_threads = True
    sys.stderr.write(f"usage-proxy-webui serving on http://{args.host}:{args.port}  db={args.db}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nShutting down.\n")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
