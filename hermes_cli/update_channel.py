"""Per-install update-channel records.

Channel storage — per install, never home-global::

    update:
      installs:
        a4f3b2c1d0e9f8a7:                      # install id (sha16 of the
          path: /home/u/.hermes/hermes-agent   #   canonical install root)
          channel: canary

One config.yaml serves many installs (host + docker gateway + desktop all
bind-mount one ``~/.hermes``), so a home-global ``update.channel`` key is
UNSAFE and does not exist: setting canary for a dev checkout must not
flip the desktop app's feed. The id is sha16 of the canonical
install-root PATH — the same key that names the ``installs/<sha16>/``
state folder (``boot_bootstrap._install_key``; a byte-identical helper is
inlined below until that module lands). Path-derived on purpose: an
electron-updater update replaces the artifact (new stamp bytes) at the
same path, and the channel opt-in must survive that.

* Written by ``hermes update --set-channel <x>`` from inside an install
  (it knows its own id — the user never types a sha).
* Shown by ``hermes update --install-id`` and the desktop About page.
* Channels are meaningful ONLY where the mechanism is ``self`` (which git
  ref: main / stable / canary→main) or ``electron-updater`` (which feed:
  latest.yml / canary.yml). ``external`` installs have no channel; the
  steward owns updates.

Pure-stdlib leaf module (plus hermes-internal imports done lazily): the
installers and boot paths read it before the full config machinery loads.
"""

from __future__ import annotations

from hermes_cli.runtime_paths import install_key, installs_root
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

CHANNEL_MAIN = "main"
CHANNEL_STABLE = "stable"
CHANNEL_CANARY = "canary"
VALID_CHANNELS = (CHANNEL_MAIN, CHANNEL_STABLE, CHANNEL_CANARY)

# A canary release tag: v<major>.<minor>.<patch>-canary.<YYYYMMDDHHMMSS>,
# or the legacy date-only shape. THIS is the single authority for the
# canary tag shape — scripts/release.py (produces them) and
# scripts/write_install_stamp.py (validates the feed key) import it rather
# than re-typing the rule. Canaries are current-stable patch+1, so any
# patch is accepted here.
_CANARY_TAG_RE = re.compile(r"^v(?:0|[1-9]\d{0,2})\.\d+\.\d+-canary\.20\d{6}(?:\d{6})?$")


def is_canary_tag(tag: Any) -> bool:
    """True when ``tag`` is a canary release tag."""
    return isinstance(tag, str) and bool(_CANARY_TAG_RE.match(tag.strip()))


def canary_tag_for_date(version: str, date_utc: str) -> str:
    """The canary tag name for a UTC timestamp: next PATCH over ``version``
    (the newest stable's patch + 1), second-precision UTC suffix —
    v0.27.5-canary.20260818103000 when stable is v0.27.4. A canary
    outversions every stable at or below its patch and loses to the next
    stable patch, which is exactly the channel-switch upgrade path
    (canary→stable = wait for that patch bump to ship as stable).
    """
    parts = version.lstrip("v").split(".")
    major, minor = int(parts[0]), int(parts[1])
    patch = int(parts[2]) if len(parts) >= 3 else 0
    return f"v{major}.{minor}.{patch + 1}-canary.{date_utc}"


def _default_root() -> Path:
    """This process's install root.

    Mirrors version_info's stamp resolution: HERMES_INSTALL_ROOT when the
    steward wrapper sets it (Nix points it at the sealed tree), else the
    code root of the executing checkout.
    """
    root = os.environ.get("HERMES_INSTALL_ROOT")
    return Path(root) if root else Path(__file__).parent.parent.resolve()


def install_id(project_root: Optional[Path] = None) -> str:
    """The sha16 id of the install at ``project_root`` (default: this one).

    Same identity as the ``installs/<sha16>/`` state folder key.
    """
    if project_root is None:
        project_root = _default_root()
    return install_key(Path(project_root))


def _read_stamp(root: Path) -> dict:
    """The install stamp of ``root``, or ``{}`` (tolerant, like steward.py)."""
    from hermes_cli.steward import read_install_stamp

    return read_install_stamp(root)


def _install_records(config: Optional[dict]) -> dict:
    if not isinstance(config, dict):
        return {}
    update_cfg = config.get("update")
    if not isinstance(update_cfg, dict):
        return {}
    installs = update_cfg.get("installs")
    return installs if isinstance(installs, dict) else {}


def channel_record(config: Optional[dict], project_root: Optional[Path] = None) -> dict:
    """This install's ``{path, channel}`` record from config, or ``{}``."""
    record = _install_records(config).get(install_id(project_root))
    return record if isinstance(record, dict) else {}


