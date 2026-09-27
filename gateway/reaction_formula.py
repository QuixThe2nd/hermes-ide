"""The reaction gate's decision formula: a tiny, safely interpreted expression.

``discord.reaction_gate.decision_formula`` decides whether the highest-probability
whitelisted emoji is selected. It is a single Boolean expression over six fixed
numeric names — ``none``/``other`` (the abstention options' probabilities),
``abstain`` (their sum), ``top``/``second`` (the two highest whitelisted-emoji
probabilities; ``second`` is ``0`` with no runner-up) and ``emoji_count`` — using
only comparisons, ``and``/``or``/``not`` and ``+ - *`` arithmetic.

The expression is parsed with the stdlib ``ast`` module and executed by a MANUAL
recursive interpreter over the validated tree — never ``eval``/``exec``/``compile``
— so an operator-supplied string can never execute code. Everything outside the
allowlist below is refused at config-load time:

* allowed: finite numeric literals, ``True``/``False``, the six fixed names,
  parentheses, binary ``+ - *``, unary ``+ -``, the comparisons
  ``< <= > >= == !=``, and ``and``/``or``/``not``;
* refused: division and every other arithmetic operator, powers, calls,
  attributes, indexing, strings, tuples/collections, lambdas, conditionals,
  comprehensions, imports, unknown names — and any input whose static type is not
  numeric-where-numeric-is-needed / Boolean-where-Boolean-is-needed, or whose root
  is not Boolean.

Size is bounded (characters, AST nodes, nesting depth, literal magnitude) so a
pathological formula can cost neither at parse time nor per decision: the formula
is validated ONCE at config load (the same path catches typos for the reload
watcher) and the prepared tree is then evaluated per decision with no re-parsing.

This module is a leaf (stdlib only): ``gateway.config`` validates with it and the
Discord reaction gate evaluates with it, with no import cycle in either direction.
"""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass
from typing import Any, Mapping, Union

#: The exact default rule — the hardcoded decision the gate shipped with before
#: the formula became configurable. An unchanged config keeps this behavior
#: byte-for-byte: strict abstention boundary OR a strict 5x runaway winner that
#: only exists from two whitelisted entries up (the ``emoji_count`` guard is what
#: preserves single-emoji whitelists, since ``second`` is 0 there).
DEFAULT_DECISION_FORMULA = "abstain < 0.5 or (emoji_count >= 2 and top > 5 * second)"

#: Every name a formula may read. All six are numeric; there are no Boolean names
#: (``True``/``False`` are constants, and comparisons produce Booleans).
FORMULA_VARIABLES = frozenset(
    {"none", "other", "abstain", "top", "second", "emoji_count"}
)

#: Bounds. Character/node/depth caps keep parsing and evaluation trivially cheap;
#: the literal cap keeps arithmetic from reaching float overflow without a check
#: on every intermediate (evaluation still refuses non-finite results).
MAX_FORMULA_CHARS = 200
MAX_FORMULA_NODES = 100
MAX_FORMULA_DEPTH = 12
MAX_FORMULA_LITERAL = 1e9

_NUMERIC = "numeric"
_BOOLEAN = "boolean"

_COMPARE_OPS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)

_ALLOWED_NAMES = ", ".join(sorted(FORMULA_VARIABLES))


@dataclass(frozen=True)
class PreparedFormula:
    """One validated formula, ready to evaluate repeatedly without re-parsing."""

    source: str
    tree: ast.Expression

    def evaluate(self, variables: Mapping[str, Any]) -> bool:
        """Evaluate against the six fixed inputs. Raises ``ValueError``, never executes."""
        for name, value in variables.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"variable {name!r} is not a number")
            if not math.isfinite(value):
                raise ValueError(f"variable {name!r} is not finite")
        result = _evaluate(self.tree.body, variables)
        if not isinstance(result, bool):
            # Unreachable for a prepared formula (the root is type-checked Boolean);
            # kept so a hand-built tree can never silently yield a number.
            raise ValueError("formula did not produce a true/false result")
        return result


def prepare_decision_formula(source: str) -> PreparedFormula:
    """Parse, structurally allowlist, bound and type-check one formula.

    Raises ``ValueError`` describing the first refusal; the caller (config load)
    prefixes the field name. Everything a formula may do is decided HERE, once,
    before any runtime exists.
    """
    if not isinstance(source, str):
        raise ValueError(f"formula must be a string, got {type(source).__name__}")
    if len(source) > MAX_FORMULA_CHARS:
        raise ValueError(f"formula is longer than {MAX_FORMULA_CHARS} characters")
    try:
        tree = ast.parse(source, mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError) as exc:
        raise ValueError(f"is not a valid expression ({exc})") from None
    root_kind, nodes = _check(tree, 0)
    if nodes > MAX_FORMULA_NODES:
        raise ValueError(f"has more than {MAX_FORMULA_NODES} expression nodes")
    if root_kind != _BOOLEAN:
        raise ValueError("must produce a true/false result (compare numbers, or combine comparisons)")
    return PreparedFormula(source=source, tree=tree)


