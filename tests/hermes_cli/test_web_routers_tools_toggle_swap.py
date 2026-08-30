"""Swapped toolset pairs through the dashboard toggle (PUT endpoint).

``file`` and ``file_readonly`` are mutually exclusive: they share
read_file/search_files but disagree on write_file/patch, so persisting both
would quietly re-grant the write surface the operator removed by picking the
read-only variant. The dashboard's ``PUT /api/tools/toolsets/{name}`` must
apply the swap when enabling — same as ``hermes tools enable`` — so a toggle
is effective in both directions instead of leaving both variants saved.
"""

import pytest


class TestToggleToolsetSwappedPair:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, _isolate_hermes_home):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        import hermes_state
        from hermes_constants import get_hermes_home
        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

        monkeypatch.setattr(
            hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db"
        )
        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    def _saved_cli_toolsets(self):
        from hermes_cli.config import load_config

        cfg = load_config()
        return cfg.get("platform_toolsets", {}).get("cli", [])

    def test_enable_file_readonly_swaps_file_out(self):
        """Toggling file_readonly ON against a stock config (which resolves
        `file`) must not leave both variants enabled — that was the Codex
        P1: the save succeeded but write_file/patch stayed available."""
        resp = self.client.put(
            "/api/tools/toolsets/file_readonly", json={"enabled": True}
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

        cli = self._saved_cli_toolsets()
        assert "file_readonly" in cli
        assert "file" not in cli

    def test_enable_file_swaps_file_readonly_out(self):
        """Symmetric: re-granting writes on a read-only platform drops the
        mirror instead of the save's fail-closed tiebreak silently ignoring
        the toggle."""
        from hermes_cli.config import load_config, save_config

        cfg = load_config()
        cfg["platform_toolsets"] = {"cli": ["file_readonly", "web", "terminal"]}
        save_config(cfg)

        resp = self.client.put("/api/tools/toolsets/file", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

        cli = self._saved_cli_toolsets()
        assert "file" in cli
        assert "file_readonly" not in cli
        # Untouched siblings survive the swap.
        assert "web" in cli and "terminal" in cli
