"""Shared helpers for canonicalising WhatsApp sender identity.

WhatsApp's bridge can surface the same human under two different JID shapes
within a single conversation:

- LID form: ``999999999999999@lid``
- Phone form: ``15551234567@s.whatsapp.net``

Both the authorisation path (:mod:`gateway.run`) and the session-key path
(:mod:`gateway.session`) need to collapse these aliases to a single stable
identity. This module is the single source of truth for that resolution so
the two paths can never drift apart.

Public helpers:

- :func:`normalize_whatsapp_identifier` — strip JID/LID/device/plus syntax
  down to the bare numeric identifier.
- :func:`canonical_whatsapp_identifier` — walk the bridge's
  ``lid-mapping-*.json`` files and return a stable canonical identity
  across phone/LID variants.
- :func:`expand_whatsapp_aliases` — return the full alias set for an
  identifier. Used by authorisation code that needs to match any known
  form of a sender against an allow-list.

Plugins that need per-sender behaviour on WhatsApp (role-based routing,
per-contact authorisation, policy gating in a gateway hook) should use
``canonical_whatsapp_identifier`` so their bookkeeping lines up with
Hermes' own session keys.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Set

from hermes_constants import get_hermes_dir, get_process_hermes_home

logger = logging.getLogger(__name__)

# WhatsApp JIDs are numeric (or plus-prefixed numeric) with optional
# ``@``, ``.`` and ``:`` separators. ``\w`` is pinned to ASCII so
# full-width digits / Unicode word chars can't sneak through.
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9@.+\-]+$")


def normalize_whatsapp_identifier(value: str) -> str:
    """Strip WhatsApp JID/LID syntax down to its stable numeric identifier.

    Accepts any of the identifier shapes the WhatsApp bridge may emit:
    ``"60123456789@s.whatsapp.net"``, ``"60123456789:47@s.whatsapp.net"``,
    ``"60123456789@lid"``, or a bare ``"+601****6789"`` / ``"60123456789"``.
    Returns just the numeric identifier (``"60123456789"``) suitable for
    equality comparisons.

    Useful for plugins that want to match sender IDs against
    user-supplied config (phone numbers in ``config.yaml``) without
    worrying about which variant the bridge happens to deliver.
    """
    return (
        str(value or "")
        .strip()
        .replace("+", "", 1)
        .split(":", 1)[0]
        .split("@", 1)[0]
    )


# A target that is "just a phone number" — optional leading ``+`` then digits
# and the usual human separators (spaces, dots, dashes, parens). Anything that
# already carries an ``@`` is a fully-qualified JID and must pass through
# untouched (group ``@g.us``, LID ``@lid``, ``status@broadcast`` etc.).
_BARE_PHONE_RE = re.compile(r"^\+?[\d\s().\-]+$")


def to_whatsapp_jid(value: str) -> str:
    """Normalize an *outbound* WhatsApp target to a bridge-safe JID.

    Baileys' ``jidDecode`` crashes on a bare phone number — it expects a
    fully-qualified JID such as ``50766715226@s.whatsapp.net``. This helper
    is the inverse of :func:`normalize_whatsapp_identifier`: instead of
    stripping a JID down to its numeric core for comparison, it *builds* the
    JID a send must use.

    Behaviour:

    - ``"+50766715226"`` / ``"50766715226"`` → ``"50766715226@s.whatsapp.net"``
    - ``"50766715226@s.whatsapp.net"`` → unchanged
    - ``"group-id@g.us"`` / ``"130631430344750@lid"`` → unchanged
    - ``"user:device@s.whatsapp.net"`` style colon-before-``@`` → ``@`` form
    - anything that isn't a recognizable bare phone → returned unchanged so
      the bridge can surface a meaningful error rather than us mangling it.

    Returns ``""`` for an empty/whitespace input.
    """
    if not value:
        return ""

    normalized = str(value).strip()
    # Drop a device suffix before the domain: ``user:device@domain`` is a
    # legacy Baileys shape whose ``:device`` part is not addressable — collapse
    # it to ``user@domain``. (Mirrors normalize_whatsapp_identifier, which
    # splits the bare id on ``:`` for the same reason.)
    if ":" in normalized and "@" in normalized:
        prefix, _, domain = normalized.partition("@")
        normalized = f"{prefix.split(':', 1)[0]}@{domain}"

    # Already a fully-qualified JID — leave it alone.
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
        if not current or current in resolved:
            continue
        # Defense-in-depth: reject identifiers that could sneak path
        # separators / traversal segments into the ``lid-mapping-{current}``
        # filename below. The hardcoded ``lid-mapping-`` prefix already
        # prevents escape via pathlib's component split (an attacker can't
        # create ``lid-mapping-..`` as a real directory in session_dir), but
        # this keeps the identifier space to the characters WhatsApp JIDs
        # actually use and avoids depending on that filesystem-layout
        # invariant.
        if not _SAFE_IDENTIFIER_RE.match(current):
            continue

        resolved.add(current)
        for suffix in ("", "_reverse"):
            mapping_path = session_dir / f"lid-mapping-{current}{suffix}.json"
            if not mapping_path.exists():
                continue
            try:
                mapped = normalize_whatsapp_identifier(
                    json.loads(mapping_path.read_text(encoding="utf-8"))
                )
            except (OSError, json.JSONDecodeError) as exc:
                logger.debug("whatsapp_identity: failed to read %s: %s", mapping_path, exc)
                continue
            if mapped and mapped not in resolved:
                queue.append(mapped)

    return resolved


def canonical_whatsapp_identifier(identifier: str) -> str:
    """Return a stable WhatsApp sender identity across phone-JID/LID variants.

    WhatsApp may surface the same person under either a phone-format JID
    (``60123456789@s.whatsapp.net``) or a LID (``1234567890@lid``). This
    applies to a DM ``chat_id`` *and* to the ``participant_id`` of a
    member inside a group chat — both represent a user identity, and the
    bridge may flip between the two for the same human.

    This helper reads the bridge's ``whatsapp/session/lid-mapping-*.json``
    files, walks the mapping transitively, and picks the shortest
    (numeric-preferred) alias as the canonical identity.
    :func:`gateway.session.build_session_key` uses this for both WhatsApp
    DM chat_ids and WhatsApp group participant_ids, so callers get the
    same session-key identity Hermes itself uses.

    Plugins that need per-sender behaviour (role-based routing,
    authorisation, per-contact policy) should use this so their
    bookkeeping lines up with Hermes' session bookkeeping even when
    the bridge reshuffles aliases.

    Returns an empty string if ``identifier`` normalizes to empty. If no
    mapping files exist yet (fresh bridge install), returns the
    normalized input unchanged.
    """
    normalized = normalize_whatsapp_identifier(identifier)
    if not normalized:
        return ""

    # expand_whatsapp_aliases always includes `normalized` itself in the
    # returned set, so the min() below degrades gracefully to `normalized`
    # when no lid-mapping files are present.
    aliases = expand_whatsapp_aliases(normalized)
    return min(aliases, key=lambda candidate: (len(candidate), candidate))
