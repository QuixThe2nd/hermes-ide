"""Windows PID liveness reads every non-``WAIT_OBJECT_0`` as live (#77184).

The old probe answered ``WaitForSingleObject(handle, 0) == 0x102`` — so
``WAIT_FAILED`` (0xFFFFFFFF), a FAILED probe, read as "process absent" and
authorized the detached watcher to launch a replacement while the old
gateway could still be alive, racing it for platform locks and ports.
These tests drive the real Win32 result codes through the injected-kernel32
seam (platform logic as data — the same pattern as
``hidden_windows_child_options(opts, is_windows=True)``) so they run
deterministically on any host.
"""

import ctypes
from types import SimpleNamespace

import pytest

import gateway.restart as gateway_restart

_HANDLE = 0xDEADBEEF
_WAIT_OBJECT_0 = 0x0
_WAIT_TIMEOUT = 0x102
_WAIT_FAILED = 0xFFFFFFFF


class _WinFunc:
    """Callable that accepts ``restype`` assignments like a ctypes pointer."""

    def __init__(self, fn):
        self._fn = fn
        self.restype = None

    def __call__(self, *args):
        return self._fn(*args)


class _FakeKernel32:
    """Deterministic kernel32 double for the handle-based liveness probe."""

    def __init__(self, *, wait_result: int = _WAIT_TIMEOUT, last_error: int = 0):
        self._wait_result = wait_result
        self._last_error = last_error
        self.opened: list[tuple[int, bool, int]] = []
        self.waits: list[tuple[int, int]] = []
        self.closed: list[int] = []
        # ctypes-style entry points: bound methods cannot take the restype
        # assignments pid_alive_fail_closed makes on the real kernel32.
        self.OpenProcess = _WinFunc(self._open_process)
        self.WaitForSingleObject = _WinFunc(self._wait_for_single_object)
        self.GetLastError = _WinFunc(self._get_last_error)
        self.CloseHandle = _WinFunc(self._close_handle)

    def _open_process(self, access, inherit, pid):
        self.opened.append((access, inherit, pid))
        return 0 if self._last_error else _HANDLE

    def _wait_for_single_object(self, handle, milliseconds):
        self.waits.append((handle, milliseconds))
        return self._wait_result

    def _get_last_error(self):
        return self._last_error

    def _close_handle(self, handle):
        self.closed.append(handle)
        return True


# ── the wait-result reading ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("result", "expected_live"),
    [
        (_WAIT_OBJECT_0, False),        # signaled — the ONLY absence proof
        (_WAIT_TIMEOUT, True),          # 0x102 — still running
        (_WAIT_FAILED, True),           # 0xFFFFFFFF — the probe FAILED
        (0x80, True),                        # WAIT_ABANDONED — unknown here
        (0x1, True),                         # any other value is ignorance
        (0x103, True),
        (-1, True),                          # sign-error variant of FAILED
    ],
)
def test_wait_result_reading(result, expected_live):
    assert gateway_restart._win32_wait_result_means_live(result) is expected_live


def test_wait_failed_specifically_remains_live():
    """The exact fail-open this repair closes: WAIT_FAILED must never read
    as absence — a failed probe cannot prove anything."""
    k32 = _FakeKernel32(wait_result=_WAIT_FAILED)
    assert gateway_restart._windows_pid_alive_fail_closed(4242, k32) is True
    assert k32.waits == [(_HANDLE, 0)]  # the probe really did run and fail


# ── the full Windows probe, result codes as data ────────────────────────────


def test_successful_open_wait_timeout_reads_live_and_closes_the_handle():
    k32 = _FakeKernel32(wait_result=_WAIT_TIMEOUT)
    assert gateway_restart._windows_pid_alive_fail_closed(4242, k32) is True
    assert k32.opened == [(0x1000 | 0x100000, False, 4242)]
    assert k32.closed == [_HANDLE]


def test_signaled_handle_is_the_only_absent_answer():
    k32 = _FakeKernel32(wait_result=_WAIT_OBJECT_0)
    assert gateway_restart._windows_pid_alive_fail_closed(4242, k32) is False
    assert k32.closed == [_HANDLE]


def test_openprocess_invalid_parameter_is_absent_everything_else_is_live():
    # ERROR_INVALID_PARAMETER (87): the PID slot cannot be addressed — the
    # process is gone.
    k32 = _FakeKernel32(last_error=87)
    assert gateway_restart._windows_pid_alive_fail_closed(4242, k32) is False
    assert k32.waits == []  # no handle was opened, nothing to wait on

    # Every other OpenProcess failure — access denied on a protected
    # process (5), invalid handle (6), anything — is unknown, i.e. live.
    for error in (5, 6, 1722, 998):
        k32 = _FakeKernel32(last_error=error)
        assert gateway_restart._windows_pid_alive_fail_closed(4242, k32) is True, error


def test_pid_alive_fail_closed_wait_failed_remains_live(monkeypatch):
    """On ``nt`` the POSIX ``os.kill`` arm must never run (bpo-14484): the
    call delegates to the handle probe with the same fail-closed reading.

    The module-level ``os`` binding is swapped for a stand-in namespace
    rather than patching the real ``os.name`` — a global ``os.name = "nt"``
    would make pathlib instantiate WindowsPath and break the test process
    itself on POSIX hosts."""
    monkeypatch.setattr(
        gateway_restart,
        "os",
        SimpleNamespace(
            name="nt",
            kill=lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("os.kill on nt")
            ),
        ),
    )
    k32 = _FakeKernel32(wait_result=_WAIT_FAILED)
    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(kernel32=k32), raising=False
    )
    assert gateway_restart.pid_alive_fail_closed(4242) is True
    assert k32.waits == [(_HANDLE, 0)]
