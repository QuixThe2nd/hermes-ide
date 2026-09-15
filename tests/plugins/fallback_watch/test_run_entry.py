"""run.py entrypoint contracts: gating, validation, and no-send paths."""

from __future__ import annotations

from pathlib import Path

from plugins.fallback_watch.run import main
from tests.plugins.fallback_watch._helpers import write_home


def _config_path(home: Path) -> Path:
    return home / "config.yaml"


class TestDisabledGate:
    def test_disabled_config_exits_zero_without_watching(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        home = write_home(
            tmp_path,
            config={"fallback_watch": {"enabled": False, "chat_id": "123"}},
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        # a missing agent.log proves the watcher never started tailing
        exit_code = main(["--config", str(_config_path(home))])
        assert exit_code == 0
        assert "disabled" in capsys.readouterr().out
        assert not (home / "logs" / "agent.log").exists()

    def test_absent_section_exits_zero(self, tmp_path: Path, monkeypatch):
        home = write_home(tmp_path, config={})
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert main(["--config", str(_config_path(home))]) == 0


class TestUnconfigured:
    def test_enabled_without_chat_id_exits_one_with_message(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        home = write_home(tmp_path, config={"fallback_watch": {"enabled": True}})
        monkeypatch.setenv("HERMES_HOME", str(home))
        exit_code = main(["--config", str(_config_path(home))])
        assert exit_code == 1
        assert "chat_id" in capsys.readouterr().err

    def test_enabled_with_unsupported_platform_exits_one(
        self, tmp_path: Path, monkeypatch
    ):
        home = write_home(
            tmp_path,
            config={
                "fallback_watch": {"enabled": True, "chat_id": "1", "platform": "sms"}
            },
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert main(["--config", str(_config_path(home))]) == 1

    def test_missing_token_fails_fast_without_watching(
        self, tmp_path: Path, monkeypatch, capsys
    ):
        home = write_home(
            tmp_path,
            config={"fallback_watch": {"enabled": True, "chat_id": "123"}},
            env=None,
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        exit_code = main(["--config", str(_config_path(home))])
        assert exit_code == 1
        assert "DISCORD_BOT_TOKEN" in capsys.readouterr().err
        assert not (home / "logs" / "agent.log").exists()


class TestCheckMode:
    def test_check_ok_when_configured(self, tmp_path: Path, monkeypatch, capsys):
        home = write_home(
            tmp_path, config={"fallback_watch": {"enabled": True, "chat_id": "123"}}
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        exit_code = main(["--config", str(_config_path(home)), "--check"])
        assert exit_code == 0
        assert "ok" in capsys.readouterr().out

    def test_check_disabled_reports_disabled(self, tmp_path: Path, monkeypatch):
        home = write_home(tmp_path, config={})
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert main(["--config", str(_config_path(home)), "--check"]) == 0

    def test_check_missing_token_exits_two(self, tmp_path: Path, monkeypatch):
        home = write_home(
            tmp_path,
            config={"fallback_watch": {"enabled": True, "chat_id": "123"}},
            env=None,
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert main(["--config", str(_config_path(home)), "--check"]) == 2
