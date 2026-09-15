"""Two-sided contract: every notification name ``tui_gateway`` emits, and every
server→client request method it sends, is listed in ``apps/shared/src/gateway-events.json``
(``events`` / ``server_requests``) and nothing in the JSON is orphaned.

The TypeScript half (``apps/shared/src/gateway-events.test.ts``) pins the typed
``GatewayEventMap`` and ``ServerRequestMap`` to the same JSON, so a name added on either
side alone goes red somewhere. This file reads only Python sources and the JSON (never
``.ts`` text — see ``tui_gateway/AGENTS.md``).

Names are collected from the emitter side: literal first arguments to the emit helpers
and the server-request helpers (``server_requests.send`` / ``send_async`` / ``_ask``),
plus the tables that derive names at runtime (the change-watcher table, child delta
mirroring, the subagent relay events from ``tools/delegate_tool*.py``, the ``desktop_ui``
tool emitters, and the literal ``gateway.ready`` / ``setup.ready`` / browser-controller frames).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CONTRACT = REPO / "apps" / "shared" / "src" / "gateway-events.json"
GATEWAY_DIR = REPO / "tui_gateway"

# Every helper whose first positional argument is the wire ``type``.
_EMIT_HELPERS = ("_emit", "_broadcast_global_event", "_voice_emit", "_pet_emit", "_emit_tool_lifecycle")
_LITERAL_EMIT = re.compile(r"\b(?:%s)\(\s*\"([a-z_][a-z0-9_.]*)\"" % "|".join(_EMIT_HELPERS))
# Server→client requests: ``server_requests.send("x", …)`` / ``send_async`` / the string-answer ``_ask`` and
# ``_read_block`` bridges.
_REQUEST_HELPERS = ("server_requests\\.send", "server_requests\\.send_async", "_ask", "_read_block")
_LITERAL_REQUEST = re.compile(r"\b(?:%s)\(\s*\"([a-z_][a-z0-9_.]*)\"" % "|".join(_REQUEST_HELPERS))
# ``{"type": "gateway.ready", ...}`` literal frames (entry.py / ws.py) and other
# ``"type": "<name>"`` params written straight into an ``event`` frame.
_LITERAL_FRAME = re.compile(r"\"method\":\s*\"event\".{0,120}?\"type\":\s*\"([a-z_][a-z0-9_.]*)\"", re.S)
_SIDE_AGENT = re.compile(r"_spawn_side_agent\((?:[^()]|\([^()]*\))*?\"([a-z_][a-z0-9_.]*\.complete)\"", re.S)
_SUBAGENT_RELAY = re.compile(r"\"(subagent\.[a-z_]+)\"")
_DESKTOP_UI_EMIT = re.compile(r"desktop_ui\.(?:emit|emit_or_error)\(\s*\"([a-z_][a-z0-9_.]*)\"")
_BROKER_FRAME = re.compile(r"^FRAME_[A-Z_]+ = \"(browser\.controller\.[a-z_]+)\"", re.M)
_SETUP_READY = re.compile(r"^SETUP_READY_EVENT = \"([a-z_.]+)\"", re.M)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def emitted_event_names() -> set[str]:
    names: set[str] = set()
    for src in GATEWAY_DIR.glob("*.py"):
        text = _read(src)
        names.update(_LITERAL_EMIT.findall(text))
        names.update(_LITERAL_FRAME.findall(text))
        names.update(_SIDE_AGENT.findall(text))
    from tui_gateway.change_watcher import _CHANGE_WATCHES

    names.update(_CHANGE_WATCHES)
    from tui_gateway.agent_callbacks import _CHILD_DELTA_EVENTS

    names.update(_CHILD_DELTA_EVENTS.values())
    # ``_progress_subagent`` relays every ``subagent.*`` event verbatim EXCEPT the child's per-token
    # ``subagent.text`` (mirrored into the watch window as ``message.delta`` instead); the names live in the relay.
    for src in (REPO / "tools").glob("delegate_tool*.py"):
        names.update(_SUBAGENT_RELAY.findall(_read(src)))
    names.discard("subagent.text")
    for src in (REPO / "tools").glob("*.py"):
        names.update(_DESKTOP_UI_EMIT.findall(_read(src)))
    names.update(_BROKER_FRAME.findall(_read(REPO / "gateway" / "browser_control_broker.py")))
    names.update(_SETUP_READY.findall(_read(REPO / "hermes_cli" / "free_tier_bootstrap.py")))
    # Dispatch-table keys that double as the emitted name (``_PROGRESS_HANDLERS`` re-emits
    # ``event_type``) are already literal ``_emit("...")`` calls inside their handlers.
    return names


def server_request_methods() -> set[str]:
    names: set[str] = set()
    for src in GATEWAY_DIR.glob("*.py"):
        names.update(_LITERAL_REQUEST.findall(_read(src)))
    return names


@pytest.fixture(scope="module")
def contract() -> list[str]:
    return json.loads(_read(CONTRACT))["events"]


@pytest.fixture(scope="module")
def request_contract() -> list[str]:
    return json.loads(_read(CONTRACT))["server_requests"]


def test_contract_is_sorted_and_unique(contract, request_contract):
    assert contract == sorted(set(contract)), "gateway-events.json events must be a sorted, duplicate-free list"
    assert request_contract == sorted(set(request_contract)), "gateway-events.json server_requests must be sorted"


def test_server_request_methods_match_the_contract(request_contract):
    sent = server_request_methods()
    assert sent == set(request_contract), (
        f"server requests sent {sorted(sent)} vs gateway-events.json server_requests {request_contract}; "
        "fix the JSON AND SERVER_REQUEST_METHODS / ServerRequestMap in apps/shared/src/gateway-events.ts")


def test_every_emitted_event_is_in_the_contract(contract):
    missing = emitted_event_names() - set(contract)
    assert not missing, (
        f"tui_gateway emits {sorted(missing)} but apps/shared/src/gateway-events.json does not list them; "
        "add the name(s) there AND to BACKEND_EVENT_NAMES / GatewayEventMap in apps/shared/src/gateway-events.ts")


def test_contract_has_no_orphan_names(contract):
    orphans = set(contract) - emitted_event_names()
    assert not orphans, (
        f"apps/shared/src/gateway-events.json lists {sorted(orphans)} but no tui_gateway emitter names them; "
        "drop the entry (and its GatewayEventMap key) or wire the emitter")
