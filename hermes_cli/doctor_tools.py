"""External-tool checks for hermes doctor: terminal backends, git/rg, Node + Camofox, npm audit, tool availability.
Split out of ``hermes_cli/doctor.py``, which re-exports every name so ``hermes_cli.doctor.<name>`` keeps resolving (and monkeypatching)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from hermes_cli.doctor_platform import _system_package_install_cmd
from hermes_cli.doctor_report import Finding, _fail_and_issue, check_bool, check_info, check_ok, check_warn, doctor_check
from hermes_constants import is_termux as _is_termux


def _safe_which(cmd: str) -> str | None:
    """shutil.which wrapper resilient to platform monkeypatching in tests."""
    try:
        return shutil.which(cmd)
    except Exception:
        return None


def _pm_tool_path(name: str) -> Path | None:
    """Answer a tool's binary from the pm store (facts.json), not PATH.

    Doctor probes tools (git, rg, ...) that pinned installs run out of
    the store; nothing puts the store on an interactive shell's PATH, so
    a PATH-only probe reports a healthy managed install as "not found".
    This answers from pm's registry/store/package authority: the recorded
    entry's binary when it exists on disk, None when unstaged or the
    recorded binary is gone (deleted = report missing, never guess).
    Contract tests: tests/hermes_cli/test_doctor_pm_store_probe.py.
    """
    try:
        import pm  # noqa: F401 — imports pm.packages, registering the definitions
        from pm import paths, registry, store
        from pm.lock import Facts

        fact = Facts(paths.facts_path()).get(name) or {}
        entry_name = fact.get("entry")
        if not entry_name:
            return None
        package = registry.get_package(name)
        binary = package.binary(store.Store(paths.store_root()).entry(entry_name), store.current_target())
    except Exception:
        return None
    if binary is None or not binary.is_file():
        return None
    return binary


def _pm_package_for_command(command: str) -> str | None:
    """The pm package that provisions *command*, derived from pm's own
    package definitions (binary_rel → executable basename) — no restated
    name table, so new pm packages are covered without doctor changes."""
    try:
        import pm  # noqa: F401 — imports pm.packages, registering the definitions
        from pm import registry, store
        from pm.packages import BinaryPackage

        target = store.current_target()
        for package_name in registry.all_packages():
            package = registry.get_package(package_name)
            if isinstance(package, BinaryPackage):
                rel = package._rel(target)
                if rel and Path(rel).name.removesuffix(".exe").removesuffix(".cmd") == command:
                    return package_name
    except Exception:
        return None
    return None


def _doctor_tool(name: str) -> tuple[str | None, str]:
    """Resolve the tool Hermes would actually run: the pm store first
    (pinned installs run tools out of the store, which nothing puts on
    PATH), then PATH. *name* is the command ("rg"); its pm package
    ("ripgrep") is resolved from pm's own definitions. Returns
    ``(path, ok-row detail)``."""
    for package_name in (name, _pm_package_for_command(name)):
        if not package_name:
            continue
        staged = _pm_tool_path(package_name)
        if staged:
            return str(staged), "(pm store)"
    return _safe_which(name), ""


def _run_ok(cmd: list[str], timeout: int, **kw) -> bool:
    """True when *cmd* exits 0 within *timeout*; a timeout counts as failure."""
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, **kw).returncode == 0
    except subprocess.TimeoutExpired:
        return False




_TERMUX_INSTALL_ALL_FALLBACK_NOTES = (
    "Termux install profile: use .[termux-all] for broad compatibility (installer default on Termux).",
    "Matrix E2EE extra is excluded on Termux (python-olm currently fails to build).",
)


def _doctor_web_capability_rows() -> list[tuple[str, str, str]]:
    """Return ``(status, label, detail)`` rows (status ``ok``/``warn``) for web search/extract readiness.

    Uses the same active-provider resolvers as the tools but reports ``is_available()``
    readiness, so an explicitly selected but unconfigured backend does not look healthy.

    See #78412.
    """
    rows: list[tuple[str, str, str]] = []
    try:
        from agent.web_search_registry import get_active_extract_provider, get_active_search_provider
        from tools.web_tools import _ensure_web_plugins_loaded, _provider_is_ready
        # Fresh process: bundled web providers only register during plugin discovery (idempotent, cheap).
        _ensure_web_plugins_loaded()
    except Exception:
        return rows
    for capability, getter in (("web search", get_active_search_provider), ("web extract", get_active_extract_provider)):
        try:
            provider = getter()
        except Exception:
            provider = None
        if provider is None:
            rows.append(("warn", capability, "(no provider selected or registered)"))
            continue
        name = getattr(provider, "name", None) or type(provider).__name__
        rows.append(("ok", capability, f"({name})") if _provider_is_ready(provider)
                    else ("warn", capability, f"({name} selected; provider not configured)"))
    return rows


def _apply_doctor_tool_availability_overrides(available: list[str], unavailable: list[dict]) -> tuple[list[str], list[dict]]:
    """Adjust runtime-gated tool availability for doctor diagnostics."""
    from hermes_cli.doctor_state import _honcho_is_configured_for_doctor
    updated_available, updated_unavailable = list(available), []
    for item in unavailable:
        if item.get("name") == "honcho" and _honcho_is_configured_for_doctor():
            gated = "honcho"
        else:
            updated_unavailable.append(item)
            continue
        if gated not in updated_available:
            updated_available.append(gated)
    return updated_available, updated_unavailable


def _enabled_cli_toolsets_for_doctor() -> set[str] | None:
    """Return toolsets enabled for the CLI, or None if config resolution fails."""
    try:
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools
        return {str(toolset) for toolset in _get_platform_tools(load_config() or {}, "cli")}
    except Exception:
        return None


# Toolsets gated by a multi-path setup (several providers / managed auth) declare no single
# `requires_env`, so the generic branch would call a missing credential a "system dependency".
# Name the real fix instead (#9516).
_TOOLSET_SETUP_HINTS: dict[str, str] = {
    "image_gen": "(image generation unavailable — check the provider selection and its key or SDK with 'hermes tools')",
}


def _setup_gated(item: dict) -> bool:
    return bool(item.get("missing_vars") or item.get("env_vars") or item.get("name") in _TOOLSET_SETUP_HINTS)


def _missing_api_key_toolsets_for_summary(unavailable: list[dict]) -> list[dict]:
    """Filter unavailable setup-gated toolsets (missing key OR setup hint) to those enabled for the CLI."""
    api_key_unavailable = [item for item in unavailable if _setup_gated(item)]
    enabled_toolsets = _enabled_cli_toolsets_for_doctor()
    return api_key_unavailable if enabled_toolsets is None else [i for i in api_key_unavailable if str(i.get("name") or "") in enabled_toolsets]


@doctor_check()
def _check_git_and_rg(should_fix: bool, f: Finding) -> None:
    git, git_detail = _doctor_tool("git")
    rg, rg_detail = _doctor_tool("rg")
    check_bool(git, ("git", git_detail), ("git not found", "(optional)"))
    if not check_bool(rg, ("ripgrep (rg)", f"{rg_detail} (faster file search)".strip()),
                      ("ripgrep (rg) not found", "(file search uses grep fallback)")):
        check_info(f"Install for faster search: {_system_package_install_cmd('ripgrep')}")


_BUILTIN_TERMINAL_BACKENDS = {"local", "ssh"}


def _check_ssh_backend(issues: list[str]) -> None:
    ssh_host = os.getenv("TERMINAL_SSH_HOST")
    if not ssh_host:
        return _fail_and_issue("SSH host not configured", "(needed for the 'ssh' terminal backend)",
                               "run `hermes setup terminal` and enter the SSH host and user.", issues)
    ssh_user, ssh_port, ssh_key = (os.getenv(f"TERMINAL_SSH_{k}") for k in ("USER", "PORT", "KEY"))
    cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes"]
    if ssh_port:
        cmd += ["-p", ssh_port]
    if ssh_key:
        cmd += ["-i", os.path.expanduser(ssh_key)]
    cmd += [f"{ssh_user}@{ssh_host}" if ssh_user else ssh_host, "echo ok"]
    _require(_run_ok(cmd, timeout=15, text=True, encoding='utf-8', errors='replace'),
             f"SSH connection to {ssh_host}", (f"SSH connection to {ssh_host}", ""), f"Check SSH configuration for {ssh_host}", issues)


def _require(cond, ok, bad, issue: str, issues: list[str]) -> None:
    """``check_ok(*ok)`` when *cond*, else ``check_fail(*bad)`` and record *issue*."""
    if not check_bool(cond, ok, bad, fail=True):
        issues.append(issue)



def _check_plugin_backend(terminal_env: str, issues: list[str]) -> None:
    try:
        from hermes_cli.plugins import discover_plugins
        discover_plugins()
        from agent.terminal_env_registry import get_provider
        provider = get_provider(terminal_env)
    except Exception:
        provider = None
    if provider is None:
        return _fail_and_issue(f"Unknown terminal backend '{terminal_env}'", "(no built-in or plugin backend by that name)",
                               "Fix terminal.backend in config.yaml, or install/enable the plugin that provides it", issues)
    for ok, label, detail in provider.doctor_checks():
        _require(ok, (label, detail), (label, detail), detail.strip("()"), issues)


_BACKEND_CHECKS = {"ssh": _check_ssh_backend}


@doctor_check()
def _check_terminal_backend(should_fix: bool, f: Finding) -> None:
    """SSH/plugin terminal backends, gated on TERMINAL_ENV."""
    terminal_env = os.getenv("TERMINAL_ENV", "local")
    if terminal_env in _BACKEND_CHECKS:
        _BACKEND_CHECKS[terminal_env](f.issues)
    elif terminal_env not in _BUILTIN_TERMINAL_BACKENDS:
        _check_plugin_backend(terminal_env, f.issues)




@doctor_check()
def _check_node_and_browser(should_fix: bool, f: Finding) -> None:
    """Node.js for the optional local Camofox server, plus Camofox configuration."""
    if _safe_which("node"):
        check_ok("Node.js")
    else:
        check_warn("Node.js not found", "(optional, needed to install/run a local Camofox server)")
    from tools.browser_camofox import get_camofox_url
    if get_camofox_url():
        check_ok("Camofox server URL", "(configured)")
    else:
        check_info("Camofox server URL not configured — set CAMOFOX_URL before using browser tools")


def _plural(n: int) -> str:
    return "vulnerability" if n == 1 else "vulnerabilities"


def _audit_one(npm_bin: str, npm_dir, label: str, audit_extra: list[str], issues: list[str]) -> None:
    """Run one `npm audit --json` and report; any failure is silently skipped.

    Workspace-scoped (`--workspace <name>`) advisories are build-time tooling (esbuild/vite), not runtime
    code. `npm audit fix --workspace` crashes on current npm (arborist "edgesOut") and the root-level fix can
    crash on the same tree ("isDescendantOf"), so no manual fix command is offered — they clear via a lockfile bump.
    """
    import json
    try:
        # Resolved absolute path so Windows can execute npm.cmd (CreateProcessW can't run bare .cmd names).
        audit_result = subprocess.run([npm_bin, "audit", "--json", *audit_extra], cwd=str(npm_dir),
                                      capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
        audit_data = json.loads(audit_result.stdout) if audit_result.stdout.strip() else {}
        counts = audit_data.get("metadata", {}).get("vulnerabilities", {})
        critical, high, moderate = (counts.get(k, 0) for k in ("critical", "high", "moderate"))
        total = critical + high + moderate
        workspace_scoped = bool(audit_extra) and audit_extra[0] == "--workspace"
        if total == 0:
            check_ok(f"{label} deps", "(no known vulnerabilities)")
        elif critical > 0 or high > 0:
            flag = " --workspaces=false" if audit_extra == ["--workspaces=false"] else ""
            remedy = "build-tool advisory; clears via lockfile bump" if workspace_scoped else f"run: cd {npm_dir} && npm audit fix{flag}"
            check_warn(f"{label} deps", f"({critical} critical, {high} high, {moderate} moderate — {remedy})")
            if workspace_scoped:
                check_info("  ^ build-time tooling (not runtime); if manual npm remediation "
                           "errors with an arborist crash it's a known npm bug — clears via a lockfile bump")
            issues.append(f"{label} has {total} npm {_plural(total)}")
        else:
            check_ok(f"{label} deps", f"({moderate} moderate {_plural(moderate)})")
    except Exception:
        pass


@doctor_check()
def _check_npm_audit(should_fix: bool, f: Finding) -> None:
    """npm audit per Node package tree (root, WhatsApp bridge).

    PROJECT_ROOT is audited with --workspaces=false so the workspace glob is never
    resolved for a routine check. The WhatsApp bridge may live under a writable
    HERMES_HOME mirror rather than the (possibly read-only) Docker install tree, hence the shared resolver.
    """
    from hermes_cli.doctor import PROJECT_ROOT
    npm_bin = _safe_which("npm")
    if npm_bin:
        try:
            # Each entry: (cwd, label, extra_audit_args) PROJECT_ROOT is audited with --workspaces=false so
            # that the workspace glob is never resolved for a routine security check. The web
            # workspace is audited separately via --workspace flags.
            # See #38772. The WhatsApp bridge may live under a writable HERMES_HOME mirror instead of the
            # (possibly read-only) install tree in Docker — resolve it through the shared helper so we audit
            # the dir that actually holds node_modules. See #49561.
            from gateway.platforms.whatsapp_common import resolve_whatsapp_bridge_dir
            whatsapp_bridge_dir = resolve_whatsapp_bridge_dir()
        except Exception:
            whatsapp_bridge_dir = PROJECT_ROOT / "scripts" / "whatsapp-bridge"
        for npm_dir, label, audit_extra in (
            (PROJECT_ROOT, "Node.js dependencies", ["--workspaces=false"]),
            (whatsapp_bridge_dir, "WhatsApp bridge", []),
        ):
            # Workspace-scoped audits check the root node_modules; standalone dirs check their own.
            if ((PROJECT_ROOT if audit_extra else npm_dir) / "node_modules").exists():
                _audit_one(npm_bin, npm_dir, label, audit_extra, f.issues)
    if _is_termux():
        check_info("Termux compatibility fallbacks:")
        for note in _TERMUX_INSTALL_ALL_FALLBACK_NOTES:
            check_info(note)


@doctor_check("Could not check tool availability", "({e})")
def _check_tool_availability(should_fix: bool, f: Finding) -> None:
    from hermes_cli.doctor import PROJECT_ROOT
    sys.path.insert(0, str(PROJECT_ROOT))
    from model_tools import check_tool_availability, TOOLSET_REQUIREMENTS
    available, unavailable = _apply_doctor_tool_availability_overrides(*check_tool_availability())
    # Web is split into search/extract readiness rows so an explicitly
    # selected but unconfigured backend cannot look healthy.
    web_rows = []
    # See #78412.
    if "web" in available or any(item.get("name") == "web" for item in unavailable):
        web_rows = _doctor_web_capability_rows()
        if web_rows:
            available = [tid for tid in available if tid != "web"]
            unavailable = [item for item in unavailable if item.get("name") != "web"]
    for tid in available:
        check_ok(TOOLSET_REQUIREMENTS.get(tid, {}).get("name", tid))
    for status, label, detail in web_rows:
        (check_ok if status == "ok" else check_warn)(label, detail)
    for item in unavailable:
        env_vars = item.get("missing_vars") or item.get("env_vars") or []
        detail = f"(missing {', '.join(env_vars)})" if env_vars else _TOOLSET_SETUP_HINTS.get(item["name"], "(system dependency not met)")
        check_warn(item["name"], detail)
    # Only toolsets enabled for the CLI count toward the summary; default-off or
    # disabled toolsets may warn above but must not pollute it.
    api_disabled = _missing_api_key_toolsets_for_summary(unavailable)
    if api_disabled or any(status != "ok" for status, _, _ in web_rows):
        f.issues.append("Run 'hermes setup' to configure missing API keys for full tool access")
