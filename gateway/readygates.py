"""Readiness gate for the gateway's ``sd_notify READY=1``.

Canonical policy lives in ``internal/readygates`` (Go twin); this module is
the Python twin the runtime executes. Both run the shared table in
``internal/readygates/testdata/cases.json`` (see
``tests/gateway/test_readygates_contract.py``).

Policy: READY means the gateway is genuinely serving — every enabled platform
adapter connected, plus cron and housekeeping started. MCP tool discovery and
boot-path notification sends are observed but never gate readiness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class PlatformState:
    enabled: bool = False
    connected: bool = False


@dataclass
class ReadyInput:
    platforms: list[PlatformState] = field(default_factory=list)
    cron_started: bool = False
    housekeeping_started: bool = False
    mcp_discovery_state: str = "pending"
    boot_path_sends_state: str = "pending"


@dataclass
class ReadyResult:
    ready: bool
    blocked_by: list[str] = field(default_factory=list)


def decide(inp: ReadyInput | dict) -> ReadyResult:
    if isinstance(inp, dict):
        platforms = [
            PlatformState(enabled=bool(p.get("enabled", False)),
                          connected=bool(p.get("connected", False)))
            for p in (inp.get("platforms") or [])
        ]
        inp = ReadyInput(
            platforms=platforms,
            cron_started=bool(inp.get("cron_started", False)),
            housekeeping_started=bool(inp.get("housekeeping_started", False)),
            mcp_discovery_state=str(inp.get("mcp_discovery_state", "pending")),
            boot_path_sends_state=str(inp.get("boot_path_sends_state", "pending")),
        )
    blocked: list[str] = []
    if any(p.enabled and not p.connected for p in inp.platforms):
        blocked.append("platforms")
    if not inp.cron_started:
        blocked.append("cron")
    if not inp.housekeeping_started:
        blocked.append("housekeeping")
    return ReadyResult(ready=not blocked, blocked_by=blocked)


def ready_gate(
    platforms: Iterable[PlatformState] | None = None,
    *,
    cron_started: bool = False,
    housekeeping_started: bool = False,
    mcp_discovery_state: str = "pending",
    boot_path_sends_state: str = "pending",
) -> ReadyResult:
    return decide(ReadyInput(
        platforms=list(platforms or []),
        cron_started=cron_started,
        housekeeping_started=housekeeping_started,
        mcp_discovery_state=mcp_discovery_state,
        boot_path_sends_state=boot_path_sends_state,
    ))
