"""Bot Desktop package install: sudo hand-off and single-flight invariants."""

from __future__ import annotations

import threading

import pytest

from tools.bot_desktop import install, runtime


@pytest.fixture(autouse=True)
def _isolated_host(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(install, "_sudo_nopasswd", lambda: False)
    monkeypatch.setattr(runtime, "is_supported_host", lambda: True)
    monkeypatch.setattr(runtime, "install_command", lambda: "sudo apt-get install -y tigervnc-standalone-server")
    yield
    install._running.clear()


def test_empty_password_cancels_without_spawning(monkeypatch):
    monkeypatch.setattr(install.subprocess, "Popen", lambda *a, **k: pytest.fail("package manager spawned"))
    lines: list[str] = []
    code = install.install_packages(ask_password=lambda: "", on_line=lines.append)
    assert code == -1
    assert any("cancelled" in line for line in lines)


def test_second_install_for_same_profile_is_refused(monkeypatch):
    gate = threading.Event()
    entered = threading.Event()

    def slow_run(cmd, *, ask_password, on_line, timeout_seconds):
        entered.set()
        gate.wait(5)
        return 0

    monkeypatch.setattr(install, "_run", slow_run)
    worker = threading.Thread(target=install.install_packages, kwargs={"ask_password": lambda: "pw", "on_line": lambda _l: None})
    worker.start()
    assert entered.wait(5)
    with pytest.raises(install.InstallBusy):
        install.install_packages(ask_password=lambda: "pw", on_line=lambda _l: None)
    gate.set()
    worker.join(5)
    install.assert_not_running()  # slot released once the run finishes


def test_claim_is_atomic_and_refuses_a_second_claim():
    """The gateway claims BEFORE spawning its worker; a second Install click must fail at claim time, not
    pass a read-only check and race the worker for the slot."""
    key = install.claim()
    with pytest.raises(install.InstallBusy):
        install.claim()
    with pytest.raises(install.InstallBusy):
        install.install_packages(ask_password=lambda: "pw", on_line=lambda _l: None)
    install.release(key)
    install.claim()  # free again
    install.release(key)


@pytest.mark.linux_only
def test_timeout_kills_the_package_managers_whole_process_group(monkeypatch):
    """sudo forks the package manager into the same (new) session; killing sudo alone leaves apt/dnf
    holding the dpkg lock as root. The timeout must take the group."""
    import subprocess
    import time

    monkeypatch.setattr(install, "_sudo_nopasswd", lambda: True)
    # stand-in for `sudo apt-get ...`: a parent that spawns a child and waits, both in the new session
    fake = ["sudo"]
    real_popen = subprocess.Popen

    def popen(argv, **kw):
        if argv[:1] != fake:
            return real_popen(argv, **kw)
        return real_popen(["bash", "-c", "sleep 30 >/dev/null 2>&1 & echo child $!; wait"], **kw)

    monkeypatch.setattr(install.subprocess, "Popen", popen)
    lines: list[str] = []
    code = install._run("sudo apt-get install -y x", ask_password=lambda: "", on_line=lines.append, timeout_seconds=0.5)
    assert code != 0
    child = next(int(line.split()[1]) for line in lines if line.startswith("child "))
    from pathlib import Path

    def gone() -> bool:  # /proc-based: a reparented orphan sits outside our subtree, where os.kill(pid, 0) is guarded
        try:
            return "Z" in (Path(f"/proc/{child}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split() or ["Z"])[0]
        except OSError:
            return True

    for _ in range(50):  # the child must die with the group, not linger reparented to init
        if gone():
            break
        time.sleep(0.1)
    else:
        subprocess.run(["kill", "-9", str(child)], check=False)
        pytest.fail("grandchild survived the install timeout")


def test_passwordless_sudo_runs_the_install_without_asking_for_a_password(monkeypatch):
    """A host with NOPASSWD sudo must not pop the masked password card — the card blocks the install
    for up to five minutes on an answer nobody needs to give — and the package command still runs
    and streams its output."""
    monkeypatch.setattr(install, "_sudo_nopasswd", lambda: True)
    spawned: list[list[str]] = []

    class _Proc:
        pid = 4242
        stdin = __import__("io").StringIO()

        def __init__(self, argv, **kw):
            spawned.append(argv)
            self.stdout = iter(["Reading package lists...\n", "Done\n"])

        def wait(self):
            return 0

    monkeypatch.setattr(install.subprocess, "Popen", _Proc)
    monkeypatch.setattr(install.os, "killpg", lambda *a: None)
    lines: list[str] = []
    code = install.install_packages(ask_password=lambda: pytest.fail("sudo password asked despite NOPASSWD"),
                                    on_line=lines.append)
    assert code == 0
    assert spawned and spawned[0][:1] == ["sudo"] and "-S" not in spawned[0]
    assert "Done" in lines
