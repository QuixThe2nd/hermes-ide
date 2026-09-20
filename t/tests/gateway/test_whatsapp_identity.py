"""Tests for gateway.whatsapp_identity alias resolution path."""

import json

import pytest

from gateway.whatsapp_identity import (
    canonical_whatsapp_identifier,
    expand_whatsapp_aliases,
)
from hermes_constants import (
    get_hermes_home,
    get_process_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)


def test_aliases_resolve_on_modern_platforms_layout(tmp_path, monkeypatch):
    tmp_home = tmp_path / "hermes-home"
    mapping_dir = tmp_home / "platforms" / "whatsapp" / "session"
    mapping_dir.mkdir(parents=True, exist_ok=True)
    (mapping_dir / "lid-mapping-999999999999999.json").write_text(
        json.dumps("15551234567@s.whatsapp.net"),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_home))

    assert expand_whatsapp_aliases("999999999999999@lid") == {
        "999999999999999",
        "15551234567",
    }


def _write_lid_mapping_pair(session_dir, phone: str, lid: str) -> None:
    """Create the bridge's forward + reverse LID mapping files."""
    (session_dir / f"lid-mapping-{phone}.json").write_text(
        json.dumps(lid), encoding="utf-8"
    )
    (session_dir / f"lid-mapping-{lid}_reverse.json").write_text(
        json.dumps(phone), encoding="utf-8"
    )


