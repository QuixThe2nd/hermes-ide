"""Regression for #114592: a cyclic SOUL.md symlink must not kill home initialization.

A cyclic SOUL.md link makes ``stat`` fail with ELOOP, so the seeding write followed the
link and raised OSError (errno 40 on Linux, 62 on macOS). ``initialize_home`` turned that
into ``HomeInitializationError``, so every gateway spawn died before boot (exit-75
relaunch storm). The seed now replaces a cyclic link instead of writing through it.

Boundary: only a cyclic chain is replaced — a dangling link or a link that resolves is
left alone, so operator wiring and dangling-link preservation stay out of scope.
"""

import os

from hermes_cli.config import DEFAULT_SOUL_MD, _ensure_default_soul_md
from hermes_cli.config_home import initialize_home
from hermes_cli.default_soul import _LEGACY_TEMPLATE_SOULS

_SUBDIRS = ("cron", "sessions", "logs", "memories")


def _seed_temps(home):
    return [p.name for p in home.iterdir() if p.name.startswith(".SOUL.md.")]


def test_initialize_home_survives_self_referential_soul_symlink(tmp_path):
    """The reported boot-kill path: a home whose SOUL.md link points at itself."""
    home = tmp_path / ".hermes"
    home.mkdir()
    soul = home / "SOUL.md"
    soul.symlink_to(soul)

    initialize_home(home, _SUBDIRS, set())

    assert not soul.is_symlink()
    assert soul.read_text(encoding="utf-8") == DEFAULT_SOUL_MD
    assert not _seed_temps(home)


def test_soul_symlink_to_customized_file_is_left_alone(tmp_path):
    """A resolving link is operator wiring: the link survives, content is untouched."""
    home = tmp_path / ".hermes"
    home.mkdir()
    target = tmp_path / "shared-identity.md"
    target.write_text("custom identity\n", encoding="utf-8")
    soul = home / "SOUL.md"
    soul.symlink_to(target)

    _ensure_default_soul_md(home)
    initialize_home(home, _SUBDIRS, set())

    assert soul.is_symlink()
    assert soul.read_text(encoding="utf-8") == "custom identity\n"


def test_soul_symlink_to_legacy_scaffold_keeps_link_and_upgrades_target(tmp_path):
    """A resolving link keeps pointing at its target, which is upgraded in place."""
    home = tmp_path / ".hermes"
    home.mkdir()
    target = tmp_path / "shared-scaffold.md"
    target.write_text(_LEGACY_TEMPLATE_SOULS[1], encoding="utf-8")
    soul = home / "SOUL.md"
    soul.symlink_to(target)

    _ensure_default_soul_md(home)

    assert soul.is_symlink()
    assert target.read_text(encoding="utf-8") == DEFAULT_SOUL_MD


def test_dangling_soul_symlink_is_not_replaced(tmp_path):
    """Boundary: a dangling link is not a cycle — the replacement branch must not touch it."""
    home = tmp_path / ".hermes"
    home.mkdir()
    soul = home / "SOUL.md"
    soul.symlink_to(home / "does-not-exist.md")

    _ensure_default_soul_md(home)

    assert soul.is_symlink()


def test_unseedable_cyclic_soul_symlink_still_boots(tmp_path, monkeypatch):
    """A home where the seed cannot be committed: initialization must not raise."""
    import hermes_cli.config as config_mod

    home = tmp_path / ".hermes"
    home.mkdir()
    soul = home / "SOUL.md"
    soul.symlink_to(soul)

    class _ReadOnlyOS:
        def __getattr__(self, name):
            return getattr(os, name)

        def replace(self, *args, **kwargs):
            raise PermissionError("read-only home")

    monkeypatch.setattr(config_mod, "os", _ReadOnlyOS())

    initialize_home(home, _SUBDIRS, set())

    assert soul.is_symlink()
    assert not _seed_temps(home)


def test_missing_soul_md_still_seeds_default(tmp_path):
    """Unchanged happy path: first run seeds DEFAULT_SOUL_MD as a regular file."""
    home = tmp_path / ".hermes"
    home.mkdir()

    initialize_home(home, _SUBDIRS, set())

    assert not (home / "SOUL.md").is_symlink()
    assert (home / "SOUL.md").read_text(encoding="utf-8") == DEFAULT_SOUL_MD
