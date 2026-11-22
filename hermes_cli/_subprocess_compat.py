"""Subprocess helpers shared by the agent core: bounded fail-open probes, hardened
git invocations, and command-line tokenization.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Mapping, Sequence

__all__ = [
    "resolve_node_command",
    "split_command_line",
    "restore_ambient_pythonpath",
    "bounded_git_probe",
    "bounded_probe_run",
    "noninteractive_git_env",
    "NO_DRIVER_DIFF_FLAGS",
]

# Flags that neutralize *attribute-scoped* diff drivers on any diff-rendering git command. A
# malicious repo can name a driver in ``.gitattributes`` (``* diff=evil``) and point it at an
# arbitrary program via ``[diff "evil"] command=/textconv=`` in ``.git/config``; because the
# attacker chooses the name, ``GIT_CONFIG_KEY`` overrides in ``noninteractive_git_env`` cannot
# enumerate it — only these flags do. ``--no-ext-diff`` kills ``command=``; ``--no-textconv`` kills
# ``textconv=``; each alone leaves the other live. Smudge/clean filters are neutralized by the env
# layer's ``core.hooksPath`` + running against the index without checkout.
NO_DRIVER_DIFF_FLAGS = ("--no-ext-diff", "--no-textconv")

# Only these subcommands accept ``NO_DRIVER_DIFF_FLAGS`` — ``status`` and friends reject them
# (``unknown option``), so the helper gates on this set rather than blanket-prepending.
_DIFF_RENDERING_SUBCOMMANDS = frozenset({"diff", "show", "log", "blame"})

# Options that consume the FOLLOWING token, so that value is never mistaken for the subcommand
# (``-C diff`` is a path; ``-c diff=x`` is a config pair).
_GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


def harden_git_argv(args: Sequence[str]) -> list[str]:
    """Copy of subcommand-first git *args* (no leading ``"git"``) with :data:`NO_DRIVER_DIFF_FLAGS`
    inserted right after a diff-rendering subcommand; other subcommands are returned unchanged.

    Pair with :func:`noninteractive_git_env`: the env layer disables fsmonitor/hooks/pager/editor/
    credential sinks, this closes the one class (attacker-named attribute drivers) env cannot reach.
    """
    out = list(args)
    i = 0
    while i < len(out):
        tok = out[i]
        if tok in _GIT_VALUE_OPTS:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        if tok in _DIFF_RENDERING_SUBCOMMANDS:
            return out[: i + 1] + list(NO_DRIVER_DIFF_FLAGS) + out[i + 1 :]
        return out  # first non-option token is a non-diff subcommand
    return out


def split_command_line(line: str) -> list[str]:
    """Split a user-supplied command line into tokens; raises ValueError on unbalanced quotes."""

    import shlex

    return shlex.split(line)



# -----------------------------------------------------------------------------
# Node ecosystem launcher resolution
# -----------------------------------------------------------------------------


def restore_ambient_pythonpath(env: Mapping[str, str]) -> dict:
    """Re-add the ambient ``PYTHONPATH`` to a child environment that a
    ``build_subprocess_env``-style factory already built.

    No-boot-through-venv: the boot interpreter is the pm STORE python, whose
    imports arrive via ``PYTHONPATH=<repo>;<venv>/site-packages`` (it has no
    editable install). The subprocess-env factories strip Hermes-owned
    PYTHONPATH entries so agent-run children on DIFFERENT interpreter
    versions never load the backend's C extensions — but a child that
    re-execs THIS interpreter (``sys.executable -m hermes_cli.main``) runs
    on the same version and needs those entries back. Prepending keeps the
    launcher's repo-first ordering intact.
    """
    merged = dict(env)
    ambient = os.environ.get("PYTHONPATH")
    if ambient:
        existing = merged.get("PYTHONPATH", "")
        merged["PYTHONPATH"] = (
            ambient + os.pathsep + existing if existing else ambient
        )
    return merged


def resolve_node_command(name: str, argv: Sequence[str]) -> list[str]:
    """Resolve a Node-ecosystem command name (``npm``, ``npx``, ``yarn``…) to an absolute-path argv."""
    resolved = shutil.which(name)
    return [resolved or name, *argv]




# GIT_CONFIG_KEY_n/VALUE_n overrides for internal git children: no credential/askpass prompts, no
# repo-configured fsmonitor/hooks/pager/editor/external-diff programs.
_GIT_CONFIG_INJECT_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
_GIT_CONFIG_OVERRIDES = {
    "credential.helper": "",
    "core.askPass": "",
    "core.fsmonitor": "false",
    "core.untrackedCache": "false",
    "core.hooksPath": os.devnull,
    "core.pager": "cat",
    "core.editor": "true",
    "sequence.editor": "true",
    "diff.external": "",
    # ssh itself bypasses stdin=DEVNULL/GIT_TERMINAL_PROMPT and opens /dev/tty directly — an
    # unknown host key (or password auth) prompts there and steals the caller's terminal (#104591).
    # BatchMode makes ssh fail instead of prompting; a working ssh-agent still succeeds. Injected
    # at the config layer so an explicit user GIT_SSH_COMMAND (env) still takes precedence.
    "core.sshCommand": "ssh -o BatchMode=yes",
}


def _safe_directory_cache_key(env: "Mapping[str, str]") -> tuple:
    """Everything that decides which files ``git config --system/--global`` reads, plus the
    global candidates' mtimes so an edit to ``~/.gitconfig`` is picked up without a restart."""
    home = env.get("HOME", "")
    xdg = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    candidates = (
        env.get("GIT_CONFIG_SYSTEM") or "/etc/gitconfig",
        env.get("GIT_CONFIG_GLOBAL") or os.path.join(home, ".gitconfig"),
        os.path.join(xdg, "git", "config"),
    )
    stamps = []
    for path in candidates:
        try:
            stamps.append(os.stat(path).st_mtime_ns)
        except OSError:
            stamps.append(None)
    return (
        env.get("GIT_CONFIG_GLOBAL"), env.get("GIT_CONFIG_SYSTEM"), env.get("GIT_CONFIG_NOSYSTEM"),
        home, env.get("XDG_CONFIG_HOME"), env.get("PATH"), *stamps,
    )