def test_alias_resolution_ignores_profile_scoped_home_override(tmp_path, monkeypatch):
    """Regression: alias resolution must stay anchored
    on the PROCESS home while a per-profile HERMES_HOME override is active.

    The ``lid-mapping-*.json`` files are transport-level state written by the
    default profile's bridge, so they live under the process home — a profile
    directory has none. ``expand_whatsapp_aliases()`` used to resolve the
    session dir via ``get_hermes_dir()`` alone, which follows
    ``set_hermes_home_override()``: under the assistant profile it found no
    mapping files, ``canonical_whatsapp_identifier()`` diverged between the
    phone and LID forms, and ``find_active_mission_for_chat()`` missed the
    active mission — routing the inbound DM to the main profile with full
    tool access instead of the mission-bound assistant profile.
    """
    process_home = tmp_path / "process-home"
    profile_home = tmp_path / "profiles" / "assistant"
    session_dir = process_home / "platforms" / "whatsapp" / "session"
    session_dir.mkdir(parents=True)
    profile_home.mkdir(parents=True)
    # Reserved test identifiers: phone 15550001111 <-> LID
    # 900000000000001.
    _write_lid_mapping_pair(session_dir, "15550001111", "900000000000001")

    # Mission store (process-anchored, see plugins.missions._missions_dir)
    # holds an active DM mission bound to the phone-form chat target.
    missions_dir = process_home / "missions"
    missions_dir.mkdir(parents=True)
    (missions_dir / "mission-mission-test-001.json").write_text(
        json.dumps(
            {
                "mission_id": "mission-test-001",
                "status": "active",
                "platform": "whatsapp",
                "chat_type": "dm",
                "chat_id": "15550001111@s.whatsapp.net",
                "chat_name": "Test Contact",
                "profile": "assistant",
                "goal": "Agree picnic time",
                "created_at": "2026-08-25T11:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(process_home))

    import plugins.missions as pm

    token = set_hermes_home_override(str(profile_home))
    try:
        # The override is genuinely active for profile-scoped lookups, while
        # the process home (where the bridge wrote its mappings) is unchanged.
        assert get_hermes_home() == profile_home
        assert get_process_hermes_home() == process_home

        # Alias expansion still sees the bridge's process-home mapping files.
        assert expand_whatsapp_aliases("900000000000001@lid") == {
            "900000000000001",
            "15550001111",
        }
        # Both identifier forms canonicalise to the SAME identity.
        assert canonical_whatsapp_identifier("900000000000001@lid") == "15550001111"
        assert (
            canonical_whatsapp_identifier("15550001111@s.whatsapp.net")
            == "15550001111"
        )
        # ...so the mission-bound LID inbound routes to the active mission.
        mission = pm.find_active_mission_for_chat("900000000000001@lid")
        assert mission is not None
        assert mission["mission_id"] == "mission-test-001"
        assert mission["status"] == "active"
    finally:
        reset_hermes_home_override(token)


def test_alias_resolution_under_profile_override_uses_legacy_layout(tmp_path, monkeypatch):
    """The legacy ``whatsapp/session`` fallback stays honoured from the
    process home when a profile-scoped override is active (installs that
    predate the ``platforms/`` consolidation keep their mapping files
    there)."""
    process_home = tmp_path / "process-home"
    profile_home = tmp_path / "profiles" / "assistant"
    legacy_dir = process_home / "whatsapp" / "session"
    legacy_dir.mkdir(parents=True)
    profile_home.mkdir(parents=True)
    _write_lid_mapping_pair(legacy_dir, "15550001111", "900000000000001")

    monkeypatch.setenv("HERMES_HOME", str(process_home))

    token = set_hermes_home_override(str(profile_home))
    try:
        assert expand_whatsapp_aliases("15550001111@s.whatsapp.net") == {
            "15550001111",
            "900000000000001",
        }
        assert canonical_whatsapp_identifier("900000000000001@lid") == "15550001111"
    finally:
        reset_hermes_home_override(token)


# ── Store selection: active-profile store vs process-home store ─────────────
#
# A separately configured secondary-profile WhatsAppAdapter writes its
# session and lid-mapping files under its active profile home, so a local
# store that holds ANY adapter/session state is authoritative. Only a
# profile with no local store at all (the mission assistant, which reuses
# the default transport) may resolve aliases against the process home.


def test_secondary_profile_local_store_resolves_locally(tmp_path, monkeypatch):
    """A secondary profile with its own bridge resolves aliases from its own
    store — the process home's mappings are not substituted for it."""
    process_home = tmp_path / "process-home"
    profile_home = tmp_path / "profiles" / "secondary"
    process_dir = process_home / "platforms" / "whatsapp" / "session"
    process_dir.mkdir(parents=True)
    local_dir = profile_home / "platforms" / "whatsapp" / "session"
    local_dir.mkdir(parents=True)
    _write_lid_mapping_pair(local_dir, "15550001111", "777777777777777")
    # An unrelated mapping in the process home must never be consulted.
    _write_lid_mapping_pair(process_dir, "15551234567", "888888888888888")

    monkeypatch.setenv("HERMES_HOME", str(process_home))

    token = set_hermes_home_override(str(profile_home))
    try:
        assert get_hermes_home() == profile_home
        assert get_process_hermes_home() == process_home
        assert expand_whatsapp_aliases("15550001111@s.whatsapp.net") == {
            "15550001111",
            "777777777777777",
        }
        assert canonical_whatsapp_identifier("777777777777777@lid") == "15550001111"
    finally:
        reset_hermes_home_override(token)


def test_secondary_profile_legacy_layout_resolves_locally(tmp_path, monkeypatch):
    """The active-scope store keeps the legacy ``whatsapp/session`` fallback
    for installs that predate the ``platforms/`` consolidation."""
    process_home = tmp_path / "process-home"
    profile_home = tmp_path / "profiles" / "secondary"
    process_dir = process_home / "platforms" / "whatsapp" / "session"
    process_dir.mkdir(parents=True)
    legacy_local = profile_home / "whatsapp" / "session"
    legacy_local.mkdir(parents=True)
    _write_lid_mapping_pair(legacy_local, "15550001111", "777777777777777")
    _write_lid_mapping_pair(process_dir, "15550001111", "900000000000001")

    monkeypatch.setenv("HERMES_HOME", str(process_home))

    token = set_hermes_home_override(str(profile_home))
    try:
        assert expand_whatsapp_aliases("15550001111@s.whatsapp.net") == {
            "15550001111",
            "777777777777777",
        }
        assert canonical_whatsapp_identifier("777777777777777@lid") == "15550001111"
    finally:
        reset_hermes_home_override(token)


def test_conflicting_mappings_local_store_wins(tmp_path, monkeypatch):
    """When both stores hold an edge for the same identifier, the active
    profile's store wins — the process-home mapping is not merged in."""
    process_home = tmp_path / "process-home"
    profile_home = tmp_path / "profiles" / "secondary"
    process_dir = process_home / "platforms" / "whatsapp" / "session"
    process_dir.mkdir(parents=True)
    local_dir = profile_home / "platforms" / "whatsapp" / "session"
    local_dir.mkdir(parents=True)
    _write_lid_mapping_pair(local_dir, "15550001111", "777777777777777")
    _write_lid_mapping_pair(process_dir, "15550001111", "900000000000001")

    monkeypatch.setenv("HERMES_HOME", str(process_home))

    token = set_hermes_home_override(str(profile_home))
    try:
        aliases = expand_whatsapp_aliases("15550001111@s.whatsapp.net")
        assert aliases == {"15550001111", "777777777777777"}
        assert "900000000000001" not in aliases
    finally:
        reset_hermes_home_override(token)


def test_unrelated_local_state_blocks_process_home_fallback(tmp_path, monkeypatch):
    """A non-empty local store is authoritative for EVERY identifier.

    Existing local state — creds or mappings for unrelated identifiers —
    means the profile owns its transport identity namespace, so alias
    resolution must not dip into the process home just because the queried
    identifier has no local edge."""
    process_home = tmp_path / "process-home"
    profile_home = tmp_path / "profiles" / "secondary"
    process_dir = process_home / "platforms" / "whatsapp" / "session"
    process_dir.mkdir(parents=True)
    local_dir = profile_home / "platforms" / "whatsapp" / "session"
    local_dir.mkdir(parents=True)
    # Local store has real adapter/session state but no edge for the
    # incident's LID.
    (local_dir / "creds.json").write_text("{}", encoding="utf-8")
    _write_lid_mapping_pair(local_dir, "15551234567", "888888888888888")
    # The test pair lives only in the process home...
    _write_lid_mapping_pair(process_dir, "15550001111", "900000000000001")
    # ...so the phone-bound mission there must NOT be matched via the LID.
    missions_dir = process_home / "missions"
    missions_dir.mkdir(parents=True)
    (missions_dir / "mission-mission-test-001.json").write_text(
        json.dumps(
            {
                "mission_id": "mission-test-001",
                "status": "active",
                "platform": "whatsapp",
                "chat_type": "dm",
                "chat_id": "15550001111@s.whatsapp.net",
                "chat_name": "Test Contact",
                "profile": "assistant",
                "goal": "Agree picnic time",
                "created_at": "2026-08-25T11:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(process_home))

    import plugins.missions as pm

    token = set_hermes_home_override(str(profile_home))
    try:
        assert expand_whatsapp_aliases("900000000000001@lid") == {
            "900000000000001",
        }
        assert canonical_whatsapp_identifier("900000000000001@lid") == "900000000000001"
        assert pm.find_active_mission_for_chat("900000000000001@lid") is None
    finally:
        reset_hermes_home_override(token)


def test_no_cross_store_chaining(tmp_path, monkeypatch):
    """Alias traversal stays inside the selected store: an edge that exists
    only in the non-selected store must never extend the walk."""
    process_home = tmp_path / "process-home"
    profile_home = tmp_path / "profiles" / "secondary"
    process_dir = process_home / "platforms" / "whatsapp" / "session"
    process_dir.mkdir(parents=True)
    local_dir = profile_home / "platforms" / "whatsapp" / "session"
    local_dir.mkdir(parents=True)
    _write_lid_mapping_pair(local_dir, "15550001111", "900000000000001")
    # Process home holds a further hop off the shared LID.
    (process_dir / "lid-mapping-900000000000001.json").write_text(
        json.dumps("999999888888@s.whatsapp.net"), encoding="utf-8"
    )

    monkeypatch.setenv("HERMES_HOME", str(process_home))

    token = set_hermes_home_override(str(profile_home))
    try:
        aliases = expand_whatsapp_aliases("15550001111@s.whatsapp.net")
        assert aliases == {"15550001111", "900000000000001"}
        assert "999999888888" not in aliases
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("layout", ["modern", "legacy"])
def test_same_home_override_reads_single_store(tmp_path, monkeypatch, layout):
    """When the active scope and the process home are the SAME home, the
    canonical paths match and the store is read once, unchanged — for both
    the modern and legacy layouts."""
    home = tmp_path / "hermes-home"
    session_dir = (
        home / "platforms" / "whatsapp" / "session"
        if layout == "modern"
        else home / "whatsapp" / "session"
    )
    session_dir.mkdir(parents=True)
    _write_lid_mapping_pair(session_dir, "15550001111", "900000000000001")

    monkeypatch.setenv("HERMES_HOME", str(home))

    token = set_hermes_home_override(str(home))
    try:
        assert get_hermes_home() == get_process_hermes_home()
        assert expand_whatsapp_aliases("900000000000001@lid") == {
            "900000000000001",
            "15550001111",
        }
        assert canonical_whatsapp_identifier("900000000000001@lid") == "15550001111"
    finally:
        reset_hermes_home_override(token)


def test_store_selection_never_creates_directories(tmp_path, monkeypatch):
    """Store selection is a pure lookup: the mission-assistant fallback to
    the process home must not scaffold the profile's session directory."""
    process_home = tmp_path / "process-home"
    profile_home = tmp_path / "profiles" / "assistant"
    process_dir = process_home / "platforms" / "whatsapp" / "session"
    process_dir.mkdir(parents=True)
    profile_home.mkdir(parents=True)
    _write_lid_mapping_pair(process_dir, "15550001111", "900000000000001")

    monkeypatch.setenv("HERMES_HOME", str(process_home))

    token = set_hermes_home_override(str(profile_home))
    try:
        assert expand_whatsapp_aliases("900000000000001@lid") == {
            "900000000000001",
            "15550001111",
        }
    finally:
        reset_hermes_home_override(token)
    assert not (profile_home / "platforms" / "whatsapp" / "session").exists()
    assert not (profile_home / "whatsapp" / "session").exists()
