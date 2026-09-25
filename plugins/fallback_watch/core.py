"""Core logic — agent.log tail, fallback-line parsing, cooldown state, Discord alerts."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Tuple

HttpFn = Callable[[urllib.request.Request, float], Tuple[int, bytes]]
NowFn = Callable[[], float]
SendFn = Callable[[str], None]
SleepFn = Callable[[float], None]

DEFAULT_COOLDOWN_SECONDS = 120
DEFAULT_POLL_SECONDS = 1.0
DEFAULT_PLATFORM = "discord"
SEND_FAILURE_BACKOFF_SECONDS = 10.0
STATE_FILENAME = Path("state") / "fallback_watch.json"
LOG_FILENAME = Path("logs") / "agent.log"

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_USER_AGENT = "Hermes Agent (https://hermes-agent.nousresearch.com)"

FALLBACK_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*?"
    r"Fallback activated: (?P<from>.+?) → (?P<to>.+?) \((?P<provider>[^)]+)\)"
)
SESSION_RE = re.compile(r"\[(?P<session>\d{8}_\d{6}_[0-9a-f]+)\]")


class FallbackWatchError(Exception):
    """Raised instead of sys.exit from the service entrypoint."""


@dataclass(frozen=True)
class WatchConfig:
    enabled: bool
    platform: str
    chat_id: str
    cooldown_seconds: int
    poll_seconds: float


@dataclass(frozen=True)
class FallbackEvent:
    line: str
    timestamp: str
    from_model: str
    to_model: str
    provider: str
    session: str


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def state_path() -> Path:
    return _hermes_home() / STATE_FILENAME


def log_path() -> Path:
    return _hermes_home() / LOG_FILENAME


def _read_env_key(path: Path, key: str) -> Optional[str]:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FallbackWatchError(f"cannot read {path}: {exc}") from exc
    return None


def discord_token() -> str:
    """DISCORD_BOT_TOKEN from HERMES_HOME/.env, then secrets/discord.env.

    The resolved value is never logged or included in error text.
    """
    home = _hermes_home()
    candidates = (home / ".env", home / "secrets" / "discord.env")
    for candidate in candidates:
        token = _read_env_key(candidate, "DISCORD_BOT_TOKEN")
        if token:
            return token
    raise FallbackWatchError(
        "DISCORD_BOT_TOKEN missing in "
        + " or ".join(str(candidate) for candidate in candidates)
    )


def load_config_section(config_path: Optional[Path] = None) -> dict:
    if config_path is None:
        try:
            from hermes_cli.config import load_config_readonly

            raw = load_config_readonly()
        except Exception as exc:
            raise FallbackWatchError(f"cannot load config: {exc}") from exc
    else:
        import yaml

        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except OSError as exc:
            raise FallbackWatchError(f"cannot read {config_path}: {exc}") from exc
        except Exception as exc:
            raise FallbackWatchError(f"cannot parse {config_path}: {exc}") from exc
    return raw if isinstance(raw, dict) else {}


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _coerce_int(value: Any, default: int, *, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def load_watch_config(raw: Mapping[str, Any]) -> WatchConfig:
    """Normalize the ``fallback_watch`` config.yaml section.

    An absent section means disabled — the plugin is opt-in. Only Discord
    is wired today, so any other platform fails loudly instead of
    silently not alerting.
    """
    section = raw.get("fallback_watch")
    if section is None:
        section = {}
    if not isinstance(section, Mapping):
        raise FallbackWatchError("fallback_watch config must be a mapping")

    platform = (
        str(section.get("platform") or DEFAULT_PLATFORM).strip().lower()
        or DEFAULT_PLATFORM
    )
    if platform != DEFAULT_PLATFORM:
        raise FallbackWatchError(
            f"fallback_watch.platform {platform!r} is not supported;"
            f" only {DEFAULT_PLATFORM!r} is wired today"
        )

    enabled = _coerce_bool(section.get("enabled"), False)
    chat_id = str(section.get("chat_id") or "").strip()
    if enabled and not chat_id:
        raise FallbackWatchError(
            "fallback_watch.chat_id is required when fallback_watch.enabled is true"
        )

    cooldown_seconds = _coerce_int(
        section.get("cooldown_seconds"), DEFAULT_COOLDOWN_SECONDS, minimum=0
    )

    try:
        poll_seconds = float(section.get("poll_seconds", DEFAULT_POLL_SECONDS))
    except (TypeError, ValueError):
        poll_seconds = DEFAULT_POLL_SECONDS
    if poll_seconds <= 0:
        raise FallbackWatchError("fallback_watch.poll_seconds must be > 0")

    return WatchConfig(
        enabled=enabled,
        platform=platform,
        chat_id=chat_id,
        cooldown_seconds=cooldown_seconds,
        poll_seconds=poll_seconds,
    )


def load_config(config_path: Optional[Path] = None) -> WatchConfig:
    return load_watch_config(load_config_section(config_path))


def load_state() -> dict:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".fallback-watch.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        raise FallbackWatchError(f"cannot write {path}: {exc}") from exc


def parse_fallback_line(line: str) -> Optional[FallbackEvent]:
    """Return the fallback event a log line reports, or None.

    The session id is taken from the same line's ``[YYYYMMDD_HHMMSS_hex]``
    bracket; lines without one report ``unknown``.
    """
    match = FALLBACK_RE.search(line)
    if not match:
        return None
    session_match = SESSION_RE.search(line)
    return FallbackEvent(
        line=line,
        timestamp=match.group("ts"),
        from_model=match.group("from").strip(),
        to_model=match.group("to").strip(),
        provider=match.group("provider").strip(),
        session=session_match.group("session") if session_match else "unknown",
    )


def format_alert(event: FallbackEvent, suppressed: int = 0) -> str:
    message = (
        "⚠️ Hermes primary model fallback activated\n"
        f"Primary: `{event.from_model}`\n"
        f"Fallback: `{event.to_model}` via `{event.provider}`\n"
        f"Session: `{event.session}`\n"
        f"Time: `{event.timestamp}`"
    )
    if suppressed > 0:
        message += (
            f"\nNote: `{suppressed}` additional fallback event(s)"
            " were suppressed during cooldown."
        )
    return message


def default_http(
    req: urllib.request.Request, timeout: float = 25.0
) -> Tuple[int, bytes]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:
        raise FallbackWatchError(f"network error: {type(exc).__name__}: {exc}") from exc


def send_discord_alert(
    content: str,
    chat_id: str,
    *,
    token: Optional[str] = None,
    http_fn: HttpFn = default_http,
) -> None:
    """POST one message to a Discord channel via plain REST.

    ``allowed_mentions.parse=[]`` keeps a compromised log line from
    turning an alert into a ping storm. The token never appears in any
    exception text.
    """
    if token is None:
        token = discord_token()
    url = f"{DISCORD_API_BASE}/channels/{chat_id}/messages"
    data = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode(
        "utf-8"
    )
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": DISCORD_USER_AGENT,
        },
        method="POST",
    )
    status, body = http_fn(req)
    if status >= 300:
        if isinstance(body, bytes):
            detail = body[:200].decode("utf-8", errors="replace")
        else:
            detail = str(body)[:200]
        raise FallbackWatchError(f"discord message post returned {status}: {detail}")


def follow_from_eof(
    path: Path,
    *,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    stop_event: Optional[threading.Event] = None,
    sleep_fn: SleepFn = time.sleep,
) -> Iterator[str]:
    """Yield appended lines from ``path``, starting at EOF.

    The first open jumps to EOF so enabling the watch never replays
    historical outages. Rotation (new inode) reopens the fresh file from
    the start — everything in it postdates the old one — and an
    in-place truncate rewinds to byte 0. Polls every ``poll_seconds``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    # binary mode so tell() is an exact byte offset — text-mode tell()
    # returns an opaque cookie that cannot be compared against st_size
    # once multi-byte characters (the → in real fallback lines) appear
    handle = path.open("rb")
    try:
        handle.seek(0, os.SEEK_END)
        inode = os.fstat(handle.fileno()).st_ino
        while stop_event is None or not stop_event.is_set():
            raw = handle.readline()
            if raw:
                yield raw.decode("utf-8", errors="ignore").rstrip("\r\n")
                continue
            if stop_event is not None and stop_event.is_set():
                break
            sleep_fn(poll_seconds)
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            if st.st_ino != inode:
                handle.close()
                handle = path.open("rb")
                inode = os.fstat(handle.fileno()).st_ino
            elif st.st_size < handle.tell():
                handle.seek(0)
    finally:
        handle.close()


