"""Narrow HTTP routes for the secure-login entry form.

Mounted on the EXISTING gateway API listener (``gateway/platforms/api_server.py``
extends its route table with ``_http_routes`` from this module, same pattern as
``api_server_room_grants``). Four routes only:

- ``GET  /hermes-secure-entry/form`` — renders the form. Never consumes the
  capability (link previews / prefetches are harmless), echoes no input.
- ``POST /hermes-secure-entry/site`` — non-consuming scoped lookup: the form
  proves its capability and gets back the exact site origin/host it is about
  to save a login for, BEFORE the user types anything.
- ``POST /hermes-secure-entry/submit`` — same-origin JSON POST carrying the
  fragment capability + identifier + password. Atomic single redemption via
  ``gateway/secure_entry.py``.
- ``POST /hermes-secure-entry/cancel`` — single-use cancellation driven by the
  capability itself (the form's Cancel button).

These routes are deliberately NOT behind the API-server bearer auth — their
auth IS the single-use ≥256-bit capability fragment. Every unrelated API route
keeps its existing auth. Security posture:

- exact same-origin checks against the operator-configured HTTPS origin
  (Host on every route; Origin/Referer on POST when present);
- bounded body (read to EOF under byte+time bounds) + content-type checks,
  strict field validation (exact keys, string values only, length caps),
  generic validation errors (nothing reflected);
- ``no-store``, ``no-referrer``, frame denial, nonce-based CSP (no
  ``unsafe-inline`` script), no third-party resources; site labels
  HTML-escaped;
- the capability lives in the URL fragment and the POST body, so the default
  aiohttp access log (method + path only) never captures secret material;
- the success reply means the broker ACCEPTED the entry — the vault save is
  performed by the owning tool afterwards, so the wording the user sees is
  "Submitted", never a fabricated "Saved".
"""

import html
import json
import logging
import secrets
import time

try:
    from aiohttp import web
except ImportError:  # pragma: no cover — gateway without aiohttp has no listener at all
    web = None  # type: ignore[assignment]

from gateway import secure_entry

logger = logging.getLogger(__name__)

# Reading a chunked body: a single content.read(n) may return only the current
# TCP chunk, so accumulate until EOF — but never past these bounds.
_BODY_TIME_BUDGET_S = 10.0

_GENERIC_INVALID = "Invalid or expired link."

_ALLOWED_KEYS = frozenset({"capability", "identifier", "password"})


def _http_routes(adapter) -> list:
    """Route table rows for ``api_server``; capability auth, not bearer auth."""
    return [
        ("GET", secure_entry.FORM_PATH, _handle_form),
        ("POST", secure_entry.SITE_PATH, _handle_site),
        ("POST", secure_entry.SUBMIT_PATH, _handle_submit),
        ("POST", secure_entry.CANCEL_PATH, _handle_cancel),
    ]


def _same_origin_ok(request) -> bool:
    """Exact same-origin check against the configured public HTTPS origin."""
    origin = secure_entry.configured_origin()
    if origin is None:
        return False
    expected_host = origin.split("://", 1)[1]
    host = (request.headers.get("Host") or "").strip().lower()
    if host != expected_host:
        return False
    # Behind TLS ingress the browser speaks https to us over plain HTTP.
    for header in ("Origin", "Referer"):
        value = (request.headers.get(header) or "").strip()
        if value:
            if not (value == origin or value.startswith(origin + "/")):
                return False
    return True


def _refused(status: int = 403) -> "web.Response":
    return web.Response(status=status, text=_GENERIC_INVALID, content_type="text/plain",
                        headers={"Cache-Control": "no-store"})


def _response_headers(nonce: str = "") -> dict:
    headers = {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        ),
    }
    return headers


async def _read_bounded(request) -> "bytes | None":
    """Read the request body to EOF, or None if it exceeds the byte/time budget.

    ``read(n)`` may return only what is currently buffered (a single TCP
    chunk), so accumulate ``readany()`` until EOF — bounded by bytes and a
    per-read time budget so a dribbling peer cannot hold the handler.
    """
    import asyncio
    limit = secure_entry.MAX_BODY_BYTES
    deadline = time.monotonic() + _BODY_TIME_BUDGET_S
    chunks = []
    total = 0
    while True:
        if total > limit:
            return None
        try:
            chunk = await asyncio.wait_for(request.content.readany(),
                                           max(0.05, deadline - time.monotonic()))
        except Exception:
            return None
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)


