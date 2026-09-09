#!/usr/bin/env python3
"""Read-only web UI for the Hermes usage-proxy SQLite ledger."""

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
DEFAULT_HOST = "192.168.30.20"
DEFAULT_PORT = 9136
DEFAULT_DB = "/root/.hermes/usage-proxy/usage.sqlite"


def open_db_readonly(path: str) -> sqlite3.Connection:
    """Open SQLite in read-only URI mode."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


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
        return dt.astimezone(SYDNEY).strftime("%Y-%m-%d %H:%M:%S %Z")
    except (TypeError, ValueError):
        return html.escape(str(ts))


def fmt_int(value: Any) -> str:
    if value is None:
        return "—"
    return str(value)


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

    return {
        "total_requests": total_requests,
        "total_tokens": total_tokens,
        "per_model": per_model,
    }


def query_events(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            id, ts, upstream, model, path, status_code, latency_ms,
            prompt_tokens, completion_tokens, cached_tokens, reasoning_tokens,
            cache_creation_tokens, total_tokens, outcome, usage_complete
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
            }
        )
    return events


def error_page(title: str, message: str) -> bytes:
    body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
body {{ font-family: system-ui, sans-serif; background: #1a1a2e; color: #eee; margin: 2rem; }}
.error {{ background: #3d1f1f; border: 1px solid #a44; padding: 1.5rem; border-radius: 8px; max-width: 40rem; }}
h1 {{ color: #f88; margin-top: 0; }}
</style>
</head>
<body>
<div class="error">
<h1>{html.escape(title)}</h1>
<p>{html.escape(message)}</p>
</div>
</body>
</html>"""
    return body.encode("utf-8")


