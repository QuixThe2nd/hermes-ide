"""Protected admin panel for the usage-proxy WebUI — sibling of ``server.py``.

This module owns the whole admin topic so the dashboard owner stays read-only
and GET-only for its existing routes:

* **Auth model.** Writes (and the admin reads that accompany them) require an
  operator session minted by presenting the contents of a root-only secret
  file (``webui-admin.secret`` next to the ledger by default, auto-generated
  on first start, 0600). The secret never appears in config, URLs or logs;
  only its file path is printed (stderr) when it is created. Sessions are
  in-memory random tokens (HttpOnly + SameSite=Strict cookie, 12 h TTL,
  bounded count) — restarting the webui revokes every session.
* **CSRF / origin.** Every mutating POST requires the per-session CSRF token
  (hidden form field, compared with ``hmac.compare_digest``) and, when an
  ``Origin`` header is present, an origin host equal to the request ``Host``.
  There are no cookies other than the session cookie and no CORS headers, so
  a cross-origin form can never read the CSRF token it would need.
* **No new secrets surface.** Admin pages show policy-caller names with the
  key store's *fingerprints* only; provider keys, caller tokens and the admin
  secret itself are never rendered, and anonymous requests to ``/admin*``
  get a redirect (GET) or 403 (POST) with no state disclosed. Minting and
  revoking policy-caller tokens stays with the CLI
  (``hermes llm_usage_proxy policy-callers``), which prints a token once.
* **Writes go through the existing store write path.** Policy save / reorder
  / enable / disable / remove call ``RoutingStore`` (which validates against
  the configured route table and declared endpoint capabilities, and holds a
  cross-process flock over the whole reload→mutate→save), so the proxy, the
  CLI and this panel can never overwrite each other's changes, and a corrupt
  ``routing.json`` refuses mutation instead of being silently rewritten.
* **Reads.** Cooldowns come from the same store file the proxy reloads on
  mtime; routing decisions are read from the ledger's ``routing_decisions``
  table (opened read-only, like every other dashboard query); quota cards
  render the Discord-independent collector snapshot
  (``plugins.quota_channels.collector.read_snapshot``), whose failed refresh
  retains last-good data with its age — never a fabricated zero.

Everything plugin-facing is imported lazily and guarded: if the repository
modules (or PyYAML, for the quota/proxy config section) are not importable in
the environment running the dashboard, the panel still renders and reports
the affected section as unavailable instead of failing the request.
"""

from __future__ import annotations

import hmac
import html
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import parse_qs, urlsplit

SESSION_COOKIE = "up_admin_session"
SESSION_TTL_SECONDS = 12 * 3600
MAX_SESSIONS = 64
LOGIN_FAIL_LIMIT = 5
LOGIN_FAIL_WINDOW_SECONDS = 300.0
LOGIN_LOCKOUT_SECONDS = 60.0
MAX_POST_BYTES = 64 * 1024
DECISION_LIMIT = 50

# The key store file shares the ledger's root-only directory; the name mirrors
# plugins.llm_usage_proxy.server.KEYS_FILENAME (imported lazily everywhere
# else, but the path is needed before any backend import is attempted).
KEYS_FILENAME = "keys.json"
SECRET_FILENAME = "webui-admin.secret"

_SECURITY_HEADERS = (
    ("X-Frame-Options", "DENY"),
    ("Referer-Policy", "no-referrer"),
    ("Content-Security-Policy", "frame-ancestors 'none'"),
)


class AdminBackendUnavailable(RuntimeError):
    """The routing/quota backend modules are not importable here."""


class AdminConfig:
    """Where the panel finds its state; one instance per webui process.

    ``extra_routes`` mirrors the proxy's ``--upstream`` flags: the panel has
    no connection to the proxy process, so routes the operator passed to the
    proxy directly (rather than via resolvable config) are declared to the
    panel the same way. Config-resolved routes merge underneath; an explicit
    declaration wins for its name.
    """

    def __init__(
        self,
        *,
        db_path: str,
        config_path: Optional[str] = None,
        secret_file: Optional[str] = None,
        extra_routes: Optional[Mapping[str, str]] = None,
    ):
        self.db_path = os.path.abspath(db_path)
        self.config_path = config_path
        self.secret_file = os.path.abspath(
            secret_file
            or os.path.join(os.path.dirname(self.db_path), SECRET_FILENAME)
        )
        self.extra_routes = {
            str(name).strip(): str(base).strip().rstrip("/")
            for name, base in (extra_routes or {}).items()
            if str(name).strip() and str(base).strip()
        }

    @property
    def keys_path(self) -> str:
        return os.path.join(os.path.dirname(self.db_path), KEYS_FILENAME)


# ── sessions ─────────────────────────────────────────────────────────────────

_sessions: dict[str, dict[str, Any]] = {}
_sessions_lock = threading.Lock()
_login_failures: dict[str, list[float]] = {}


def _purge_sessions(now: float) -> None:
    """Caller holds the lock. Drop expired sessions, then oldest overflow."""
    expired = [t for t, s in _sessions.items() if now - s["created"] > SESSION_TTL_SECONDS]
    for token in expired:
        del _sessions[token]
    while len(_sessions) > MAX_SESSIONS:
        oldest = min(_sessions, key=lambda t: _sessions[t]["created"])
        del _sessions[oldest]


def _new_session() -> dict[str, str]:
    now = time.time()
    with _sessions_lock:
        _purge_sessions(now)
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        _sessions[token] = {"created": now, "csrf": csrf}
    return {"token": token, "csrf": csrf}


def _drop_session(token: str) -> None:
    with _sessions_lock:
        _sessions.pop(token, None)


