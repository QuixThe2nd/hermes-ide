"""Live GitHub webhook listener — HMAC-verified HTTP, stdlib only.

GitHub POSTs ``pull_request`` events here; after signature verification the
PR is reviewed through ``review_one_pr`` — the same function the poll loop
uses — so webhook and poll share one seen-map, one marker, one idempotency
contract. The HTTP handler never blocks on the LLM: it answers 202 and the
review runs on a single background worker.

This is deliberately NOT the ``platforms.webhook`` agent platform: that one
binds loopback and spends a whole agent turn per delivery, while this plugin
already knows how to review and calls that path directly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlparse

from plugins.pr_intent_watch import github
from plugins.pr_intent_watch.core import (
    DEFAULT_WEBHOOK_PATH,
    PrIntentWatchError,
    WatchConfig,
    load_config_section,
    load_state,
    review_one_pr,
    run_tick,
    save_state,
    watch_config_from_raw,
)

logger = logging.getLogger(__name__)

HEALTH_PATH = "/health"
EVENT_HEADER = "X-GitHub-Event"
SIGNATURE_HEADER = "X-Hub-Signature-256"

#: Intent is about opening, not every push — only these actions review.
REVIEWED_ACTIONS = frozenset({"opened", "reopened"})

#: One review worker: webhook reviews serialize, so two deliveries of the
#: same PR cannot race the seen-map, and a burst of openings cannot fan out
#: into a crowd of LLM calls.
REVIEW_WORKERS = 1

#: GitHub pull_request payloads run ~100KB; anything past this is not GitHub.
MAX_BODY_BYTES = 5 * 1024 * 1024


# ── Signature ───────────────────────────────────────────────────────────────


def verify_signature(body: bytes, signature: Optional[str], secret: str) -> bool:
    """Constant-time check of GitHub's ``sha256=<hex>`` HMAC header."""
    if not signature or not secret:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    try:
        return hmac.compare_digest(
            expected.encode("ascii"), signature.strip().lower().encode("ascii")
        )
    except UnicodeEncodeError:
        # Non-ASCII header junk — not a signature GitHub would ever send.
        return False


# ── Secret ──────────────────────────────────────────────────────────────────


def ensure_webhook_secret(state: dict) -> str:
    """The shared secret GitHub signs deliveries with — generated once.

    Lives in the state file (mode 0600 via ``save_state``), never in
    config.yaml and never in a log line. ``run.py --print-webhook-secret``
    is the operator escape hatch for registering the hook.
    """
    secret = state.get("webhook_secret")
    if isinstance(secret, str) and secret.strip():
        return secret
    secret = secrets.token_urlsafe(32)
    state["webhook_secret"] = secret
    save_state(state)
    return secret


# ── Review delivery ─────────────────────────────────────────────────────────


def review_delivery(number: int, config: WatchConfig) -> dict:
    """Review one webhook-delivered PR (runs on the worker thread).

    Shares the poll's state file and skip rules, but never the poll's
    baseline: a PR GitHub just told us about is by definition new.
    """
    token = github.resolve_token()
    if not token:
        logger.warning(
            "pr_intent_watch webhook: no GitHub token; PR %s left for the poll",
            number,
        )
        return {"action": "no_token"}

    state = load_state()
    result = review_one_pr(number, config=config, token=token, state=state)
    if result.get("dirty"):
        save_state(state)
    logger.info(
        "pr_intent_watch webhook: PR %s handled (%s)",
        number,
        result.get("action") or "nothing",
    )
    return result


class ReviewQueue:
    """Serialized webhook reviews — submit never blocks the HTTP handler."""

    def __init__(self, config: WatchConfig, *, max_workers: int = REVIEW_WORKERS):
        self._config = config
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="pr-intent-review"
        )

    def submit(self, number: int) -> None:
        self._executor.submit(self._review, number)

    def _review(self, number: int) -> None:
        try:
            review_delivery(number, self._config)
        except Exception:  # noqa: BLE001 — a worker crash must not kill the pool
            logger.exception("pr_intent_watch webhook review of PR %s failed", number)

    def close(self) -> None:
        self._executor.shutdown(wait=False)


# ── HTTP ────────────────────────────────────────────────────────────────────


class WebhookServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        config: WatchConfig,
        *,
        on_delivery: Callable[[int], None],
        secret: str,
    ) -> None:
        self.watch_config = config
        self.on_delivery = on_delivery
        self.webhook_secret = secret
        super().__init__(address, WebhookHandler)


