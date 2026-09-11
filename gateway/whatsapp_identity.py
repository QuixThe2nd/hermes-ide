"""Shared helpers for canonicalising WhatsApp sender identity.

The bridge can surface one human as a LID (``999...@lid``) or a phone JID
(``1555...@s.whatsapp.net``) within one conversation. Authorisation (:mod:`gateway.run`) and
session keys (:mod:`gateway.session`) both resolve aliases here so they never drift apart;
plugins should use :func:`canonical_whatsapp_identifier` to line up with Hermes' session keys.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Set

from hermes_constants import get_hermes_dir, get_process_hermes_home

logger = logging.getLogger(__name__)

# WhatsApp JIDs are numeric (or plus-prefixed) with ``@``/``.``/``:`` separators.
# Explicit ASCII class so full-width digits / Unicode word chars can't sneak through.
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9@.+\-]+$")

# "Just a phone number": optional ``+`` then digits and human separators.
# Anything carrying ``@`` is already a JID (``@g.us``, ``@lid``, ``status@broadcast``).
_BARE_PHONE_RE = re.compile(r"^\+?[\d\s().\-]+$")


def normalize_whatsapp_identifier(value: str) -> str:
    """Strip JID/LID/device/plus syntax down to the bare numeric identifier:
    ``"6012:47@s.whatsapp.net"``, ``"6012@lid"`` and ``"+6012"`` all become ``"6012"``."""
    return str(value or "").strip().replace("+", "", 1).split(":", 1)[0].split("@", 1)[0]


def to_whatsapp_jid(value: str) -> str:
    """Normalize an *outbound* target to a bridge-safe JID (inverse of normalize).  Baileys'
    ``jidDecode`` crashes on a bare phone, so bare phones become ``<digits>@s.whatsapp.net``;
    ``user:device@domain`` collapses to ``user@domain``; anything else is returned unchanged
    so the bridge can surface a real error.  ``""`` for empty input."""
    if not value:
        return ""
    normalized = str(value).strip()
    if ":" in normalized and "@" in normalized:
        prefix, _, domain = normalized.partition("@")
        normalized = f"{prefix.split(':', 1)[0]}@{domain}"
    if "@" in normalized:
        return normalized
    if _BARE_PHONE_RE.fullmatch(normalized):
        digits = re.sub(r"\D+", "", normalized)
        if digits:
            return f"{digits}@s.whatsapp.net"
    return normalized


def _bridge_session_dir() -> Path:
    """Return the single WhatsApp bridge session store alias resolution reads.

    Exactly ONE store is selected here — callers never union or chain across
    stores, so an alias walk can never hop from one profile's transport into
    another's.

    Candidates:

    - The **active-scope** dir, resolved with the same
      ``get_hermes_dir("platforms/whatsapp/session", "whatsapp/session")``
      semantics :mod:`plugins.platforms.whatsapp.adapter` uses for its own
      default session path. A separately configured secondary-profile bridge
      writes its ``lid-mapping-*.json`` files under *its* profile home, so
      they must be read from the store that adapter actually wrote.
    - The **process-home** dir, resolved with those same modern/legacy rules
      anchored on :func:`hermes_constants.get_process_hermes_home`
      (same anchoring as ``plugins.missions._missions_dir``).

    Selection:

    - Same canonical path → one store, read once (the no-override case).
    - A non-empty active-scope store — *any* adapter/session state at all,
      creds, bridge bookkeeping, even mappings for unrelated identifiers —
      is AUTHORITATIVE for every identifier. That profile owns its own
      transport identity namespace; resolution must not dip into the
      process home just because a given identifier has no edge locally.
    - Only when the active-scope store is absent or empty (no adapter ever
      ran there — the mission-assistant case, which reuses the default
      profile's transport) does resolution use the process-home store.

    Resolution never creates directories, and read/parse errors inside the
    chosen store keep the resilient log-and-skip behaviour — they never
    trigger a cross-profile fallback.
    """
    active_dir = get_hermes_dir("platforms/whatsapp/session", "whatsapp/session")
    process_dir = get_hermes_dir(
        "platforms/whatsapp/session",
        "whatsapp/session",
        home=get_process_hermes_home(),
    )
    if _resolved_store(active_dir) == _resolved_store(process_dir):
        return active_dir
    if _session_store_has_state(active_dir):
        return active_dir
    return process_dir


def _resolved_store(path: Path) -> Path:
    """Canonicalise a candidate store dir for same-path comparison."""
    try:
        return path.expanduser().resolve(strict=False)
    except OSError:
        # Pathological symlink/permission cases — compare as-is rather than
        # failing the lookup outright.
        return path


def _session_store_has_state(session_dir: Path) -> bool:
    """True when the resolved session dir holds any adapter/session state.

    Any entry — creds, bridge bookkeeping, ``lid-mapping-*.json`` for
    unrelated identifiers — means a WhatsApp adapter owns this home's
    transport identity namespace. An absent or empty directory means no
    adapter ever ran there. Directories that cannot be inspected count as
    occupied, so a permission error can never silently redirect alias
    resolution across profiles.
    """
    try:
        return any(session_dir.iterdir())
    except FileNotFoundError:
        return False
    except OSError:
        # NotADirectoryError (a file where the store dir should be) and
        # permission/IO failures — treat as occupied, never fall back.
        return True


def expand_whatsapp_aliases(identifier: str) -> Set[str]:
    """Resolve WhatsApp phone/LID aliases via bridge session mapping files.

    Returns the set of all identifiers transitively reachable through the
    ``lid-mapping-*.json`` files of the bridge session store selected by
    :func:`_bridge_session_dir` (the active profile's store when it has
    session state, otherwise the process-home store), starting from
    ``identifier``. The result always includes the
    normalized input itself, so callers can safely ``in`` check against
    the return value without a separate fallback branch.

    Returns an empty set if ``identifier`` normalizes to empty.
    """
    normalized = normalize_whatsapp_identifier(identifier)
    if not normalized:
        return set()

    session_dir = _bridge_session_dir()
    resolved: Set[str] = set()
    queue = [normalized]
    while queue:
        current = queue.pop(0)
        # _SAFE_IDENTIFIER_RE: defense-in-depth against path separators / traversal in the
        # ``lid-mapping-{current}`` filename (the fixed prefix already prevents escape).
        if not current or current in resolved or not _SAFE_IDENTIFIER_RE.match(current):
            continue
        resolved.add(current)
        for suffix in ("", "_reverse"):
            mapping_path = session_dir / f"lid-mapping-{current}{suffix}.json"
            if not mapping_path.exists():
                continue
            try:
                raw = json.loads(mapping_path.read_text(encoding="utf-8"))
                mapped = normalize_whatsapp_identifier(raw)
            except (OSError, json.JSONDecodeError) as exc:
                logger.debug("whatsapp_identity: failed to read %s: %s", mapping_path, exc)
                continue
            if mapped and mapped not in resolved:
                queue.append(mapped)
    return resolved


def canonical_whatsapp_identifier(identifier: str) -> str:
    """Stable sender identity across phone-JID/LID variants (DM ``chat_id`` and group
    ``participant_id`` alike): the shortest alias from :func:`expand_whatsapp_aliases`, which
    degrades to the normalized input when no mapping files exist.  ``""`` for empty input."""
    normalized = normalize_whatsapp_identifier(identifier)
    if not normalized:
        return ""
    # expand_whatsapp_aliases includes ``normalized`` itself, so min() degrades to it
    # when no lid-mapping files are present.
    aliases = expand_whatsapp_aliases(normalized)
    return min(aliases, key=lambda c: (len(c), c))
