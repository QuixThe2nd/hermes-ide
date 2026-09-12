#!/usr/bin/env python3
"""Read-only live web UI for the Hermes usage-proxy SQLite ledger.

Single file, Python stdlib only (http.server + sqlite3):

* the ledger is opened read-only per request (``mode=ro``), so the dashboard
  can never block the proxy's writers;
* the page polls ``/api/summary`` and ``/api/events`` every 5 s and re-renders
  in place — no full page reloads;
* any database failure (missing file, locked, corrupt) degrades to a soft
  error card at HTTP 200, so the poll loop never crashes.

Launch flags are unchanged — see ``usage-proxy-webui.service``.
"""

import argparse
import html
import json
import sqlite3
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
DASHBOARD_EVENTS = 50
API_EVENTS_DEFAULT = 200
API_EVENTS_MAX = 1000
HOURS = 24

# Status colours (dots paired with text labels; never the only carrier of meaning)
TONE_GOOD = "good"        # completed / final
TONE_WARN = "warn"        # usage partial / missing
TONE_SERIOUS = "serious"  # aborted
TONE_CRIT = "crit"        # rejected / upstream_error / HTTP >= 400

OUTCOME_TONE = {
    "completed": TONE_GOOD,
    "aborted": TONE_SERIOUS,
    "rejected": TONE_CRIT,
    "upstream_error": TONE_CRIT,
}

