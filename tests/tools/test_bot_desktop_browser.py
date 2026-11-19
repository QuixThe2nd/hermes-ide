"""Tests for Bot Desktop's human-facing Chromium launcher."""

from __future__ import annotations

from tools.bot_desktop import browser, runtime


def test_dock_launch_uses_the_profile_persistent_browser(tmp_path, monkeypatch):
    exe = tmp_path / "chrome"
    monkeypatch.setattr(browser, "executable", lambda: str(exe))
    monkeypatch.setattr(runtime, "state_dir", lambda: tmp_path / "bot-desktop")

    assert browser.dock_launch() == (str(exe), str(tmp_path / "bot-desktop" / "browser-profile"))


def test_dock_launch_is_absent_without_chromium(monkeypatch):
    monkeypatch.setattr(browser, "executable", lambda: None)
    assert browser.dock_launch() is None


def _install_browsers(tmp_path, monkeypatch, *, playwright: bool, system: bool):
    """A Playwright build under a private PLAYWRIGHT_BROWSERS_PATH and/or a system chromium on PATH."""
    roots = tmp_path / "pw"
    roots.mkdir(parents=True)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(roots))
    monkeypatch.setattr(browser, "_chromium_search_roots", lambda: [str(roots)])
    pw_exe = roots / "chromium-1200" / "chrome-linux" / "chrome"
    if playwright:
        pw_exe.parent.mkdir(parents=True)
        pw_exe.write_text("#!/bin/sh\n", encoding="utf-8")
        pw_exe.chmod(0o755)
    sys_exe = tmp_path / "bin" / "chromium"
    if system:
        sys_exe.parent.mkdir(parents=True)
        sys_exe.write_text("#!/bin/sh\n", encoding="utf-8")
        sys_exe.chmod(0o755)
    monkeypatch.setattr("shutil.which", lambda name, *a, **k: str(sys_exe) if system and name == "chromium" else None)
    return str(pw_exe), str(sys_exe)


def test_unprivileged_user_under_apparmor_userns_restriction_gets_the_system_browser(tmp_path, monkeypatch):
    """A distro Chromium wins where Playwright's bundle cannot start its sandbox."""
    pw_exe, sys_exe = _install_browsers(tmp_path, monkeypatch, playwright=True, system=True)
    monkeypatch.setattr(browser.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(browser, "_userns_restricted", lambda: True)
    assert browser.executable() == sys_exe

    monkeypatch.setattr(browser, "_userns_restricted", lambda: False)
    assert browser.executable() == pw_exe

    _install_browsers(tmp_path / "only-pw", monkeypatch, playwright=True, system=False)
    monkeypatch.setattr(browser, "_userns_restricted", lambda: True)
    exe = browser.executable()
    assert exe and exe.endswith("chrome-linux/chrome")
    assert "--no-sandbox" not in browser.dock_argv(exe, "/p/dir")


def test_root_dock_browser_uses_sandbox_bypass_flags(monkeypatch):
    monkeypatch.setattr(browser.os, "geteuid", lambda: 0)
    argv = browser.dock_argv("/opt/chrome", "/p/dir")
    assert {"--no-sandbox", "--disable-dev-shm-usage"} <= set(argv)
    assert not any(arg.startswith("--remote-debugging") for arg in argv)


def test_status_reports_the_headed_browser_or_its_absence(monkeypatch):
    """The official image ships only chromium_headless_shell: executable() is None and the dock silently
    has no Browser icon. Status must say so instead of leaving the pane to guess."""
    monkeypatch.setattr(runtime, "_launcher_pid", lambda: None)
    monkeypatch.setattr(runtime, "published_env", lambda: {})
    monkeypatch.setattr(runtime, "geometry", lambda: "1440x900")
    monkeypatch.setattr(browser, "executable", lambda: None)
    assert runtime.status().as_dict()["browser"] is None
    monkeypatch.setattr(browser, "executable", lambda: "/usr/bin/chromium")
    assert runtime.status().browser == "/usr/bin/chromium"


def test_dock_exec_line_survives_spaces_in_the_executable_and_profile_paths():
    """The launcher used to split the shell line on the first space to find the executable, so a
    Chromium under '/opt/Google Chrome/' or a profile under a spaced HERMES_HOME broke the dock icon.
    Exec= follows the Desktop Entry spec: each argument double-quoted, with the reserved characters
    backslash-escaped inside the quotes."""
    exe = "/opt/Google Chrome/chrome"
    profile = '/home/a b/.hermes/browser "x"/profile'
    line = browser.dock_exec_line(exe, profile)
    assert line.startswith('Exec="/opt/Google Chrome/chrome" ')
    assert r'"--user-data-dir=/home/a b/.hermes/browser \\"x\\"/profile"' in line  # spec: \" quoted, then \ string-escaped
    assert not any(arg.startswith("--remote-debugging") for arg in browser.dock_argv(exe, profile))