def _session_from(handler) -> Optional[dict[str, Any]]:
    """The session a request carries, or None — a forged cookie is no session."""
    raw = handler.headers.get("Cookie") or ""
    presented = ""
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == SESSION_COOKIE:
            presented = value
            break
    if not presented:
        return None
    now = time.time()
    with _sessions_lock:
        _purge_sessions(now)
        session = _sessions.get(presented)
        if session is None:
            return None
        return {"token": presented, "csrf": session["csrf"]}


def _login_allowed(ip: str) -> bool:
    now = time.monotonic()
    with _sessions_lock:
        fails = [t for t in _login_failures.get(ip, []) if now - t < LOGIN_FAIL_WINDOW_SECONDS]
        _login_failures[ip] = fails
        if len(fails) >= LOGIN_FAIL_LIMIT:
            return now - fails[-1] > LOGIN_LOCKOUT_SECONDS
        return True


def _record_login_fail(ip: str) -> None:
    now = time.monotonic()
    with _sessions_lock:
        fails = [t for t in _login_failures.get(ip, []) if now - t < LOGIN_FAIL_WINDOW_SECONDS]
        fails.append(now)
        _login_failures[ip] = fails


# ── secret file ──────────────────────────────────────────────────────────────


def ensure_secret_file(path: str) -> None:
    """Create the admin secret file (0600) when missing; never prints it.

    The file lives in the ledger's root-only directory next to the key store;
    the operator reads it out-of-band (``cat`` over SSH) to log in. Rotation
    is ``rm`` + restart (or replacing the file contents) — reads re-read the
    file, so a replaced secret takes effect on the next login attempt.
    """
    if os.path.exists(path):
        return
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(secrets.token_hex(32) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_secret(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            secret = handle.read().strip()
    except OSError:
        return None
    return secret or None


def _secret_matches(path: str, presented: str) -> bool:
    secret = _read_secret(path)
    if not secret or not presented:
        return False
    return hmac.compare_digest(secret.encode("utf-8"), presented.encode("utf-8", "replace"))


# ── backend glue (lazy, guarded repo imports) ────────────────────────────────


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _backend():
    """The routing backend modules, imported on first use.

    The webui ships in this repository but is launched as a standalone script
    (its own directory on ``sys.path``); the repo root is added explicitly so
    ``plugins.*`` resolves to the checkout this file lives in — never to a
    differently-versioned install elsewhere on ``sys.path``.
    """
    root = str(_repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from plugins.llm_usage_proxy import routing as routing_mod
        from plugins.llm_usage_proxy.config import load_llm_usage_proxy_config
        from plugins.llm_usage_proxy.routes import build_route_table
        from plugins.llm_usage_proxy.server import KeyStore
    except Exception as exc:
        raise AdminBackendUnavailable(f"routing backend unavailable: {exc}") from exc
    return routing_mod, load_llm_usage_proxy_config, build_route_table, KeyStore


def _load_llm_section(cfg: AdminConfig) -> Optional[Mapping[str, Any]]:
    """The raw ``llm_usage_proxy`` config section, or None for the default loader."""
    if not cfg.config_path:
        return None
    try:
        import yaml
    except ImportError as exc:
        raise AdminBackendUnavailable(
            f"PyYAML is not importable, cannot read {cfg.config_path}"
        ) from exc
    try:
        raw = yaml.safe_load(Path(cfg.config_path).read_text(encoding="utf-8")) or {}
    except Exception as exc:
        from plugins.quota_channels.core import format_config_parse_error

        raise AdminBackendUnavailable(
            f"cannot read {cfg.config_path}: {format_config_parse_error(exc)}"
        ) from exc
    section = raw.get("llm_usage_proxy") if isinstance(raw, Mapping) else None
    return section if isinstance(section, Mapping) else {}


def _route_context(cfg: AdminConfig):
    """(routing module, route_families, known_routes) for reads and writes.

    Known routes are the config-resolved table, plus routes declared to the
    panel directly (``--route``, mirroring the proxy's ``--upstream``), plus
    every route the key store holds keys for — the same union the CLI uses.
    """
    routing_mod, load_llm_usage_proxy_config, build_route_table, KeyStore = _backend()
    llm_cfg = load_llm_usage_proxy_config(raw=_load_llm_section(cfg))
    route_table = build_route_table(llm_cfg)
    route_table.update(cfg.extra_routes)
    route_families = routing_mod.route_family_map(
        route_table, llm_cfg.get("route_capabilities")
    )
    known_routes = sorted(set(route_table) | set(KeyStore(cfg.keys_path).describe()["routes"]))
    return routing_mod, KeyStore, route_families, known_routes


def _routing_state(cfg: AdminConfig) -> dict[str, Any]:
    """Policies, callers, cooldowns and route capabilities for render + JSON.

    Never carries a secret: caller/token values are fingerprints from the key
    store's ``describe*`` methods, and route keys surface as counts only.
    """
    routing_mod, KeyStore, route_families, known_routes = _route_context(cfg)
    store = routing_mod.RoutingStore(routing_mod.default_routing_path(cfg.db_path))
    key_store = KeyStore(cfg.keys_path)
    key_counts = {name: len(key_store.route_keys(name)) for name in known_routes}

    now = time.time()
    policies: dict[str, Any] = {}
    for caller, policy in store.policies_snapshot().items():
        entries = []
        for entry in policy["entries"]:
            cooldown = store.active_cooldown(entry["route"], entry["model"], now)
            unusable = None
            if entry["route"] not in known_routes:
                unusable = "unknown_route"
            elif entry["family"] not in route_families.get(entry["route"], frozenset()):
                unusable = "incompatible_family"
            elif cooldown is not None:
                unusable = "cooldown"
            entries.append(
                {
                    **entry,
                    "usable": unusable is None,
                    "unusable_reason": unusable,
                    "cooldown_until": cooldown.get("until") if cooldown else None,
                }
            )
        policies[caller] = {"enabled": policy["enabled"], "entries": entries}

    return {
        "policies": policies,
        "policy_callers": key_store.describe_policy_callers(),
        "cooldowns": store.cooldowns_snapshot(now),
        "known_routes": known_routes,
        "route_families": {
            name: sorted(families) for name, families in sorted(route_families.items())
        },
        "route_key_counts": key_counts,
        "families": sorted(routing_mod.API_FAMILIES),
        "state_error": store.state_error,
    }


def _recent_decisions(db_path: str, limit: int = DECISION_LIMIT) -> dict[str, Any]:
    """Newest routing-decision journal rows from the ledger, read-only.

    A ledger whose proxy never ran with key-manager mode has no
    ``routing_decisions`` table at all — that is reported, not an error.
    """
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'routing_decisions'"
        ).fetchone()
        if not present:
            return {"available": False, "rows": []}
        rows = conn.execute(
            "SELECT id, ts, caller, family, arrived_route, arrived_model,"
            " selected_route, selected_model, reason, detail"
            " FROM routing_decisions ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return {"available": True, "rows": [dict(row) for row in rows]}
    except (sqlite3.Error, OSError) as exc:
        return {"available": False, "rows": [], "error": f"ledger unavailable: {exc}"}
    finally:
        if conn is not None:
            conn.close()


def _quota_snapshot(cfg: AdminConfig) -> dict[str, Any]:
    """The collector snapshot, or an explicit unavailable payload.

    ``read_snapshot`` itself never raises and never exposes credentials; the
    guard here covers the imports and the config read.
    """
    try:
        root = str(_repo_root())
        if root not in sys.path:
            sys.path.insert(0, root)
        from plugins.quota_channels import collector
        from plugins.quota_channels.core import QuotaChannelsError, load_quota_config

        config = None
        config_error = None
        if cfg.config_path:
            try:
                config = load_quota_config(
                    Path(cfg.config_path), require_discord=False
                )
            except QuotaChannelsError as exc:
                config_error = str(exc)
            except Exception as exc:
                from plugins.quota_channels.core import format_config_parse_error

                config_error = format_config_parse_error(exc)
        snapshot = collector.read_snapshot(config)
        snapshot["config_error"] = config_error
        return {"available": True, "snapshot": snapshot}
    except Exception as exc:
        return {"available": False, "error": f"quota collector unavailable: {exc}"}


# ── small render helpers ─────────────────────────────────────────────────────


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _iso(epoch: Any) -> str:
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat(
            timespec="seconds"
        )
    except (TypeError, ValueError, OSError):
        return ""


_PAGE_CSS = """
:root{--bg:#0b0e13;--surface:#11151c;--surface-2:#161b24;--border:#232a36;
--text:#e8edf4;--text-2:#9aa7b8;--accent:#3987e5;--good:#65a46c;--warn:#bd8714;
--crit:#d46c8b;--chip:#1c2330}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 -apple-system,
"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:1080px;margin:0 auto;padding:20px 16px 64px}
h1{font-size:20px;margin:8px 0 4px}h2{font-size:15px;margin:0 0 10px;color:var(--text-2);
text-transform:uppercase;letter-spacing:.06em}
a{color:var(--accent);text-decoration:none}
.top{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
.top form{margin-left:auto}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;
padding:14px 16px;margin:14px 0}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--border);
vertical-align:top}
th{color:var(--text-2);font-weight:600}
code,textarea,input,select{font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
textarea{width:100%;min-height:64px;background:var(--surface-2);color:var(--text);
border:1px solid var(--border);border-radius:6px;padding:6px 8px}
input[type=text],input[type=password],select{background:var(--surface-2);color:var(--text);
border:1px solid var(--border);border-radius:6px;padding:6px 8px}
button,.btn{background:var(--chip);color:var(--text);border:1px solid var(--border);
border-radius:6px;padding:5px 12px;cursor:pointer;font-size:13px}
button:hover,.btn:hover{border-color:var(--accent)}
.chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;
background:var(--chip);border:1px solid var(--border)}
.chip.good{color:var(--good)}.chip.warn{color:var(--warn)}.chip.crit{color:var(--crit)}
.muted{color:var(--text-2)}
.err{background:#2a1620;border:1px solid var(--crit);border-radius:8px;
padding:10px 14px;margin:14px 0;font-size:13px}
.ok{background:#142016;border:1px solid var(--good);border-radius:8px;
padding:10px 14px;margin:14px 0;font-size:13px}
.entry{display:flex;gap:10px;align-items:center;padding:4px 0;
border-bottom:1px solid var(--border)}
.entry code{flex:1}
.fam{margin:10px 0 18px}
.quota-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));
gap:10px}
.quota{background:var(--surface-2);border:1px solid var(--border);border-radius:8px;
padding:10px 12px}
.quota .pct{font-size:22px;font-weight:600}
.inline{display:inline}
""".replace("\n", " ")


def _page(title: str, body: str, csrf: str = "") -> bytes:
    logout = (
        '<form class=inline method=post action="/admin/logout">'
        f'<input type=hidden name=csrf value="{esc(csrf)}">'
        "<button>log out</button></form>"
        if csrf
        else ""
    )
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)} · usage proxy admin</title><style>{_PAGE_CSS}</style>"
        "</head><body><main>"
        '<div class=top><h1>Usage Proxy Admin</h1>'
        '<a href="/">← dashboard</a>'
        '<a href="/admin">refresh</a>'
        f"{logout}</div>"
        f"{body}</main></body></html>"
    ).encode("utf-8")