def _parse_payload(raw: bytes):
    """Strict JSON object with exactly the allowed keys, all string values."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if set(payload.keys()) - _ALLOWED_KEYS:
        return None
    for key in _ALLOWED_KEYS & set(payload.keys()):
        if not isinstance(payload[key], str):
            return None
    return payload


def _form_page() -> "web.Response":
    nonce = secrets.token_urlsafe(16)
    body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<!-- In-band policy survives ingress overwriting the Referrer-Policy response header. -->
<meta name="referrer" content="no-referrer">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Hermes — save login</title>
<style nonce="{nonce}">
 body{{font-family:system-ui,sans-serif;background:#14161a;color:#e6e6e6;display:flex;justify-content:center;padding:2rem 1rem}}
 card{{max-width:26rem;width:100%}}
 h1{{font-size:1.1rem}} label{{display:block;margin:.8rem 0 .2rem;font-size:.9rem}}
 input{{width:100%;box-sizing:border-box;padding:.55rem;border-radius:6px;border:1px solid #444;background:#1e2128;color:#e6e6e6}}
 button{{margin-top:1.2rem;width:100%;padding:.6rem;border:0;border-radius:6px;background:#4f8cff;color:#fff;font-size:1rem;cursor:pointer}}
 button.danger{{background:#8c3a3a}}
 button:disabled{{opacity:.5;cursor:default}}
 #status{{margin-top:1rem;font-size:.9rem;min-height:1.2em}}
 .err{{color:#ff8f8f}}.ok{{color:#8fe3a0}}.muted{{color:#9aa0a8;font-size:.85rem}}
 #site{{margin-top:.4rem;font-size:.9rem}}
</style></head><body><card>
<h1>Save login</h1>
<p id="site" class="muted">Checking which site this form is for…</p>
<p class="muted">This one-time form was opened from your Discord request. The password goes
straight into Hermes' encrypted vault — never into the chat or the conversation.</p>
<form id="f" autocomplete="off">
 <label for="i">Email, phone or username</label>
 <input id="i" name="identifier" autocomplete="off" required disabled>
 <label for="p">Password</label>
 <input id="p" name="password" type="password" autocomplete="new-password" required disabled>
 <button type="submit" id="go" disabled>Submit</button>
 <button type="button" id="cx" class="danger" disabled>Cancel</button>
</form>
<p id="status"></p>
<script nonce="{nonce}">
const st = document.getElementById('status');
const f = document.getElementById('f');
let cap = '';
try {{ cap = decodeURIComponent((location.hash || "").slice(1)) || ''; }} catch (_) {{ cap = ''; }}
// The capability lives only in the fragment; drop it from the visible URL
// (and thus history) as soon as it is held in memory.
try {{ history.replaceState(null, '', location.pathname); }} catch (_) {{}}
let done = false, inFlight = false, ready = false;

async function post(path, obj) {{
  const res = await fetch(path, {{method: 'POST',
    headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(obj)}});
  return {{ok: res.ok, status: res.status, text: await res.text()}};
}}

(async () => {{
  if (!cap) {{ st.textContent = '{_GENERIC_INVALID}'; st.className = 'err'; lock(); return; }}
  try {{
    const info = await post('{secure_entry.SITE_PATH}', {{capability: cap}});
    if (!info.ok) {{ st.textContent = '{_GENERIC_INVALID}'; st.className = 'err'; lock(); return; }}
    const meta = JSON.parse(info.text);
    // Exact target origin, from the broker's scoped metadata — shown before typing.
    const origin = String(meta.origin || '');
    const hostEl = document.getElementById('site');
    hostEl.textContent = 'For: ' + origin + ' — check this is the site you expected.';
    if (!origin) {{ st.textContent = '{_GENERIC_INVALID}'; st.className = 'err'; lock(); return; }}
    // Fail closed: entry stays IMPOSSIBLE until the exact scoped site is
    // confirmed. While the lookup is in flight (or has failed) the inputs and
    // both buttons stay disabled, so nothing can be typed or submitted against
    // an unconfirmed (possibly wrong-site) request.
    for (const el of document.querySelectorAll('input, button')) el.disabled = false;
    ready = true;
    hostEl.setAttribute('data-ready', '1');
  }} catch (_) {{
    st.textContent = 'Could not reach the gateway.'; st.className = 'err'; lock();
  }}
}})();

function lock() {{
  done = true;
  for (const b of document.querySelectorAll('button')) b.disabled = true;
  for (const i of document.querySelectorAll('input')) i.disabled = true;
}}

f.addEventListener('submit', async (e) => {{
  e.preventDefault();
  if (done || inFlight || !cap || !ready) return;
  inFlight = true;
  document.getElementById('go').disabled = true;
  st.textContent = 'Submitting…'; st.className = '';
  try {{
    const r = await post('{secure_entry.SUBMIT_PATH}',
      {{capability: cap, identifier: document.getElementById('i').value,
        password: document.getElementById('p').value}});
    if (r.ok) {{
      // Honest wording: Hermes has RECEIVED the entry; the vault save happens
      // in the owning tool. Watch Discord for the confirmation there.
      st.textContent = 'Submitted. Hermes received your login and will save it to the ' +
                       'encrypted vault — check Discord for the result.';
      st.className = 'ok';
      lock();
      f.reset();
    }} else {{
      inFlight = false;
      document.getElementById('go').disabled = false;
      st.textContent = r.text || '{_GENERIC_INVALID}'; st.className = 'err';
      if (r.status !== 200 && r.status !== 410) {{ /* transport-level issue: keep form */ }}
    }}
  }} catch (_) {{
    inFlight = false;
    document.getElementById('go').disabled = false;
    st.textContent = 'Could not reach the gateway.'; st.className = 'err';
  }}
}});

document.getElementById('cx').addEventListener('click', async () => {{
  if (done || !ready) return;
  try {{
    const r = await post('{secure_entry.CANCEL_PATH}', {{capability: cap}});
    st.textContent = (r.ok ? 'Cancelled — nothing was submitted or saved.'
                           : (r.text || '{_GENERIC_INVALID}'));
    st.className = r.ok ? 'ok' : 'err';
  }} catch (_) {{ st.textContent = 'Could not reach the gateway.'; st.className = 'err'; }}
  lock();
}});
</script>
</card></body></html>"""
    return web.Response(text=body, content_type="text/html", headers=_response_headers(nonce))


