"""Tests for the install-kind uninstall gate + the data-only mode.

The rule under test: only a git checkout may have its code removed by the
uninstaller. Sealed trees (Nix, Docker) refuse the code-removing modes with
the steward's own instructions, and the one mode valid everywhere — ``data``
— removes user data without touching code.

Adapted from the restack branch: detection here reads the install stamp
(``install-stamp.json`` with its required ``updateMechanism``) through
``hermes_cli.steward`` instead of restack's ``installation.tree``.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_cli.uninstall as un
from hermes_cli.steward import (
    STEWARD_DOCKER,
    STEWARD_NIX,
    steward_uninstall_message,
)


# ---------------------------------------------------------------------------
# steward_uninstall_message
# ---------------------------------------------------------------------------


def test_steward_uninstall_messages_name_the_steward():
    assert "Nix" in steward_uninstall_message(STEWARD_NIX)
    assert "Docker" in steward_uninstall_message(STEWARD_DOCKER)
    # Unknown stewards fall back to a generic refusal that still names them.
    assert "somepkg" in steward_uninstall_message("somepkg")


def test_all_refusals_point_at_the_data_mode():
    # Every refusal must leave the user a working next step for their data.
    for steward in (STEWARD_NIX, STEWARD_DOCKER, "somepkg"):
        assert "hermes uninstall --data" in steward_uninstall_message(steward)


# ---------------------------------------------------------------------------
# code_removal_refusal — the gate itself
# ---------------------------------------------------------------------------


def _fake_project_root(monkeypatch, tmp_path: Path, *, git: bool, distribution: "str | None" = None) -> Path:
    root = tmp_path / "hermes-agent"
    root.mkdir()
    if git:
        (root / ".git").mkdir()
    if distribution is not None:
        # The code-scoped stamp (see hermes_cli.steward.BUILD_INFO_NAME).
        (root / "install-stamp.json").write_text(
            json.dumps({"distribution": distribution, "updateMechanism": "external"})
        , encoding="utf-8")
    monkeypatch.setattr(un, "get_project_root", lambda: root)
    return root


def test_git_checkout_allows_code_removal(monkeypatch, tmp_path):
    _fake_project_root(monkeypatch, tmp_path, git=True)
    assert un.code_removal_refusal() is None


def test_sealed_nix_tree_refuses_code_removal(monkeypatch, tmp_path):
    _fake_project_root(monkeypatch, tmp_path, git=False, distribution="nix")
    refusal = un.code_removal_refusal()
    assert refusal is not None
    assert "Nix" in refusal


def test_stampless_sealed_tree_still_refuses(monkeypatch, tmp_path):
    # No .git and no stamp: we cannot prove the tree is ours, so refuse.
    _fake_project_root(monkeypatch, tmp_path, git=False)
    assert un.code_removal_refusal() is not None


def test_run_uninstall_exits_on_sealed_tree(monkeypatch, tmp_path, capsys):
    _fake_project_root(monkeypatch, tmp_path, git=False, distribution="nix")
    args = SimpleNamespace(yes=True, full=True, dry_run=False)
    with pytest.raises(SystemExit) as exc:
        un.run_uninstall(args)
    assert exc.value.code == 1
    assert "Nix" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# run_data_uninstall — user data only, on any install kind
# ---------------------------------------------------------------------------


def _make_home(tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    (home / "sessions").mkdir(parents=True)
    (home / "sessions" / "s1.json").write_text("{}", encoding="utf-8")
    (home / "logs").mkdir()
    (home / "config.yaml").write_text("x: 1\n", encoding="utf-8")
    (home / ".env").write_text("KEY=secret\n", encoding="utf-8")
    agent = home / "hermes-agent"
    (agent / "hermes_cli").mkdir(parents=True)
    (agent / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    return home


def test_run_data_uninstall_removes_data_keeps_code(monkeypatch, tmp_path):
    home = _make_home(tmp_path)
    monkeypatch.setattr(un, "get_hermes_home", lambda: home)

    un.run_data_uninstall(SimpleNamespace(yes=True))

    # Data gone.
    assert not (home / "config.yaml").exists()
    assert not (home / ".env").exists()
    assert not (home / "sessions").exists()
    assert not (home / "logs").exists()
    # Code untouched — even on a sealed tree the data mode never touches it.
    assert (home / "hermes-agent" / "hermes_cli" / "__init__.py").exists()


def test_run_data_uninstall_works_on_sealed_trees(monkeypatch, tmp_path):
    # The whole point of the data mode: no code-removal gate applies.
    home = _make_home(tmp_path)
    (home / "hermes-agent" / "install-stamp.json").write_text(
        json.dumps({"distribution": "nix", "updateMechanism": "external"})
    , encoding="utf-8")
    monkeypatch.setattr(un, "get_hermes_home", lambda: home)

    un.run_data_uninstall(SimpleNamespace(yes=True))

    assert not (home / "config.yaml").exists()
    assert (home / "hermes-agent").exists()


# ---------------------------------------------------------------------------
# module entrypoint routing
# ---------------------------------------------------------------------------


def test_module_entrypoint_routes_data_mode(monkeypatch):
    calls = []
    monkeypatch.setattr(un, "run_data_uninstall", lambda args: calls.append(("data", args)))
    monkeypatch.setattr(un, "run_uninstall", lambda args: calls.append(("agent", args)))

    assert un.main(["--mode", "data"]) == 0
    assert calls[-1][0] == "data"
    assert un.main(["--mode", "full"]) == 0
    assert calls[-1][0] == "agent"
    assert calls[-1][1].full is True