def _login_page(message: str = "") -> bytes:
    notice = f'<div class=err>{esc(message)}</div>' if message else ""
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        "<title>usage proxy admin · sign in</title>"
        f"<style>{_PAGE_CSS}</style></head><body><main>"
        "<h1>Usage Proxy Admin</h1>"
        '<p class=muted>Sign in with the admin secret from the secret file on the'
        " host (default: <code>webui-admin.secret</code> next to the ledger)."
        " The secret is never stored in config, URLs or logs.</p>"
        f"{notice}"
        '<form method=post action="/admin/login">'
        '<input type=password name=secret autocomplete=off autofocus required>'
        "<button>sign in</button></form>"
        '<p class=muted><a href="/">← back to the dashboard</a></p>'
        "</main></body></html>"
    ).encode("utf-8")


def _status_chip(status: str) -> str:
    tone = {
        "fresh": "good",
        "stale": "warn",
        "error": "crit",
        "unknown": "",
        "disabled": "",
    }.get(status, "")
    return f'<span class="chip {tone}">{esc(status)}</span>'


def _quota_cards(quota: dict[str, Any]) -> str:
    if not quota.get("available"):
        return f'<div class=err>{esc(quota.get("error") or "unavailable")}</div>'
    snap = quota["snapshot"]
    parts: list[str] = []
    if snap.get("config_error"):
        parts.append(
            f'<p class=muted>quota_channels config not loaded ({esc(snap["config_error"])});'
            " showing collector state for all known providers.</p>"
        )
    if snap.get("state_error"):
        parts.append(f'<div class=err>collector state: {esc(snap["state_error"])}</div>')
    cards: list[str] = []
    for key, entry in (snap.get("providers") or {}).items():
        data = entry.get("data") or {}
        rows = [f'<div class=pct>{esc(data.get("pct"))}%</div>' if data.get("pct") is not None else ""]
        detail: list[str] = []
        if data.get("reset_at"):
            detail.append(f"resets {esc(_iso(data['reset_at']))}")
        elif data.get("reset_seconds") is not None:
            detail.append(f"resets in {int(data['reset_seconds'])} s")
        if data.get("tokens_7d") is not None:
            detail.append(f"7d tokens: {esc(data['tokens_7d'])}")
        if data.get("reset_count") is not None:
            detail.append(f"reset credits: {esc(data['reset_count'])}")
        if entry.get("age_seconds") is not None:
            detail.append(f"collected {int(entry['age_seconds'])} s ago")
        if entry.get("error"):
            detail.append(f"last error: {entry['error']}")
        cards.append(
            '<div class=quota>'
            f"<div><strong>{esc(entry.get('label') or key)}</strong> "
            f"{_status_chip(str(entry.get('status')))}</div>"
            + "".join(rows)
            + (f'<div class=muted>{esc(" · ".join(detail))}</div>' if detail else "")
            + "</div>"
        )
    if snap.get("wallets"):
        wallet_bits = []
        for wallet in snap["wallets"]:
            data = wallet.get("data") or {}
            pct = data.get("pct")
            wallet_bits.append(
                f"<tr><td>{esc(wallet.get('display'))}</td>"
                f"<td>{_status_chip(str(wallet.get('status')))}</td>"
                f"<td>{esc(pct) if pct is not None else '—'}</td>"
                f"<td class=muted>{esc(wallet.get('error') or '')}</td></tr>"
            )
        wallets_html = (
            "<h2>z.ai wallets</h2><table><tr><th>wallet</th><th>status</th>"
            f"<th>remaining %</th><th>error</th></tr>{''.join(wallet_bits)}</table>"
        )
    else:
        wallets_html = ""
    last = snap.get("last_collect_success")
    nxt = snap.get("next_collect_due_at")
    meta = []
    if last:
        meta.append(f"last successful collection {_iso(last)}")
    if nxt:
        meta.append(f"next due {_iso(nxt)}")
    parts.append(
        (f'<p class=muted>{esc(" · ".join(meta))}</p>' if meta else "")
        + '<div class=quota-grid id=quota-grid>'
        + "".join(cards)
        + "</div>"
        + wallets_html
    )
    return "".join(parts)


