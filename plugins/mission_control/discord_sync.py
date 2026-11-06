"""Discord archive-mirror client helpers for mission-control: rate
limiting, request/error handling and active-thread fetching.

Moved byte-identical from server.py. The stateful passes
(apply_discord_snapshot, discord_sync_once, discord_sync_loop) stay in
the facade — they own the archive-epoch registry and DB discovery.
"""

import json
import re
import threading
import time
import urllib.error
import urllib.request

from plugins.mission_control.redaction import REDACTED, redact_secret_text
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


_discord_lock = threading.Lock()
_discord_last_request = 0.0
_discord_cooldown_until = 0.0
_discord_sync_stop = threading.Event()