def render_dashboard(
    summary_24h: dict[str, Any],
    summary_all: dict[str, Any],
    events: list[dict[str, Any]],
) -> bytes:
    def render_cards(label: str, summary: dict[str, Any]) -> str:
        models_html = ""
        for m in summary["per_model"]:
            models_html += (
                "<tr>"
                f"<td>{html.escape(str(m['model']))}</td>"
                f"<td>{m['requests']}</td>"
                f"<td>{m['prompt_tokens']}</td>"
                f"<td>{m['completion_tokens']}</td>"
                f"<td>{m['total_tokens']}</td>"
                "</tr>"
            )
        if not models_html:
            models_html = '<tr><td colspan="5" class="muted">No events</td></tr>'

        return f"""
<section class="summary-block">
<h2>{html.escape(label)}</h2>
<div class="cards">
  <div class="card"><div class="label">Requests</div><div class="value">{summary['total_requests']}</div></div>
  <div class="card"><div class="label">Total tokens</div><div class="value">{summary['total_tokens']}</div></div>
</div>
<table class="model-table">
<thead><tr><th>Model</th><th>Requests</th><th>Prompt</th><th>Completion</th><th>Total</th></tr></thead>
<tbody>{models_html}</tbody>
</table>
</section>"""

    events_html = ""
    for e in events:
        row_class = ""
        if e.get("outcome") and e["outcome"] != "completed":
            row_class = ' class="row-error"'

        uc = e.get("usage_complete") or ""
        uc_class = ""
        if uc in ("partial", "missing"):
            uc_class = ' class="amber"'

        uc_attr = f' {uc_class.strip()}' if uc_class else ""
        events_html += (
            f"<tr{row_class}>"
            f"<td>{html.escape(to_sydney(e.get('ts')))}</td>"
            f"<td>{html.escape(str(e.get('upstream') or '—'))}</td>"
            f"<td>{html.escape(str(e.get('model') or '—'))}</td>"
            f"<td>{html.escape(str(e.get('path') or '—'))}</td>"
            f"<td>{fmt_int(e.get('status_code'))}</td>"
            f"<td>{fmt_int(e.get('latency_ms'))}</td>"
            f"<td>{fmt_int(e.get('prompt_tokens'))}</td>"
            f"<td>{fmt_int(e.get('completion_tokens'))}</td>"
            f"<td>{fmt_int(e.get('cached_tokens'))}</td>"
            f"<td>{fmt_int(e.get('reasoning_tokens'))}</td>"
            f"<td>{fmt_int(e.get('cache_creation_tokens'))}</td>"
            f"<td>{fmt_int(e.get('total_tokens'))}</td>"
            f"<td>{html.escape(str(e.get('outcome') or '—'))}</td>"
            f"<td{uc_attr}>{html.escape(str(e.get('usage_complete') or '—'))}</td>"
            "</tr>"
        )

    if not events_html:
        events_html = '<tr><td colspan="14" class="muted">No events</td></tr>'

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<title>Usage Proxy Ledger</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: system-ui, -apple-system, sans-serif; background: #12121a; color: #e0e0e8; margin: 0; padding: 1.5rem; line-height: 1.4; }}
h1 {{ margin: 0 0 0.25rem; font-size: 1.5rem; }}
.sub {{ color: #888; margin-bottom: 1.5rem; font-size: 0.9rem; }}
.summary-block {{ margin-bottom: 2rem; }}
.summary-block h2 {{ font-size: 1.1rem; color: #aab; margin-bottom: 0.75rem; border-bottom: 1px solid #333; padding-bottom: 0.25rem; }}
.cards {{ display: flex; gap: 1rem; margin-bottom: 1rem; flex-wrap: wrap; }}
.card {{ background: #1e1e2e; border: 1px solid #333; border-radius: 8px; padding: 1rem 1.5rem; min-width: 10rem; }}
.card .label {{ color: #888; font-size: 0.85rem; }}
.card .value {{ font-size: 1.75rem; font-weight: 600; color: #7ec8e3; }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
th, td {{ border: 1px solid #2a2a3a; padding: 0.4rem 0.5rem; text-align: left; }}
th {{ background: #1a1a28; color: #aaa; position: sticky; top: 0; }}
.model-table {{ max-width: 48rem; }}
.events-wrap {{ overflow-x: auto; }}
.events-table td {{ white-space: nowrap; }}
.muted {{ color: #666; text-align: center; }}
.amber {{ color: #e6a23c; font-weight: 600; }}
.row-error {{ background: #2a1515; }}
.row-error td {{ color: #f0a0a0; }}
</style>
</head>
<body>
<h1>Usage Proxy Ledger</h1>
<p class="sub">Read-only dashboard · auto-refresh 30s</p>
{render_cards("Last 24 hours", summary_24h)}
{render_cards("All time", summary_all)}
<section>
<h2>Recent events (200)</h2>
<div class="events-wrap">
<table class="events-table">
<thead>
<tr>
<th>Timestamp (Sydney)</th><th>Upstream</th><th>Model</th><th>Path</th>
<th>Status</th><th>Latency ms</th><th>Prompt</th><th>Completion</th>
<th>Cached</th><th>Reasoning</th><th>Cache creation</th><th>Total</th>
<th>Outcome</th><th>Usage complete</th>
</tr>
</thead>
<tbody>{events_html}</tbody>
</table>
</div>
</section>
</body>
</html>"""
    return page.encode("utf-8")


class UsageProxyHandler(BaseHTTPRequestHandler):
    db_path: str = DEFAULT_DB

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} {format % args}")

    def _try_db(self) -> sqlite3.Connection | None:
        try:
            return open_db_readonly(self.db_path)
        except (sqlite3.Error, OSError) as exc:
            self.send_response(500)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                error_page(
                    "Database unavailable",
                    f"Could not open database read-only: {self.db_path}\n{exc}",
                )
            )
            return None

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
        conn = self._try_db()
        if conn is None:
            return
        try:
            cutoff = cutoff_24h_iso()
            summary_24h = query_summary(conn, cutoff)
            summary_all = query_summary(conn)
            events = query_events(conn, 200)
        finally:
            conn.close()

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(render_dashboard(summary_24h, summary_all, events))

    def _handle_api_summary(self) -> None:
        conn = self._try_db()
        if conn is None:
            return
        try:
            cutoff = cutoff_24h_iso()
            payload = {
                "last_24h": query_summary(conn, cutoff),
                "all_time": query_summary(conn),
                "cutoff_24h_utc": cutoff,
            }
        finally:
            conn.close()

        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def _handle_api_events(self, parsed) -> None:
        qs = parse_qs(parsed.query)
        limit = 200
        if "limit" in qs and qs["limit"]:
            try:
                limit = min(max(1, int(qs["limit"][0])), 1000)
            except ValueError:
                self.send_error(400, "Invalid limit")
                return

        conn = self._try_db()
        if conn is None:
            return
        try:
            events = query_events(conn, limit)
        finally:
            conn.close()

        body = json.dumps({"events": events, "limit": limit}, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)


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