"""Regression tests for the conftest live-system guard's argv handling.

The guard must treat only argv[0] of a list/tuple command as the executable
(arguments are data: a file named ``skill`` is not the ``skill`` binary),
while still scanning every token of wrapper invocations like ``bash -c``.
All blocked-case commands use patterns that match no real process, so a
guard regression cannot kill anything.

The git worktree-mutation tests below follow the same fail-safe discipline,
stronger: every *blocked-case* command targets a ``tmp_path`` repo that the
test itself DESIGNATES as the protected root (via
``tests.conftest._LIVE_GUARD_PROTECTED_GIT_ROOTS``). If the guard regresses,
the mutating verb actually dispatches — against the throwaway repo only —
and the assertion fails; the real checkout is never touched, even RED.
Read-only git against the real checkout (``stash list``) is asserted ALLOWED:
the guard targets mutating verbs, not git as such.
"""

import subprocess

import pytest

import tests.conftest as _guard_conftest


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )


def _init_repo(tmp_path):
    """A real throwaway git repo with one tracked file and one commit."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    tracked = repo / "tracked.txt"
    tracked.write_text("canary\n")
    for args in (
        ("init", "-q"),
        ("config", "user.email", "guard@example.com"),
        ("config", "user.name", "guard"),
        ("add", "."),
        ("commit", "-q", "-m", "canary"),
    ):
        result = _git(repo, *args)
        assert result.returncode == 0, result.stderr
    return repo, tracked


def test_argv_arguments_are_not_treated_as_executables(tmp_path):
    """A file argument whose basename is a killer name must not trip the
    guard (the path contains "hermes" via the pytest tmp root)."""
    target = tmp_path / "skill"
    target.write_text("just a filename\n")
    result = subprocess.run(["cat", str(target)], capture_output=True, text=True)
    assert result.returncode == 0
    assert "just a filename" in result.stdout


def test_direct_killer_argv_is_still_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["pkill", "-f", "hermes-guard-regression-nomatch"])


def test_wrapped_killer_command_is_still_blocked():
    """argv[0]-only scanning must not exempt commands hidden behind a
    shell wrapper."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["bash", "-c", "pkill -f hermes-guard-regression-nomatch"])


def test_env_wrapped_killer_command_is_still_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["env", "GUARD_TEST=1", "pkill", "-f", "hermes-guard-regression-nomatch"])


# ──────────────── git worktree-mutation guard ────────────────


def test_git_init_commit_status_in_temp_repo_allowed(tmp_path):
    """Arbitrary temp repo fixtures keep full git freedom: init + commit +
    status must dispatch normally (no RuntimeError)."""
    repo, _tracked = _init_repo(tmp_path)
    result = _git(repo, "status", "--porcelain")
    assert result.returncode == 0
    assert result.stdout == ""


def test_git_stash_list_on_real_checkout_is_allowed():
    """Read-only git against the ACTUAL checkout must stay allowed.

    ``stash list`` mutates nothing, so even a total guard regression leaves
    this command harmless. (The review doc asserted the opposite — blocked —
    contradicting its own read-only exception list; rejected.)
    """
    result = _git(_guard_conftest.PROJECT_ROOT, "stash", "list")
    assert result.returncode == 0


@pytest.mark.parametrize("primitive", ["run", "Popen", "check_output", "getoutput", "os.system", "asyncio"])
def test_git_stash_on_designated_protected_root_is_blocked(tmp_path, monkeypatch, primitive):
    """``git stash push`` via the ``cwd`` kwarg (the real cmd_update incident
    vector: plain cwd, no -C) must be rejected BEFORE dispatch."""
    repo, tracked = _init_repo(tmp_path)
    monkeypatch.setattr(
        _guard_conftest, "_LIVE_GUARD_PROTECTED_GIT_ROOTS", (repo,)
    )
    monkeypatch.chdir(repo)
    command = ["git", "stash", "push", "-m", "guard-canary"]
    with pytest.raises(RuntimeError, match="live-system guard"):
        if primitive == "os.system":
            import os
            os.system("git stash push")
        elif primitive == "asyncio":
            import asyncio
            asyncio.run(asyncio.create_subprocess_exec(*command, cwd=str(repo)))
        elif primitive == "getoutput":
            subprocess.getoutput("git stash push")
        else:
            getattr(subprocess, primitive)(command, cwd=str(repo))
    # Nothing dispatched: no stash entry exists, the canary file is intact.
    assert _git(repo, "stash", "list").stdout.strip() == ""
    assert tracked.read_text() == "canary\n"


