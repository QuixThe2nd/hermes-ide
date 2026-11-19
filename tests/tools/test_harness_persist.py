"""A tool harness launched from a service must outlive that service being stopped."""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.platforms("linux")


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
