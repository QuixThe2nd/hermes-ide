"""C16: web dashboard memory-provider setup must install declared provider
extras through pm's sync authority (plugin.yaml ``extra:`` / materialized
legacy ``python_dependencies``), never ``tools.lazy_deps`` or raw pip, and
must report restart-required truthfully when the boot-selected environment
can't see a fresh install."""

from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hermes_cli.web_routers import memory_providers as mp


@pytest.fixture()
def routed(monkeypatch):
    """Mutable harness over the module-level collaborators."""
    state = SimpleNamespace(
        importable=set(),          # dep names that import cleanly
        manifest={},
        plugin_dir=None,
        sync_calls=[],             # (extras, explicit)
        materialized=[],           # plugin_dir args
        sync_error=None,
    )

    monkeypatch.setattr(mp, "_memory_provider_manifest", lambda name: state.manifest)

    import plugins.memory as plugins_memory
    monkeypatch.setattr(
        plugins_memory, "find_provider_dir", lambda name: state.plugin_dir,
        raising=False,
    )

    import hermes_cli.web_server_memory as wsm
    _IMPORT_OF = {"mem0ai": "mem0", "honcho-ai": "honcho"}

    def fake_importable(dep):
        package = dep.split("[")[0]
        for ch in "<>=!~;":
            package = package.split(ch)[0]
        return _IMPORT_OF.get(package.strip(), package.strip().replace("-", "_")) in state.importable

    monkeypatch.setattr(mp, "_dependency_importable", fake_importable)

    import pm
    def fake_sync_venv(extras=None, *, explicit=False, plugin_dirs=None):
        state.sync_calls.append((extras, explicit))
        state.materialized.extend(plugin_dirs or [])
        if state.sync_error:
            raise state.sync_error
        # A successful sync makes freshly installed dists importable in-process.
        if getattr(state, "sync_effect", True):
            state.importable |= {"mem0", "honcho", "freshpkg"}

    monkeypatch.setattr(pm, "sync_venv", fake_sync_venv)

    return state


class TestDeclaredExtraRouting:
    def test_extra_synced_explicitly_when_import_missing(self, routed):
        routed.manifest = {"extra": "mem0"}
        routed.importable = set()

        rows = mp._install_memory_provider_pip_dependencies("mem0", ["mem0ai"])

        assert routed.sync_calls == [(["mem0"], True)]
        assert rows[0]["status"] == "installed"

    def test_no_sync_when_everything_imports(self, routed):
        routed.manifest = {"extra": "mem0"}
        routed.importable = {"mem0"}

        rows = mp._install_memory_provider_pip_dependencies("mem0", ["mem0ai"])

        assert routed.sync_calls == []
        assert rows[0]["status"] == "already_installed"

    def test_sync_failure_reports_failed_row(self, routed):
        routed.manifest = {"extra": "mem0"}
        routed.sync_error = RuntimeError("sealed venv refuses extras")

        rows = mp._install_memory_provider_pip_dependencies("mem0", ["mem0ai"])

        assert rows[0]["status"] == "failed"
        assert "sealed venv" in rows[0]["stderr"]


class TestLegacyPythonDependencies:
    def test_third_party_python_dependencies_bridge(self, routed, tmp_path):
        routed.manifest = {"python_dependencies": ["freshpkg"]}
        routed.plugin_dir = tmp_path / "external-provider"
        routed.plugin_dir.mkdir()
        (routed.plugin_dir / "plugin.yaml").write_text("name: ext\npython_dependencies: [freshpkg]\n")
        routed.importable = set()

        rows = mp._install_memory_provider_pip_dependencies("ext", ["freshpkg"])

        assert routed.materialized == [routed.plugin_dir]
        assert routed.sync_calls == [(None, True)]
        assert rows[0]["status"] == "installed"

    def test_legacy_specs_without_plugin_dir_fail_honestly(self, routed):
        routed.manifest = {"python_dependencies": ["freshpkg"]}
        routed.plugin_dir = None

        rows = mp._install_memory_provider_pip_dependencies("ext", ["freshpkg"])

        assert routed.sync_calls == []
        assert rows[0]["status"] == "failed"


class TestRestartTruthfulness:
    def test_install_that_boot_env_cannot_see_is_restart_required(self, routed):
        routed.manifest = {"extra": "mem0"}
        routed.importable = set()
        # Sync succeeds but the freshly installed dist stays invisible
        # to this boot-selected interpreter.
        routed.sync_effect = False

        rows = mp._install_memory_provider_pip_dependencies("mem0", ["mem0ai"])

        assert rows[0]["status"] == "restart_required"
        assert rows[0]["status"] != "failed"

    def test_setup_result_counts_restart_required_as_not_failed(self, routed, monkeypatch):
        routed.manifest = {"extra": "mem0"}
        monkeypatch.setattr(
            mp, "_install_memory_provider_pip_dependencies",
            lambda name, deps: [{"kind": "pip", "name": "mem0ai", "status": "restart_required",
                                 "command": "hermes pm install mem0", "returncode": None,
                                 "stdout": "", "stderr": ""}],
        )
        monkeypatch.setattr(mp, "_memory_provider_setup_manifest",
                            lambda name: {"pip_dependencies": ["mem0ai"], "external_dependencies": [],
                                          "required_env": []})
        monkeypatch.setattr(mp, "_discover_memory_provider_statuses", lambda: {})

        result = mp._install_memory_provider_setup("mem0")

        assert result["ok"] is True