EMPTY_SUMMARY: dict[str, Any] = {
    "total_requests": 0,
    "total_tokens": 0,
    "per_model": [],
    "per_caller": [],
    "per_route": [],
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


def cutoff_24h_iso() -> str:
    return (utc_now() - timedelta(hours=24)).isoformat(timespec="milliseconds")


def to_sydney(ts: str | None) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(SYDNEY).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return html.escape(str(ts))


def sydney_hour_label(hour_utc: datetime) -> str:
    return hour_utc.astimezone(SYDNEY).strftime("%a %H:%M")


def caller_label(caller: Any) -> str:
    """Traffic with no recorded caller cannot be attributed to a harness."""
    return UNATTRIBUTED if not caller else str(caller)


def query_summary(conn: sqlite3.Connection, since_ts: str | None = None) -> dict[str, Any]:
    where = ""
    params: tuple[Any, ...] = ()
    if since_ts is not None:
        where = " WHERE ts >= ?"
        params = (since_ts,)

    row = conn.execute(
        f"SELECT COUNT(*), SUM(total_tokens) FROM usage_events{where}",
        params,
    ).fetchone()
    total_requests = row[0] or 0
    total_tokens = row[1] if row[1] is not None else 0

    models = conn.execute(
        f"""
        SELECT
            model,
            COUNT(*) AS requests,
            SUM(prompt_tokens) AS prompt_tokens,
            SUM(completion_tokens) AS completion_tokens,
            SUM(total_tokens) AS total_tokens
        FROM usage_events{where}
        GROUP BY model
        ORDER BY total_tokens DESC
        """,
        params,
    ).fetchall()

    per_model = [
        {
            "model": m[0] if m[0] is not None else "(null)",
            "requests": m[1] or 0,
            "prompt_tokens": m[2] if m[2] is not None else 0,
            "completion_tokens": m[3] if m[3] is not None else 0,
            "total_tokens": m[4] if m[4] is not None else 0,
        }
        for m in models
    ]

    callers = conn.execute(
        f"""
        SELECT
            caller,
            COUNT(*) AS requests,
            SUM(total_tokens) AS total_tokens
        FROM usage_events{where}
        GROUP BY caller
        ORDER BY total_tokens DESC
        """,
        params,
    ).fetchall()

    per_caller = [
        {
            "caller": caller_label(c[0]),
            "requests": c[1] or 0,
            "total_tokens": c[2] if c[2] is not None else 0,
        }
        for c in callers
    ]

    routes = conn.execute(
        f"""
        SELECT
            path,
            COUNT(*) AS requests,
            SUM(total_tokens) AS total_tokens
        FROM usage_events{where}
        GROUP BY path
        ORDER BY total_tokens DESC
        """,
        params,
    ).fetchall()

    per_route = [
        {
            "route": r[0] if r[0] is not None else "—",
            "requests": r[1] or 0,
            "total_tokens": r[2] if r[2] is not None else 0,
        }
        for r in routes
    ]

    return {
        "total_requests": total_requests,
        "total_tokens": total_tokens,
        "per_model": per_model,
        "per_caller": per_caller,
        "per_route": per_route,
    }


def hour_buckets() -> list[dict[str, Any]]:
    """24 empty hourly buckets ending with the current (just-started) hour."""
    current = utc_now().replace(minute=0, second=0, microsecond=0)
    buckets = []
    for i in range(HOURS - 1, -1, -1):
        start = current - timedelta(hours=i)
        buckets.append(
            {
                "hour_utc": start.isoformat(),
                "label_sydney": sydney_hour_label(start),
                "requests": 0,
                "tokens": 0,
            }
        )
    return buckets


def query_per_hour(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Tokens per hour over the last 24 h, bucketed on the ts column."""
    # Every recorded ts is a UTC ISO string, so its first 13 characters
    # ("YYYY-MM-DDTHH") are the hour bucket key.
    rows = conn.execute(
        """
        SELECT substr(ts, 1, 13) AS hour_key,
               COUNT(*) AS requests,
               SUM(total_tokens) AS tokens
        FROM usage_events
        WHERE ts >= ?
        GROUP BY hour_key
        """,
        (cutoff_24h_iso(),),
    ).fetchall()

    by_key = {r[0]: (r[1] or 0, r[2] if r[2] is not None else 0) for r in rows}
    buckets = hour_buckets()
    for bucket in buckets:
        requests, tokens = by_key.get(bucket["hour_utc"][:13], (0, 0))
        bucket["requests"] = requests
        bucket["tokens"] = tokens
    return buckets


def query_events(conn: sqlite3.Connection, limit: int = API_EVENTS_DEFAULT) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            id, ts, upstream, model, path, status_code, latency_ms,
            prompt_tokens, completion_tokens, cached_tokens, reasoning_tokens,
            cache_creation_tokens, total_tokens, outcome, usage_complete, caller
        FROM usage_events
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    events = []
    for r in rows:
        events.append(
            {
                "id": r[0],
                "ts": r[1],
                "ts_sydney": to_sydney(r[1]),
                "upstream": r[2],
                "model": r[3],
                "path": r[4],
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
            }
        )
    return events


def error_snapshot(message: str) -> dict[str, Any]:
    """A payload shaped like a real snapshot, but flagged as failed."""
    return {
        "error": message,
        "generated_at_utc": utc_now().isoformat(timespec="seconds"),
        "cutoff_24h_utc": cutoff_24h_iso(),
        "last_24h": dict(EMPTY_SUMMARY),
        "all_time": dict(EMPTY_SUMMARY),
        "total": {"requests": 0, "tokens": 0},
        "per_caller": [],
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
        cutoff = cutoff_24h_iso()
        last_24h = query_summary(conn, cutoff)
        all_time = query_summary(conn)
        per_hour = query_per_hour(conn)
        events = query_events(conn, event_limit) if event_limit > 0 else []
    except (sqlite3.Error, OSError) as exc:
        return error_snapshot(f"ledger query failed: {exc}")
    finally:
        conn.close()

    return {
        "error": None,
        "generated_at_utc": utc_now().isoformat(timespec="seconds"),
        "cutoff_24h_utc": cutoff,
        "last_24h": last_24h,
        "all_time": all_time,
        "total": {"requests": all_time["total_requests"], "tokens": all_time["total_tokens"]},
        "per_caller": all_time["per_caller"],
        "per_route": all_time["per_route"],
        "per_hour": per_hour,
        "events": events,
    }


# --------------------------------------------------------------------------
# Formatting helpers (server-side mirrors of the client JS)
# --------------------------------------------------------------------------

def fmt_int(value: Any) -> str:
    if value is None:
        return "—"
    return f"{value:,}"


def fmt_compact(value: Any) -> str:
    n = float(value or 0)
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if abs(n) >= divisor:
            text = f"{n / divisor:.1f}".rstrip("0").rstrip(".")
            return text + suffix
    return str(int(n))


def fmt_stat(value: Any) -> str:
    n = int(value or 0)
    return f"{n:,}" if abs(n) < 10_000 else fmt_compact(n)


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else "—"))


# --------------------------------------------------------------------------
# HTML rendering (initial paint; the browser re-renders from JSON afterwards)
# --------------------------------------------------------------------------

CSS = """
:root {
  color-scheme: dark;
  --bg: #0b1220;
  --surface: #101a2b;
  --surface-2: #0d1524;
  --border: #1e293b;
  --border-strong: #2a3a52;
  --grid: #1b2740;
  --text: #e7eef8;
  --text-2: #9fb2c9;
  --muted: #64748b;
  --accent: #3987e5;
  --accent-soft: rgba(57, 135, 229, 0.13);
  --good: #0ca30c;
  --warn: #fab219;
  --serious: #ec835a;
  --crit: #e05252;
  --unattr: #7c8aa0;
}
* { box-sizing: border-box; }
html, body { margin: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  font-size: 15px;
  line-height: 1.45;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1180px; margin: 0 auto; padding: 26px 20px 56px; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.9em; color: var(--text-2); }

.topbar { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }
h1 { margin: 0; font-size: 1.35rem; font-weight: 650; letter-spacing: -0.01em; }
.subtitle { margin: 3px 0 0; color: var(--text-2); font-size: 0.84rem; }
.live { display: flex; align-items: center; gap: 8px; font-size: 0.8rem; color: var(--text-2); font-variant-numeric: tabular-nums; }
.live .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--good); box-shadow: 0 0 0 3px rgba(12, 163, 12, 0.15); }
.live.stale .dot { background: var(--warn); box-shadow: 0 0 0 3px rgba(250, 178, 25, 0.15); }
.live.error .dot { background: var(--crit); box-shadow: 0 0 0 3px rgba(224, 82, 82, 0.15); }
@media (prefers-reduced-motion: no-preference) {
  .live .dot { animation: pulse 2.4s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: 0.45; } }
}