def _cooldowns_table(cooldowns: list[dict[str, Any]]) -> str:
    if not cooldowns:
        return '<p class=muted>No active cooldowns.</p>'
    rows = "".join(
        f"<tr><td>{esc(cd.get('route'))}</td><td>{esc(cd.get('model') or '—')}</td>"
        f"<td>{esc(cd.get('until_iso'))}</td>"
        f"<td>{esc(cd.get('remaining_seconds'))} s</td>"
        f"<td class=muted>{esc(cd.get('reason'))}</td></tr>"
        for cd in cooldowns
    )
    return (
        "<table><tr><th>route</th><th>model</th><th>until (UTC)</th>"
        f"<th>remaining</th><th>reason</th></tr>{rows}</table>"
    )


def _decisions_table(decisions: dict[str, Any]) -> str:
    if not decisions.get("available"):
        note = decisions.get("error") or (
            "no routing_decisions table in this ledger — the decision journal is"
            " written by the proxy in key-manager mode"
        )
        return f'<p class=muted>{esc(note)}</p>'
    rows = decisions.get("rows") or []
    if not rows:
        return '<p class=muted>No routing decisions recorded yet.</p>'
    body = "".join(
        f"<tr><td class=muted>{esc(row.get('ts'))}</td>"
        f"<td>{esc(row.get('caller'))}</td>"
        f"<td>{esc(row.get('family') or '—')}</td>"
        f"<td>{esc(row.get('arrived_route'))} / {esc(row.get('arrived_model') or '—')}</td>"
        f"<td>{esc(row.get('selected_route') or '—')} / {esc(row.get('selected_model') or '—')}</td>"
        f"<td>{esc(row.get('reason'))}</td>"
        f"<td class=muted>{esc(row.get('detail') or '')}</td></tr>"
        for row in rows
    )
    return (
        "<table><tr><th>time (UTC)</th><th>caller</th><th>family</th>"
        "<th>arrived</th><th>selected</th><th>reason</th><th>detail</th></tr>"
        f"{body}</table>"
    )


