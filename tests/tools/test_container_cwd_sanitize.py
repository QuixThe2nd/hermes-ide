"""E2E pins: a per-task cwd OVERRIDE that is a host path must NOT reach a
container-style plugin backend's environment builder.

The gateway/TUI registers the host launch dir as a cwd override; without the
sanitizer that raw path flows into the backend's working directory and the
sandbox either fails to start or silently sees an empty workspace (#50636 /
#54447). Built-in backends are local/ssh only, so the container path is
exercised through a registered plugin provider declaring ``is_container``.
"""

import tools.terminal_tool as tt
import tools.file_tools as ft
from agent.terminal_env_provider import TerminalEnvironmentProvider


class _PluginContainerProvider(TerminalEnvironmentProvider):
    name = "plugbox"
    display_name = "Plugbox"

    def is_available(self) -> bool:
        return True

    def create_environment(self, *, cwd, timeout, task_id="default", image=None,
                           container_config=None, **kwargs):
        return _DummyEnv(cwd)


class _DummyEnv:
    def __init__(self, cwd):
        self.cwd = cwd

    def execute(self, *a, **k):
        return {"output": "", "exit_code": 0}


def _register_provider(monkeypatch):
    import agent.terminal_env_registry as reg

    monkeypatch.setattr(reg, "_registry",
                        reg._registry.__class__(
                            label=reg._registry.label,
                            provider_cls=TerminalEnvironmentProvider,
                            logger=reg._registry.logger,
                        ))
    reg._registry.register(_PluginContainerProvider())


def _base_config(config_cwd):
    return {
        "env_type": "plugbox",
        "cwd": config_cwd,
        "host_cwd": None,
        "timeout": 180,
        "lifetime_seconds": 300,
        "persistent_shell": False,
        "local_persistent": False,
        "ssh_host": "",
        "ssh_user": "",
    }


def _capture_cwd(monkeypatch, override_cwd, config_cwd, drive):
    captured = {}
    config = _base_config(config_cwd)
    def fake_create_environment(env_type, image, cwd, timeout, **kwargs):
        captured["cwd"] = cwd
        return _DummyEnv(cwd)

    monkeypatch.setattr("tools.terminal_tool_backends._create_environment",
                        fake_create_environment)
    monkeypatch.setattr(tt, "_create_environment", fake_create_environment)
    def fake_create_environment(env_type, image, cwd, timeout, **kwargs):
        captured["cwd"] = cwd
        return _DummyEnv(cwd)

    _register_provider(monkeypatch)
    monkeypatch.setattr(tt, "_get_env_config", lambda: config)
    monkeypatch.setattr(tt, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(tt, "_pre_exec_block", lambda *a, **k: None)
    monkeypatch.setattr("tools.terminal_tool_backends._create_environment",
                        fake_create_environment)
    # Force a fresh environment build.
    monkeypatch.setattr(tt, "_active_environments", {})
    monkeypatch.setattr(tt, "_last_activity", {})

    task_id = "sess-host-cwd"
    tt.register_task_env_overrides(task_id, {"cwd": override_cwd})
    try:
        if drive == "terminal":
            tt.terminal_tool(command="pwd", task_id=task_id)
        else:
            monkeypatch.setattr(ft, "_file_ops_cache", {})
            monkeypatch.setattr(tt, "_session_cwd", {})
            ft._get_file_ops(task_id)
    finally:
        tt.clear_task_env_overrides(task_id)
        tt._active_environments.pop(task_id, None)
        tt._active_environments.pop("default", None)
    return captured.get("cwd")


class TestIsUnusableContainerCwd:
    def test_windows_backslash_host_path_rejected(self):
        from tools.terminal_tool_config import _is_unusable_container_cwd
        assert _is_unusable_container_cwd(r"C:\Users\someuser")

    def test_posix_home_host_path_rejected(self):
        from tools.terminal_tool_config import _is_unusable_container_cwd
        assert _is_unusable_container_cwd("/Users/me/workspace")
        assert not _is_unusable_container_cwd("/workspace")

    def test_plugin_container_flag(self, monkeypatch):
        """``_is_container_backend`` answers from the provider's declared flag."""
        from tools.terminal_tool_config import _is_container_backend

        _register_provider(monkeypatch)
        assert _is_container_backend("plugbox")
        assert not _is_container_backend("local")
        assert not _is_container_backend("ssh")


class TestOverrideCwdSanitizedAtCallSite:
    def test_windows_host_override_does_not_reach_container(self, monkeypatch):
        cwd = _capture_cwd(monkeypatch, r"C:\Users\someuser", "/root", "terminal")
        assert cwd == "/root", (
            f"Host-path cwd override leaked to the container builder: {cwd!r}. "
            "It must be sanitized back to config['cwd']."
        )

    def test_valid_container_override_is_preserved(self, monkeypatch):
        cwd = _capture_cwd(monkeypatch, "/workspace/task42", "/root", "terminal")
        assert cwd == "/workspace/task42"


class TestFileOpsCwdSanitizedAtCallSite:
    def test_macos_host_override_does_not_reach_container(self, monkeypatch):
        cwd = _capture_cwd(monkeypatch, "/Users/me/workspace", "/workspace", "file_ops")
        assert cwd == "/workspace", (
            f"Host-path cwd override leaked to the container builder: {cwd!r}. "
            "It must be sanitized back to config['cwd']."
        )

    def test_valid_container_override_is_preserved(self, monkeypatch):
        cwd = _capture_cwd(monkeypatch, "/workspace/task42", "/workspace", "file_ops")
        assert cwd == "/workspace/task42"
