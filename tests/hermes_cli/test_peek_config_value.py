"""Programmatic config reads return values without CLI output or exits."""


def _set_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)


def test_peek_returns_stored_value(tmp_path, monkeypatch):
    from hermes_cli.config import peek_config_value

    _set_home(tmp_path, monkeypatch)
    (tmp_path / "config.yaml").write_text(
        "security:\n  allow_lazy_installs: false\n", encoding="utf-8"
    )

    assert peek_config_value("security.allow_lazy_installs", True) is False


def test_peek_returns_default_for_unset_key(tmp_path, monkeypatch):
    from hermes_cli.config import peek_config_value

    _set_home(tmp_path, monkeypatch)

    assert peek_config_value("custom.unset_key", "fallback") == "fallback"


def test_lazy_installs_allowed_respects_disabled_config(tmp_path, monkeypatch):
    from pm.ensure import lazy_installs_allowed

    _set_home(tmp_path, monkeypatch)
    (tmp_path / "config.yaml").write_text(
        "security:\n  allow_lazy_installs: false\n", encoding="utf-8"
    )

    assert lazy_installs_allowed() is False


def test_lazy_installs_allowed_defaults_to_enabled(tmp_path, monkeypatch):
    from pm.ensure import lazy_installs_allowed

    _set_home(tmp_path, monkeypatch)

    assert lazy_installs_allowed() is True


def test_lazy_installs_allowed_honors_disable_env(tmp_path, monkeypatch):
    from pm.ensure import lazy_installs_allowed

    _set_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")

    assert lazy_installs_allowed() is False


def test_peek_never_prints(tmp_path, monkeypatch, capsys):
    from hermes_cli.config import peek_config_value

    _set_home(tmp_path, monkeypatch)
    (tmp_path / "config.yaml").write_text(
        "security:\n  allow_lazy_installs: false\n", encoding="utf-8"
    )

    assert peek_config_value("security.allow_lazy_installs", True) is False
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
