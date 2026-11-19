"""Contract: the Python /model twin answers exactly like the Go core.

``internal/modelcmd`` (Go) is the canonical pure logic of the /model slash
command (argument/flag parsing, no-arg listing text, picker rows);
``hermes_cli/modelcmd.py`` is the twin the runtime calls (parsing stays in
``hermes_cli.model_switch.parse_model_switch_args`` — the ONE parser every
surface shares). This test pipes every shared-table case
(testdata/cases.json) through the RUNNING Go binary (cmd/modelcmd) when a
Go toolchain exists and asserts the Python twin computes identical answers.

Without a toolchain the table still runs against the Python twin alone, so
a table edit can never silently diverge from the documented expectations.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from hermes_cli.model_switch import MODEL_SWITCH_ERROR_TEXT, parse_model_switch_args
from hermes_cli.modelcmd import (
    CLI_CONFIRM_LABELS, build_picker_rows, render_confirmation, render_model_listing)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CASES = _REPO_ROOT / "internal" / "modelcmd" / "testdata" / "cases.json"
_GO_DIR = _REPO_ROOT / "internal" / "modelcmd"


def _load_table():
    doc = json.loads(_CASES.read_text())
    for section in ("parse", "listing", "picker"):
        assert doc.get(section), f"shared modelcmd table missing {section}"
    return doc


def _python_parse(raw: str) -> dict:
    """Map the shared parser's dataclass onto the Go wire object."""
    r = parse_model_switch_args(raw)
    return {
        "raw": r.raw,
        "target": r.target,
        "explicit_provider": r.explicit_provider,
        "reasoning_effort": r.reasoning_effort,
        "is_global": r.is_global,
        "is_session": r.is_session,
        "is_once": r.is_once,
        "force_refresh": r.force_refresh,
        "scope": r.scope,
        "errors": [
            {"code": code, "message": MODEL_SWITCH_ERROR_TEXT[code]} for code in r.errors
        ],
    }


@pytest.mark.parametrize("case", _load_table()["parse"], ids=lambda c: c["raw"] or "empty")
def test_parse_twin_matches_shared_table(case):
    assert _python_parse(case["raw"]) == case["want"]

@pytest.mark.parametrize("case", _load_table()["confirmation"], ids=lambda c: c["name"])
def test_confirmation_twin_matches_shared_table(case):
    assert render_confirmation(case["record"]) == case["want"]


@pytest.mark.parametrize("case", _load_table()["picker"], ids=lambda c: c["name"])
def test_picker_twin_matches_shared_table(case):
    got = build_picker_rows(
        case["providers"],
        current_provider=case["current_provider"],
        max_providers=case["max_providers"],
        max_models=case["max_models"],
    )
    assert got == case["want"]


def _go_answers(request: dict) -> str:
    proc = subprocess.run(
        ["go", "run", "./cmd/modelcmd"],
        cwd=_GO_DIR,
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


@pytest.mark.skipif(shutil.which("go") is None, reason="no Go toolchain")
def test_twin_matches_running_go_core():
    """The Python twin must agree with the RUNNING Go binary on every case."""
    table = _load_table()
    for case in table["parse"]:
        got = json.loads(_go_answers({"op": "parse", "raw": case["raw"]}))
        assert got == _python_parse(case["raw"]), f"parse {case['raw']!r}"
    for case in table["listing"]:
        got = _go_answers({"op": "listing", **case["record"]})
        assert got.rstrip("\n") == render_model_listing(case["record"]), (
            f"listing {case['name']}"
        )
    for case in table["picker"]:
        got = json.loads(
            _go_answers(
                {
                    "op": "picker",
                    "providers": case["providers"],
                    "current_provider": case["current_provider"],
                    "max_providers": case["max_providers"],
                    "max_models": case["max_models"],
                }
            )
        )
        want = build_picker_rows(
            case["providers"],
            current_provider=case["current_provider"],
            max_providers=case["max_providers"],
            max_models=case["max_models"],
        )
        assert got == want, f"picker {case['name']}: go={got!r} python={want!r}"
    for case in table["confirmation"]:
        got = _go_answers({"op": "confirmation", **case["record"]})
        assert got.rstrip("\n") == render_confirmation(case["record"]), (
            f"confirmation {case['name']}"
        )
    # The CLI flavor preset must render through Go too — it feeds the same op.
    rec = {"model": "kimi-k2", "provider": "Moonshot", "scope": "session",
           "labels": CLI_CONFIRM_LABELS}
    got = _go_answers({"op": "confirmation", **rec})
    assert got.rstrip("\n") == render_confirmation(rec)
