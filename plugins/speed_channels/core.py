"""Core speed_channels logic — downloader polling and Discord renames.

Three Discord voice channels act as live download walls. Every tick (intended
to run every minute via cron) renames them with the downloader's current
throughput and queue depth, and refreshes the Speeds category label:

* ``qBittorrent: 2.4 MB/s ↓ • 5 in queue``
* ``SABnzbd: 1.1 MB/s ↓ • 12 in queue``
* ``slskd: 340 KB/s ↓ • 96 KB/s ↑ • 3 in queue``
* ``Speeds • 33ms • 21/8 6:29pm • Next: 6:34pm``

Channel renames are rate-limited by Discord (2 per 10 min per channel), so a
channel is only PATCHed when its name actually changes, and the category label
— which is touched every tick — skips on 429 rather than raising. The label's
ICMP latency to 1.1.1.1 is measured every tick but only accepted for display
when it moves by 5ms or more, so jitter keeps the label byte-identical and the
rename short-circuits as unchanged. Latency redisplay flushes only on polling
ticks; between polls it is held in ``pending_latency_ms`` so every category
PATCH carries the scheduled timestamp update and latency never steals the
2-per-10-min rename budget.

Downloader API shapes, env-file names and label formats follow the reference
script the live deployment ran; the peer's absolute secret paths are resolved
relative to ``HERMES_HOME`` here instead.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

# Transport contract. Unlike quota_channels' (status, body) pair this also
# returns response headers, because qBittorrent's session cookie arrives on the
# login response's Set-Cookie and has to be replayed on the two reads that
# follow it.
HttpFn = Callable[
    [urllib.request.Request, float], Tuple[int, bytes, Dict[str, str]]
]
NowFn = Callable[[], float]
# Latency probe: one ICMP echo to Cloudflare DNS, milliseconds or None.
PingFn = Callable[[], Optional[float]]

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_USER_AGENT = "DiscordBot (https://github.com/hermes-agent, 1.0)"

DEFAULT_POLL_INTERVAL_SECONDS = 300
STATE_FILENAME = "speed_channels_state.json"

# Hardcoded on purpose — a config knob for one host is more surface than the
# feature needs.
PING_HOST = "1.1.1.1"
# Redisplay latency only when it moves this far from the last displayed value,
# so a jittering RTT cannot eat the category's 2-renames-per-10-min budget.
LATENCY_HYSTERESIS_MS = 5.0

DOWNLOADERS: Tuple[Tuple[str, str], ...] = (
    ("qbittorrent", "qBittorrent"),
    ("sabnzbd", "SABnzbd"),
    ("slskd", "slskd"),
)


class SpeedChannelsError(Exception):
    """Raised instead of sys.exit, mirroring quota_channels."""


# ---------------------------------------------------------------------------
# Paths, secrets, state
# ---------------------------------------------------------------------------


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def state_path() -> Path:
    return _hermes_home() / STATE_FILENAME


def _secret_env(name: str) -> Path:
    """Secret env files always resolve under HERMES_HOME, never a fixed path."""
    return _hermes_home() / "secrets" / name


def _read_env_key(path: Path, key: str) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError as exc:
        raise SpeedChannelsError(f"cannot read {path}: {exc}") from exc
    raise SpeedChannelsError(f"{key} missing in {path}")


# Env var holding the bot token in HERMES_HOME/secrets/discord.env, same
# source as home_server/quota_channels. A constant so tests reference the key
# instead of re-typing the literal.
DISCORD_TOKEN_ENV_KEY = "DISCORD_BOT_TOKEN"


def discord_token() -> str:
    """Bot token from HERMES_HOME/secrets/discord.env. Never logged."""
    return _read_env_key(_secret_env("discord.env"), DISCORD_TOKEN_ENV_KEY)


def load_state() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(
    last_poll_success: int,
    last_latency_ms: Optional[float] = None,
    pending_latency_ms: Optional[float] = None,
) -> int:
    """Atomically persist the tick's state; ``pending_latency_ms`` is the
    latency move accepted between polls and waiting for the next rename."""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".speeds-state.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "last_poll_success": last_poll_success,
                    "last_latency_ms": last_latency_ms,
                    "pending_latency_ms": pending_latency_ms,
                },
                handle,
                indent=2,
            )
        os.replace(tmp, path)
    except OSError as exc:
        raise SpeedChannelsError(f"cannot write {path}: {exc}") from exc
    return last_poll_success


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def validate_speed_config(section: Mapping[str, Any]) -> dict:
    if not isinstance(section, Mapping):
        raise SpeedChannelsError("speed_channels config must be a mapping")

    guild_id = section.get("guild_id")
    category_id = section.get("category_id")
    if not guild_id or not category_id:
        raise SpeedChannelsError(
            "speed_channels requires guild_id and category_id in config.yaml"
        )

    channel_ids = section.get("channel_ids") or {}
    if not isinstance(channel_ids, Mapping):
        raise SpeedChannelsError("speed_channels.channel_ids must be a mapping")

    resolved: Dict[str, str] = {}
    for key, _label in DOWNLOADERS:
        channel_id = channel_ids.get(key)
        if not channel_id:
            raise SpeedChannelsError(
                f"speed_channels.channel_ids.{key} required"
            )
        resolved[key] = str(channel_id)

    return {
        "guild_id": str(guild_id),
        "category_id": str(category_id),
        "channel_ids": resolved,
        "poll_interval_seconds": int(
            section.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)
        ),
    }


def load_speed_config(config_path: Optional[Path] = None) -> dict:
    if config_path is None:
        from hermes_cli.config import load_config_readonly

        raw = load_config_readonly()
    else:
        import yaml

        try:
            raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        except OSError as exc:
            raise SpeedChannelsError(f"cannot read {config_path}: {exc}") from exc
        except Exception as exc:
            raise SpeedChannelsError(f"cannot parse {config_path}: {exc}") from exc
    section = raw.get("speed_channels")
    if section is None:
        raise SpeedChannelsError("speed_channels section missing in config.yaml")
    return validate_speed_config(section)


def check_minimum_config_from_mapping(config: Mapping[str, Any]) -> bool:
    try:
        section = config.get("speed_channels")
        if not isinstance(section, Mapping):
            return False
        validate_speed_config(section)
        return True
    except SpeedChannelsError:
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def default_http(
    req: urllib.request.Request, timeout: float = 20.0
) -> Tuple[int, bytes, Dict[str, str]]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, resp.read(), headers
    except urllib.error.HTTPError as exc:
        headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return exc.code, exc.read(), headers
    except Exception as exc:
        raise SpeedChannelsError(
            f"network error: {type(exc).__name__}: {exc}"
        ) from exc


def _json_body(status: int, body: bytes, what: str) -> Any:
    if status != 200:
        raise SpeedChannelsError(f"{what} returned {status}: {body[:200]!r}")
    try:
        return json.loads(body.decode(errors="replace"))
    except json.JSONDecodeError as exc:
        raise SpeedChannelsError(f"{what} response not JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Formatting (label formats come straight from the reference script)
# ---------------------------------------------------------------------------


def fmt_speed(bytes_per_s: float) -> str:
    """Compact human speed; the narrow no-break space keeps units unwrapped."""
    b = max(0.0, float(bytes_per_s))
    if b < 1024:
        return f"{int(b)} B/s"
    if b < 1024 ** 2:
        return f"{b / 1024:.0f} KB/s"
    if b < 1024 ** 3:
        return f"{b / 1024 ** 2:.1f} MB/s"
    return f"{b / 1024 ** 3:.2f} GB/s"


def _fmt_clock(dt: datetime) -> str:
    hour = dt.hour % 12 or 12
    suffix = "am" if dt.hour < 12 else "pm"
    return f"{hour}:{dt.minute:02d}{suffix}"


def fmt_latency(latency_ms: Optional[float]) -> str:
    """``33ms`` / ``<1ms`` / ``timeout`` — None means no answer this tick."""
    if latency_ms is None:
        return "timeout"
    if 0 <= latency_ms < 1:
        return "<1ms"
    return f"{int(latency_ms)}ms"


def fmt_ts(epoch: float) -> str:
    dt = datetime.fromtimestamp(epoch)  # local time, like the reference script
    return f"{dt.day}/{dt.month} {_fmt_clock(dt)}"


def fmt_time(epoch: float) -> str:
    return _fmt_clock(datetime.fromtimestamp(epoch))


def channel_names(
    qbit_dl: float,
    qbit_queue: int,
    sab_dl: float,
    sab_queue: int,
    slsk_dl: float,
    slsk_up: float,
    slsk_queue: int,
) -> Dict[str, str]:
    """The three voice-channel names for one poll."""
    return {
        "qbittorrent": f"qBittorrent: {fmt_speed(qbit_dl)} ↓ • {qbit_queue} in queue",
        "sabnzbd": f"SABnzbd: {fmt_speed(sab_dl)} ↓ • {sab_queue} in queue",
        "slskd": (
            f"slskd: {fmt_speed(slsk_dl)} ↓ • {fmt_speed(slsk_up)} ↑"
            f" • {slsk_queue} in queue"
        ),
    }


def category_name(
    last_success: float,
    interval: int,
    latency_ms: Optional[float] = None,
    now_fn: NowFn = time.time,
) -> str:
    """``Speeds • <latency> • <last-success ts | never> • Next: <time | Due>``.

    ``latency_ms`` is the value already accepted by hysteresis — the caller
    decides what to display; this function never pings.
    """
    lat = fmt_latency(latency_ms)
    if last_success <= 0:
        return f"Speeds • {lat} • never • Next: Due"
    now = now_fn()
    next_due = last_success + interval
    ts_part = fmt_ts(last_success)
    if now >= next_due:
        return f"Speeds • {lat} • {ts_part} • Next: Due"
    return f"Speeds • {lat} • {ts_part} • Next: {fmt_time(next_due)}"


# ---------------------------------------------------------------------------
# Latency (best-effort ICMP, independent of the download walls)
# ---------------------------------------------------------------------------

# iputils: ``time=33.338 ms`` / ``time=33.3ms`` / ``time<1ms`` / ``time<1 ms``;
# Windows: ``time=33ms`` / ``time<1ms``, localized as ``Zeit=33ms`` on German
# Windows and other labels elsewhere. So match any short alphabetic label
# before the ``=``/``<`` — the unit ``ms`` is the stable anchor, and a false
# positive from one line per reply is not a realistic shape.
_PING_TIME_RE = re.compile(
    r"[A-Za-z]+\s*([=<])\s*([0-9]+(?:\.[0-9]+)?)\s*ms", re.IGNORECASE
)


def _parse_ping_time(stdout: str) -> Optional[float]:
    """RTT in ms from ping's stdout, or None when no time= is reported."""
    match = _PING_TIME_RE.search(stdout)
    if match is None:
        return None
    value = float(match.group(2))
    if match.group(1) == "<":
        # ``time<1ms`` is a ceiling, not a measurement — pin it inside
        # [0, 1) so fmt_latency renders ``<1ms`` instead of a bogus exact
        # ``1ms``.
        value -= 0.5
    return value