.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; margin-bottom: 14px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; min-width: 0; }
.card-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 10px; }
.card h2 { margin: 0; font-size: 0.95rem; font-weight: 600; }
.card .win { color: var(--muted); font-size: 0.75rem; }
.stat .label { color: var(--muted); font-size: 0.78rem; margin-bottom: 6px; }
.stat .value { font-size: 1.9rem; font-weight: 650; line-height: 1.1; }
.stat .hint { color: var(--muted); font-size: 0.74rem; margin-top: 6px; }

.grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; margin-bottom: 14px; }

table { width: 100%; border-collapse: collapse; font-size: 0.83rem; }
th { text-align: left; color: var(--muted); font-size: 0.7rem; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; padding: 7px 10px; border-bottom: 1px solid var(--border); }
td { padding: 8px 10px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover td { background: rgba(255, 255, 255, 0.02); }
tr.row-bad td { background: rgba(224, 82, 82, 0.05); }
th.num, td.num { text-align: right; font-variant-numeric: tabular-nums; }
td.bar-cell { width: 34%; min-width: 90px; }
.muted { color: var(--muted); }
.caller { font-weight: 550; }
.unattr { color: var(--unattr); font-style: italic; }

.bar-track { height: 8px; background: var(--accent-soft); border-radius: 4px; overflow: hidden; }
.bar-fill { height: 100%; min-width: 3px; background: var(--accent); border-radius: 0 4px 4px 0; }
.bar-fill.unattr { background: var(--unattr); }

.spark-wrap { position: relative; outline: none; }
.spark-wrap:focus-visible { outline: 2px solid var(--accent); outline-offset: 4px; border-radius: 6px; }
.spark-wrap svg { display: block; width: 100%; height: 150px; }
.spark-line { fill: none; stroke: var(--accent); stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; vector-effect: non-scaling-stroke; }
.spark-area { fill: var(--accent); opacity: 0.1; stroke: none; }
.spark-base { stroke: var(--grid); stroke-width: 1; vector-effect: non-scaling-stroke; }
.spark-dot { fill: var(--accent); stroke: var(--surface); stroke-width: 2; }
.spark-cross { position: absolute; top: 12px; bottom: 20px; width: 1px; background: var(--border-strong); pointer-events: none; }
.spark-axis { display: flex; justify-content: space-between; gap: 12px; color: var(--muted); font-size: 0.72rem; margin-top: 4px; font-variant-numeric: tabular-nums; }

.tooltip { position: absolute; z-index: 5; transform: translate(-50%, 0); background: rgba(13, 21, 36, 0.97); border: 1px solid var(--border-strong); border-radius: 8px; padding: 7px 10px; font-size: 0.78rem; pointer-events: none; box-shadow: 0 10px 26px rgba(0, 0, 0, 0.45); white-space: nowrap; }
.tooltip .tv { font-weight: 650; font-variant-numeric: tabular-nums; }
.tooltip .tl { color: var(--text-2); margin-top: 2px; }

.badge { display: inline-flex; align-items: center; gap: 7px; }
.dot-s { flex: none; width: 8px; height: 8px; border-radius: 50%; }
.tone-good { background: var(--good); }
.tone-warn { background: var(--warn); }
.tone-serious { background: var(--serious); }
.tone-crit { background: var(--crit); }
.badge .code { font-variant-numeric: tabular-nums; }

.error-card { display: none; margin-bottom: 14px; border-color: rgba(224, 82, 82, 0.4); background: rgba(224, 82, 82, 0.07); }
.error-card.show { display: block; }
.error-card h2 { color: #f1a1a1; }
.error-card p { margin: 6px 0 0; color: var(--text-2); font-size: 0.82rem; overflow-wrap: anywhere; }

.scroll-x { overflow-x: auto; }
footer { margin-top: 22px; color: var(--muted); font-size: 0.75rem; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }

@media (max-width: 560px) {
  body { font-size: 14px; }
  .wrap { padding: 18px 12px 44px; }
  h1 { font-size: 1.15rem; }
  .stat .value { font-size: 1.55rem; }
  .card { padding: 13px 14px; }
  th, td { padding: 7px 8px; }
}
"""

JS = """
(function () {
  'use strict';

  var POLL_MS = __POLL_MS__;
  var FETCH_TIMEOUT_MS = __FETCH_TIMEOUT_MS__;
  var EVENTS_URL = '/api/events?limit=__DASHBOARD_EVENTS__';
  var UNATTR = 'unattributed';
  var OUTCOME_TONE = { completed: 'good', aborted: 'serious', rejected: 'crit', upstream_error: 'crit' };

  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function fmtCompact(value) {
    var n = Number(value) || 0;
    var steps = [[1e9, 'B'], [1e6, 'M'], [1e3, 'k']];
    for (var i = 0; i < steps.length; i++) {
      if (Math.abs(n) >= steps[i][0]) {
        var v = n / steps[i][0];
        var text = Math.abs(v) >= 100 ? String(Math.round(v)) : v.toFixed(1).replace(/\\.0$/, '');
        return text + steps[i][1];
      }
    }
    return String(Math.round(n));
  }

  function fmtTok(value) { return (value === null || value === undefined) ? '\\u2014' : fmtCompact(value); }

  function fmtInt(value) {
    if (value === null || value === undefined) return '\\u2014';
    return Number(value).toLocaleString('en-US');
  }

  function fmtStat(value) {
    var n = Number(value) || 0;
    return Math.abs(n) < 10000 ? n.toLocaleString('en-US') : fmtCompact(n);
  }

  var sydFmt = null;
  try {
    sydFmt = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Australia/Sydney', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
    });
  } catch (e) { /* no ICU tz data: fall back to UTC below */ }

  function fmtSydney(ts) {
    if (!ts) return '\\u2014';
    var d = new Date(/(?:Z|[+-]\\d\\d:\\d\\d)$/.test(ts) ? ts : ts + 'Z');
    if (isNaN(d.getTime())) return ts;
    if (!sydFmt) return d.toISOString().replace('T', ' ').slice(0, 19);
    return sydFmt.format(d).replace(', ', ' ');
  }

  function isUnattributed(caller) { return !caller || caller === UNATTR; }

  /* ---- stat cards ---- */

  function renderCards(summary) {
    var last24 = summary.last_24h || {}, all = summary.all_time || {};
    $('s-req-24h').textContent = fmtStat(last24.total_requests);
    $('s-tok-24h').textContent = fmtStat(last24.total_tokens);
    $('s-req-all').textContent = fmtStat(all.total_requests);
    $('s-tok-all').textContent = fmtStat(all.total_tokens);
  }

  /* ---- bar tables (per harness / per route) ---- */

  function renderBarTable(tbodyId, rows, labelKey, unattrCheck) {
    var tbody = $(tbodyId);
    tbody.textContent = '';
    if (!rows || !rows.length) {
      var empty = el('tr');
      var cell = el('td', 'muted', 'No events');
      cell.colSpan = 4;
      empty.appendChild(cell);
      tbody.appendChild(empty);
      return;
    }
    var max = 0;
    rows.forEach(function (r) { max = Math.max(max, Number(r.total_tokens) || 0); });
    rows.forEach(function (r) {
      var tokens = Number(r.total_tokens) || 0;
      var un = unattrCheck(r);
      var tr = el('tr');
      var tdLabel = el('td');
      tdLabel.appendChild(el('span', un ? 'unattr' : 'caller', r[labelKey] || UNATTR));
      tr.appendChild(tdLabel);
      tr.appendChild(el('td', 'num', fmtInt(r.requests)));
      tr.appendChild(el('td', 'num', fmtTok(r.total_tokens)));
      var tdBar = el('td', 'bar-cell');
      var track = el('div', 'bar-track');
      track.setAttribute('aria-hidden', 'true');
      if (max > 0 && tokens > 0) {
        var fill = el('div', un ? 'bar-fill unattr' : 'bar-fill');
        fill.style.width = Math.max(1.5, (tokens / max) * 100) + '%';
        track.appendChild(fill);
      }
      tdBar.appendChild(track);
      tr.appendChild(tdBar);
      tbody.appendChild(tr);
    });
  }

  /* ---- sparkline ---- */

  var sparkGeom = null;
  var H = 150, PAD_L = 6, PAD_R = 12, PAD_T = 12, PAD_B = 20;

  function renderSparkline(buckets) {
    var wrap = $('spark-wrap');
    var svg = $('spark-svg');
    var width = wrap.clientWidth || 720;
    var tokens = buckets.map(function (b) { return Number(b.tokens) || 0; });
    var max = Math.max.apply(null, tokens.concat([0]));
    var n = buckets.length;
    var base = H - PAD_B, top = PAD_T;

    function x(i) { return PAD_L + (n < 2 ? 0 : i * (width - PAD_L - PAD_R) / (n - 1)); }
    function y(v) { return max > 0 ? top + (1 - v / max) * (base - top) : base; }

    var line = '', area = 'M ' + x(0).toFixed(1) + ' ' + base;
    for (var i = 0; i < n; i++) {
      var px = x(i).toFixed(1), py = y(tokens[i]).toFixed(1);
      line += (i === 0 ? 'M ' : ' L ') + px + ' ' + py;
      area += ' L ' + px + ' ' + py;
    }
    area += ' L ' + x(n - 1).toFixed(1) + ' ' + base + ' Z';

    var markup = '<path class="spark-base" d="M ' + PAD_L + ' ' + base + ' H ' + (width - PAD_R) + '"/>' +
      '<path class="spark-area" d="' + area + '"/>' +
      '<path class="spark-line" d="' + line + '"/>';
    if (max > 0) {
      markup += '<circle class="spark-dot" cx="' + x(n - 1).toFixed(1) + '" cy="' + y(tokens[n - 1]).toFixed(1) + '" r="4.5"/>';
    }
    svg.setAttribute('viewBox', '0 0 ' + width + ' ' + H);
    svg.setAttribute('width', width);
    svg.setAttribute('height', H);
    svg.innerHTML = markup;

    var peakIdx = 0;
    for (var k = 1; k < n; k++) { if (tokens[k] > tokens[peakIdx]) peakIdx = k; }
    $('spark-peak').textContent = max > 0
      ? 'peak ' + fmtCompact(tokens[peakIdx]) + ' tokens \\u00b7 ' + buckets[peakIdx].label_sydney
      : 'no traffic in the last 24h';
    $('spark-first').textContent = buckets.length ? buckets[0].label_sydney : '';

    sparkGeom = { width: width, n: n, buckets: buckets, max: max };
    hideSparkHover();
  }

  function bucketAt(px) {
    if (!sparkGeom || sparkGeom.n < 2) return 0;
    var step = (sparkGeom.width - PAD_L - PAD_R) / (sparkGeom.n - 1);
    return Math.max(0, Math.min(sparkGeom.n - 1, Math.round((px - PAD_L) / step)));
  }

  function showSparkHover(i) {
    if (!sparkGeom || !sparkGeom.n) return;
    var b = sparkGeom.buckets[i];
    var step = sparkGeom.n < 2 ? 0 : (sparkGeom.width - PAD_L - PAD_R) / (sparkGeom.n - 1);
    var x = PAD_L + i * step;
    var cross = $('spark-cross'), tip = $('spark-tip');
    cross.hidden = false;
    cross.style.left = x + 'px';
    tip.textContent = '';
    tip.appendChild(el('div', 'tv', fmtCompact(b.tokens) + ' tokens'));
    tip.appendChild(el('div', 'tl', b.label_sydney + ' \\u00b7 ' + fmtInt(b.requests) + ' req'));
    tip.hidden = false;
    var half = tip.offsetWidth / 2 + 6;
    tip.style.left = Math.max(half, Math.min(sparkGeom.width - half, x)) + 'px';
    tip.style.top = '10px';
  }

  function hideSparkHover() {
    $('spark-cross').hidden = true;
    $('spark-tip').hidden = true;
  }

  function wireSparkline() {
    var wrap = $('spark-wrap');
    var focusIdx = -1;

    wrap.addEventListener('pointermove', function (ev) {
      focusIdx = bucketAt(ev.clientX - wrap.getBoundingClientRect().left);
      showSparkHover(focusIdx);
    });
    wrap.addEventListener('pointerleave', function () { focusIdx = -1; hideSparkHover(); });
    wrap.addEventListener('keydown', function (ev) {
      var n = sparkGeom ? sparkGeom.n : 0;
      if (!n) return;
      if (ev.key === 'ArrowRight' || ev.key === 'ArrowLeft') {
        focusIdx = focusIdx < 0 ? n - 1 : Math.max(0, Math.min(n - 1, focusIdx + (ev.key === 'ArrowRight' ? 1 : -1)));
        showSparkHover(focusIdx);
        ev.preventDefault();
      } else if (ev.key === 'Home') { focusIdx = 0; showSparkHover(0); ev.preventDefault(); }
      else if (ev.key === 'End') { focusIdx = n - 1; showSparkHover(n - 1); ev.preventDefault(); }
      else if (ev.key === 'Escape') { focusIdx = -1; hideSparkHover(); }
    });
    wrap.addEventListener('blur', function () { focusIdx = -1; hideSparkHover(); });
  }

  /* ---- events ---- */

  function renderEvents(events) {
    var tbody = $('events-body');
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
      var un = isUnattributed(e.caller);
      var tr = el('tr');
      if (e.outcome && e.outcome !== 'completed') tr.className = 'row-bad';

      var tdTime = el('td', 'num', e.ts_sydney || fmtSydney(e.ts));
      tdTime.title = e.ts || '';
      tr.appendChild(tdTime);

      var tdCaller = el('td');
      tdCaller.appendChild(el('span', un ? 'unattr' : 'caller', e.caller || UNATTR));
      tr.appendChild(tdCaller);

      tr.appendChild(el('td', '', e.path || '\\u2014'));
      tr.appendChild(el('td', '', e.model || '\\u2014'));

      var tdStatus = el('td');
      var badge = el('span', 'badge');
      var tone = OUTCOME_TONE[e.outcome] ||
        (e.status_code !== null && e.status_code !== undefined && e.status_code >= 400 ? 'crit' : null);
      if (tone) badge.appendChild(el('span', 'dot-s tone-' + tone));
      badge.appendChild(el('span', 'code', (e.status_code === null || e.status_code === undefined) ? '\\u2014' : e.status_code));
      if (e.outcome) badge.appendChild(el('span', 'muted', e.outcome));
      tdStatus.appendChild(badge);
      tr.appendChild(tdStatus);

      tr.appendChild(el('td', 'num', fmtTok(e.prompt_tokens)));
      tr.appendChild(el('td', 'num', fmtTok(e.completion_tokens)));

      var tdTotal = el('td', 'num');
      tdTotal.appendChild(document.createTextNode(fmtTok(e.total_tokens)));
      if (e.usage_complete === 'partial' || e.usage_complete === 'missing') {
        var warn = el('span', 'dot-s tone-warn');
        warn.style.marginLeft = '6px';
        warn.title = 'usage ' + e.usage_complete;
        tdTotal.appendChild(warn);
      }
      tr.appendChild(tdTotal);

      tbody.appendChild(tr);
    });
  }

  /* ---- error card, live tick, polling ---- */

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
    live.classList.remove('stale', 'error');
    if (state === 'error') live.classList.add('error');
    else if (state === 'stale') live.classList.add('stale');
  }

  function tick() {
    var live = $('live');
    if (live.classList.contains('error')) { $('tick').textContent = 'update failed \\u00b7 retrying'; return; }
    if (lastOkAt === null) { $('tick').textContent = 'loading\\u2026'; return; }
    var age = Math.max(0, Math.round((Date.now() - lastOkAt) / 1000));
    $('tick').textContent = age < 2 ? 'updated just now' : 'updated ' + age + 's ago';
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
  var lastSummary = null;

  async function poll() {
    if (inFlight) return;
    inFlight = true;
    try {
      var results = await Promise.all([fetchJson('/api/summary'), fetchJson(EVENTS_URL)]);
      var summary = results[0], events = results[1];

      var errors = [];
      if (summary && summary.error) errors.push(summary.error);
      if (events && events.error) errors.push(events.error);
      if (errors.length) showError(errors.join(' \\u00b7 '));
      else hideError();

      if (summary && !summary.error) {
        lastSummary = summary;
        renderSummary(summary);
      }
      if (Array.isArray(events)) renderEvents(events);

      if (!errors.length) { lastOkAt = Date.now(); setLive('ok'); }
      else setLive('error');
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
    renderBarTable('harness-body', summary.per_caller, 'caller', function (r) { return isUnattributed(r.caller); });
    var last24 = summary.last_24h || {};
    renderBarTable('route-body', last24.per_route, 'route', function () { return false; });
    renderSparkline(summary.per_hour || []);
  }

  var boot = {};
  try { boot = JSON.parse($('bootstrap').textContent); } catch (e) { boot = {}; }

  if (boot.error) showError(boot.error);
  renderSummary(boot);
  renderEvents(boot.events || []);
  wireSparkline();
  tick();

  var resizeTimer = null;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      renderSparkline((lastSummary || boot).per_hour || []);
    }, 150);
  });

  setInterval(poll, POLL_MS);
  setInterval(tick, 1000);
})();
"""


def sparkline_svg_fallback(buckets: list[dict[str, Any]]) -> str:
    """Static sparkline for the initial (no-JS) paint. The browser rebuilds
    this from JSON on every poll with pixel-exact geometry."""
    width, height = 720, 150
    pad_l, pad_r, pad_t, pad_b = 6, 12, 12, 20
    base = height - pad_b
    top = pad_t
    n = len(buckets)
    tokens = [float(b.get("tokens") or 0) for b in buckets]
    peak = max(tokens) if tokens else 0.0

    def x(i: int) -> float:
        return pad_l if n < 2 else pad_l + i * (width - pad_l - pad_r) / (n - 1)

    def y(v: float) -> float:
        return base if peak <= 0 else top + (1 - v / peak) * (base - top)

    line = " ".join(f"{'M' if i == 0 else 'L'} {x(i):.1f} {y(v):.1f}" for i, v in enumerate(tokens))
    area = (
        f"M {x(0):.1f} {base} "
        + " ".join(f"L {x(i):.1f} {y(v):.1f}" for i, v in enumerate(tokens))
        + f" L {x(n - 1):.1f} {base} Z"
    )

    parts = [
        f'<path class="spark-base" d="M {pad_l} {base} H {width - pad_r}"/>',
        f'<path class="spark-area" d="{area}"/>',
        f'<path class="spark-line" d="{line}"/>',
    ]
    if peak > 0:
        parts.append(f'<circle class="spark-dot" cx="{x(n - 1):.1f}" cy="{y(tokens[-1]):.1f}" r="4.5"/>')
    return (
        f'<svg id="spark-svg" viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" '
        f'aria-label="Tokens per hour over the last 24 hours">' + "".join(parts) + "</svg>"
    )


def bar_table_body(tbody_id: str, rows: list[dict[str, Any]], label_key: str) -> str:
    if not rows:
        return f'<tbody id="{tbody_id}"><tr><td colspan="4" class="muted">No events</td></tr></tbody>'
    peak = max((float(r.get("total_tokens") or 0) for r in rows), default=0.0)
    out = []
    for r in rows:
        tokens = float(r.get("total_tokens") or 0)
        unattr = label_key == "caller" and not r.get(label_key)
        label = r.get(label_key) or UNATTRIBUTED
        fill = ""
        if peak > 0 and tokens > 0:
            width = max(1.5, tokens / peak * 100)
            fill = f'<div class="bar-fill{" unattr" if unattr else ""}" style="width:{width:.1f}%"></div>'
        out.append(
            "<tr>"
            f'<td><span class="{"unattr" if unattr else "caller"}">{esc(label)}</span></td>'
            f'<td class="num">{fmt_int(r.get("requests"))}</td>'
            f'<td class="num">{esc(fmt_compact(tokens))}</td>'
            f'<td class="bar-cell"><div class="bar-track" aria-hidden="true">{fill}</div></td>'
            "</tr>"
        )
    return f'<tbody id="{tbody_id}">' + "".join(out) + "</tbody>"


def events_table_body(events: list[dict[str, Any]]) -> str:
    if not events:
        return '<tr><td colspan="8" class="muted">No events yet</td></tr>'
    out = []
    for e in events:
        unattr = not e.get("caller") or e.get("caller") == UNATTRIBUTED
        outcome = e.get("outcome")
        status_code = e.get("status_code")
        tone = OUTCOME_TONE.get(outcome)
        if tone is None and isinstance(status_code, int) and status_code >= 400:
            tone = TONE_CRIT
        dot = f'<span class="dot-s tone-{tone}"></span>' if tone else ""
        outcome_txt = f'<span class="muted">{esc(outcome)}</span>' if outcome else ""
        row_cls = ' class="row-bad"' if outcome and outcome != "completed" else ""
        warn = ""
        if e.get("usage_complete") in ("partial", "missing"):
            warn = f'<span class="dot-s tone-{TONE_WARN}" title="usage {esc(e["usage_complete"])}"></span>'
        out.append(
            f"<tr{row_cls}>"
            f'<td class="num" title="{esc(e.get("ts"))}">{esc(e.get("ts_sydney"))}</td>'
            f'<td><span class="{"unattr" if unattr else "caller"}">{esc(e.get("caller") or UNATTRIBUTED)}</span></td>'
            f"<td>{esc(e.get('path'))}</td>"
            f"<td>{esc(e.get('model'))}</td>"
            f'<td><span class="badge">{dot}<span class="code">{fmt_int(status_code)}</span>{outcome_txt}</span></td>'
            f'<td class="num">{esc(fmt_compact(e.get("prompt_tokens")))}</td>'
            f'<td class="num">{esc(fmt_compact(e.get("completion_tokens")))}</td>'
            f'<td class="num">{esc(fmt_compact(e.get("total_tokens")))}{warn}</td>'
            "</tr>"
        )
    return "".join(out)


def stat_card(label: str, value: Any, hint: str) -> str:
    return (
        '<div class="card stat">'
        f'<div class="label">{esc(label)}</div>'
        f'<div class="value">{esc(fmt_stat(value))}</div>'
        f'<div class="hint">{esc(hint)}</div>'
        "</div>"
    )


def render_page(snapshot: dict[str, Any], db_path: str) -> bytes:
    events = snapshot.get("events") or []
    last_24h = snapshot.get("last_24h") or dict(EMPTY_SUMMARY)
    all_time = snapshot.get("all_time") or dict(EMPTY_SUMMARY)
    per_hour = snapshot.get("per_hour") or hour_buckets()

    cards = "".join(
        [
            stat_card("Requests · last 24h", last_24h["total_requests"], "rolling 24-hour window"),
            stat_card("Tokens · last 24h", last_24h["total_tokens"], "prompt + completion"),
            stat_card("Requests · all time", all_time["total_requests"], "since first event"),
            stat_card("Tokens · all time", all_time["total_tokens"], db_path.rsplit("/", 1)[-1]),
        ]
    )

    if snapshot.get("error"):
        error_class = " error-card show"
        error_msg = esc(snapshot["error"])
    else:
        error_class = " error-card"
        error_msg = "The usage ledger is temporarily unavailable; the page will keep retrying."

    bootstrap = json.dumps(
        {
            "error": snapshot.get("error"),
            "last_24h": last_24h,
            "all_time": all_time,
            "per_caller": snapshot.get("per_caller") or [],
            "per_route": snapshot.get("per_route") or [],
            "per_hour": per_hour,
            "events": events,
        },
        separators=(",", ":"),
    ).replace("</", "<\\/")

    peak_bucket = max(per_hour, key=lambda b: float(b.get("tokens") or 0), default=None)
    peak_tokens = float(peak_bucket.get("tokens") or 0) if peak_bucket else 0.0
    peak_note = (
        f"peak {fmt_compact(peak_tokens)} tokens · {peak_bucket['label_sydney']}"
        if peak_tokens > 0 and peak_bucket
        else "no traffic in the last 24h"
    )

    js = (
        JS.replace("__POLL_MS__", str(POLL_SECONDS * 1000))
        .replace("__FETCH_TIMEOUT_MS__", str(FETCH_TIMEOUT_MS))
        .replace("__DASHBOARD_EVENTS__", str(DASHBOARD_EVENTS))
    )

    page = (
        """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Usage Proxy Ledger</title>
