"""The decomposed turn-loop modules must not reference undefined bare names.

Witness: 2026-09-11 cron turn hit ``openai.BadRequestError``; the error path then
crashed with ``NameError: name 'classify_api_error' is not defined`` at
``agent/conversation_loop.py:4941`` because the Sep 2026 ``run_agent`` extraction
left call sites whose imports were never carried over. ``scripts/check_compat_pointers.py``
only sees facade-path references, so nothing gated this.

Repo-wide ``ruff --select F821`` has ~2400 pre-existing hits (dynamic patterns), so
this canary pins only the two decomposed modules — the full select list is out of scope.
"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGETS = [
    "agent/conversation_loop.py",
    "agent/auxiliary_client.py",
]


def test_no_undefined_bare_names_in_decomposed_modules():
    """ruff F821 on the two modules exits 0 — every bare name resolves at module scope."""
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--select", "F821", *TARGETS],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"`{sys.executable} -m ruff check --select F821 {' '.join(TARGETS)}` "
        f"exited {proc.returncode} (undefined bare names — run the same command for "
        f"the annotated findings):\n{proc.stdout}{proc.stderr}"
    )
