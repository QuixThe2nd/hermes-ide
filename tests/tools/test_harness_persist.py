"""A tool harness launched from a service must outlive that service being stopped."""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.platforms("linux")


def test_detached_wrapper_is_not_a_py_file(tmp_path, monkeypatch):
    # The wrapper lands next to the stdout log — which can be a checkout root
    # (``.hermes-agent-cli.out`` from tools/agent_cli_runner.py). A ``*.py`` name
    # there is picked up by setup.py's root-module scan; the stem is not a valid
    # identifier and the wheel build fails. The wrapper runs as
    # ``sys.executable <path>``, so it needs no suffix at all.
    import tools.harness_persist as hp

    launched = {}

    def fake_run(cmd, **kwargs):
        launched["cmd"] = cmd

        class _Done:
            returncode = 0

        return _Done()

    monkeypatch.setattr(hp.subprocess, "run", fake_run)
    monkeypatch.setattr(hp, "active_service_main_pid", lambda unit: os.getpid())
    hp.launch_detached_service(
        ["/bin/true"], cwd=str(tmp_path), env={},
        stdout_path=tmp_path / ".hermes-agent-cli.out", stderr_path=tmp_path / "err",
    )
    wrapper = tmp_path / ".hermes-agent-cli.out.run"
    assert wrapper.is_file()
    assert not (tmp_path / ".hermes-agent-cli.out.run.py").exists()
    runner_arg = launched["cmd"][launched["cmd"].index("--") + 2]
    assert runner_arg == str(wrapper)
    assert not runner_arg.endswith(".py")


def test_system_manager_denial_falls_back_to_user_manager(tmp_path, monkeypatch):
    # A gateway running as a *user* service is refused by polkit on the system
    # manager ("Access denied"), which used to kill every detached harness in 0s.
    import tools.harness_persist as hp

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "--user" not in cmd:
            raise subprocess.CalledProcessError(1, cmd, "", "Failed to start transient service unit: Access denied")

        class _Done:
            returncode = 0

        return _Done()

    monkeypatch.setattr(hp.subprocess, "run", fake_run)
    monkeypatch.setattr(hp, "active_service_main_pid", lambda unit: os.getpid())
    launched = hp.launch_detached_service(
        ["/bin/true"], cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin"},
        stdout_path=tmp_path / "out", stderr_path=tmp_path / "err",
    )
    assert launched["pid"] == os.getpid()
    assert len(calls) == 2
    assert calls[0][0] == "systemd-run" and "--user" not in calls[0]
    assert calls[1][:2] == ["systemd-run", "--user"]
    assert calls[1][2:] == calls[0][1:]


def test_both_managers_failing_raises_the_system_error(tmp_path, monkeypatch):
    import tools.harness_persist as hp

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, "", "user" if "--user" in cmd else "system")

    monkeypatch.setattr(hp.subprocess, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        hp.launch_detached_service(
            ["/bin/true"], cwd=str(tmp_path), env={},
            stdout_path=tmp_path / "out", stderr_path=tmp_path / "err",
        )
    assert excinfo.value.stderr == "system"

def _stop(unit: str) -> None:
    subprocess.run(["systemctl", "stop", unit], check=False, timeout=30)


def test_detached_harness_survives_caller_unit_stop():
    from tools.harness_persist import active_service_main_pid, launch_detached_service

    caller = "probe-harness-persist-test.service"
    work = Path(tempfile.mkdtemp(prefix="harness-persist-test-", dir="/tmp"))
    receipt = work / "receipt"
    sibling = work / "sibling"
    worker = work / "worker.py"
    worker.write_text(
        "import sys, time\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, %r)\n"
        "from tools.harness_persist import launch_detached_service\n"
        "work = Path(%r)\n"
        "sibling = subprocess_pid = None\n"
        "import subprocess\n"
        "child = subprocess.Popen(['/usr/bin/sleep', '90'])\n"
        "launched = launch_detached_service(\n"
        "    ['/usr/bin/sleep', '90'], cwd=str(work), env={'PATH': '/usr/bin:/bin'},\n"
        "    stdout_path=work / 'out', stderr_path=work / 'err',\n"
        ")\n"
        "Path(%r).write_text(launched['unit'] + '\\n' + str(launched['pid']) + '\\n' + launched['cgroup'] + '\\n' + str(child.pid))\n"
        "time.sleep(120)\n" % (str(Path(__file__).resolve().parents[2]), str(work), str(receipt))
    )
    try:
        subprocess.run(
            [
                "systemd-run", "--collect", f"--unit={caller}", "--property=KillMode=mixed",
                "--property=Type=exec", f"--working-directory={work}", "--",
                sys.executable, str(worker),
            ],
            check=True, capture_output=True, text=True, timeout=30,
        )
        deadline = time.time() + 20
        while time.time() < deadline and not receipt.exists():
            time.sleep(0.2)
        assert receipt.exists(), "caller did not launch the harness"
        unit, pid, cgroup, sibling_pid = receipt.read_text().splitlines()
        pid = int(pid)
        sibling_pid = int(sibling_pid)
        assert not cgroup.endswith(f"/{caller}")
        _stop(caller)
        time.sleep(1)
        assert Path(f"/proc/{sibling_pid}").exists() is False
        assert Path(f"/proc/{pid}").exists()
        assert active_service_main_pid(unit) == pid
        assert Path(f"/proc/{pid}/cgroup").read_text().strip().endswith(f"/{unit}")
    finally:
        if receipt.exists():
            _stop(receipt.read_text().splitlines()[0])
        _stop(caller)
