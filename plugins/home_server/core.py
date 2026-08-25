"""Core home_server logic — Discord Home Server provisioning and wiring.

A user sets a HOME SERVER once (``/sethomeserver``) and this module provisions
and keeps in sync the whole structure:

* ``Chat``            — text channels ``inbox``, ``outbox``, ``home``
* ``Honcho Memory``   — text channels ``explicit-facts``, ``deductions``,
  ``patterns``, ``contradictions``
* ``Quotas``          — voice channels ``Codex``, ``Kimi``, ``z.ai``,
  ``Cursor``, ``Grok``
* ``Speeds``          — voice channels ``qBittorrent``, ``SABnzbd``, ``slskd``

Creation alone is worthless, so reconcile also *wires* what it provisions:
``hermes_starts`` targets the shared inbox, each memory channel gets a Discord
webhook exported as a ``HONCHO_DISCORD_WEBHOOK_*`` secret, and the
``quota_channels`` / ``speed_channels`` config sections are pointed at the
created voice channels.

Reconcile is idempotent and conservative: channel IDs are always *discovered*
from the guild (matched on name + parent + type) or created, unknown or extra
channels are left alone, and nothing is ever deleted.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

HttpFn = Callable[[urllib.request.Request, float], Tuple[int, bytes]]
SleepFn = Callable[[float], None]
NowFn = Callable[[], float]

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_USER_AGENT = "DiscordBot (https://github.com/hermes-agent, 1.0)"

# Discord channel types (v10).
CHANNEL_TYPE_TEXT = 0
CHANNEL_TYPE_VOICE = 2
CHANNEL_TYPE_CATEGORY = 4

STATE_DIRNAME = "home_server"
STATE_FILENAME = "state.json"

# Reconcile is debounced to at most once per hour when driven from the
# gateway-connect / cron entry point (``sync_if_due``). An explicit
# ``/sethomeserver`` bypasses the debounce.
SYNC_DEBOUNCE_SECONDS = 3600

# Honour Discord's ``retry_after`` on 429, but never sleep unboundedly inside a
# slash command — cap a single backoff and give up after one retry.
MAX_RETRY_AFTER_SECONDS = 10.0

MODULE_KEYS = ("chat", "memory", "quotas", "speeds")

# Env var holding the bot token in HERMES_HOME/secrets/discord.env. Exposed as
# a constant so tests (and any future caller) reference the same key instead of
# re-typing the literal.
DISCORD_TOKEN_ENV_KEY = "DISCORD_BOT_TOKEN"

# Memory channel -> exported webhook env var. Keys mirror the conclusion levels
# of the Honcho discord_notifications patch (see its manifest.json), so the
# channel named "patterns" really is the one Honcho's *inductive* stream posts to.
MEMORY_WEBHOOK_ENV = {
    "explicit-facts": "HONCHO_DISCORD_WEBHOOK_EXPLICIT",
    "deductions": "HONCHO_DISCORD_WEBHOOK_DEDUCTIVE",
    "patterns": "HONCHO_DISCORD_WEBHOOK_INDUCTIVE",
    "contradictions": "HONCHO_DISCORD_WEBHOOK_CONTRADICTION",
}


class HomeServerError(Exception):
    """Raised instead of sys.exit, mirroring quota_channels."""


# ---------------------------------------------------------------------------
# Canonical template
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelSpec:
    """One channel of the canonical template.

    ``key`` is the wiring slug used by the consumers we hand the channel to
    (quota provider key, speeds downloader key, or memory env slug); it is only
    a lookup key, never a Discord ID.
    """

    name: str
    kind: int
    key: str = ""


@dataclass(frozen=True)
class ModuleSpec:
    key: str
    category: str
    channels: Tuple[ChannelSpec, ...]
    embed_title: str
    embed_description: str


TEMPLATE: Dict[str, ModuleSpec] = {
    "chat": ModuleSpec(
        key="chat",
        category="Chat",
        channels=(
            ChannelSpec("inbox", CHANNEL_TYPE_TEXT, "inbox"),
            ChannelSpec("outbox", CHANNEL_TYPE_TEXT, "outbox"),
            ChannelSpec("home", CHANNEL_TYPE_TEXT, "home"),
        ),
        embed_title="💬 Chat",
        embed_description=(
            "This inbox is where Hermes starts conversations when it has "
            "something to tell you. The outbox is for messages you hand off to "
            "Hermes, and home is the channel cron jobs and cross-platform "
            "messages are delivered to."
        ),
    ),
    "memory": ModuleSpec(
        key="memory",
        category="Honcho Memory",
        channels=(
            ChannelSpec("explicit-facts", CHANNEL_TYPE_TEXT, "explicit"),
            ChannelSpec("deductions", CHANNEL_TYPE_TEXT, "deductive"),
            ChannelSpec("patterns", CHANNEL_TYPE_TEXT, "inductive"),
            ChannelSpec("contradictions", CHANNEL_TYPE_TEXT, "contradiction"),
        ),
        embed_title="🧠 Honcho Memory",
        embed_description=(
            "Hermes learns as you talk, and this category shows that learning "
            "as it happens. explicit-facts are things you stated directly, "
            "deductions are inferences from them, patterns are recurring "
            "behaviour, and contradictions flag where something new disagreed "
            "with what was already believed."
        ),
    ),
    "quotas": ModuleSpec(
        key="quotas",
        category="Quotas",
        channels=(
            ChannelSpec("Codex", CHANNEL_TYPE_VOICE, "codex"),
            ChannelSpec("Kimi", CHANNEL_TYPE_VOICE, "kimi"),
            ChannelSpec("z.ai", CHANNEL_TYPE_VOICE, "zai"),
            ChannelSpec("Cursor", CHANNEL_TYPE_VOICE, "cursor"),
            ChannelSpec("Grok", CHANNEL_TYPE_VOICE, "grok"),
        ),
        embed_title="📊 Quotas",
        embed_description=(
            "Each voice channel is named after how much of that subscription's "
            "quota is left and when it resets, so you can see your remaining "
            "allowance at a glance. Hermes keeps the names up to date "
            "automatically."
        ),
    ),
    "speeds": ModuleSpec(
        key="speeds",
        category="Speeds",
        channels=(
            ChannelSpec("qBittorrent", CHANNEL_TYPE_VOICE, "qbittorrent"),
            ChannelSpec("SABnzbd", CHANNEL_TYPE_VOICE, "sabnzbd"),
            ChannelSpec("slskd", CHANNEL_TYPE_VOICE, "slskd"),
        ),
        embed_title="⚡ Speeds",
        embed_description=(
            "These voice channels are live download walls: each one is renamed "
            "with the downloader's current throughput and how much is queued. "
            "Nothing is ever posted here — the channel name is the display."
        ),
    ),
}


# ---------------------------------------------------------------------------
# Paths, secrets, state
# ---------------------------------------------------------------------------


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def state_path() -> Path:
    return _hermes_home() / STATE_DIRNAME / STATE_FILENAME


def _read_env_key(path: Path, key: str) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError as exc:
        raise HomeServerError(f"cannot read {path}: {exc}") from exc
    raise HomeServerError(f"{key} missing in {path}")


def discord_token() -> str:
    """Read the bot token from HERMES_HOME/secrets/discord.env.

    Same source as quota_channels. Never logged.
    """
    return _read_env_key(
        _hermes_home() / "secrets" / "discord.env", DISCORD_TOKEN_ENV_KEY
    )


def _env_path() -> Path:
    return _hermes_home() / ".env"


def load_state() -> Dict[str, Any]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: Mapping[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".home-server.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(state), handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        raise HomeServerError(f"cannot write {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _normalize_modules(raw: Any) -> Dict[str, bool]:
    if raw is None:
        return {key: True for key in MODULE_KEYS}
    if isinstance(raw, Mapping):
        modules = {key: True for key in MODULE_KEYS}
        for key in MODULE_KEYS:
            value = raw.get(key)
            if value is not None:
                modules[key] = bool(value)
        return modules
    raise HomeServerError("discord_home_server.modules must be a mapping")


def load_home_server_config(
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Read the ``discord_home_server`` section. Empty guild_id disables."""
    if config_path is None:
        from hermes_cli.config import load_config_readonly

        raw = load_config_readonly()
    else:
        import yaml

        try:
            raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        except OSError as exc:
            raise HomeServerError(f"cannot read {config_path}: {exc}") from exc
        except Exception as exc:
            raise HomeServerError(f"cannot parse {config_path}: {exc}") from exc

    section = raw.get("discord_home_server") if isinstance(raw, Mapping) else None
    section = section if isinstance(section, Mapping) else {}
    return {
        "guild_id": str(section.get("guild_id") or "").strip(),
        "modules": _normalize_modules(section.get("modules")),
    }


