"""dev_pipeline attempt resource knobs: MemoryMax and the agent-wake gate.

The 2026-08-25 incident had two halves with one root: job t_135a3014 was
OOM-killed at a ``MemoryMax`` nobody could change without editing the
source, and the resulting block reached only a human. These tests pin the
config contract for both — the memory ceiling the executor can now be
given, and the gate that turns agent wakes off.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from plugins.dev_pipeline import executor as ex
from plugins.dev_pipeline.pipeline import (
    get_dev_pipeline_config,
    normalize_memory_max,
)


class _ArgvRecorder:
    """run_subprocess double: records argv, returns scripted results."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return self._results.pop(0)


def _proc(*, rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def _spawn(memory_max=None) -> _ArgvRecorder:
    recorder = _ArgvRecorder([_proc(out="Running as unit"), _proc(out="4242")])
    kwargs = {} if memory_max is None else {"memory_max": memory_max}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ex, "run_subprocess", recorder)
        mp.setattr(ex, "get_host_start_time", lambda pid: 12345)
        ok, pid, start = ex.systemd_run_attempt(
            unit="hermes-dev-t-1",
            runtime_max_sec=1800,
            working_directory=Path("/ws/repo"),
            env={"K": "V"},
            argv=["cmd", "arg"],
            **kwargs,
        )
    assert (ok, pid, start) == (True, 4242, 12345)
    return recorder


def _memory_property(argv: list[str]) -> str:
    return next(p for p in argv if p.startswith("--property=MemoryMax="))


def test_default_memory_max_is_byte_identical():
    """Unset knob → the historical hardcoded property, byte for byte."""
    assert _memory_property(_spawn().calls[0]) == "--property=MemoryMax=6G"


def test_configured_memory_max_flows_into_spawn_property():
    assert _memory_property(_spawn("12G").calls[0]) == "--property=MemoryMax=12G"
    assert _memory_property(_spawn("512MiB").calls[0]) == "--property=MemoryMax=512MiB"
    assert _memory_property(_spawn("infinity").calls[0]) == "--property=MemoryMax=infinity"


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "6G"),
        ("", "6G"),
        ("   ", "6G"),
        ("6G", "6G"),
        ("6g", "6g"),
        (" 12G ", "12G"),
        ("512MiB", "512MiB"),
        ("1.5G", "1.5G"),
        ("infinity", "infinity"),
        ("max", "max"),
        ("bogus", "6G"),
        ("6 GB", "6 GB"),  # systemd tolerates the space; passed through verbatim
        (6, "6G"),
    ],
)
def test_normalize_memory_max(raw, expected):
    assert normalize_memory_max(raw) == expected


def _write_config(tmp_path, monkeypatch, body: str) -> None:
    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (home / "config.yaml").write_text(body, encoding="utf-8")


def test_config_defaults_when_unset(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, "dev_pipeline:\n  enabled: true\n")
    cfg = get_dev_pipeline_config()
    assert cfg["attempt_memory_max"] == "6G"
    assert cfg["agent_wake_on_block"] is True


def test_config_reads_both_knobs(tmp_path, monkeypatch):
    _write_config(
        tmp_path, monkeypatch,
        "dev_pipeline:\n"
        "  attempt_memory_max: 16G\n"
        "  agent_wake_on_block: false\n",
    )
    cfg = get_dev_pipeline_config()
    assert cfg["attempt_memory_max"] == "16G"
    assert cfg["agent_wake_on_block"] is False


def test_config_rejects_non_size_memory_value(tmp_path, monkeypatch):
    _write_config(
        tmp_path, monkeypatch,
        "dev_pipeline:\n  attempt_memory_max: 'a lot'\n",
    )
    assert get_dev_pipeline_config()["attempt_memory_max"] == "6G"


def test_executor_cfg_reaches_the_spawn_seam(tmp_path, monkeypatch):
    """The executor passes its resolved config through to systemd-run —
    the link that makes the knob real rather than merely readable."""
    db_path = tmp_path / "dev-memory.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (home / "config.yaml").write_text(
        "dev_pipeline:\n  attempt_memory_max: 9G\n", encoding="utf-8",
    )
    kb.init_db()

    recorder = _ArgvRecorder([_proc(out="Running as unit"), _proc(out="4242")])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ex, "run_subprocess", recorder)
        mp.setattr(ex, "get_host_start_time", lambda pid: 1)
        executor = ex.DevExecutor()
        assert executor.cfg["attempt_memory_max"] == "9G"
        ok, _, _ = ex.systemd_run_attempt(
            unit="u",
            runtime_max_sec=60,
            working_directory=Path("/ws"),
            env={},
            argv=["cmd"],
            memory_max=str(
                executor.cfg.get("attempt_memory_max")
                or ex.DEFAULT_ATTEMPT_MEMORY_MAX
            ),
        )
    assert ok
    assert _memory_property(recorder.calls[0]) == "--property=MemoryMax=9G"