class WebhookHandler(BaseHTTPRequestHandler):
    """One GitHub delivery. Never logs the secret, token, or raw HMAC."""

    # Default banners leak the Python version; nothing here needs to.
    server_version = "hermes-pr-intent-watch"
    sys_version = ""

    @property
    def watch_config(self) -> WatchConfig:
        return self.server.watch_config  # type: ignore[attr-defined,no-any-return]

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        path = urlparse(self.path).path
        if path == HEALTH_PATH:
            self._send_json(200, {"status": "ok"})
            return
        if path == self.watch_config.webhook_path:
            # One line so reverse-proxy probes get a body they can see.
            self._send_text(200, "pr_intent_watch\n")
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 — http.server API
        path = urlparse(self.path).path
        if path != self.watch_config.webhook_path:
            self._send_json(404, {"error": "not found"})
            return

        body = self._read_body()
        if body is None:
            return  # already answered (413)

        if not verify_signature(
            body, self.headers.get(SIGNATURE_HEADER), self.server.webhook_secret
        ):
            self._send_json(401, {"error": "bad signature"})
            return

        event = (self.headers.get(EVENT_HEADER) or "").strip().lower()
        if event == "ping":
            # GitHub sends this when the hook is created.
            self._send_json(200, {"ok": True})
            return
        if event != "pull_request":
            self._send_json(204)
            return

        payload = self._parse_json(body)
        if payload is None:
            return  # already answered (400)

        action = str(payload.get("action") or "")
        if action not in REVIEWED_ACTIONS:
            # synchronize/closed/labeled/edited/… — intent is about opening.
            self._send_json(204)
            return

        repository = payload.get("repository")
        full_name = (
            str(repository.get("full_name") or "") if isinstance(repository, dict) else ""
        )
        if full_name.lower() != self.watch_config.repo.lower():
            self._send_json(204)
            return

        pull = payload.get("pull_request")
        try:
            number = int(pull.get("number")) if isinstance(pull, Mapping) else 0
        except (TypeError, ValueError):
            number = 0
        if number <= 0:
            self._send_json(400, {"error": "no pull request number"})
            return

        # Answer first, review after — GitHub retries slow endpoints, and the
        # LLM call takes seconds. Submitting to the queue is instantaneous.
        self._send_json(202, {"queued": number})
        try:
            self.server.on_delivery(number)
        except Exception:  # noqa: BLE001 — delivery must never kill the handler
            logger.exception("pr_intent_watch webhook could not enqueue PR %s", number)

    # ── plumbing ────────────────────────────────────────────────────────────

    def _read_body(self) -> Optional[bytes]:
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = 0
        if size <= 0:
            return b""
        if size > MAX_BODY_BYTES:
            self._send_json(413, {"error": "payload too large"})
            return None
        return self.rfile.read(size)

    def _parse_json(self, body: bytes) -> Optional[dict]:
        if not body:
            self._send_json(400, {"error": "empty payload"})
            return None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "bad payload"})
            return None
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "bad payload"})
            return None
        return payload

    def _send_json(self, status: int, payload: Optional[Mapping[str, Any]] = None) -> None:
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        self._send(status, body, "application/json")

    def _send_text(self, status: int, text: str) -> None:
        self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _send(
        self, status: int, body: Optional[bytes], content_type: str
    ) -> None:
        self.send_response(status)
        if body is None:
            # 204 and friends carry no body; HTTP/1.0 closes the connection.
            self.end_headers()
            return
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("pr_intent_watch http: " + format, *args)


def make_server(
    config: WatchConfig,
    *,
    on_delivery: Callable[[int], None],
    secret: str,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> WebhookServer:
    """Bind the listener. ``host``/``port`` override the config (tests, exotic
    setups) — production reads them from ``pr_intent_watch.listen_*``."""
    address = (
        host if host is not None else config.listen_host,
        int(port if port is not None else config.listen_port),
    )
    return WebhookServer(address, config, on_delivery=on_delivery, secret=secret)


# ── Serve ───────────────────────────────────────────────────────────────────


def _install_signal_stop(stop: threading.Event) -> None:
    def _request_stop(_signum: Any, _frame: Any) -> None:
        stop.set()

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _request_stop)
        except (ValueError, OSError):
            # Not the main thread, or the platform refuses — the caller's
            # stop_event remains the way out.
            return


def serve(
    *,
    config_path: Optional[Path] = None,
    stop_event: Optional[threading.Event] = None,
    bind_host: Optional[str] = None,
    bind_port: Optional[int] = None,
) -> int:
    """The long-running entry: webhook HTTP + in-process poll backup.

    HTTP runs in a daemon thread; the main thread is the poll loop so a
    SIGTERM stops both. Never calls the scheduler reconcile — arming timers
    is the gateway's job, and this process IS the schedule now.
    """
    try:
        raw = load_config_section(config_path)
    except PrIntentWatchError as exc:
        logger.warning("pr_intent_watch serve could not read config (%s); defaults", exc)
        raw = {}
    config = watch_config_from_raw(raw)

    if not config.enabled:
        logger.info("pr_intent_watch disabled; not serving")
        return 0

    state = load_state()
    secret = ensure_webhook_secret(state)

    queue = ReviewQueue(config)
    try:
        server = make_server(
            config,
            on_delivery=queue.submit,
            secret=secret,
            host=bind_host,
            port=bind_port,
        )
    except OSError as exc:
        logger.error(
            "pr_intent_watch could not bind %s:%d: %s",
            config.listen_host,
            config.listen_port,
            exc,
        )
        queue.close()
        return 1
    http_thread = threading.Thread(
        target=server.serve_forever, name="pr-intent-watch-http", daemon=True
    )
    http_thread.start()
    logger.info(
        "pr_intent_watch webhook listening on http://%s:%d%s (poll backup every %ds)",
        server.server_address[0],
        server.server_address[1],
        config.webhook_path,
        config.poll_seconds,
    )

    stop = stop_event if stop_event is not None else threading.Event()
    _install_signal_stop(stop)
    try:
        while not stop.is_set():
            try:
                run_tick(config_path=config_path)
            except Exception:  # noqa: BLE001 — the poll must never kill serving
                logger.exception("pr_intent_watch poll tick failed")
            stop.wait(config.poll_seconds)
    finally:
        server.shutdown()
        server.server_close()
        queue.close()
    return 0


__all__ = [
    "DEFAULT_WEBHOOK_PATH",
    "REVIEWED_ACTIONS",
    "ReviewQueue",
    "WebhookHandler",
    "WebhookServer",
    "ensure_webhook_secret",
    "make_server",
    "review_delivery",
    "serve",
    "verify_signature",
]
