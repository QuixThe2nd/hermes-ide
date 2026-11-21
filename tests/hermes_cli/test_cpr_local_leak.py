"""Local CPR leak reproduction for the classic CLI.

* Deterministic local-PTY proof that delayed CPR replies leak as
  ``ESC[row;colR`` / ``^[[row;colR`` when ``enable_cpr=True`` (no SSH).
* The CPR-disabled output builder emits no ``ESC[6n`` query at all.
"""

from __future__ import annotations

import os
import select
import threading
import time

import pytest

from cli import _build_cpr_disabled_output


def _openpty_or_skip():
    import pty

    try:
        return pty.openpty()
    except OSError as exc:
        pytest.skip(f"no PTY devices available: {exc}")


class TestDelayedCprLocalPtyLeak:
    def test_delayed_cpr_reply_leaks_when_enable_cpr_true(self):
        """Local (no SSH) delayed ESC[6n reply lands as ESC[39;1R on stdin."""
        import tty

        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.output.vt100 import Vt100_Output

        master, slave = _openpty_or_skip()
        try:
            tty.setraw(slave)
            slave_w = os.fdopen(os.dup(slave), "w", buffering=1)
            stop = threading.Event()
            queries = 0

            def terminal() -> None:
                nonlocal queries
                buf = b""
                while not stop.is_set():
                    try:
                        r, _, _ = select.select([master], [], [], 0.05)
                    except OSError:
                        break
                    if not r:
                        continue
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        idx = buf.find(b"\x1b[6n")
                        if idx < 0:
                            buf = buf[-8:] if len(buf) > 8 else buf
                            break
                        buf = buf[idx + 4 :]
                        queries += 1
                        time.sleep(0.12)
                        try:
                            os.write(master, b"\x1b[39;1R")
                        except OSError:
                            pass

            threading.Thread(target=terminal, daemon=True).start()
            out = Vt100_Output(
                slave_w, lambda: Size(rows=40, columns=80), enable_cpr=True
            )
            out.ask_for_cpr()
            out.flush()
            for i in range(4):
                slave_w.write(f"\rgpt-5.6-sol Q {i}\n")
                slave_w.flush()
                time.sleep(0.02)
            time.sleep(0.3)

            data = b""
            while True:
                r, _, _ = select.select([slave], [], [], 0.05)
                if not r:
                    break
                data += os.read(slave, 4096)

            stop.set()
            slave_w.close()

            assert queries >= 1
            assert b"\x1b[39;1R" in data
        finally:
            try:
                os.close(slave)
            except OSError:
                pass
            try:
                os.close(master)
            except OSError:
                pass

    def test_cpr_disabled_output_sends_no_query(self):
        """Hermes CPR-disabled builder must not emit ESC[6n."""
        master, slave = _openpty_or_skip()
        try:
            slave_w = os.fdopen(slave, "w", buffering=1)
            out = _build_cpr_disabled_output(slave_w)
            assert out is not None
            assert out.enable_cpr is False

            seen = b""

            def reader() -> None:
                nonlocal seen
                r, _, _ = select.select([master], [], [], 0.25)
                if r:
                    seen = os.read(master, 4096)

            threading.Thread(target=reader, daemon=True).start()
            slave_w.write("status ok\n")
            slave_w.flush()
            # Do not call ask_for_cpr — renderer skips it when NOT_SUPPORTED.
            time.sleep(0.3)
            slave_w.close()
            assert b"\x1b[6n" not in seen
        finally:
            try:
                os.close(master)
            except OSError:
                pass