def _entry_editor(
    caller: str, family: str, entries: list[dict[str, Any]], enabled: bool, csrf: str
) -> str:
    lines = "\n".join(f"{e['route']}:{e['model']}" for e in entries)
    rows = []
    for index, entry in enumerate(entries):
        usability = (
            '<span class="chip good">usable</span>'
            if entry.get("usable")
            else f'<span class="chip warn">{esc(entry.get("unusable_reason"))}</span>'
        )
        move = ""
        if index > 0:
            move += (
                f'<form class=inline method=post action="/admin/policy/move">'
                f'<input type=hidden name=csrf value="{esc(csrf)}">'
                f'<input type=hidden name=caller value="{esc(caller)}">'
                f'<input type=hidden name=family value="{esc(family)}">'
                f'<input type=hidden name=index value="{index}">'
                f'<input type=hidden name=direction value="-1">'
                "<button title='move up'>↑</button></form>"
            )
        if index < len(entries) - 1:
            move += (
                f'<form class=inline method=post action="/admin/policy/move">'
                f'<input type=hidden name=csrf value="{esc(csrf)}">'
                f'<input type=hidden name=caller value="{esc(caller)}">'
                f'<input type=hidden name=family value="{esc(family)}">'
                f'<input type=hidden name=index value="{index}">'
                f'<input type=hidden name=direction value="1">'
                "<button title='move down'>↓</button></form>"
            )
        rows.append(
            f'<div class=entry><code>{esc(entry["route"])}:{esc(entry["model"])}</code>'
            f"{usability}{move}</div>"
        )
    return (
        f"<div class=fam><h2>{esc(family)}</h2>"
        + ("".join(rows) or '<p class=muted>no entries yet</p>')
        + f'<form method=post action="/admin/policy/set">'
        f'<input type=hidden name=csrf value="{esc(csrf)}">'
        f'<input type=hidden name=caller value="{esc(caller)}">'
        f'<input type=hidden name=family value="{esc(family)}">'
        + '<p class=muted>Ordered candidates, one <code>route:model</code> per line,'
        " highest priority first. Order is operator priority — nothing reorders it."
        " Edit to add, remove or reorder, then save.</p>"
        f'<textarea name=entries rows={max(3, len(entries) + 1)}>{esc(lines)}</textarea>'
        "<button>save this family</button></form></div>"
    )


def _policies_section(state: dict[str, Any], csrf: str) -> str:
    parts: list[str] = []
    if state.get("state_error"):
        parts.append(
            f'<div class=err>routing state file is unreadable: {esc(state["state_error"])}'
            " — mutations are refused until it is repaired; the last-good view"
            " is shown.</div>"
        )
    callers = state.get("policy_callers") or {}
    policies = state.get("policies") or {}
    if callers:
        rows = "".join(
            f"<tr><td>{esc(name)}</td><td><code>{esc(fingerprint)}</code></td>"
            f"<td>{'yes' if name in policies else '<span class=muted>no policy saved</span>'}</td></tr>"
            for name, fingerprint in callers.items()
        )
        parts.append(
            "<h2>policy callers</h2><table><tr><th>caller</th>"
            "<th>token fingerprint</th><th>policy</th></tr>"
            f"{rows}</table>"
            '<p class=muted>Mint or revoke tokens with'
            " <code>hermes llm_usage_proxy policy-callers create|remove</code> — a token"
            " is shown once at creation and never rendered here.</p>"
        )
    else:
        parts.append(
            '<p class=muted>No policy callers — no request can arm routing policies.'
            " Mint one with <code>hermes llm_usage_proxy policy-callers create</code>.</p>"
        )

    for caller, policy in sorted(policies.items()):
        enabled = bool(policy.get("enabled"))
        badge = (
            '<span class="chip good">enabled</span>'
            if enabled
            else '<span class="chip warn">disabled</span>'
        )
        toggle_action = "disable" if enabled else "enable"
        controls = (
            f'<form class=inline method=post action="/admin/policy/{toggle_action}">'
            f'<input type=hidden name=csrf value="{esc(csrf)}">'
            f'<input type=hidden name=caller value="{esc(caller)}">'
            f"<button>{toggle_action}</button></form> "
            f'<form class=inline method=post action="/admin/policy/remove">'
            f'<input type=hidden name=csrf value="{esc(csrf)}">'
            f'<input type=hidden name=caller value="{esc(caller)}">'
            "<button>remove policy</button></form>"
        )
        parts.append(f'<div class=card><h2>{esc(caller)} {badge}</h2>{controls}')
        by_family: dict[str, list[dict[str, Any]]] = {}
        for entry in policy.get("entries") or []:
            by_family.setdefault(str(entry.get("family")), []).append(entry)
        for family in state.get("families") or []:
            entries = by_family.get(family)
            if entries:
                parts.append(_entry_editor(caller, family, entries, enabled, csrf))
        missing = [
            family
            for family in (state.get("families") or [])
            if family not in by_family
        ]
        if missing:
            options = "".join(f"<option>{esc(f)}</option>" for f in missing)
            parts.append(
                f'<form method=post action="/admin/policy/set">'
                f'<input type=hidden name=csrf value="{esc(csrf)}">'
                f'<input type=hidden name=caller value="{esc(caller)}">'
                f'<p class=muted>Add a family: <select name=family>{options}</select></p>'
                '<textarea name=entries rows=2 placeholder="route:model"></textarea>'
                "<button>save family</button></form>"
            )
        parts.append("</div>")

    if callers:
        options = "".join(
            f"<option>{esc(f)}</option>" for f in (state.get("families") or [])
        )
        caller_options = "".join(f"<option>{esc(name)}</option>" for name in callers)
        parts.append(
            "<div class=card><h2>new policy / new family</h2>"
            f'<form method=post action="/admin/policy/set">'
            f'<input type=hidden name=csrf value="{esc(csrf)}">'
            "<p>caller (a minted policy caller): "
            f'<input type=text name=caller list=policy-callers required>'
            f"<datalist id=policy-callers>{caller_options}</datalist> "
            f"family: <select name=family>{options}</select> "
            '<label><input type=checkbox name=enabled value="1" checked> enabled</label></p>'
            '<textarea name=entries rows=3 placeholder="route:model — one per line,'
            ' priority order"></textarea>'
            "<button>save policy</button></form></div>"
        )

    routes = state.get("known_routes") or []
    families = state.get("route_families") or {}
    key_counts = state.get("route_key_counts") or {}
    route_rows = "".join(
        f"<tr><td>{esc(name)}</td><td>{esc(', '.join(families.get(name) or [])) or '—'}</td>"
        f"<td>{esc(key_counts.get(name, 0))}</td></tr>"
        for name in routes
    )
    parts.append(
        "<div class=card><h2>configured routes</h2>"
        "<table><tr><th>route</th><th>declared API families</th>"
        f"<th>stored keys</th></tr>{route_rows}</table>"
        '<p class=muted>A route with no declared families cannot serve policy'
        " traffic — declare <code>llm_usage_proxy.route_capabilities.&lt;route&gt;</code>"
        " in config. Key counts come from the key store; keys themselves are never"
        " shown.</p></div>"
    )
    return "".join(parts)