def _default_error_log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def watch_lines(
    lines: Iterable[str],
    config: WatchConfig,
    state: dict,
    *,
    send: SendFn,
    now_fn: NowFn = time.time,
    sleep_fn: SleepFn = time.sleep,
    on_alert: Optional[Callable[[str], None]] = None,
    on_error: Optional[Callable[[str], None]] = _default_error_log,
    failure_backoff_seconds: float = SEND_FAILURE_BACKOFF_SECONDS,
) -> dict:
    """Consume tail lines, alert once per cooldown window, count the rest.

    State (``last_alert_at``, ``last_line``, ``suppressed_since_last``)
    is persisted after every fallback event, so a restart resumes
    cooldown exactly where it left off. Suppressed events are mentioned
    in the next alert that does go out.
    """
    last_alert_at = _as_float(state.get("last_alert_at"), 0.0)
    last_line = str(state.get("last_line") or "")

    for line in lines:
        event = parse_fallback_line(line)
        if event is None:
            continue
        if event.line == last_line:
            continue
        last_line = event.line
        state["last_line"] = event.line

        if now_fn() - last_alert_at < config.cooldown_seconds:
            state["suppressed_since_last"] = (
                int(state.get("suppressed_since_last") or 0) + 1
            )
            save_state(state)
            continue

        suppressed = int(state.pop("suppressed_since_last", 0) or 0)
        message = format_alert(event, suppressed)
        try:
            send(message)
        except Exception as exc:
            # keep the tally for the next successful alert; the event
            # itself is not retried (last_line already recorded it)
            if suppressed:
                state["suppressed_since_last"] = suppressed
            save_state(state)
            if on_error:
                on_error(f"failed to send fallback alert: {exc}")
            sleep_fn(failure_backoff_seconds)
            continue

        last_alert_at = now_fn()
        state["last_alert_at"] = last_alert_at
        save_state(state)
        if on_alert:
            on_alert(message)
    return state


def _as_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed
