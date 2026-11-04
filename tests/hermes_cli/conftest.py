"""Fixtures shared across hermes_cli tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
import subprocess

import pytest


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_hermes_gate(request, monkeypatch):
    """Default ``_detect_concurrent_hermes_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``hermes.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``hermes`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_hermes_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    # raising=False: under pytest's per-test spawn isolation, a concurrent
    # process importing a module that transitively touches hermes_cli.main
    # can briefly expose a partially-initialized module object here — one where
    # _detect_concurrent_hermes_instances isn't defined yet. A bare setattr
    # would raise AttributeError and error the (unrelated) test. The attribute
    # always exists once main.py finishes importing, so a no-op when it's
    # transiently absent is the correct, race-free default.
    monkeypatch.setattr(
        _cli_main,
        "_detect_concurrent_hermes_instances",
        lambda *_a, **_k: [],
        raising=False,
    )


@pytest.fixture(autouse=True)
def _inline_post_swap_handoff(request, monkeypatch):
    """Run the post-swap tail in-process instead of re-executing ``hermes update --post-swap``.

    ``_apply_pulled_update`` / ``_update_via_zip`` hand the rest of the run to a child
    interpreter on the pulled tree. A mocked updater flow must not spawn that child (it would
    run a real dependency sync against the worktree), so the tail runs here through the same
    payload round-trip — every step stays patchable and the payload shape is still exercised.
    Tests of the hand-off itself opt out with ``@pytest.mark.real_post_swap_handoff``.
    """
    if request.node.get_closest_marker("real_post_swap_handoff"):
        return
    try:
        from hermes_cli import update_cmd, update_receipt
    except Exception:
        return

    def _inline(args, **payload_kwargs):
        payload = update_cmd._post_swap_payload(**payload_kwargs)
        if payload["receipt"]:
            update_receipt.resume_update_receipt(payload["receipt"])
        update_cmd._execute_post_swap(payload, args, payload_kwargs["gateway_mode"])

    monkeypatch.setattr(update_cmd, "_hand_off_post_swap", _inline, raising=False)


@pytest.fixture
def isolated_update_runtime(monkeypatch, tmp_path, request):
    """Keep mocked updater flows off the host checkout and runtime fleet."""
    from hermes_cli import gateway, main, update_cmd, update_cmd_fleet
    from hermes_cli import update_inventory, update_receipt

    checkout = tmp_path / "isolated-update-checkout"
    (checkout / ".git").mkdir(parents=True)
    monkeypatch.setattr(main, "PROJECT_ROOT", checkout)
    if hasattr(request.module, "PROJECT_ROOT"):
        monkeypatch.setattr(request.module, "PROJECT_ROOT", checkout)

    monkeypatch.setattr(gateway, "find_gateway_pids", lambda *a, **k: [])
    monkeypatch.setattr(gateway, "find_profile_gateway_processes", lambda *a, **k: [])
    monkeypatch.setattr(gateway, "_get_service_pids", lambda *a, **k: set())
    monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
    monkeypatch.setattr(main, "_pause_windows_gateways_for_update", lambda: None)
    monkeypatch.setattr(main, "_resume_windows_gateways_after_update", lambda *a, **k: None)
    monkeypatch.setattr(main, "_detect_venv_python_processes", lambda: [])
    monkeypatch.setattr(update_cmd, "_clear_windows_venv_holders_or_exit", lambda *a, **k: None)
    monkeypatch.setattr(update_cmd, "_finish_dashboard_update_cleanup", lambda *a, **k: None)
    monkeypatch.setattr(update_cmd, "_apply_pending_fleet_restart_catchup", lambda *a, **k: None)
    monkeypatch.setattr(update_cmd_fleet, "_restart_macos_launchd_gateways", lambda *a, **k: None)
    monkeypatch.setattr(update_inventory, "collect_runtime_inventory", lambda: None)
    monkeypatch.setattr(update_receipt, "collect_fleet_versions", lambda *a, **k: [])


# ---- prompt_toolkit / capsys isolation ----
# ``cli._cprint`` renders through ``prompt_toolkit.print_formatted_text``,
# which — when called with no explicit ``output=`` — lazily creates an
# ``Output`` from ``sys.stdout`` **and caches it on the process-global default
# ``AppSession``** (``prompt_toolkit.application.current._current_app_session``,
# a ``ContextVar`` with a module-level default). The cache is keyed to nothing
# and never re-reads ``sys.stdout``.
#
# Under pytest, ``capsys`` swaps ``sys.stdout`` for a fresh buffer per test.
# So the first CLI test that emits through ``_cprint`` (e.g. one exercising
# ``/queue``, which prints a "Queued: …" line) locks prompt_toolkit's cached
# output onto *its* captured stdout. Every later ``capsys`` test that asserts
# on ``_cprint`` output then reads an empty buffer, because the render went to
# the first test's now-dead capture target. That is the mechanism behind the
# order-dependent ``test_resume_quiet_stderr`` failure: it passes in isolation
# and in its own file, but fails in a full ``tests/cli`` run.
#
# Reset the cached output before every CLI test so each one re-creates a fresh
# prompt_toolkit ``Output`` bound to its own ``sys.stdout`` on first use. This
# is a no-op when prompt_toolkit isn't importable and cheap otherwise (the
# property re-creates lazily).


@pytest.fixture(autouse=True)
def _reset_prompt_toolkit_output_cache():
    """Clear prompt_toolkit's cached AppSession output around each CLI test.

    See the module docstring for the capsys/prompt_toolkit interaction this
    guards against.
    """

    def _clear() -> None:
        try:
            from prompt_toolkit.application.current import get_app_session

            get_app_session()._output = None
        except Exception:
            # prompt_toolkit not importable / internal shape changed — the
            # tests that rely on this simply keep their prior behavior.
            pass

    _clear()
    yield
    _clear()


@pytest.fixture
def patch_pm_uv_to_shutil_which():
    """Make pm.uv follow shutil.which mocking; update-flow test modules that
    stub uv availability via shutil.which patches apply this autouse=True."""
    import os
    import shutil
    from unittest.mock import patch

    def _fake_pm_uv(*, venv=None, realize=True):
        env = dict(os.environ)
        if venv is not None:
            env["VIRTUAL_ENV"] = str(venv)
        return shutil.which("uv"), env

    with patch("pm.uv", side_effect=_fake_pm_uv):
        yield


@pytest.fixture
def isolated_update_uv(patch_pm_uv_to_shutil_which):
    """Make pm.uv follow shutil.which mocking in tests."""
    yield


@pytest.fixture
def isolated_update_processes():
    """Keep cmd_update's gateway auto-restart phase off this machine's gateways.

    The restart phase used to swallow every exception at debug level, so these
    end-to-end tests never noticed it touching real gateway discovery. Since
    the phase is surfaced (#78574: an aborted restart now fails the update),
    an unmocked ``find_gateway_pids`` on a box with a live gateway reaches the
    conftest live-system guard and turns into a spurious ``sys.exit(1)``.
    Discovery returning nothing makes the phase a clean no-op for every test
    in this module (none of them assert on gateway restarts).
    """
    # create=True: the fork's main.py resolves several of these names lazily
    # (via _m() on first read) rather than importing them at module load, so
    # the attribute may legitimately not exist until the update flow runs.
    with patch("hermes_cli.gateway.find_gateway_pids", return_value=[]), \
         patch("hermes_cli.gateway.supports_systemd_services", return_value=False), \
         patch("hermes_cli.gateway.find_profile_gateway_processes", return_value=[]), \
         patch("hermes_cli.main._detect_venv_python_processes", return_value=[], create=True), \
         patch("hermes_cli.main._fleet_probe_expected_runtimes", return_value=False, create=True), \
         patch("os.kill"), \
         patch("pm.sync_venv"), \
         patch(
             "hermes_cli.update_inventory.collect_runtime_inventory",
             return_value=SimpleNamespace(runtimes=[], to_dict=lambda: {}),
         ), \
         patch("hermes_cli.main._purge_stale_hermes_modules", create=True), \
         patch("hermes_cli.main._pause_windows_gateways_for_update", return_value=None, create=True), \
         patch("hermes_cli.main._resume_windows_gateways_after_update", create=True), \
         patch(
             "hermes_cli.main._install_hangup_protection",
             return_value={
                 "prev_stdout": None, "prev_stderr": None,
                 "log_file": None, "installed": False,
             },
             create=True,
         ), \
         patch("hermes_cli.main._finalize_update_output", create=True), \
         patch("hermes_cli.update_cmd._reload_config_modules", create=True):
        yield


@pytest.fixture
def isolated_update_checkout(monkeypatch, tmp_path):
    """Keep the updater on an isolated checkout and intercept the web build's Popen path."""
    import hermes_cli.main as cli_main
    from hermes_cli import main_web_build

    (tmp_path / ".git").mkdir(exist_ok=True)
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", tmp_path, raising=False)
    noop_build = lambda *args, **kwargs: True  # noqa: E731
    monkeypatch.setattr(cli_main, "_build_web_ui", noop_build, raising=False)
    monkeypatch.setattr(main_web_build, "_build_web_ui", noop_build, raising=False)
    monkeypatch.setattr(
        main_web_build, "_web_ui_build_needed", lambda *a, **k: False, raising=False
    )
    fake_npm = lambda *a, **k: subprocess.CompletedProcess(  # noqa: E731
        [], 0, stdout="", stderr=""
    )
    monkeypatch.setattr(
        main_web_build, "_run_npm_install_deterministic", fake_npm, raising=False
    )

    # Tests that exercise ZIP fallback must override this tripwire explicitly.
    def _no_zip_fallback(*args, **kwargs):
        pytest.fail(
            "test reached _update_via_zip — the git checkout path was not "
            "isolated correctly (missing tmp .git or unexpected fallback)"
        )

    monkeypatch.setattr("hermes_cli.update_cmd._update_via_zip", _no_zip_fallback)