_LIVE_JS = """
(function(){
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){
return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]})}
function iso(epoch){if(epoch==null)return"";var d=new Date(epoch*1000);
return isNaN(d)?"":d.toISOString().replace(".000Z","Z")}
function chip(status){var tone={fresh:"good",stale:"warn",error:"crit"}[status]||"";
return '<span class="chip '+tone+'">'+esc(status)+"</span>"}
function renderCooldowns(list){var el=document.getElementById("cooldowns-body");
if(!el)return;if(!list||!list.length){el.innerHTML='<p class=muted>No active cooldowns.</p>';return}
el.innerHTML="<table><tr><th>route</th><th>model</th><th>until (UTC)</th><th>remaining</th><th>reason</th></tr>"+
list.map(function(cd){return "<tr><td>"+esc(cd.route)+"</td><td>"+esc(cd.model||"—")+"</td><td>"+
esc(cd.until_iso||iso(cd.until))+"</td><td>"+esc(cd.remaining_seconds)+" s</td><td class=muted>"+
esc(cd.reason)+"</td></tr>"}).join("")+"</table>"}
function renderDecisions(d){var el=document.getElementById("decisions-body");
if(!el)return;if(!d||!d.available){el.innerHTML='<p class=muted>'+
esc((d&&d.error)||"no routing_decisions table in this ledger")+"</p>";return}
var rows=d.rows||[];if(!rows.length){el.innerHTML='<p class=muted>No routing decisions recorded yet.</p>';return}
el.innerHTML="<table><tr><th>time (UTC)</th><th>caller</th><th>family</th><th>arrived</th><th>selected</th><th>reason</th><th>detail</th></tr>"+
rows.map(function(r){return "<tr><td class=muted>"+esc(r.ts)+"</td><td>"+esc(r.caller)+"</td><td>"+
esc(r.family||"—")+"</td><td>"+esc(r.arrived_route)+" / "+esc(r.arrived_model||"—")+"</td><td>"+
esc(r.selected_route||"—")+" / "+esc(r.selected_model||"—")+"</td><td>"+esc(r.reason)+
"</td><td class=muted>"+esc(r.detail||"")+"</td></tr>"}).join("")+"</table>"}
function renderQuotas(q){var el=document.getElementById("quotas-body");if(!el)return;
if(!q||!q.available){el.innerHTML='<div class=err>'+esc((q&&q.error)||"unavailable")+"</div>";return}
var snap=q.snapshot||{};var out="";
if(snap.state_error){out+='<div class=err>collector state: '+esc(snap.state_error)+"</div>"}
out+='<div class=quota-grid>';
Object.keys(snap.providers||{}).forEach(function(key){var e=snap.providers[key];
var data=e.data||{};var detail=[];
if(data.reset_at)detail.push("resets "+iso(data.reset_at));
else if(data.reset_seconds!=null)detail.push("resets in "+Math.round(data.reset_seconds)+" s");
if(data.tokens_7d!=null)detail.push("7d tokens: "+data.tokens_7d);
if(e.age_seconds!=null)detail.push("collected "+Math.round(e.age_seconds)+" s ago");
if(e.error)detail.push("last error: "+e.error);
out+='<div class=quota><div><strong>'+esc(e.label||key)+"</strong> "+chip(String(e.status))+"</div>"+
(data.pct!=null?'<div class=pct>'+esc(data.pct)+"%</div>":"")+
(detail.length?'<div class=muted>'+esc(detail.join(" · "))+"</div>":"")+"</div>"});
out+="</div>";
if((snap.wallets||[]).length){out+="<h2>z.ai wallets</h2><table><tr><th>wallet</th><th>status</th><th>remaining %</th><th>error</th></tr>"+
snap.wallets.map(function(w){var data=w.data||{};return "<tr><td>"+esc(w.display)+"</td><td>"+
chip(String(w.status))+"</td><td>"+(data.pct!=null?esc(data.pct):"—")+"</td><td class=muted>"+
esc(w.error||"")+"</td></tr>"}).join("")+"</table>"}
el.innerHTML=out}
function tick(){fetch("/admin/api/state",{credentials:"same-origin"}).then(function(r){
if(!r.ok)return null;return r.json()}).then(function(state){if(!state)return;
renderCooldowns(state.cooldowns);renderDecisions(state.decisions);renderQuotas(state.quotas)}).catch(function(){})}
setInterval(tick,5000);
})();
""".replace("\n", " ")


