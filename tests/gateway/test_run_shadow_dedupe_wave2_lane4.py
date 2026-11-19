"""Anti-regression guard: wave-2 lane-4 diverged shadow pairs stay consolidated (inbound/turn/watchers).

History: the Sep-2026 mixin split (run.py -> run_inbound.py / run_turn.py / run_watchers.py) was
clobbered by a repo-restore, leaving diverged duplicate definitions — the ``GatewayRunner``
class-body copies in ``gateway/run.py`` shadowed their mixin counterparts via the MRO. Wave 2
lane 4 resolved every pair in the lane-4 manifest: exactly ONE definition of each name remains
across the ``GatewayRunner`` class body in ``gateway/run.py`` and the owning sibling module.
This test fails if any pair is re-introduced.
"""

import ast
import json
from pathlib import Path

import gateway.run as run_mod

REPO = Path(run_mod.__file__).resolve().parent.parent


def _load_manifest() -> dict:
    embedded = Path(__file__).resolve().parent / "wave2-lane4.manifest.json"
    return json.loads(embedded.read_text())


# module -> mixin class holding the (single) surviving definition.
LANE4_MIXINS = {
    "run_turn.py": "GatewayTurnMixin",
    "run_inbound.py": "GatewayInboundMixin",
    "run_watchers.py": "GatewaySessionWatchersMixin",
}

FACADE_CLASSES = ("GatewayRunner", "TurnRunner")


def _facade_class_def_names() -> set:
    tree = ast.parse((REPO / "gateway" / "run.py").read_text())
    names = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in FACADE_CLASSES:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(item.name)
    return names


def _mixin_method_names(module: str, cls: str) -> set:
    tree = ast.parse((REPO / "gateway" / module).read_text())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            return {
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    raise AssertionError(f"class {cls} not found in {module}")


_MANIFEST = _load_manifest()
LANE4_NAMES: dict[str, list[str]] = {
    mod: [e["name"].split(".", 1)[1] for e in entries]
    for mod, entries in _MANIFEST.items()
}


def test_wave2_lane4_single_def_invariant():
    """Every manifest name has exactly one def across facade class bodies + sibling module."""
    facade = _facade_class_def_names()
    for mod, names in LANE4_NAMES.items():
        sibling = _mixin_method_names(mod, LANE4_MIXINS[mod])
        for name in names:
            count = int(name in facade) + int(name in sibling)
            assert count == 1, (
                f"{mod}:{name} has {count} defs (facade={name in facade}, "
                f"sibling={name in sibling}); the shadow duplicate is back"
            )


def test_wave2_lane4_method_resolves_to_mixin():
    """Each manifest method resolves on GatewayRunner (mixin copy is live and importable)."""
    runner_cls = run_mod.GatewayRunner
    for mod, names in LANE4_NAMES.items():
        for name in names:
            assert callable(getattr(runner_cls, name, None)), f"{name} missing on GatewayRunner"