def default_ping() -> Optional[float]:
    """One ICMP echo to 1.1.1.1; milliseconds, or None on any failure.

    Never raises: a missing ping binary, a timeout, a nonzero exit, or
    unparseable output all mean "no answer this tick", which renders as
    ``timeout`` rather than failing the tick.
    """
    if sys.platform == "darwin":
        # macOS ping's -W is milliseconds, not seconds — a literal 2 waits
        # 2ms and times every probe out. There is no clean fail-wait flag
        # there, so the outer subprocess timeout does the waiting.
        argv = ["ping", "-c", "1", PING_HOST]
    elif os.name == "nt":
        argv = ["ping", "-n", "1", "-w", "2000", PING_HOST]
    else:
        argv = ["ping", "-c", "1", "-W", "2", PING_HOST]
    try:
        proc = subprocess.run(
            argv, capture_output=True, timeout=3, text=True,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return _parse_ping_time(proc.stdout)


def displayed_latency(
    previous: Optional[float], sample: Optional[float]
) -> Optional[float]:
    """Which latency value this tick accepts for display.

    Keep ``previous`` unless there was none, the sample timed out, or the
    sample moved by ``LATENCY_HYSTERESIS_MS`` or more — a stable number keeps
    the category name byte-identical, so ``rename_channel`` short-circuits and
    the timestamp/"Next:" updates keep their share of the rename budget. The
    accepted value is shown immediately on polling ticks and held in
    ``pending_latency_ms`` between polls (see ``run_tick``).
    """
    if previous is None or sample is None:
        return sample
    if abs(sample - previous) < LATENCY_HYSTERESIS_MS:
        return previous
    return sample


# ---------------------------------------------------------------------------
# Downloader polls
# ---------------------------------------------------------------------------


def qbit_speeds(http_fn: HttpFn = default_http) -> Tuple[float, float, int]:
    """qBittorrent: login → transfer/info + torrents/info?filter=downloading."""
    env = _secret_env("qbittorrent.env")
    base = _read_env_key(env, "QBIT_BASE_URL").rstrip("/")
    user = _read_env_key(env, "QBIT_USER")
    password = _read_env_key(env, "QBIT_PASS")

    # Field names only — values come from the env file, never this diff.
    form = urllib.parse.urlencode({"username": user, "pass": password}).encode()
    login_req = urllib.request.Request(
        base + "/api/v2/auth/login",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    status, body, headers = http_fn(login_req, 20.0)
    if status not in (200, 204):
        raise SpeedChannelsError(f"qbit login returned {status}")
    if body and body.strip() not in (b"Ok.", b""):
        raise SpeedChannelsError("qbit login rejected (bad credentials)")

    # The session id rides on Set-Cookie; replay it on the two reads.
    cookie = "; ".join(v for k, v in headers.items() if k == "set-cookie")
    auth_headers = {"Cookie": cookie} if cookie else {}

    info_req = urllib.request.Request(
        base + "/api/v2/transfer/info", headers=auth_headers
    )
    status, body, _ = http_fn(info_req, 20.0)
    info = _json_body(status, body, "qbit transfer/info")

    torrents_req = urllib.request.Request(
        base + "/api/v2/torrents/info?filter=downloading", headers=auth_headers
    )
    status, body, _ = http_fn(torrents_req, 20.0)
    downloading = _json_body(status, body, "qbit torrents/info")
    if not isinstance(downloading, list):
        raise SpeedChannelsError("qbit torrents/info not a list")

    return (
        float(info.get("dl_info_speed") or 0),
        float(info.get("up_info_speed") or 0),
        len(downloading),
    )


def sab_speed(http_fn: HttpFn = default_http) -> Tuple[float, int]:
    """SABnzbd: GET /api?mode=queue&output=json — kbpersec, noofslots."""
    env = _secret_env("sabnzbd.env")
    base = _read_env_key(env, "SABNZBD_URL").rstrip("/")
    apikey = _read_env_key(env, "SABNZBD_API_KEY")

    url = f"{base}/api?mode=queue&output=json&apikey={urllib.parse.quote(apikey)}"
    status, body, _ = http_fn(urllib.request.Request(url), 20.0)
    payload = _json_body(status, body, "sab queue endpoint")
    queue = payload.get("queue") if isinstance(payload, dict) else None
    if queue is None:
        raise SpeedChannelsError("no queue object in sab payload")
    try:
        speed = float(queue.get("kbpersec") or 0) * 1024
        slots = int(queue.get("noofslots") or 0)
    except (TypeError, ValueError):
        raise SpeedChannelsError("unparsable sab queue fields") from None
    return speed, slots


def _slsk_totals(payload: Any) -> Tuple[float, int]:
    speed = 0.0
    active = 0
    if not isinstance(payload, list):
        return speed, active
    for user in payload:
        if not isinstance(user, dict):
            continue
        for directory in user.get("directories", []) or []:
            if not isinstance(directory, dict):
                continue
            for file_entry in directory.get("files", []) or []:
                if not isinstance(file_entry, dict):
                    continue
                state = str(file_entry.get("state") or "")
                if "Succeeded" in state or "Cancelled" in state:
                    continue
                active += 1
                speed += float(file_entry.get("averageSpeed") or 0)
    return speed, active


def slskd_speeds(http_fn: HttpFn = default_http) -> Tuple[float, float, int]:
    """slskd: sum per-file averageSpeed over downloads/uploads via X-API-Key."""
    env = _secret_env("slskd.env")
    base = _read_env_key(env, "SLSKD_URL").rstrip("/")
    apikey = _read_env_key(env, "SLSKD_API_KEY")
    headers = {"X-API-Key": apikey}

    def fetch(path: str) -> Any:
        status, body, _ = http_fn(
            urllib.request.Request(base + path, headers=headers), 20.0
        )
        return _json_body(status, body, f"slskd {path}")

    dl_speed, dl_active = _slsk_totals(fetch("/api/v0/transfers/downloads"))
    up_speed, _ = _slsk_totals(fetch("/api/v0/transfers/uploads"))
    return dl_speed, up_speed, dl_active


# ---------------------------------------------------------------------------
# Discord renames
# ---------------------------------------------------------------------------


def discord_headers() -> dict:
    return {
        "Authorization": "Bot " + discord_token(),
        "User-Agent": DISCORD_USER_AGENT,
        "Content-Type": "application/json",
    }


def fetch_channel_name(
    channel_id: str, headers: dict, http_fn: HttpFn = default_http
) -> str:
    req = urllib.request.Request(
        f"{DISCORD_API_BASE}/channels/{channel_id}", headers=headers
    )
    status, body, _ = http_fn(req, 25.0)
    data = _json_body(status, body, f"discord channel fetch ({channel_id})")
    return str(data.get("name") or "")


def rename_channel(
    channel_id: str,
    name: str,
    headers: dict,
    *,
    skip_on_429: bool = False,
    http_fn: HttpFn = default_http,
) -> str:
    """Rename only when the name changed (2 renames / 10 min / channel).

    ``skip_on_429`` is for the category label, which is touched every tick —
    a 429 there is expected and must not fail the cron run.
    """
    if fetch_channel_name(channel_id, headers, http_fn=http_fn) == name:
        return "unchanged"
    req = urllib.request.Request(
        f"{DISCORD_API_BASE}/channels/{channel_id}",
        data=json.dumps({"name": name}).encode(),
        headers=headers,
        method="PATCH",
    )
    status, body, _ = http_fn(req, 25.0)
    if status == 429 and skip_on_429:
        return "skipped"
    if status != 200:
        raise SpeedChannelsError(
            f"discord rename returned {status}: {body[:200]!r}"
        )
    return "renamed"


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------


def poll_due(state: Mapping[str, Any], interval: int, *, force: bool, now_fn: NowFn) -> bool:
    if force:
        return True
    try:
        last = float(state.get("last_poll_success") or 0)
    except (TypeError, ValueError):
        return True
    return (now_fn() - last) >= interval


def run_tick(
    config: dict,
    *,
    force: bool = False,
    now_fn: NowFn = time.time,
    http_fn: HttpFn = default_http,
    ping_fn: PingFn = default_ping,
) -> dict:
    """One tick: poll the downloaders if due, then always refresh the label.

    All three downloaders must succeed for a poll to count — a failing one
    raises before ``save_state``, so the next tick retries instead of silently
    freezing a wall at a stale speed. The 1.1.1.1 ping runs after that
    all-or-nothing block on every tick (polled or not) and is strictly
    best-effort: it can never raise, block a poll, or advance
    ``last_poll_success`` — its only effects are the label's latency slot and
    ``last_latency_ms`` / ``pending_latency_ms`` in the state file.

    Rename-budget rule: latency redisplay flushes only on polling ticks;
    between polls it is held in ``pending_latency_ms`` so every category PATCH
    carries the scheduled timestamp update and latency never steals the
    2-per-10-min rename budget.
    """
    state = load_state()
    interval = config["poll_interval_seconds"]
    headers = discord_headers()

    try:
        last = float(state.get("last_poll_success") or 0)
    except (TypeError, ValueError):
        last = 0.0

    did_poll = False
    names: Dict[str, str] = {}
    if poll_due(state, interval, force=force, now_fn=now_fn):
        qbit_dl, _qbit_up, qbit_queue = qbit_speeds(http_fn)
        sab_dl, sab_queue = sab_speed(http_fn)
        slsk_dl, slsk_up, slsk_queue = slskd_speeds(http_fn)
        names = channel_names(
            qbit_dl, qbit_queue, sab_dl, sab_queue, slsk_dl, slsk_up, slsk_queue
        )
        for key, name in names.items():
            rename_channel(config["channel_ids"][key], name, headers, http_fn=http_fn)
        last = float(now_fn())
        did_poll = True

    # Best-effort latency: a raise here would be a helper bug (default_ping
    # never raises), but the guard keeps a bad ping_fn from failing the tick.
    try:
        sample = ping_fn()
        sample = float(sample) if sample is not None else None
    except Exception:
        sample = None

    try:
        previous = state.get("last_latency_ms")
        previous = float(previous) if previous is not None else None
    except (TypeError, ValueError):
        previous = None
    accepted = displayed_latency(previous, sample)

    if did_poll:
        # This tick renames the category anyway (new timestamp), so the
        # freshest accepted latency rides along in the same PATCH at zero
        # extra budget cost, and nothing is left held.
        latency_ms = accepted
        pending_latency_ms: Optional[float] = None
    else:
        # No budget to spend on a latency-only rename: keep the label at the
        # old value (byte-identical ⇒ rename_channel short-circuits as
        # "unchanged") and hold the accepted move until the next polling
        # tick. A held value stays held — it only ever flushes on a poll.
        latency_ms = previous
        pending_latency_ms = accepted if accepted != previous else None

    save_state(int(last), latency_ms, pending_latency_ms)
    label = category_name(last, interval, latency_ms, now_fn=now_fn)
    category_result = rename_channel(
        config["category_id"], label, headers, skip_on_429=True, http_fn=http_fn
    )

    return {
        "success": True,
        "did_poll": did_poll,
        "category": category_result,
        "latency_ms": latency_ms,
        "pending_latency_ms": pending_latency_ms,
        "names": names,
    }