def _admin_page(cfg: AdminConfig, session: dict[str, Any], banner: str = "", ok: str = "") -> bytes:
    sections: list[str] = []
    if banner:
        sections.append(f'<div class=err>{esc(banner)}</div>')
    if ok:
        sections.append(f'<div class=ok>{esc(ok)}</div>')

    try:
        state = _routing_state(cfg)
        routing_html = _policies_section(state, session["csrf"])
    except AdminBackendUnavailable as exc:
        state = None
        routing_html = f'<div class=card><h2>routing policies</h2><div class=err>{esc(exc)}</div></div>'
    cooldowns = state.get("cooldowns") if state else []
    sections.append(routing_html)
    sections.append(
        f'<div class=card><h2>active cooldowns</h2><div id=cooldowns-body>'
        f"{_cooldowns_table(cooldowns or [])}</div></div>"
    )
    decisions = _recent_decisions(cfg.db_path)
    sections.append(
        '<div class=card><h2>recent routing decisions</h2><div id=decisions-body>'
        f"{_decisions_table(decisions)}</div></div>"
    )
    quota = _quota_snapshot(cfg)
    sections.append(
        f'<div class=card><h2>provider quotas</h2><div id=quotas-body>'
        f"{_quota_cards(quota)}</div>"
        '<p class=muted>Quota readings come from the collection-only collector'
        " state; a failed refresh keeps the last-good reading and its age — a"
        " failure is never a zero. Collection runs on its own schedule"
        " (<code>run.py --collect-only</code>); this panel only reads.</p></div>"
    )
    body = "".join(sections) + f"<script>{_LIVE_JS}</script>"
    return _page("panel", body, csrf=session["csrf"])


# ── request handling ─────────────────────────────────────────────────────────

Response = tuple[int, str, bytes, tuple[tuple[str, str], ...]]

_JSON = "application/json; charset=utf-8"
_HTML = "text/html; charset=utf-8"
_PLAIN = "text/plain; charset=utf-8"


def _redirect(location: str, extra: tuple[tuple[str, str], ...] = ()) -> Response:
    return 303, _PLAIN, b"see other\n", (("Location", location),) + extra


def _cookie_header(session: dict[str, str]) -> tuple[str, str]:
    return (
        "Set-Cookie",
        f"{SESSION_COOKIE}={session['token']}; HttpOnly; SameSite=Strict; Path=/;"
        f" Max-Age={SESSION_TTL_SECONDS}",
    )


def _expired_cookie_header() -> tuple[str, str]:
    return (
        "Set-Cookie",
        f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0",
    )


def _origin_ok(handler) -> bool:
    """A present Origin must name this host; an absent one is fine (non-browser
    clients), because the CSRF token below is the actual cross-site guard."""
    origin = handler.headers.get("Origin")
    if not origin:
        return True
    host = (handler.headers.get("Host") or "").lower()
    return urlsplit(origin).netloc.lower() == host and bool(host)


def _read_body(handler) -> Optional[bytes]:
    """The whole POST body, or None when it is missing/oversized.

    Always called before any response is written: an unread request body
    would otherwise stay in the keep-alive buffer and be misparsed as the
    next request line on the connection.
    """
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        return None
    if length <= 0:
        return b""
    if length > MAX_POST_BYTES:
        # Drain a bounded prefix so the connection stays usable, then refuse.
        handler.rfile.read(MAX_POST_BYTES)
        return None
    return handler.rfile.read(length)


def _parse_form(handler, raw: bytes) -> Optional[dict[str, list[str]]]:
    """Parsed urlencoded form, or None when the submission is not one."""
    content_type = (handler.headers.get("Content-Type") or "").split(";")[0].strip()
    if content_type != "application/x-www-form-urlencoded":
        return None
    try:
        return parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    except (UnicodeDecodeError, ValueError):
        return None


def _field(form: Mapping[str, list[str]], name: str) -> str:
    values = form.get(name) or []
    return values[-1].strip() if values else ""


def _csrf_ok(form: Mapping[str, list[str]], session: dict[str, Any]) -> bool:
    presented = _field(form, "csrf")
    return bool(presented) and hmac.compare_digest(
        session["csrf"].encode("utf-8"), presented.encode("utf-8", "replace")
    )


def handle_get(handler, cfg: AdminConfig, parsed) -> Response:
    """GET /admin… — login page, the panel, and its JSON state endpoint."""
    route = parsed.path
    session = _session_from(handler)
    if route == "/admin/login":
        if session is not None:
            return _redirect("/admin")
        return 200, _HTML, _login_page(), _SECURITY_HEADERS
    if route == "/admin":
        if session is None:
            return _redirect("/admin/login")
        return 200, _HTML, _admin_page(cfg, session), _SECURITY_HEADERS
    if route == "/admin/api/state":
        if session is None:
            return 401, _JSON, b'{"error": "authentication required"}\n', _SECURITY_HEADERS
        try:
            state = _routing_state(cfg)
        except AdminBackendUnavailable as exc:
            state = {"error": str(exc), "policies": {}, "policy_callers": {},
                     "cooldowns": [], "known_routes": [], "route_families": {},
                     "route_key_counts": {}, "families": [], "state_error": None}
        payload = {
            "policies": state.get("policies"),
            "policy_callers": state.get("policy_callers"),
            "cooldowns": state.get("cooldowns"),
            "known_routes": state.get("known_routes"),
            "route_families": state.get("route_families"),
            "route_key_counts": state.get("route_key_counts"),
            "families": state.get("families"),
            "routing_state_error": state.get("state_error"),
            "backend_error": state.get("error"),
            "decisions": _recent_decisions(cfg.db_path),
            "quotas": _quota_snapshot(cfg),
        }
        return 200, _JSON, json.dumps(payload, indent=2).encode("utf-8"), _SECURITY_HEADERS
    return 404, _PLAIN, b"not found\n", _SECURITY_HEADERS