async def _handle_form(request) -> "web.Response":
    if not _same_origin_ok(request):
        return _refused()
    # GET never consumes the capability; the exact site label is fetched
    # afterwards via the scoped, non-consuming SITE lookup.
    return _form_page()


async def _handle_site(request) -> "web.Response":
    if not _same_origin_ok(request):
        return _refused()
    if (request.content_type or "").lower() != "application/json":
        return _refused(415)
    raw = await _read_bounded(request)
    if raw is None:
        return _refused(413)
    payload = _parse_payload(raw)
    if payload is None or set(payload.keys()) != {"capability"}:
        return _refused(400)
    info = secure_entry.find_by_capability(payload["capability"])
    if info is None:
        return web.json_response({"ok": False}, status=410, headers={"Cache-Control": "no-store"})
    # Scoped metadata only: exact site origin/host; never a secret.
    return web.json_response({"ok": True, "origin": info["origin"], "host": info["host"]},
                             headers={"Cache-Control": "no-store"})


async def _handle_submit(request) -> "web.Response":
    if not _same_origin_ok(request):
        return _refused()
    if (request.content_type or "").lower() != "application/json":
        return _refused(415)
    raw = await _read_bounded(request)
    if raw is None:
        return _refused(413)
    payload = _parse_payload(raw)
    if payload is None or set(payload.keys()) != {"capability", "identifier", "password"}:
        return _refused(400)
    ok, err = secure_entry.redeem(payload["capability"], payload["identifier"], payload["password"])
    # Generic errors only; identifiers/passwords are never echoed or logged.
    if not ok:
        return web.Response(status=410, text=err or _GENERIC_INVALID, content_type="text/plain",
                            headers={"Cache-Control": "no-store"})
    # Accepted by the broker — the vault save is the owning tool's job; the
    # reply wording must not claim it already happened.
    return web.json_response({"ok": True, "status": "submitted"},
                             headers={"Cache-Control": "no-store"})


async def _handle_cancel(request) -> "web.Response":
    if not _same_origin_ok(request):
        return _refused()
    if (request.content_type or "").lower() != "application/json":
        return _refused(415)
    raw = await _read_bounded(request)
    if raw is None:
        return _refused(413)
    payload = _parse_payload(raw)
    if payload is None or set(payload.keys()) != {"capability"}:
        return _refused(400)
    ok = secure_entry.cancel_by_capability(payload["capability"])
    if not ok:
        return web.Response(status=410, text=_GENERIC_INVALID, content_type="text/plain",
                            headers={"Cache-Control": "no-store"})
    return web.json_response({"ok": True, "status": "cancelled"},
                             headers={"Cache-Control": "no-store"})