def default_channel(project_root: Optional[Path] = None) -> str:
    """The channel an unconfigured install tracks.

    ``self`` source installs follow main (historical behavior).
    ``electron-updater`` bundles follow the channel their own artifact was
    published to: a canary artifact tracks canary, every other bundle
    tracks stable. The stamp's ``tag`` is the authority, the same fact
    apps/desktop/product-identity.cjs keys the published feed name on — so
    the feed a canary artifact asks for and the feed it was published to
    can never disagree. Deriving stable here instead would send a fresh
    canary install to look for its ``canary.yml`` feed file under the
    newest STABLE release, where that file does not exist (404), leaving
    the install unable to update at all.
    """
    root = Path(project_root) if project_root is not None else _default_root()
    stamp = _read_stamp(root)
    if stamp.get("updateMechanism") != "electron-updater":
        return CHANNEL_MAIN
    return CHANNEL_CANARY if is_canary_tag(stamp.get("tag")) else CHANNEL_STABLE


def resolve_update_channel(
    config: Optional[dict] = None,
    project_root: Optional[Path] = None,
) -> str:
    """The effective update channel for this install.

    Resolution: the per-install record (``update.installs.<sha16>.channel``)
    when valid; otherwise the mechanism default (main for self-source,
    stable/canary for electron-updater bundles by artifact tag). Source
    installs asking for canary normalize to main — canary builds are
    release artifacts, and a git checkout tracks branches; callers print
    the note.
    """
    configured: Any = channel_record(config, project_root).get("channel")
    if isinstance(configured, str) and configured.strip().lower() in VALID_CHANNELS:
        channel = configured.strip().lower()
    else:
        channel = default_channel(project_root)

    if channel == CHANNEL_CANARY:
        root = Path(project_root) if project_root is not None else _default_root()
        if _read_stamp(root).get("updateMechanism") != "electron-updater":
            # canary→main normalization for source installs.
            return CHANNEL_MAIN
    return channel


def canary_normalized_note() -> str:
    """The one-line note callers print when canary normalizes to main."""
    return (
        "→ Channel 'canary' on a source install tracks main "
        "(canary builds are desktop release artifacts)."
    )


def set_install_channel(
    channel: str,
    project_root: Optional[Path] = None,
) -> str:
    """Persist ``channel`` for THIS install in config.yaml. Returns the id.

    Refuses on ``external`` mechanism — those installs have no channel;
    the steward owns updates. Raises ``ValueError`` for both bad channel
    values and external installs; the CLI surfaces the message.
    """
    channel = (channel or "").strip().lower()
    if channel not in VALID_CHANNELS:
        raise ValueError(
            f"unknown channel {channel!r} (one of {', '.join(VALID_CHANNELS)})"
        )

    root = Path(project_root) if project_root is not None else _default_root()
    stamp = _read_stamp(root)
    if stamp.get("updateMechanism") == "external":
        distribution = stamp.get("distribution") or "an external steward"
        raise ValueError(
            f"channels don't apply here; updates are owned by {distribution}"
        )

    sha16 = install_id(root)
    _write_channel_record(sha16, str(root), channel)
    return sha16


def _write_channel_record(sha16: str, path: str, channel: str) -> None:
    """Write ``update.installs.<sha16>`` into config.yaml, preserving the rest.

    Persists through the shared comment-preserving atomic writer
    (:func:`utils.atomic_roundtrip_yaml_update` — the same ruamel round-trip
    path ``hermes config set`` uses), fail-closed via
    :func:`hermes_cli.config.require_readable_config_before_write`. Malformed
    ``update`` / ``update.installs`` values are refused, never replaced —
    the dotted writer would otherwise turn a scalar into a mapping and
    destroy whatever the user had there.
    """
    from utils import atomic_roundtrip_yaml_update

    from hermes_cli.config import (
        get_config_path,
        require_readable_config_before_write,
    )

    config_path = get_config_path()
    existing = require_readable_config_before_write(config_path)
    update_cfg = existing.get("update")
    if update_cfg is not None and not isinstance(update_cfg, dict):
        raise ValueError("config key 'update' is not a mapping")
    installs = update_cfg.get("installs") if isinstance(update_cfg, dict) else None
    if installs is not None and not isinstance(installs, dict):
        raise ValueError("config key 'update.installs' is not a mapping")
    record = installs.get(sha16) if isinstance(installs, dict) else None
    new_record = dict(record) if isinstance(record, dict) else {}
    new_record["path"] = path  # DATA, for humans + doctor GC
    new_record["channel"] = channel
    atomic_roundtrip_yaml_update(config_path, f"update.installs.{sha16}", new_record)