_safe_directory_cache: dict[tuple, list[str]] = {}


def _user_safe_directories(base_env: "Mapping[str, str]") -> list[str]:
    """The user's configured ``safe.directory`` values, in git's own effective order.

    Read with ``git config -z --get-all`` under *base_env* (the caller's untouched environment) so
    an explicit ``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM`` still points at the file the user means.
    Best-effort: any failure (git missing, malformed config, timeout) yields no entries and leaves
    the caller exactly as it behaved before. Memoised per process on the inputs that select the
    config files (and the global file's mtime): ``noninteractive_git_env()`` runs on every internal
    git call, including the startup banner probe, and two ``git config`` children per call is
    ~10 ms against ~0.2 ms for the rest of the function.

    ``safe.directory`` is an *ordered* multi-valued setting and an empty value resets every entry
    seen so far, so a user can revoke a system-wide ``safe.directory=*`` and then name only the
    repositories they actually trust. Order and empty resets are therefore policy, not formatting:
    scopes are read lowest-precedence first (system, then global) and every value is preserved
    verbatim -- no de-duplication (it is a sequence, not a set) and no dropping of the reset
    marker, either of which would resurrect a revoked wildcard and widen trust. ``-z`` keeps a
    value containing whitespace or a newline as the single entry git reads it as.
    """
    cache_key = _safe_directory_cache_key(base_env)
    cached = _safe_directory_cache.get(cache_key)
    if cached is not None:
        return list(cached)
    env = dict(base_env)
    # --get-all itself must not be derailed by ambient injection or an interactive prompt.
    for key in list(env):
        if key == "GIT_CONFIG_PARAMETERS" or key.startswith(_GIT_CONFIG_INJECT_PREFIXES):
            env.pop(key, None)
    env.pop("GIT_CONFIG_COUNT", None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    values: list[str] = []
    for scope in ("--system", "--global"):
        try:
            proc = subprocess.run(
                ["git", "config", scope, "-z", "--get-all", "safe.directory"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=5, stdin=subprocess.DEVNULL, env=env, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        # -z terminates every value with NUL, so the trailing split field is always empty and is
        # not a config entry; interior empty fields are real reset markers and must survive.
        records = proc.stdout.split("\0")
        if records and records[-1] == "":
            records.pop()
        values.extend(records)
    _safe_directory_cache[cache_key] = list(values)
    return values


def noninteractive_git_env(base: "Mapping[str, str] | None" = None) -> dict[str, str]:
    """Environment for *internal* git invocations that must never prompt.

    Copy of ``base`` (default ``os.environ``) with ``GIT_TERMINAL_PROMPT=0`` (fail instead of
    prompting), ``GCM_INTERACTIVE=Never`` (no Git Credential Manager dialog), and isolated git
    config: inherited ``GIT_CONFIG_*`` injection, global/system config, pagers, editors, fsmonitor,
    external diff and hooks are all disabled so a user's repo/global config cannot hang or mutate
    Hermes's plumbing calls. ``core.sshCommand`` is pinned to ``ssh -o BatchMode=yes`` so the ssh
    child of a fetch/ls-remote fails instead of prompting — ssh bypasses ``stdin=DEVNULL`` and
    opens ``/dev/tty`` directly (#104591); an agent-authenticated ssh still succeeds, and an
    explicit user ``GIT_SSH_COMMAND`` env var still takes precedence over this config-layer pin.
    ``GIT_ASKPASS``/``SSH_ASKPASS`` env vars are left alone, but OpenSSH BatchMode disables
    passphrase/password prompts, including SSH askpass. Usable keys and ssh-agent authentication
    still work; Git's own working askpass helper is unaffected. Pair with
    ``stdin=subprocess.DEVNULL``. Internal plumbing only — the agent-facing terminal tool has its
    own policy layer and visible PTY.

    Hermes shells out to git from many non-interactive contexts — MCP catalog installs, plugin
    install/update, profile distribution staging, worktree base fetches, desktop review-pane fetch/push.
    When the remote is private, misconfigured, or requires auth, git's default behavior is to prompt on the
    inherited terminal (or via an askpass helper), which silently hangs the operation until its timeout — or
    forever at call sites without one. Ported from openai/codex#34540 / #34612 ("detach non-interactive
    subprocesses from stdin"): a background tool invocation must fail fast with a readable error, not wait
    for input nobody can type.
    """
    env = dict(base if base is not None else os.environ)
    # Captured before the isolation below rewrites GIT_CONFIG_GLOBAL/SYSTEM to /dev/null --
    # reading after that point would resolve the user's config to an empty file.
    safe_directories = _user_safe_directories(base if base is not None else os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "Never"
    # Drop caller-supplied config injection; the GIT_CONFIG_COUNT block is rebuilt below so
    # ambient -c values cannot re-enable pagers, hooks, fsmonitor, editors or credential prompts.
    for key in list(env):
        if key == "GIT_CONFIG_PARAMETERS" or key.startswith(_GIT_CONFIG_INJECT_PREFIXES):
            env.pop(key, None)
    env.pop("GIT_CONFIG_COUNT", None)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_PAGER"] = "cat"
    env["PAGER"] = "cat"
    env["GIT_EDITOR"] = "true"
    overrides = list(_GIT_CONFIG_OVERRIDES.items())
    # safe.directory is honoured ONLY from global/system config (git rejects it from repo-level
    # config so a hostile repo cannot self-authorise), and both are blanked just above. Without
    # re-injection every internal git call fails "detected dubious ownership" on any repo whose
    # st_uid != geteuid() -- NFS/CIFS mounts without idmapping, shared checkouts, containers with
    # a remapped uid -- even though the user's own `git config --global --add safe.directory` is
    # correctly set and their interactive git works fine. Carried over the GIT_CONFIG_KEY_n
    # channel, which survives GIT_CONFIG_GLOBAL=/dev/null. Read-only and non-widening: the values
    # are replayed in git's own effective order, empty reset markers included (see
    # _user_safe_directories), so a global reset still revokes a system-wide wildcard exactly as it
    # does for the user's interactive git. Appended last, but the hardening overrides above are
    # distinct keys, so they are unaffected by ordering within safe.directory.
    overrides.extend(("safe.directory", value) for value in safe_directories)
    env["GIT_CONFIG_COUNT"] = str(len(overrides))
    for idx, (key, value) in enumerate(overrides):
        env[f"GIT_CONFIG_KEY_{idx}"] = key
        env[f"GIT_CONFIG_VALUE_{idx}"] = value
    return env




def kill_process_tree(proc: "subprocess.Popen") -> None:
    """Best-effort terminate *proc* and its descendants; never raises.

    This is cleanup on an already-failing path whose contract is to fail open, so every failure
    (already reaped, access denied) is swallowed rather than escaping the caller's ``except``.
    ``proc.kill()`` alone only terminates the direct child: killing the launcher leaves
    descendants (credential helpers, ``git-remote-https``, hook children) running and holding
    the pipe write ends. Callers spawn the child in its own process group (``process_group=0``,
    Python ≥3.11), so when — and only when — the child leads its own group (``pgid == pid``),
    the entire group is signalled with ``os.killpg``. The ownership check means a fallback spawn
    that shares our group can never cause us to kill unrelated processes. Ported from
    openai/codex#36793 ("Terminate timed-out Git process trees"); generalized for the
    shell-hook runner via openai/codex#37527 ("Terminate timed-out hook process trees").
    """
    try:
        from agent.deadline import kill_process_tree as _deadline_kill_tree

        _deadline_kill_tree(proc.pid)
    except Exception:
        _legacy_kill_process_tree(proc)
        return
    # Ensure Popen's own bookkeeping sees the exit so communicate()/wait() cannot hang.
    try:
        proc.kill()
    except OSError:
        pass


def _legacy_kill_process_tree(proc: "subprocess.Popen") -> None:
    """Local tree-kill fallback when agent.deadline is unavailable (partial install, cycle)."""
    # Verify the child leads its own process group before signalling, never a shared group.
    try:
        import signal as _signal

        pgid = os.getpgid(proc.pid)
        if pgid == proc.pid:
            os.killpg(pgid, _signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except OSError:
        pass


def bounded_probe_run(
    argv: Sequence[str], *, timeout: float, errors: str = "replace",
    env: "Mapping[str, str] | None" = None, cwd: "str | os.PathLike[str] | None" = None,
    raise_on_spawn_failure: bool = False,
) -> "subprocess.CompletedProcess[str] | None":
    """Deadlock-safe ``subprocess.run(argv, capture_output=True, timeout=…)`` for fail-open probes.

    Returns a ``CompletedProcess`` when the child finished within *timeout* (any exit code), or
    ``None`` on spawn failure or timeout. With ``raise_on_spawn_failure=True`` the ``Popen``
    exception propagates instead, so callers that treat a *timeout* as a verdict can still tell
    "our own probe never started" apart from "the child hung".

    Why not ``subprocess.run``: killing the timed-out child can leave a descendant (credential
    helpers, ``git-remote-https``, hook children) running and holding the pipe write ends, so the
    pipes never reach EOF and ``run()``'s post-timeout ``communicate()`` blocks forever (#68609 /
    #66037). The child is spawned in its own process group (``process_group=0``) so the tree-kill
    below can take the whole group down.
    """
    try:
        proc = subprocess.Popen(
            list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors=errors, process_group=0,
            env=dict(env) if env is not None else None, cwd=cwd)
    except Exception:
        if raise_on_spawn_failure:
            raise
        return None
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except Exception:
        # Timeout OR any other communicate() failure (torn-down pipe, decode error): tree-kill and
        # drain bounded — leaving it running would leak the suspended-descendant class this guards.
        kill_process_tree(proc)
        try:
            proc.communicate(timeout=1)
        except Exception:
            pass
        return None
    return subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)



def bounded_git_probe(argv: Sequence[str], *, timeout: float) -> str:
    """Run a short ``git`` probe and return stripped stdout, or ``""`` on ANY failure.

    On Windows ``run()``'s post-timeout cleanup calls an unbounded ``communicate()``; a suspended
    descendant git.exe holding the pipe handles then blocks forever. Here: bounded ``communicate``,
    tree-kill plus a 1s drain, then abandon the pipes; on POSIX the probe gets its own process
    group so cleanup also takes down credential/remote helpers.

    Security (GHSA-7x36-8jrh-v4pw): these probes run automatically against whatever directory the
    session sits in, before any tool call or trust prompt, and an index refresh executes the
    repo-configured ``core.fsmonitor`` program. Every probe therefore runs under
    :func:`noninteractive_git_env`; diff-rendering callers additionally pass
    :data:`NO_DRIVER_DIFF_FLAGS` (attribute-scoped drivers can't be disabled via env).

    Killing the timed-out child can leave descendants (credential helpers, ``git-remote-https``,
    hook children) running and holding duplicates of the captured stdout/stderr handles, so the
    pipes never reach EOF and the reader-thread join blocks forever. On the Desktop agent-build
    path (``_start_agent_build → _session_info → branch() → run_git``) that turned an optional
    branch label into ``agent initialization timed out`` (issues #68609 / #66037). The probe gets
    its own process group (``process_group=0``, Python ≥3.11) so timeout cleanup can take down
    descendants with the launcher instead of orphaning them (see :func:`kill_process_tree`;
    port of openai/codex#36793).
    """
    result = bounded_probe_run(argv, timeout=timeout, env=noninteractive_git_env())
    if result is None or result.returncode != 0:
        return ""
    return (result.stdout or "").strip()