<style>"""
        + CSS
        + """</style>
</head>
<body>
<div class="wrap">

<header class="topbar">
  <div>
    <h1>Usage Proxy Ledger</h1>
    <p class="subtitle">Read-only view of the usage-proxy ledger · harness attribution from <code>caller</code></p>
  </div>
  <div class="live" id="live" aria-live="polite">
    <span class="dot" aria-hidden="true"></span>
    <span id="tick">updated just now</span>
  </div>
</header>

<div class="card"""
        + error_class
        + """>
  <h2>Ledger unavailable</h2>
  <p id="error-msg">"""
        + error_msg
        + """</p>
</div>

<section class="cards" aria-label="Totals">"""
        + cards
        + """</section>

<section class="grid-2" aria-label="Breakdowns">
  <div class="card" id="per-harness">
    <div class="card-head"><h2>Per-harness usage</h2><span class="win">all time</span></div>
    <div class="scroll-x">
    <table>
      <thead><tr><th scope="col">Harness</th><th scope="col" class="num">Requests</th><th scope="col" class="num">Tokens</th><th scope="col"><span class="sr-only">Share of tokens</span></th></tr></thead>
      """
        + bar_table_body("harness-body", snapshot.get("per_caller") or [], "caller")
        + """
    </table>
    </div>
  </div>
  <div class="card" id="routes">
    <div class="card-head"><h2>Per-route usage</h2><span class="win">last 24h</span></div>
    <div class="scroll-x">
    <table>
      <thead><tr><th scope="col">Route</th><th scope="col" class="num">Requests</th><th scope="col" class="num">Tokens</th><th scope="col"><span class="sr-only">Share of tokens</span></th></tr></thead>
      """
        + bar_table_body("route-body", last_24h.get("per_route") or [], "route")
        + """
    </table>
    </div>
  </div>
</section>

<section class="card" style="margin-bottom:14px" aria-label="Tokens per hour">
  <div class="card-head"><h2>Tokens per hour</h2><span class="win" id="spark-peak">"""
        + esc(peak_note)
        + """</span></div>
  <div class="spark-wrap" id="spark-wrap" tabindex="0" aria-label="Tokens per hour, last 24 hours. Use the left and right arrow keys to read values.">
    """
        + sparkline_svg_fallback(per_hour)
        + """
    <div class="spark-cross" id="spark-cross" hidden></div>
    <div class="tooltip" id="spark-tip" hidden></div>
  </div>
  <div class="spark-axis"><span id="spark-first">"""
        + esc(per_hour[0]["label_sydney"] if per_hour else "")
        + """</span><span>24h · times in Australia/Sydney</span><span>now</span></div>
</section>

<section class="card" id="events" aria-label="Recent events">
  <div class="card-head"><h2>Recent events</h2><span class="win">last """
        + str(len(events))
        + """</span></div>
  <div class="scroll-x">
  <table class="events">
    <thead>
      <tr>
        <th scope="col">Time</th><th scope="col">Harness</th><th scope="col">Route</th>
        <th scope="col">Model</th><th scope="col">Status</th>
        <th scope="col" class="num">In</th><th scope="col" class="num">Out</th><th scope="col" class="num">Total</th>
      </tr>
    </thead>
    <tbody id="events-body">"""
        + events_table_body(events)
        + """</tbody>
  </table>
  </div>
</section>

<footer>
  Polls /api/summary and /api/events every """
        + str(POLL_SECONDS)
        + """ s · read-only SQLite access · LAN-only, no authentication.
  <noscript>Live updates need JavaScript — showing the snapshot from page load.</noscript>
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

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} {format % args}")

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path

        if route == "/":
            self._handle_dashboard()
        elif route == "/api/summary":
            self._handle_api_summary()
        elif route == "/api/events":
            self._handle_api_events(parsed)
        else:
            self.send_error(404, "Not Found")

    def _handle_dashboard(self) -> None:
        # Always render the shell at HTTP 200 — even when the ledger is
        # unreachable — so the browser keeps a page that can keep polling.
        snapshot = fetch_snapshot(self.db_path, DASHBOARD_EVENTS)
        self._send(200, "text/html; charset=utf-8", render_page(snapshot, self.db_path))

    def _handle_api_summary(self) -> None:
        snapshot = fetch_snapshot(self.db_path, event_limit=0)
        payload = {k: v for k, v in snapshot.items() if k != "events"}
        self._send(200, "application/json; charset=utf-8", json.dumps(payload, indent=2).encode("utf-8"))

    def _handle_api_events(self, parsed) -> None:
        qs = parse_qs(parsed.query)
        limit = API_EVENTS_DEFAULT
        if "limit" in qs and qs["limit"]:
            try:
                limit = min(max(1, int(qs["limit"][0])), API_EVENTS_MAX)
            except ValueError:
                self.send_error(400, "Invalid limit")
                return

        conn: sqlite3.Connection | None = None
        try:
            conn = open_db_readonly(self.db_path)
            events = query_events(conn, limit)
        except (sqlite3.Error, OSError) as exc:
            # Soft failure: HTTP 200 with an error payload so pollers keep polling.
            self._send(200, "application/json; charset=utf-8",
                       json.dumps({"error": f"ledger unavailable: {exc}"}, indent=2).encode("utf-8"))
            return
        finally:
            if conn is not None:
                conn.close()

        if "envelope" in qs and qs["envelope"][0] == "object":
            body: Any = {"events": events, "limit": limit}  # pre-rewrite shape
        else:
            body = events
        self._send(200, "application/json; charset=utf-8", json.dumps(body, indent=2).encode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only usage-proxy ledger web UI")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Bind host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Bind port (default: {DEFAULT_PORT})")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite path (default: {DEFAULT_DB})")
    args = parser.parse_args()

    UsageProxyHandler.db_path = args.db
    server = ThreadingHTTPServer((args.host, args.port), UsageProxyHandler)
    print(f"Serving on http://{args.host}:{args.port}  db={args.db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