def _policy_write(cfg: AdminConfig, form: Mapping[str, list[str]]) -> Optional[str]:
    """Apply one policy mutation; returns an error message or None on success.

    Every write goes through the store's own validated mutation path (the same
    one the CLI uses) — never a hand-rolled edit of ``routing.json``.
    """
    routing_mod, KeyStore, route_families, known_routes = _route_context(cfg)
    store = routing_mod.RoutingStore(routing_mod.default_routing_path(cfg.db_path))

    caller = _field(form, "caller")
    action = form.get("_action", [""])[-1]
    if action in ("enable", "disable"):
        if store.set_policy_enabled(caller, action == "enable"):
            return None
        return f"{caller}: no saved policy."
    if action == "remove":
        if store.remove_policy(caller):
            return None
        return f"{caller}: no saved policy."
    if action == "move":
        family = _field(form, "family")
        if store.policy_for(caller) is None:
            return f"{caller}: no saved policy."
        try:
            index = int(_field(form, "index"))
            direction = int(_field(form, "direction"))
        except ValueError:
            return "invalid move request"
        try:
            store.move_policy_entry(
                caller,
                family,
                index,
                direction,
                known_routes=known_routes,
                route_families=route_families,
            )
        except ValueError as exc:
            msg = str(exc)
            if "no saved policy" in msg:
                return f"{caller}: no saved policy."
            if "move out of range" in msg:
                return "move out of range"
            return "invalid move request"
        return None
    # default: full family save (set)
    family = _field(form, "family")
    raw_lines = _field(form, "entries")
    entries = []
    for line in raw_lines.splitlines():
        line = line.strip()
        if not line:
            continue
        route, sep, model = line.partition(":")
        if not sep:
            return f"invalid entry {line!r}: expected route:model"
        entries.append({"route": route.strip(), "model": model.strip(), "family": family})
    if not entries:
        return "a policy needs at least one route:model entry"
    create_enabled: Optional[bool] = bool(form.get("enabled"))
    store.set_policy_family(
        caller,
        family,
        entries,
        enabled=create_enabled,
        known_routes=known_routes,
        route_families=route_families,
    )
    return None


def handle_post(handler, cfg: AdminConfig, parsed) -> Response:
    """POST /admin… — login/logout and policy mutations.

    Rejection order is deliberate: login is the only unauthenticated POST;
    everything else needs a session, an acceptable origin and the session's
    CSRF token before any mutation is even looked at.
    """
    route = parsed.path
    raw = _read_body(handler)
    if raw is None:
        return 413, _PLAIN, b"request body too large\n", _SECURITY_HEADERS

    if route == "/admin/login":
        if not _origin_ok(handler):
            return 403, _HTML, _login_page("Rejected: cross-origin request."), _SECURITY_HEADERS
        ip = handler.client_address[0] if handler.client_address else ""
        if not _login_allowed(ip):
            return 429, _HTML, _login_page("Too many failed attempts — wait a minute."), _SECURITY_HEADERS
        form = _parse_form(handler, raw)
        if form is None:
            return 400, _HTML, _login_page("Malformed form submission."), _SECURITY_HEADERS
        if _secret_matches(cfg.secret_file, _field(form, "secret")):
            session = _new_session()
            return _redirect("/admin", (_cookie_header(session),))
        _record_login_fail(ip)
        return 403, _HTML, _login_page("Invalid admin secret."), _SECURITY_HEADERS

    session = _session_from(handler)
    if session is None:
        return 403, _JSON, b'{"error": "authentication required"}\n', _SECURITY_HEADERS
    if not _origin_ok(handler):
        return 403, _JSON, b'{"error": "cross-origin request rejected"}\n', _SECURITY_HEADERS
    form = _parse_form(handler, raw)
    if form is None:
        return 400, _JSON, b'{"error": "expected a urlencoded form body"}\n', _SECURITY_HEADERS
    if not _csrf_ok(form, session):
        return 403, _JSON, b'{"error": "missing or invalid CSRF token"}\n', _SECURITY_HEADERS

    if route == "/admin/logout":
        _drop_session(session["token"])
        return _redirect("/admin/login", (_expired_cookie_header(),))

    if route in (
        "/admin/policy/set",
        "/admin/policy/enable",
        "/admin/policy/disable",
        "/admin/policy/remove",
        "/admin/policy/move",
    ):
        try:
            routing_mod, _, _, _ = _backend()
            state_error_cls = routing_mod.RoutingStateError
        except AdminBackendUnavailable as exc:
            return 503, _HTML, _admin_page(cfg, session, banner=str(exc)), _SECURITY_HEADERS
        action = route.rsplit("/", 1)[-1]
        form = dict(form)
        form["_action"] = [action]
        try:
            error = _policy_write(cfg, form)
        except state_error_cls as exc:
            # A corrupt routing.json refuses mutation rather than being
            # overwritten by a partial view — say so, don't retry.
            return 409, _HTML, _admin_page(cfg, session, banner=str(exc)), _SECURITY_HEADERS
        except ValueError as exc:
            return 400, _HTML, _admin_page(cfg, session, banner=str(exc)), _SECURITY_HEADERS
        if error:
            status = 404 if "no saved policy" in error else 400
            return status, _HTML, _admin_page(cfg, session, banner=error), _SECURITY_HEADERS
        return _redirect("/admin")

    return 404, _PLAIN, b"not found\n", _SECURITY_HEADERS