def _stable_wait_target(canary_version: str) -> str:
    """The stable release a canary install waits for: the canary's own base
    version (canary is current-stable patch+1; ``v0.28.0`` from
    ``v0.28.0-canary.20260818171926``)."""
    base = re.sub(r"-canary\.\d+$", "", canary_version.strip())
    return base if base.startswith("v") else f"v{base}"


def _stamp_channel_hint(stamp: dict) -> Optional[str]:
    """The channel the RUNNING artifact implies, for stamps without a clean
    ``tag`` (desktop About-page stamps carry ``displayVersion`` like
    ``0.28.0-canary.20260818`` — the ``v`` prefix ``is_canary_tag`` requires
    is restored before the shape check, so validation stays with the single
    canary authority). Switch text only; resolution stays with
    :func:`resolve_update_channel`."""
    if stamp.get("updateMechanism") != "electron-updater":
        return None
    version = str(stamp.get("tag") or stamp.get("displayVersion") or "")
    if not version:
        return None
    candidate = version if version.startswith("v") else f"v{version}"
    return CHANNEL_CANARY if is_canary_tag(candidate) else None


def _set_channel_from_cli(channel: str) -> None:
    """Persist ``--set-channel`` and print the switch text. Never updates.

    Runs before the update lock, git, network, backups, or process pause:
    this is a configuration action, not an update. The reported previous
    channel prefers the stored per-install record over what the running
    artifact implies.
    """
    from hermes_cli.config import read_raw_config

    root = _default_root()
    stamp = _read_stamp(root)
    stored = channel_record(read_raw_config(), root).get("channel")
    if isinstance(stored, str) and stored.strip().lower() in VALID_CHANNELS:
        previous = stored.strip().lower()
    else:
        previous = _stamp_channel_hint(stamp) or resolve_update_channel(None, root)
    try:
        sha16 = set_install_channel(channel, root)
    except ValueError as exc:
        print(f"error: {exc}")
        sys.exit(1)

    print(f"Channel set to '{channel}' (was '{previous}') for install {sha16}.")
    version = str(stamp.get("tag") or stamp.get("displayVersion") or "")
    if previous == CHANNEL_CANARY and channel == CHANNEL_STABLE:
        print(f"  You are on canary build {version}.")
        print(
            f"  Stable updates begin at { _stable_wait_target(version) } —"
            " canary outversions it until that release ships."
        )
        print("  Not patient? Switch back: hermes update --set-channel canary")
        print("  Docs: https://hermes-agent.nousresearch.com")
    elif channel == CHANNEL_CANARY:
        print(
            "  Canary builds are forward-incompatible: a canary install"
            " only updates to artifacts that ship after it. Downgrading"
            " means reinstalling."
        )
    sys.exit(0)


def handle_channel_flags(args) -> None:
    """Preflight for the informational update flags.

    ``--install-id`` prints this install's id and path; ``--set-channel``
    atomically persists a valid channel record. Both terminate the command
    here — the caller never reaches the update lock, git, network, backup,
    or process-pause paths.
    """
    if getattr(args, "install_id", False):
        root = _default_root()
        print(f"{install_id(root)}  {root}")
        sys.exit(0)
    channel = getattr(args, "set_channel", None)
    if channel:
        _set_channel_from_cli(channel)


def stale_channel_records(config: Optional[dict]) -> list[tuple[str, dict, str]]:
    """Doctor's staleness triad over ``update.installs``.

    Returns ``(sha16, record, reason)`` where reason is one of:

    * ``"replaced"`` — the recorded path exists but the install there keys
      to a DIFFERENT sha16 (the tree moved / was recreated elsewhere and a
      new record claimed it; this one is a leftover).
    * ``"missing"``  — nothing at the recorded path: offer GC (keep-on-doubt).
    * ``"unclaimed"`` — the sha16 matches no live install record
      (``installs/<sha16>/install.json``): offer GC.
    """
    stale: list[tuple[str, dict, str]] = []
    for sha16, record in _install_records(config).items():
        if not isinstance(record, dict):
            continue
        recorded_path = record.get("path")
        if not isinstance(recorded_path, str) or not recorded_path:
            # No path fact — fall through to the live-record check only.
            recorded_path = None

        if recorded_path is not None:
            path = Path(recorded_path)
            if not path.exists():
                stale.append((sha16, record, "missing"))
                continue
            if install_key(path) != sha16:
                stale.append((sha16, record, "replaced"))
                continue

        # Cross-check against the live install-state records: a channel
        # record whose sha16 has no installs/<sha16>/install.json was
        # either hand-written or its install never booted post-record.
        try:
            if not (installs_root() / sha16 / "install.json").is_file():
                stale.append((sha16, record, "unclaimed"))
        except Exception as exc:  # noqa: BLE001 — doctor sweep must not raise
            logger.debug("installs root unavailable: %s", exc)
    return stale
