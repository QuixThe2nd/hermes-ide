"""Turn receipt parity: Python twin vs the running Go binary, over the shared fixture."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from gateway.turn_receipt import render_turn_html, write_turn_receipt

_REPO = Path(__file__).resolve().parents[2]
_CASES = json.loads(
    (_REPO / "internal" / "turnreceipt" / "testdata" / "receipt.json").read_text(encoding="utf-8")
)["cases"]


@pytest.fixture(scope="session")
def go_turnreceipt(tmp_path_factory):
    """cmd/turnreceipt built once per session; None without a Go toolchain."""
    if shutil.which("go") is None:
        return None
    binary = tmp_path_factory.mktemp("turnreceipt") / "turnreceipt"
    build = subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/turnreceipt"],
        cwd=_REPO / "internal" / "turnreceipt", capture_output=True, text=True, timeout=300,
    )
    if build.returncode != 0:
        pytest.fail(f"go build cmd/turnreceipt failed:\n{build.stderr}")
    return str(binary)


def _twin(case):
    data = case["input"]
    return render_turn_html(data["user_message"], data["rows"], data["final_reply"])


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c["name"])
def test_python_twin_holds_the_shared_invariants(case):
    page = _twin(case)
    cursor = 0
    for expected in case["in_order"]:
        found = page.find(expected, cursor)
        assert found >= 0, f"missing {expected!r} after offset {cursor}"
        cursor = found + len(expected)
    for forbidden in case["absent"]:
        assert forbidden not in page, forbidden


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c["name"])
def test_go_binary_and_python_twin_render_identical_bytes(case, go_turnreceipt):
    if go_turnreceipt is None:
        pytest.skip("no Go toolchain")
    run = subprocess.run(
        [go_turnreceipt], input=json.dumps(case["input"]),
        capture_output=True, text=True, timeout=60,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == _twin(case)


def test_page_name_carries_no_user_text(tmp_path):
    path = write_turn_receipt(tmp_path, "super secret request", ["row"], "reply")
    assert path.parent == tmp_path / "turn_receipts"
    assert "secret" not in path.name
    assert "super secret request" in path.read_text(encoding="utf-8")
