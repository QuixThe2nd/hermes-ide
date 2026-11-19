"""Anti-regression for the facade/mixin dedupe (branch refactor/dedupe-agent-family).

The AIAgent class dict shadows its mixin bases: any name defined on BOTH the
facade (run_agent.py) and a mixin resolves to the facade copy at runtime, so a
byte-identical mixin copy is dead code. This test walks the real AIAgent MRO
and fails if such a shadowed duplicate (re)appears, and asserts
single-definition for the names consolidated in that branch.
"""

import ast
import inspect
from textwrap import dedent

from run_agent import AIAgent

CONSOLIDATED_NAMES = (
    "_usage_summary_for_api_request_hook",
    "_hook_payload_max_chars",
    "_api_request_payload_for_hook",
    "_api_response_payload_for_hook",
    "get_rate_limit_state",
    "_capture_anthropic_response_headers",
    "get_credits_state",
    "get_credits_spent_micros",
    "_mask_api_key_for_logs",
    "_clean_error_message",
    "_neutralize_footer_paths",
    "_persist_session_activity_if_due",
    "_save_trajectory",
)


def _normalized_ast(fn) -> str:
    """AST dump of a function with its docstring stripped (decorators + body kept)."""
    src = dedent(inspect.getsource(fn))
    module = ast.parse(src)
    func = module.body[0]
    if (
        func.body
        and isinstance(func.body[0], ast.Expr)
        and isinstance(func.body[0].value, ast.Constant)
        and isinstance(func.body[0].value.value, str)
    ):
        func.body = func.body[1:]
    return ast.dump(module)


def _mixin_bases():
    """MRO classes other than AIAgent itself and object, with their source files."""
    for cls in AIAgent.__mro__:
        if cls in (AIAgent, object):
            continue
        yield cls


def test_consolidated_names_defined_once_on_mro():
    for name in CONSOLIDATED_NAMES:
        owners = [
            cls.__qualname__ for cls in AIAgent.__mro__
            if name in cls.__dict__ and cls not in (object,)
        ]
        assert owners == ["AIAgent"], (
            f"{name} is expected to have exactly one definition (on the facade); found {owners}"
        )


def test_no_shadowed_byte_identical_mixin_methods():
    offenders = []
    for name, entry in vars(AIAgent).items():
        if not inspect.isfunction(entry):
            continue
        for base in _mixin_bases():
            shadowed = base.__dict__.get(name)
            if not inspect.isfunction(shadowed):
                continue
            try:
                if _normalized_ast(entry) == _normalized_ast(shadowed):
                    offenders.append(f"{name}: run_agent.AIAgent shadows identical {base.__module__}.{base.__qualname__}.{name}")
            except OSError:
                continue  # non-file source (e.g. exec'd); skip
    assert not offenders, (
        "Byte-identical shadowed mixin methods (delete the mixin copy):\n"
        + "\n".join(offenders)
    )
