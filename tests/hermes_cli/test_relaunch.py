"""Tests for hermes_cli.relaunch — unified self-relaunch utility."""

import sys

import pytest

from hermes_cli import relaunch as relaunch_mod


class TestResolveHermesBin:
    def test_prefers_absolute_argv0_when_executable(self, monkeypatch):
        fake = "/nix/store/abc/bin/hermes"
        monkeypatch.setattr(sys, "argv", [fake])
        monkeypatch.setattr(relaunch_mod.os.path, "isfile", lambda p: p == fake)
        monkeypatch.setattr(relaunch_mod.os, "access", lambda p, mode: p == fake)
        assert relaunch_mod.resolve_hermes_bin() == fake

    def test_resolves_relative_argv0(self, monkeypatch, tmp_path):
        fake = tmp_path / "hermes"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        monkeypatch.setattr(sys, "argv", [str(fake.name)])
        monkeypatch.chdir(tmp_path)
        # Ensure we don't accidentally match a real 'hermes' on PATH
        monkeypatch.setattr(relaunch_mod.shutil, "which", lambda _name: None)
        assert relaunch_mod.resolve_hermes_bin() == str(fake)

    def test_falls_back_to_path_which(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["-c"])  # not a real path
        monkeypatch.setattr(
            relaunch_mod.shutil, "which", lambda name: "/usr/bin/hermes" if name == "hermes" else None
        )
        assert relaunch_mod.resolve_hermes_bin() == "/usr/bin/hermes"


def test_needs_interpreter_case_insensitive_match(tmp_path, monkeypatch):
    """An own-venv shebang is safe even when its path contains uppercase."""
    venv_bin = tmp_path / "MyEnv" / "bin"
    venv_bin.mkdir(parents=True)
    interpreter = venv_bin / "python"
    interpreter.write_text("", encoding="utf-8")

    console_script = venv_bin / "hermes"
    console_script.write_text(f"#!{interpreter}\nimport hermes_cli\n", encoding="utf-8")
    monkeypatch.setattr(relaunch_mod.sys, "executable", str(interpreter))

    assert relaunch_mod._needs_interpreter(console_script) is False


def test_needs_interpreter_rejects_sibling_directory(tmp_path, monkeypatch):
    """A sibling such as ``bin-extra`` is not the interpreter's ``bin`` directory."""
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    interp = venv_bin / "python"
    interp.write_text("", encoding="utf-8")
    monkeypatch.setattr(relaunch_mod.sys, "executable", str(interp))

    sibling_script = tmp_path / "sibling"
    sibling_script.write_text(
        f"#!{tmp_path}/venv/bin-extra/python\nimport hermes_cli\n",
        encoding="utf-8",
    )
    assert relaunch_mod._needs_interpreter(sibling_script) is True


def test_needs_interpreter_strips_flags_before_comparing(tmp_path, monkeypatch):
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    interp = venv_bin / "python"
    interp.write_text("", encoding="utf-8")
    monkeypatch.setattr(relaunch_mod.sys, "executable", str(interp))

    flagged = tmp_path / "flagged"
    flagged.write_text(f"#!{interp} -S\nimport hermes_cli\n", encoding="utf-8")
    assert relaunch_mod._needs_interpreter(flagged) is False


def test_needs_interpreter_env_shebang_always_escapes(tmp_path, monkeypatch):
    """PATH-resolved ``env`` escapes; an absolute interpreter after ``-S`` does not."""
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    interp = venv_bin / "python"
    interp.write_text("", encoding="utf-8")
    monkeypatch.setattr(relaunch_mod.sys, "executable", str(interp))

    env_script = tmp_path / "envscript"
    env_script.write_text(
        f"#!{venv_bin}/env python3\nimport hermes_cli\n", encoding="utf-8"
    )
    assert relaunch_mod._needs_interpreter(env_script) is True

    env_abs = tmp_path / "envabs"
    env_abs.write_text(
        f"#!/usr/bin/env -S {interp}\nimport hermes_cli\n", encoding="utf-8"
    )
    assert relaunch_mod._needs_interpreter(env_abs) is False


def test_head_helpers_recognize_native_launchers_and_missing_files(tmp_path):
    native = tmp_path / "hermes"
    native.write_bytes(b"\x7fELFpayload")
    assert relaunch_mod._read_head(native, size=4) == b"\x7fELF"
    assert relaunch_mod._is_native_binary(relaunch_mod._read_head(native)) is True
    assert relaunch_mod._read_head(tmp_path / "missing") is None
    assert relaunch_mod._shebang_tokens("#!/usr/bin/env -S python3") == [
        "/usr/bin/env", "-S", "python3",
    ]


class TestExtractInheritedFlags:
    def test_extracts_model_and_provider(self):
        argv = ["--model", "gpt", "--provider", "openrouter", "chat"]
        assert relaunch_mod._extract_inherited_flags(argv) == [
            "--model", "gpt", "--provider", "openrouter",
        ]

    def test_preserves_multiple_skills(self):
        argv = ["-s", "foo", "-s", "bar"]
        assert relaunch_mod._extract_inherited_flags(argv) == ["-s", "foo", "-s", "bar"]


