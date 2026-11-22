"""Tests for _refresh_bootstrap_cache_scripts (the stale-installer-cache fix).

Pre-#67193 installer binaries reuse ``bootstrap-cache/install-<branch>.sh``
forever without re-downloading, so a script cached at install time executes
months-stale code on every update/repair. Those binaries have no self-update
path, so ``hermes update`` now rewrites the mutable-ref cache entries from
the fresh checkout — the stale binary's unconditional reuse then always
executes current code.

Contract under test:
- ONLY the cache key for the update-target branch is overwritten with the
  checkout's scripts/install.sh content (sanitize_ref parity: bb/gui →
  install-bb_gui.sh); sibling mutable refs keep their own scripts
- commit-pin entries are immutable: is_valid_commit() parity means 7-40
  hex chars — abbreviated SHAs included — are never touched
- missing cache dir / missing sources / IO errors are silent no-ops
"""

from __future__ import annotations

from unittest.mock import patch

from hermes_cli import main as cli_main


def _setup(tmp_path, *, sh=b"echo current\n"):
    home = tmp_path / "hermes_home"
    root = tmp_path / "hermes-agent"
    (root / "scripts").mkdir(parents=True)
    (home / "bootstrap-cache").mkdir(parents=True)
    if sh is not None:
        (root / "scripts" / "install.sh").write_bytes(sh)
    return home, root


def _run(home, root, branch="main"):
    with patch.object(cli_main, "get_hermes_home", return_value=str(home)), patch.object(
        cli_main, "PROJECT_ROOT", root
    ):
        cli_main._refresh_bootstrap_cache_scripts(branch)


def test_branch_ref_sh_is_overwritten_without_bom(tmp_path):
    home, root = _setup(tmp_path)
    stale = home / "bootstrap-cache" / "install-main.sh"
    stale.write_bytes(b"echo stale\n")
    _run(home, root)
    assert stale.read_bytes() == b"echo current\n"


def test_commit_sha_entries_left_alone(tmp_path):
    home, root = _setup(tmp_path)
    pinned = home / "bootstrap-cache" / ("install-" + "a" * 40 + ".sh")
    pinned.write_bytes(b"pinned bytes")
    _run(home, root)
    assert pinned.read_bytes() == b"pinned bytes"


def test_abbreviated_commit_pin_left_alone(tmp_path):
    # install_script.rs::is_valid_commit() accepts 7-40 hex chars — an
    # abbreviated pin like install-4ce1994.sh is immutable too, and must
    # not be rewritten even when a caller passes it as the branch.
    home, root = _setup(tmp_path)
    pinned = home / "bootstrap-cache" / "install-4ce1994.sh"
    pinned.write_bytes(b"pinned bytes")
    _run(home, root, branch="4ce1994")
    assert pinned.read_bytes() == b"pinned bytes"


def test_only_target_branch_key_is_refreshed(tmp_path):
    # Coexisting mutable refs cache DIFFERENT branches' scripts: updating
    # main must not clobber install-bb_gui.sh with main's script.
    home, root = _setup(tmp_path)
    main_entry = home / "bootstrap-cache" / "install-main.sh"
    gui_entry = home / "bootstrap-cache" / "install-bb_gui.sh"
    main_entry.write_bytes(b"stale main")
    gui_entry.write_bytes(b"bb/gui branch script")
    _run(home, root, branch="main")
    assert main_entry.read_bytes() == b"echo current\n"
    assert gui_entry.read_bytes() == b"bb/gui branch script"


def test_branch_ref_is_sanitized_like_the_installer(tmp_path):
    # sanitize_ref parity: bb/gui -> install-bb_gui.sh.
    home, root = _setup(tmp_path)
    gui_entry = home / "bootstrap-cache" / "install-bb_gui.sh"
    gui_entry.write_bytes(b"stale")
    _run(home, root, branch="bb/gui")
    assert gui_entry.read_bytes() == b"echo current\n"


def test_uncached_branch_is_noop(tmp_path, capsys):
    # A ref the installer never cached has nothing to heal — do not create
    # new cache entries the bootstrapper didn't write itself.
    home, root = _setup(tmp_path)
    _run(home, root, branch="main")
    assert not (home / "bootstrap-cache" / "install-main.sh").exists()
    assert "Refreshed installer bootstrap-cache" not in capsys.readouterr().out


def test_already_current_entry_not_reported(tmp_path, capsys):
    home, root = _setup(tmp_path)
    current = home / "bootstrap-cache" / "install-main.sh"
    current.write_bytes(b"echo current\n")
    _run(home, root)
    assert "Refreshed installer bootstrap-cache" not in capsys.readouterr().out


def test_missing_cache_dir_is_noop(tmp_path):
    home, root = _setup(tmp_path)
    import shutil

    shutil.rmtree(home / "bootstrap-cache")
    _run(home, root)  # must not raise


def test_missing_sources_is_noop(tmp_path):
    home, root = _setup(tmp_path, sh=None)
    stale = home / "bootstrap-cache" / "install-main.sh"
    stale.write_bytes(b"stale")
    _run(home, root)
    assert stale.read_bytes() == b"stale"


def test_never_raises_on_io_error(tmp_path):
    home, root = _setup(tmp_path)
    with patch.object(cli_main, "get_hermes_home", side_effect=OSError("boom")):
        cli_main._refresh_bootstrap_cache_scripts()  # must not raise
