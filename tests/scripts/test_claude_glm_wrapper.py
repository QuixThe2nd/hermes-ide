"""Behavior tests for the bundled ``scripts/claude-glm`` wrapper.

The wrapper runs as a real subprocess with ``CLAUDE_BIN`` pointed at a fake
Claude Code CLI that records its argv + environment and exits 0; every
assertion is made against what the real CLI would have received. The wrapper
itself is never imported or read.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[2] / "scripts" / "claude-glm"

PRIMARY_MODEL = "glm-5.2"
FALLBACK_MODEL = "glm-5.3-flash"
DEFAULT_BASE_URL = "https://api.z.ai/api/anthropic"

MODEL_ENV_VARS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)

# Environment the wrapper consumes (or must not leak through) — scrubbed for
# every run so an ambient developer shell cannot skew the assertions.
SCRUBBED_ENV_VARS = (
    "ZAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_BIN",
)

RECORDER = """\
#!/usr/bin/env python3
import json
import os
import sys

with open(os.environ["RECORDER_OUT"], "w", encoding="utf-8") as fh:
    json.dump({"argv": sys.argv[1:], "env": dict(os.environ)}, fh)
"""


@pytest.fixture
def recorder_out(tmp_path, monkeypatch) -> Path:
    """Fake Claude Code CLI + isolated env; returns the recording location."""
    for var in SCRUBBED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    out = tmp_path / "recorded.json"
    monkeypatch.setenv("RECORDER_OUT", str(out))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_claude = bin_dir / "claude"
    fake_claude.write_text(RECORDER, encoding="utf-8")
    fake_claude.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BIN", str(fake_claude))
    return out


def run_wrapper(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(WRAPPER), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def read_recording(out: Path) -> dict:
    return json.loads(out.read_text(encoding="utf-8"))


def test_print_run_pins_models_endpoint_and_fallback(recorder_out, monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "env-key")
    proc = run_wrapper("--print", "hello")
    assert proc.returncode == 0, proc.stderr

    recording = read_recording(recorder_out)
    argv = recording["argv"]

    # Caller argv passes through untouched; wrapper flags are appended after.
    assert argv[:2] == ["--print", "hello"]

    # Exactly one --fallback-model, pinned to the flash model.
    assert argv.count("--fallback-model") == 1
    assert argv[argv.index("--fallback-model") + 1] == FALLBACK_MODEL

    # Interactive sessions get the fallback via settings JSON instead
    # (--fallback-model is print-mode only).
    assert argv.count("--settings") == 1
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings == {"fallbackModel": [FALLBACK_MODEL]}

    env = recording["env"]
    assert env["ANTHROPIC_AUTH_TOKEN"] == "env-key"
    assert env["ANTHROPIC_BASE_URL"] == DEFAULT_BASE_URL
    for var in MODEL_ENV_VARS:
        assert env[var] == PRIMARY_MODEL, var
    # A real Anthropic key must not shadow the z.ai token.
    assert "ANTHROPIC_API_KEY" not in env


def test_explicit_fallback_model_is_not_duplicated(recorder_out, monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "env-key")
    proc = run_wrapper("--print", "--fallback-model", "glm-4.6", "hello")
    assert proc.returncode == 0, proc.stderr

    argv = read_recording(recorder_out)["argv"]
    assert argv.count("--fallback-model") == 1
    # The caller's choice survives — the wrapper appends only when missing.
    assert argv[argv.index("--fallback-model") + 1] == "glm-4.6"


def test_key_from_hermes_home_secrets_dotenv(recorder_out):
    secrets_dir = Path(os.environ["HERMES_HOME"], "secrets")
    secrets_dir.mkdir()
    (secrets_dir / "zai.env").write_text(
        "# z.ai coding plan credentials\n"
        "\n"
        "OTHER_KEY=ignored\n"
        'ZAI_API_KEY="quoted-flash-key"\n',
        encoding="utf-8",
    )

    proc = run_wrapper()
    assert proc.returncode == 0, proc.stderr

    env = read_recording(recorder_out)["env"]
    assert env["ANTHROPIC_AUTH_TOKEN"] == "quoted-flash-key"


def test_missing_key_fails_clearly_without_launching_cli(recorder_out):
    proc = run_wrapper()
    assert proc.returncode != 0
    assert "ZAI_API_KEY" in proc.stdout + proc.stderr
    # The CLI must never run with a missing key.
    assert not recorder_out.exists()


def test_external_base_url_is_respected(recorder_out, monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "env-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example/api")

    proc = run_wrapper()
    assert proc.returncode == 0, proc.stderr

    env = read_recording(recorder_out)["env"]
    assert env["ANTHROPIC_BASE_URL"] == "https://proxy.example/api"