def is_configured(config_path: Optional[Path] = None) -> bool:
    try:
        return bool(load_home_server_config(config_path)["guild_id"])
    except HomeServerError:
        return False


# ---------------------------------------------------------------------------
# Discord REST
# ---------------------------------------------------------------------------


def default_http(
    req: urllib.request.Request, timeout: float = 25.0
) -> Tuple[int, bytes]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:
        raise HomeServerError(f"network error: {type(exc).__name__}: {exc}") from exc


class DiscordClient:
    """Thin Discord REST wrapper with 429/retry_after handling.

    The token is held but never included in any exception message or log line.
    """

    def __init__(
        self,
        token: str,
        *,
        http_fn: HttpFn = default_http,
        sleep_fn: SleepFn = time.sleep,
    ) -> None:
        self._token = token
        self._http_fn = http_fn
        self._sleep_fn = sleep_fn

    def _headers(self, *, json_body: bool = True) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bot {self._token}",
            "User-Agent": DISCORD_USER_AGENT,
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _raw(self, method: str, path: str, body: Any = None) -> Tuple[int, str]:
        data = None
        headers = self._headers(json_body=body is not None or method in ("POST", "PATCH"))
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{DISCORD_API_BASE}{path}", data=data, headers=headers, method=method
        )
        status, payload = self._http_fn(req, 25.0)
        if isinstance(payload, bytes):
            return status, payload.decode(errors="replace")
        return status, str(payload)

    def _retry_after(self, text: str) -> float:
        try:
            value = float(json.loads(text).get("retry_after") or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            return 0.0
        return max(0.0, min(value, MAX_RETRY_AFTER_SECONDS))

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        """One request, retrying exactly once on 429 after ``retry_after``."""
        status, text = self._raw(method, path, body)
        if status == 429:
            self._sleep_fn(self._retry_after(text))
            status, text = self._raw(method, path, body)
        if status == 403:
            raise HomeServerError(
                f"discord returned 403 for {method} {path} — the bot is missing "
                "the Manage Channels / Manage Webhooks permissions in that guild"
            )
        if status == 401:
            # Deliberately does not echo the body: it can contain token hints.
            raise HomeServerError(
                "discord rejected the bot token (401) — check "
                "DISCORD_BOT_TOKEN in HERMES_HOME/secrets/discord.env"
            )
        if status not in (200, 201, 204):
            raise HomeServerError(
                f"discord {method} {path} returned {status}: {text[:200]}"
            )
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise HomeServerError(f"discord {method} {path}: invalid JSON") from exc

    # -- guild / channel primitives -----------------------------------------

    def list_guild_channels(self, guild_id: str) -> List[Dict[str, Any]]:
        payload = self._request("GET", f"/guilds/{guild_id}/channels")
        if not isinstance(payload, list):
            raise HomeServerError("discord guild channels response was not a list")
        return payload

    def create_channel(
        self, guild_id: str, *, name: str, kind: int, parent_id: Optional[str] = None
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"name": name, "type": kind}
        if parent_id:
            body["parent_id"] = parent_id
        payload = self._request("POST", f"/guilds/{guild_id}/channels", body)
        if not isinstance(payload, dict) or not payload.get("id"):
            raise HomeServerError(f"discord channel create for {name!r} returned no id")
        return payload

    def create_webhook(self, channel_id: str, *, name: str) -> Dict[str, Any]:
        payload = self._request(
            "POST", f"/channels/{channel_id}/webhooks", {"name": name}
        )
        if not isinstance(payload, dict) or not payload.get("url"):
            raise HomeServerError(f"discord webhook create for {name!r} returned no url")
        return payload

    def post_embed(self, channel_id: str, embed: Mapping[str, Any]) -> str:
        payload = self._request(
            "POST", f"/channels/{channel_id}/messages", {"embeds": [dict(embed)]}
        )
        return str(payload.get("id") or "")

    def channel_name(self, channel_id: str) -> str:
        payload = self._request("GET", f"/channels/{channel_id}")
        return str(payload.get("name") or "")

    def move_channel(self, channel_id: str, *, parent_id: Optional[str]) -> str:
        """Re-parent a channel under a category (adoption)."""
        status, text = self._raw(
            "PATCH", f"/channels/{channel_id}", {"parent_id": parent_id}
        )
        if status != 200:
            raise HomeServerError(
                f"discord move returned {status}: {text[:200]}"
            )
        return "moved"

    def rename_channel(self, channel_id: str, name: str, *, skip_on_429: bool = False) -> str:
        """Rename only when the name actually changes (2 renames / 10 min / channel).

        ``skip_on_429`` mirrors quota_channels: the Speeds/Quotas category label
        is touched every tick, so a 429 there is expected and must not raise.
        """
        if self.channel_name(channel_id) == name:
            return "unchanged"
        status, text = self._raw("PATCH", f"/channels/{channel_id}", {"name": name})
        if status == 429 and skip_on_429:
            return "skipped"
        if status != 200:
            raise HomeServerError(
                f"discord rename returned {status}: {text[:200]}"
            )
        return "renamed"


def discord_client(
    *,
    http_fn: HttpFn = default_http,
    sleep_fn: SleepFn = time.sleep,
) -> DiscordClient:
    return DiscordClient(discord_token(), http_fn=http_fn, sleep_fn=sleep_fn)


# ---------------------------------------------------------------------------
# .env merge (no-clobber)
# ---------------------------------------------------------------------------


def _existing_env_values(path: Path, keys: Sequence[str]) -> Dict[str, str]:
    """Return the currently-set non-empty values for ``keys`` (never logged)."""
    found: Dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return found
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key in keys:
            value = value.strip().strip('"').strip("'")
            if value:
                found[key] = value
    return found


def merge_env_secrets(values: Mapping[str, str]) -> Dict[str, str]:
    """Write ``values`` into HERMES_HOME/.env only where the key is missing/empty.

    Never overwrites a non-empty value and never returns the secret payload —
    the return value maps key -> "set" | "kept" so callers can report progress
    without echoing webhook URLs.
    """
    keys = list(values)
    path = _env_path()
    existing = _existing_env_values(path, keys)
    outcome: Dict[str, str] = {}
    to_write: Dict[str, str] = {}
    for key, value in values.items():
        if existing.get(key):
            outcome[key] = "kept"
        else:
            outcome[key] = "set"
            to_write[key] = value

    if not to_write:
        return outcome

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []

    written = set()
    rebuilt: List[str] = []
    for line in lines:
        stripped = line.strip()
        matched = None
        if stripped and not stripped.startswith("#") and "=" in stripped:
            candidate = stripped.partition("=")[0].strip()
            if candidate in to_write:
                matched = candidate
        if matched is not None:
            if matched in written:
                continue  # collapse duplicate keys
            rebuilt.append(f"{matched}={to_write[matched]}")
            written.add(matched)
        else:
            rebuilt.append(line)

    for key, value in to_write.items():
        if key not in written:
            rebuilt.append(f"{key}={value}")

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(rebuilt) + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        raise HomeServerError(f"cannot write {path}: {exc}") from exc
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return outcome


# ---------------------------------------------------------------------------
# config.yaml merge (atomic, preserves unrelated keys)
# ---------------------------------------------------------------------------


def _merge_config_section(section: str, updates: Mapping[str, Any]) -> bool:
    """Deep-merge ``updates`` under ``config.yaml:`` -> ``section`` and save.

    Loads the whole document first so unrelated sections survive, and writes
    through ``save_config`` (atomic ``os.replace`` underneath). Returns True
    when something changed.
    """
    from hermes_cli.config import load_config, save_config

    config = load_config()
    target = config.get(section)
    if not isinstance(target, dict):
        target = {}
        config[section] = target

    changed = False
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            for sub_key, sub_value in value.items():
                if target[key].get(sub_key) != sub_value:
                    target[key][sub_key] = sub_value
                    changed = True
        elif target.get(key) != value:
            target[key] = value
            changed = True

    if changed:
        save_config(config)
    return changed


# ---------------------------------------------------------------------------
# Home channel link
# ---------------------------------------------------------------------------


def existing_discord_home_channel() -> Optional[Dict[str, Any]]:
    """The raw ``platforms.discord.home_channel`` mapping, or None.

    Read from the same document ``persist_home_channel`` writes to, so the
    no-clobber rule and the write agree on one source of truth.
    """
    from hermes_cli.config import load_config_readonly

    raw = load_config_readonly()
    platforms = raw.get("platforms") if isinstance(raw, Mapping) else None
    discord_cfg = platforms.get("discord") if isinstance(platforms, Mapping) else None
    if not isinstance(discord_cfg, Mapping):
        return None
    home = discord_cfg.get("home_channel")
    if isinstance(home, Mapping) and str(home.get("chat_id") or "").strip():
        return dict(home)
    return None


def link_home_channel(guild_id: str, channel_id: str) -> str:
    """Point ``platforms.discord.home_channel`` at the Chat/home channel.

    No-clobber: an existing Discord home channel is never silently replaced.
    Returns "set" or "kept".
    """
    if existing_discord_home_channel() is not None:
        return "kept"

    from gateway.config import HomeChannel, persist_home_channel
    from gateway.platforms.base import Platform

    persist_home_channel(
        HomeChannel(
            platform=Platform.DISCORD,
            chat_id=str(channel_id),
            name="home",
        ),
        enabled_if_new=False,
    )
    return "set"


# ---------------------------------------------------------------------------
# Wiring hooks
# ---------------------------------------------------------------------------


def wire_hermes_starts(state: Mapping[str, Any]) -> str:
    """Point ``start_conversations`` at the shared inbox.

    The adoption logic lives in hermes_starts (it owns that state file); this
    hook just reports the outcome so /sethomeserver can say what was wired.
    """
    from plugins.hermes_starts import adopt_home_server_inbox

    return adopt_home_server_inbox()


def wire_memory_webhooks(
    client: DiscordClient, state: Mapping[str, Any]
) -> Dict[str, str]:
    """Create one webhook per memory channel and export it as a secret.

    Idempotent in the strictest sense: a webhook is only minted for levels
    whose env var is currently missing/empty, so a second reconcile creates
    nothing at all. A level the user already pointed somewhere else is never
    clobbered.
    """
    memory = (state.get("channels") or {}).get("memory") or {}
    guild_id = str(state.get("guild_id") or "")
    if not memory or not guild_id:
        return {}

    env_keys = list(MEMORY_WEBHOOK_ENV.values())
    already_set = _existing_env_values(_env_path(), env_keys)
    missing = [key for key in env_keys if key not in already_set]
    if not missing:
        return {key: "kept" for key in env_keys}

    wanted: Dict[str, str] = {}
    for channel_name, env_key in MEMORY_WEBHOOK_ENV.items():
        if env_key not in missing:
            continue
        channel_id = str(memory.get(channel_name) or "")
        if not channel_id:
            continue
        webhook = client.create_webhook(channel_id, name="Honcho Memory")
        wanted[env_key] = str(webhook["url"])

    return merge_env_secrets(wanted)


def wire_quota_channels(state: Mapping[str, Any]) -> bool:
    quotas = (state.get("channels") or {}).get("quotas") or {}
    guild_id = str(state.get("guild_id") or "")
    category_id = str((state.get("categories") or {}).get("quotas") or "")
    if not quotas or not guild_id or not category_id:
        return False

    channel_ids = {
        spec.key: str(quotas.get(spec.name) or "")
        for spec in TEMPLATE["quotas"].channels
        if spec.key
    }
    updates: Dict[str, Any] = {
        "guild_id": guild_id,
        "category_id": category_id,
        "channel_ids": {k: v for k, v in channel_ids.items() if v},
    }
    return _merge_config_section("quota_channels", updates)


def wire_speed_channels(state: Mapping[str, Any]) -> bool:
    speeds = (state.get("channels") or {}).get("speeds") or {}
    guild_id = str(state.get("guild_id") or "")
    category_id = str((state.get("categories") or {}).get("speeds") or "")
    if not speeds or not guild_id or not category_id:
        return False

    channel_ids = {
        spec.key: str(speeds.get(spec.name) or "")
        for spec in TEMPLATE["speeds"].channels
        if spec.key
    }
    updates: Dict[str, Any] = {
        "guild_id": guild_id,
        "category_id": category_id,
        "channel_ids": {k: v for k, v in channel_ids.items() if v},
    }
    return _merge_config_section("speed_channels", updates)


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------


def _find_channel(
    channels: List[Dict[str, Any]], *, name: str, kind: int, parent_id: Optional[str]
) -> Optional[str]:
    """Discover an existing channel by name + type + parent.

    Extra channels the user made themselves are simply never matched, and
    nothing here ever deletes one.
    """
    for channel in channels:
        if str(channel.get("name") or "") != name:
            continue
        if int(channel.get("type") or 0) != kind:
            continue
        if parent_id is None:
            return str(channel["id"])
        if str(channel.get("parent_id") or "") == parent_id:
            return str(channel["id"])
    return None


def _normalize_name(name: Any) -> str:
    """Lowercase and strip separators for tolerant matching.

    ``Quotas • 25/8 3:30pm`` matches the "Quotas" category; ``inbox`` matches
    ``#inbox``. Dynamic suffixes the pollers add (quota clocks, throughput)
    are ignored for matching purposes.
    """
    return str(name or "").strip().lstrip("#").strip().lower()


def _prefix_match_channel(
    channels: List[Dict[str, Any]],
    *,
    name: str,
    kind: int,
    exclude_ids: set[str],
) -> Optional[str]:
    """Find a channel whose normalized name STARTS WITH the template name.

    Used when no exact match exists. Only claims channels of the right type
    that are not already claimed by another template entry. The dynamic-label
    categories ("Quotas • ...") match this way.
    """
    want = _normalize_name(name)
    for channel in channels:
        if int(channel.get("type") or 0) != kind:
            continue
        cid = str(channel.get("id"))
        if cid in exclude_ids:
            continue
        got = _normalize_name(channel.get("name"))
        if got == want or got.startswith(want + " ") or got.startswith(want + ":"):
            return cid
    return None


def _reconcile_module(
    client: DiscordClient,
    guild_id: str,
    spec: ModuleSpec,
    channels: List[Dict[str, Any]],
    report: Dict[str, Any],
) -> Dict[str, Any]:
    """Ensure one category and its channels exist. Mutates ``channels`` so
    later modules see what earlier ones created.

    Matching is tolerant of a live server: exact normalized names first, then
    prefix matches against dynamically-labelled channels. Channels that exist
    elsewhere in the guild under the right name/type are ADOPTED (moved under
    the module's category) instead of duplicated. Nothing is ever renamed or
    deleted.
    """
    category_id = _find_channel(
        channels, name=spec.category, kind=CHANNEL_TYPE_CATEGORY, parent_id=None
    )
    adopted_category = False
    if category_id is None:
        # Tolerant pass: an existing category whose name starts with the
        # template name ("Quotas • 25/8 ..." vs "Quotas") is the same home.
        candidate = _prefix_match_channel(
            channels,
            name=spec.category,
            kind=CHANNEL_TYPE_CATEGORY,
            exclude_ids=set(),
        )
        if candidate is not None:
            category_id = candidate
            prior_categories = (load_state().get("categories") or {})
            if str(prior_categories.get(spec.key) or "") != str(candidate):
                report["adopted"].append(f"category:{spec.category}")
            adopted_category = True
    if category_id is None:
        created = client.create_channel(
            guild_id, name=spec.category, kind=CHANNEL_TYPE_CATEGORY
        )
        category_id = str(created["id"])
        channels.append(created)
        report["created"].append(f"category:{spec.category}")

    # Never prefix-claim a category: those belong to other modules or the user.
    claimed = {
        str(c.get("id"))
        for c in channels
        if int(c.get("type") or 0) == CHANNEL_TYPE_CATEGORY
    }

    resolved: Dict[str, str] = {}
    for channel_spec in spec.channels:
        channel_id = _find_channel(
            channels,
            name=channel_spec.name,
            kind=channel_spec.kind,
            parent_id=category_id,
        )
        adopted_now = False
        if channel_id is None and channel_spec.kind != CHANNEL_TYPE_CATEGORY:
            # Exact name elsewhere in the guild → adopt it under our category.
            channel_id = _find_channel(
                channels,
                name=channel_spec.name,
                kind=channel_spec.kind,
                parent_id=None,
            )
            if channel_id is not None:
                adopted_now = True
        if channel_id is None and channel_spec.kind != CHANNEL_TYPE_CATEGORY:
            # Loose/dynamic label ("Codex: 98% ...") → match by prefix. A hit
            # under our own category is plain discovery; one elsewhere in the
            # guild is an adoption (move).
            channel_id = _prefix_match_channel(
                channels,
                name=channel_spec.name,
                kind=channel_spec.kind,
                exclude_ids=set(resolved.values()) | claimed,
            )
            if channel_id is not None:
                parent = next(
                    (
                        str(c.get("parent_id"))
                        for c in channels
                        if str(c.get("id")) == channel_id
                    ),
                    None,
                )
                if parent == str(category_id):
                    adopted_now = False
                else:
                    adopted_now = True
        if channel_id is not None and adopted_now:
            client.move_channel(channel_id, parent_id=category_id)
            for channel in channels:
                if str(channel.get("id")) == channel_id:
                    channel["parent_id"] = category_id
                    break
            report["adopted"].append(f"channel:{channel_spec.name}")
        if channel_id is None:
            created = client.create_channel(
                guild_id,
                name=channel_spec.name,
                kind=channel_spec.kind,
                parent_id=category_id,
            )
            channel_id = str(created["id"])
            channels.append(created)
            report["created"].append(f"channel:{channel_spec.name}")
        else:
            claimed.add(channel_id)
        resolved[channel_spec.name] = channel_id

    return {
        "category_id": category_id,
        "channels": resolved,
    }


def reconcile(
    *,
    config_path: Optional[Path] = None,
    http_fn: HttpFn = default_http,
    sleep_fn: SleepFn = time.sleep,
    now_fn: NowFn = time.time,
) -> Dict[str, Any]:
    """Bring the guild in line with the canonical template. Idempotent.

    Second run against an already-provisioned guild creates nothing and posts
    no embeds.
    """
    config = load_home_server_config(config_path)
    guild_id = config["guild_id"]
    modules = config["modules"]

    report: Dict[str, Any] = {
        "success": True,
        "enabled": bool(guild_id),
        "guild_id": guild_id,
        "created": [],
        "adopted": [],
        "embeds_posted": [],
        "wired": {},
        "home_channel": "skipped",
        "modules": {key: modules[key] for key in MODULE_KEYS},
    }
    if not guild_id:
        return report

    prior = load_state()
    client = discord_client(http_fn=http_fn, sleep_fn=sleep_fn)
    channels = client.list_guild_channels(guild_id)

    state: Dict[str, Any] = {
        "guild_id": guild_id,
        "last_sync": int(now_fn()),
        "categories": dict(prior.get("categories") or {}),
        "channels": dict(prior.get("channels") or {}),
        "welcome_embeds": dict(prior.get("welcome_embeds") or {}),
    }

    for key in MODULE_KEYS:
        if not modules[key]:
            continue
        spec = TEMPLATE[key]
        outcome = _reconcile_module(client, guild_id, spec, channels, report)
        state["categories"][key] = outcome["category_id"]
        state["channels"][key] = outcome["channels"]

        if key not in state["welcome_embeds"]:
            # Voice channels are display-only (the name IS the UI). Posting
            # a welcome embed there contradicts the Speeds/Quotas copy.
            text_spec = next(
                (c for c in spec.channels if c.kind == CHANNEL_TYPE_TEXT),
                None,
            )
            if text_spec is None:
                state["welcome_embeds"][key] = "skipped-no-text-channel"
            else:
                first_id = outcome["channels"][text_spec.name]
                message_id = client.post_embed(
                    first_id,
                    {
                        "title": spec.embed_title,
                        "description": spec.embed_description,
                    },
                )
                state["welcome_embeds"][key] = message_id
                report["embeds_posted"].append(f"{spec.category}/{text_spec.name}")

    # Persist before wiring: the hermes_starts hook discovers the shared inbox
    # by reading this state file, so it must exist on the very first run too.
    save_state(state)

    if modules["chat"] and state["channels"].get("chat", {}).get("home"):
        report["home_channel"] = link_home_channel(
            guild_id, state["channels"]["chat"]["home"]
        )
        report["wired"]["hermes_starts"] = wire_hermes_starts(state)

    if modules["memory"] and state["channels"].get("memory"):
        report["wired"]["memory_webhooks"] = wire_memory_webhooks(client, state)

    if modules["quotas"] and state["channels"].get("quotas"):
        report["wired"]["quota_channels"] = wire_quota_channels(state)

    if modules["speeds"] and state["channels"].get("speeds"):
        report["wired"]["speed_channels"] = wire_speed_channels(state)

    return report


# ---------------------------------------------------------------------------
# Debounced sync entry point
# ---------------------------------------------------------------------------


def should_sync(state: Mapping[str, Any], *, now_fn: NowFn = time.time) -> bool:
    """True when the last reconcile is older than the hourly debounce."""
    if not state:
        return True
    if not str(state.get("guild_id") or ""):
        return True
    try:
        last = float(state.get("last_sync") or 0)
    except (TypeError, ValueError):
        return True
    return (now_fn() - last) >= SYNC_DEBOUNCE_SECONDS


def sync_if_due(
    *,
    config_path: Optional[Path] = None,
    http_fn: HttpFn = default_http,
    sleep_fn: SleepFn = time.sleep,
    now_fn: NowFn = time.time,
) -> Dict[str, Any]:
    """Gateway-connect / cron entry point: reconcile at most once per hour.

    Only runs when the feature is configured (guild_id set) and a state file
    already exists — the very first provision must be an explicit
    ``/sethomeserver``, never a side effect of connecting.
    """
    if not is_configured(config_path):
        return {"success": True, "enabled": False, "synced": False}

    if not state_path().exists():
        return {"success": True, "enabled": True, "synced": False, "reason": "never provisioned"}

    if not should_sync(load_state(), now_fn=now_fn):
        return {"success": True, "enabled": True, "synced": False, "reason": "debounced"}

    report = reconcile(
        config_path=config_path, http_fn=http_fn, sleep_fn=sleep_fn, now_fn=now_fn
    )
    report["synced"] = True
    return report