def _check(node: ast.AST, depth: int) -> tuple:
    """Validate one subtree; returns ``(static_type, node_count)``.

    The allowlist is structural FIRST (anything not named here is refused with its
    node type), then typed: arithmetic needs numeric operands, ``and``/``or``/
    ``not`` need Boolean operands, comparisons need numeric operands and produce
    Boolean. Static typing means a formula like ``top and none`` or ``abstain + True``
    is refused at load, so evaluation can never meet a type it did not expect.
    """
    if depth > MAX_FORMULA_DEPTH:
        raise ValueError(f"is nested deeper than {MAX_FORMULA_DEPTH} levels")
    if isinstance(node, ast.Expression):
        kind, count = _check(node.body, depth + 1)
        return kind, count + 1
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool):  # bool is an int subclass: test it first
            return _BOOLEAN, 1
        if isinstance(value, (int, float)):
            if not math.isfinite(value):
                raise ValueError(f"literal {value!r} is not finite")
            if abs(value) > MAX_FORMULA_LITERAL:
                raise ValueError(f"literal {value!r} exceeds the allowed magnitude")
            return _NUMERIC, 1
        raise ValueError("only numeric or true/false constants are allowed")
    if isinstance(node, ast.Name):
        if node.id not in FORMULA_VARIABLES:
            raise ValueError(f"unknown name {node.id!r} (allowed: {_ALLOWED_NAMES})")
        return _NUMERIC, 1
    if isinstance(node, ast.UnaryOp):
        operand_kind, count = _check(node.operand, depth + 1)
        if isinstance(node.op, ast.Not):
            if operand_kind != _BOOLEAN:
                raise ValueError("'not' needs a true/false operand")
            return _BOOLEAN, count + 1
        if isinstance(node.op, (ast.USub, ast.UAdd)):
            if operand_kind != _NUMERIC:
                raise ValueError("unary '+'/'-' needs a numeric operand")
            return _NUMERIC, count + 1
        raise ValueError("unary operator is not allowed")
    if isinstance(node, ast.BinOp):
        if not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
            raise ValueError("only '+' '-' '*' arithmetic is allowed (no division or powers)")
        left_kind, left = _check(node.left, depth + 1)
        right_kind, right = _check(node.right, depth + 1)
        if left_kind != _NUMERIC or right_kind != _NUMERIC:
            raise ValueError("arithmetic needs numeric operands")
        return _NUMERIC, left + right + 1
    if isinstance(node, ast.BoolOp):
        total = 1
        for value in node.values:
            kind, count = _check(value, depth + 1)
            if kind != _BOOLEAN:
                raise ValueError("'and'/'or' need true/false operands")
            total += count
        return _BOOLEAN, total
    if isinstance(node, ast.Compare):
        if len(node.ops) != 1:
            raise ValueError("chained comparisons are not allowed (parenthesize each)")
        if not isinstance(node.ops[0], _COMPARE_OPS):
            raise ValueError("only < <= > >= == != comparisons are allowed")
        left_kind, left = _check(node.left, depth + 1)
        right_kind, right = _check(node.comparators[0], depth + 1)
        if left_kind != _NUMERIC or right_kind != _NUMERIC:
            raise ValueError("comparisons need numeric operands")
        return _BOOLEAN, left + right + 1
    raise ValueError(f"{type(node).__name__} is not allowed in a formula")


def _finite(value: Union[int, float]) -> Union[int, float]:
    if not math.isfinite(value):
        raise ValueError("arithmetic produced a non-finite result")
    return value


def _evaluate(node: ast.AST, variables: Mapping[str, Any]) -> Any:
    """The manual interpreter: dispatch by node type over the pre-validated tree."""
    if isinstance(node, ast.Constant):
        return node.value  # bool stays bool; int/float stays numeric (typed above)
    if isinstance(node, ast.Name):
        try:
            return variables[node.id]
        except KeyError:
            raise ValueError(f"no value provided for {node.id!r}") from None
    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, variables)
        if isinstance(node.op, ast.Not):
            return not value
        if isinstance(node.op, ast.USub):
            return _finite(-value)
        return _finite(+value)
    if isinstance(node, ast.BinOp):
        left = _evaluate(node.left, variables)
        right = _evaluate(node.right, variables)
        if isinstance(node.op, ast.Add):
            return _finite(left + right)
        if isinstance(node.op, ast.Sub):
            return _finite(left - right)
        return _finite(left * right)
    if isinstance(node, ast.BoolOp):
        # Short-circuiting matters for readability only (nothing can throw past
        # validation), but keep Python semantics rather than eagerly evaluating.
        outcomes = (_evaluate(value, variables) for value in node.values)
        if isinstance(node.op, ast.And):
            return all(outcomes)
        return any(outcomes)
    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, variables)
        right = _evaluate(node.comparators[0], variables)
        op = node.ops[0]
        if isinstance(op, ast.Lt):
            return left < right
        if isinstance(op, ast.LtE):
            return left <= right
        if isinstance(op, ast.Gt):
            return left > right
        if isinstance(op, ast.GtE):
            return left >= right
        if isinstance(op, ast.Eq):
            return left == right
        return left != right
    raise ValueError(f"{type(node).__name__} cannot be evaluated")
