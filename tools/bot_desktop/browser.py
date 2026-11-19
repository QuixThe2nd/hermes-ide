"""The human-facing browser on Bot Desktop, with one persistent profile per Hermes profile."""

from __future__ import annotations

import glob
import os
import shutil
import sys
from pathlib import Path
from typing import Optional, Tuple

from tools.bot_desktop import runtime

_SYSTEM_BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
_CHROMIUM_SANDBOX_BYPASS_ARGS = ("--no-sandbox", "--disable-dev-shm-usage")


def profile_dir() -> Path:
    """Persistent user-data directory for this profile's desktop browser."""
    return runtime.state_dir() / "browser-profile"


def executable() -> Optional[str]:
    """Return the newest Playwright Chromium, else a system Chrome/Chromium.

    Non-root under ``kernel.apparmor_restrict_unprivileged_userns=1`` prefers a distro browser:
    Playwright's bundle has no setuid ``chrome_sandbox`` and cannot start its sandbox there.
    """
    finders = [_playwright_executable, _system_executable]
    if not _is_root() and _userns_restricted():
        finders.reverse()
    return next((exe for find in finders if (exe := find())), None)


def _chromium_search_roots() -> list[str]:
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    home = os.path.expanduser("~")
    roots = [env_path] if env_path and env_path != "0" else []
    roots.append(os.path.join(home, ".cache", "ms-playwright"))
    if sys.platform == "darwin":
        roots.append(os.path.join(home, "Library", "Caches", "ms-playwright"))
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        roots.append(os.path.join(local, "ms-playwright"))
    return roots


def _playwright_executable() -> Optional[str]:
    candidates = sorted(
        (p for root in _chromium_search_roots() for p in glob.glob(os.path.join(root, "chromium-*", "chrome-linux*", "chrome"))),
        key=os.path.getmtime, reverse=True)
    return next((exe for exe in candidates if os.access(exe, os.X_OK)), None)


def _system_executable() -> Optional[str]:
    return next((shutil.which(name) for name in _SYSTEM_BROWSERS if shutil.which(name)), None)


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _userns_restricted() -> bool:
    try:
        with open("/proc/sys/kernel/apparmor_restrict_unprivileged_userns", encoding="utf-8-sig") as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def dock_launch() -> Optional[Tuple[str, str]]:
    """``(executable, user_data_dir)`` for the dock's Browser icon, or ``None`` when no Chromium exists."""
    exe = executable()
    return (exe, str(profile_dir())) if exe else None


def dock_argv(exe: str, user_data_dir: str) -> list[str]:
    """Command the dock's Browser icon runs without first-run/default-browser dialogs."""
    # --test-type hides automation and unsupported-root-flag infobars in the human's takeover view.
    return [exe, f"--user-data-dir={user_data_dir}", "--no-first-run",
            "--no-default-browser-check", "--test-type", *(_CHROMIUM_SANDBOX_BYPASS_ARGS if _is_root() else ())]


def dock_exec_line(exe: str, user_data_dir: str) -> str:
    """The ``Exec=`` line of the dock's ``.desktop`` entry. Every argument is double-quoted per the
    Desktop Entry spec (a browser under ``/opt/Google Chrome/`` or a profile under a spaced HERMES_HOME
    otherwise splits into garbage): inside the quotes ``" ` $ \\`` are backslash-escaped, and because the
    value is itself a string field, each of those backslashes is escaped once more."""
    def quote(arg: str) -> str:
        quoted = "".join("\\" + ch if ch in '"`$\\' else ch for ch in arg)
        return '"' + quoted.replace("\\", "\\\\") + '"'
    return "Exec=" + " ".join(quote(arg) for arg in dock_argv(exe, user_data_dir))