class TestInheritedFlagTable:
    """Sanity-check the argparse-introspected table that drives extraction."""

    def test_short_and_long_aliases_are_paired(self):
        table = dict(relaunch_mod._INHERITED_FLAGS_TABLE)
        # Each pair declared together in the parser shares takes_value.
        for short, long_ in [
            ("-p", "--profile"),
            ("-m", "--model"),
            ("-s", "--skills"),
        ]:
            assert table[short] == table[long_], f"{short}/{long_} disagree"


    def test_excluded_flags_are_not_inherited(self):
        table = dict(relaunch_mod._INHERITED_FLAGS_TABLE)
        # --worktree creates a new worktree per process; inheriting would
        # orphan the parent's. Chat-only flags (--quiet/-Q, --verbose/-v,
        # --source) can't be in argv at the existing relaunch callsites.
        for flag in ["-w", "--worktree", "-Q", "--quiet", "-v", "--verbose", "--source"]:
            assert flag not in table, f"{flag} should not be inherited"


class TestBuildRelaunchArgv:
    def test_uses_bin_when_available(self, monkeypatch):
        monkeypatch.setattr(relaunch_mod, "resolve_hermes_bin", lambda: "/usr/bin/hermes")
        argv = relaunch_mod.build_relaunch_argv(["--resume", "abc"])
        assert argv[0] == "/usr/bin/hermes"


    def test_preserves_inherited_flags(self, monkeypatch):
        monkeypatch.setattr(relaunch_mod, "resolve_hermes_bin", lambda: "/usr/bin/hermes")
        original = ["--model", "gpt", "--profile", "work", "sessions", "browse"]
        argv = relaunch_mod.build_relaunch_argv(["--resume", "abc"], original_argv=original)
        assert "--model" in argv
        assert "gpt" in argv
        assert "--profile" in argv
        assert "work" in argv
        assert "--resume" in argv
        assert "abc" in argv
        # The original subcommand should not survive
        assert "sessions" not in argv
        assert "browse" not in argv

    def test_can_disable_preserve(self, monkeypatch):
        monkeypatch.setattr(relaunch_mod, "resolve_hermes_bin", lambda: "/usr/bin/hermes")
        original = ["--model", "gpt", "chat"]
        argv = relaunch_mod.build_relaunch_argv(
            ["--resume", "abc"], preserve_inherited=False, original_argv=original
        )
        assert "--model" not in argv
        assert argv == ["/usr/bin/hermes", "--resume", "abc"]


class TestRelaunch:
    @pytest.mark.platforms("linux")
    def test_calls_execvp(self, monkeypatch):
        calls = []

        def fake_execvp(path, argv):
            calls.append((path, argv))
            raise SystemExit(0)

        monkeypatch.setattr(relaunch_mod.os, "execvp", fake_execvp)
        monkeypatch.setattr(relaunch_mod, "resolve_hermes_bin", lambda: "/usr/bin/hermes")

        with pytest.raises(SystemExit):
            relaunch_mod.relaunch(["--resume", "abc"])

        assert calls == [("/usr/bin/hermes", ["/usr/bin/hermes", "--resume", "abc"])]



class TestResolveHermesBinLauncherGuards:
    def test_posix_python_launcher_falls_through_to_path(self, monkeypatch, tmp_path):
        """A Python source launcher must not be re-executed through its shebang.

        The git installer invokes ``hermes`` with the venv interpreter while the
        source launcher's shebang uses ``/usr/bin/env python3``.  Re-executing
        that source file directly can therefore escape the venv and lose core
        dependencies such as PyYAML.
        """
        script = tmp_path / "hermes"
        script.write_text("#!/usr/bin/env python3\n")
        script.chmod(0o755)
        wrapper = tmp_path / "bin" / "hermes"
        wrapper.parent.mkdir()
        wrapper.write_text("#!/usr/bin/env bash\n")
        wrapper.chmod(0o755)
        monkeypatch.setattr(relaunch_mod.sys, "argv", [str(script), "chat"])
        monkeypatch.setattr(
            relaunch_mod.shutil, "which",
            lambda name: str(wrapper) if name == "hermes" else None,
        )
        assert relaunch_mod.resolve_hermes_bin() == str(wrapper)

    def test_posix_python_launcher_on_path_falls_back_to_current_python(
        self, monkeypatch, tmp_path
    ):
        """PATH must not re-select the Python launcher rejected from argv[0]."""
        script = tmp_path / "hermes"
        script.write_text("#!/usr/bin/env python3\n")
        script.chmod(0o755)
        monkeypatch.setattr(relaunch_mod.sys, "argv", [str(script), "chat"])
        monkeypatch.setattr(
            relaunch_mod.shutil,
            "which",
            lambda name: str(script) if name == "hermes" else None,
        )

        assert relaunch_mod.resolve_hermes_bin() is None

        # A console script pinned to the running interpreter keeps the venv: still exec-able.
        pinned = tmp_path / "pinned" / "hermes"
        pinned.parent.mkdir()
        pinned.write_text(f"#!{sys.executable}\n")
        pinned.chmod(0o755)
        monkeypatch.setattr(relaunch_mod.sys, "argv", [str(pinned), "chat"])
        assert relaunch_mod.resolve_hermes_bin() == str(pinned)