def test_git_reset_hard_on_designated_protected_root_is_blocked(tmp_path, monkeypatch):
    repo, tracked = _init_repo(tmp_path)
    monkeypatch.setattr(
        _guard_conftest, "_LIVE_GUARD_PROTECTED_GIT_ROOTS", (repo,)
    )
    tracked.write_text("dirty\n")  # reset --hard would destroy this
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["git", "reset", "--hard"], cwd=repo)
    assert tracked.read_text() == "dirty\n"


def test_git_stash_via_dash_c_target_is_blocked(tmp_path, monkeypatch):
    """The explicit ``-C <path>`` target form resolves too."""
    repo, tracked = _init_repo(tmp_path)
    monkeypatch.setattr(
        _guard_conftest, "_LIVE_GUARD_PROTECTED_GIT_ROOTS", (repo,)
    )
    with pytest.raises(RuntimeError, match="live-system guard"):
        # cwd is the tmp root (outside the checkout): if the guard ever
        # regresses, git -C dispatches against the throwaway repo only.
        subprocess.run(["git", "-C", str(repo), "stash", "push"], cwd=str(tmp_path))
    assert _git(repo, "stash", "list").stdout.strip() == ""
    assert tracked.read_text() == "canary\n"


def test_git_stash_string_shell_form_on_designated_root_is_blocked(tmp_path, monkeypatch):
    repo, _tracked = _init_repo(tmp_path)
    monkeypatch.setattr(
        _guard_conftest, "_LIVE_GUARD_PROTECTED_GIT_ROOTS", (repo,)
    )
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run("git stash push -m guard-canary", shell=True, cwd=str(repo))


@pytest.mark.parametrize("invocation", ["native-exe", "relative-c", "chained-c", "git-dir", "shell-wrapper"])
def test_git_mutation_resolves_native_executable_and_target(tmp_path, monkeypatch, invocation):
    import shutil

    repo, tracked = _init_repo(tmp_path)
    tracked.write_text("dirty\n")
    monkeypatch.setattr(_guard_conftest, "_LIVE_GUARD_PROTECTED_GIT_ROOTS", (repo,))
    git = shutil.which("git")
    assert git is not None
    commands = {
        "native-exe": [git, "reset", "--hard"],
        "relative-c": ["git", "-C", repo.name, "reset", "--hard"],
        "chained-c": ["git", "-C", str(tmp_path), "-C", repo.name, "reset", "--hard"],
        "git-dir": ["git", "--git-dir", str(repo / ".git"), "--work-tree", str(tmp_path / "elsewhere"), "reset", "--hard"],
        "shell-wrapper": ["bash", "-c", f'git -C "{repo.as_posix()}" reset --hard'],
    }
    cwd = repo if invocation == "native-exe" else tmp_path
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(commands[invocation], cwd=cwd, capture_output=True, text=True, timeout=15)
    assert tracked.read_text() == "dirty\n"


def test_git_pull_in_temp_repo_allowed(tmp_path):
    """A mutating verb OUTSIDE the protected roots stays allowed — fixture
    repos must not lose pull capability."""
    repo, _tracked = _init_repo(tmp_path)
    result = _git(repo, "pull")
    # No remote configured: git itself fails, but the GUARD must not — a
    # guard block surfaces as RuntimeError before dispatch, never as a
    # CompletedProcess.
    assert result.returncode != 0
    assert "live-system guard" not in result.stderr

def test_gateway_start_inside_a_container_exec_is_not_blocked():
    """``docker exec <ctr> hermes gateway start`` launches the gateway INSIDE the container,
    where it cannot reach the host's systemd unit or webhook port; tests/docker/ depends on it.
    The binary is a stub so the argv stays exact without needing a Docker daemon."""
    import os
    import stat

    stub_dir = os.path.join(os.environ["HERMES_HOME"], "stub-bin")
    os.makedirs(stub_dir, exist_ok=True)
    stub = os.path.join(stub_dir, "docker")
    with open(stub, "w") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(stub, os.stat(stub).st_mode | stat.S_IXUSR)
    result = subprocess.run(
        [stub, "exec", "-u", "hermes", "ctr", "sh", "-c", "hermes -p prof gateway start"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_gateway_start_on_the_host_is_still_blocked():
    with pytest.raises(RuntimeError, match="REAL.*gateway runtime"):
        subprocess.run(["python", "-m", "hermes_cli.main", "gateway", "start"])
