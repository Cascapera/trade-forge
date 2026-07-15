"""The condition tree: refs, operators, and the nodes that combine them.

A DSL condition is a small expression language, and this module is its interpreter. Three
parts, in the order data flows through them:

* **Operands** resolve a reference string — `"sma_fast"`, `"price.close"`,
  `"candle[-1].high"` — into a number, given an `EvalContext`. The string is parsed once, at
  compile time; every bar just calls the resolver.
* **Operators** compare two operands. Some (`gt`, `lt`, ...) ask about this candle. Others
  (`crosses_above`, `breaks_above`, ...) ask about this candle *and the previous one*, and
  are true only on the bar the relation flips — an *edge*, not a *level*.
* **Nodes** (`Comparison`, `AllOf`, `AnyOf`, `NotOf`) form the tree and evaluate to a bool.
  A node evaluating itself and delegating to its children is a textbook recursive
  interpreter, in miniature.

Everything here is pure and structural: the compiled nodes satisfy the `Condition` protocol
by having an `evaluate(EvalContext) -> bool` method and inheriting nothing. Adding an
operator is one entry in `OPERATORS`; adding a node kind is one branch in
`compile_condition`. New blocks, no edit to the loop — ADR-03 again.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from tradeforge_engine.domain import Candle, EvalContext, Money
from tradeforge_engine.errors import EngineError

# --------------------------------------------------------------------------- #
# Operands — a parsed reference, resolvable at any bar and at any shift back     #
# --------------------------------------------------------------------------- #

# The two structured ref shapes. A bare id (an indicator) matches neither and is the
# fall-through. These mirror `REF_PATTERN` in the schema, but relaxed: a document that
# reaches the engine has already passed schema validation, so this only has to *interpret*
# what the schema has already proven well-formed.
_PRICE_REF = re.compile(r"^price\.(open|high|low|close)$")
_CANDLE_REF = re.compile(r"^candle\[-([1-9][0-9]*)\]\.(open|high|low|close)$")


@dataclass(frozen=True, slots=True)
class Operand:
    """A compiled reference.

    `resolve(context, shift)` returns the value `shift` bars before the current one — `shift`
    is 0 for "now" and 1 for "the previous bar", which is all an edge operator needs. The
    result is `None` when the value does not exist yet: a candle before the start of the run,
    or an indicator still warming up. A comparison involving a `None` is false.

    `lookback` is how many closed candles back this operand reaches at `shift == 0` (0 for
    `price.*` and for an indicator, N for `candle[-N]`). The compiler sums it with the edge
    shift to size the strategy's candle buffer — a ref that reaches five bars back is useless
    if only four are kept.
    """

    resolve: Callable[[EvalContext, int], Money | None]
    lookback: int


def _candle_field(field: str) -> Callable[[Candle], Money]:
    def read(candle: Candle) -> Money:
        value: Money = getattr(candle, field)
        return value

    return read


def compile_operand(ref: str) -> Operand:
    """Parse one reference string into an `Operand`. Called once per operand, at compile."""
    price_match = _PRICE_REF.match(ref)
    if price_match is not None:
        read = _candle_field(price_match.group(1))

        def resolve_price(context: EvalContext, shift: int) -> Money | None:
            candle = context.candle_at(shift)
            return None if candle is None else read(candle)

        return Operand(resolve=resolve_price, lookback=0)

    candle_match = _CANDLE_REF.match(ref)
    if candle_match is not None:
        offset = int(candle_match.group(1))
        read = _candle_field(candle_match.group(2))

        def resolve_candle(context: EvalContext, shift: int) -> Money | None:
            candle = context.candle_at(offset + shift)
            return None if candle is None else read(candle)

        return Operand(resolve=resolve_candle, lookback=offset)

    # A bare id: an indicator. Its own value history carries the shift; it costs no candle
    # depth of its own.
    def resolve_indicator(context: EvalContext, shift: int) -> Money | None:
        return context.indicator_at(ref, shift)

    return Operand(resolve=resolve_indicator, lookback=0)


# --------------------------------------------------------------------------- #
# Operators — level (this bar) and edge (this bar vs the previous one)           #
# --------------------------------------------------------------------------- #

Operator = Callable[[Operand, Operand, EvalContext], bool]


def _level(compare: Callable[[Money, Money], bool]) -> Operator:
    """A level operator asks only about this bar. `None` on either side is false — you
    cannot compare a value that does not exist yet."""

    def op(left: Operand, right: Operand, context: EvalContext) -> bool:
        a = left.resolve(context, 0)
        b = right.resolve(context, 0)
        return a is not None and b is not None and compare(a, b)

    return op


def _edge(
    was: Callable[[Money, Money], bool],
    now: Callable[[Money, Money], bool],
) -> Operator:
    """An edge operator asks about this bar *and* the previous one: it is true only where the
    relation was one way last bar (`was`) and the other way this bar (`now`). That single-bar
    truth is what stops a strategy from re-entering on every bar of a move it already caught —
    a crossover happens once, a level holds for as long as the trend does.

    Any of the four values missing (this bar or last, either operand) makes it false: an edge
    needs two full bars to exist, and the bar before the first one never does.
    """

    def op(left: Operand, right: Operand, context: EvalContext) -> bool:
        now_left = left.resolve(context, 0)
        now_right = right.resolve(context, 0)
        prev_left = left.resolve(context, 1)
        prev_right = right.resolve(context, 1)
        # Checked one operand at a time (not `None in (...)`), which also narrows all four to
        # non-None for the type checker — no assert needed to prove what the guard established.
        if now_left is None or now_right is None or prev_left is None or prev_right is None:
            return False
        return was(prev_left, prev_right) and now(now_left, now_right)

    return op


# `breaks_above` and `crosses_above` are the *same* edge — Guilherme's call (PR-104): a break
# is an edge event, true once on the bar the level is pierced, not a level that re-fires while
# price stays beyond it. They stay two names because they read differently and may diverge in
# a later phase (a break is conventionally price-vs-a-level; a cross, line-vs-line), but in v1
# the mechanic is one. The boundary is `<=` / `>=` on the previous bar, so a touch that then
# breaks counts — a deliberate, tested choice, not an accident of `<` vs `<=`.
_crosses_above = _edge(lambda a, b: a <= b, lambda a, b: a > b)
_crosses_below = _edge(lambda a, b: a >= b, lambda a, b: a < b)

OPERATORS: Final[dict[str, Operator]] = {
    "gt": _level(lambda a, b: a > b),
    "lt": _level(lambda a, b: a < b),
    "gte": _level(lambda a, b: a >= b),
    "lte": _level(lambda a, b: a <= b),
    "crosses_above": _crosses_above,
    "crosses_below": _crosses_below,
    "breaks_above": _crosses_above,
    "breaks_below": _crosses_below,
}


# --------------------------------------------------------------------------- #
# Nodes — the tree                                                              #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Comparison:
    """A leaf: `left op right`."""

    op_name: str
    _op: Operator
    left: Operand
    right: Operand

    def evaluate(self, context: EvalContext) -> bool:
        return self._op(self.left, self.right, context)


@dataclass(frozen=True, slots=True)
class AllOf:
    """Logical AND. Empty is impossible — the schema requires at least one child."""

    conditions: tuple["Condition", ...]

    def evaluate(self, context: EvalContext) -> bool:
        return all(condition.evaluate(context) for condition in self.conditions)


@dataclass(frozen=True, slots=True)
class AnyOf:
    """Logical OR."""

    conditions: tuple["Condition", ...]

    def evaluate(self, context: EvalContext) -> bool:
        return any(condition.evaluate(context) for condition in self.conditions)


@dataclass(frozen=True, slots=True)
class NotOf:
    """Logical NOT."""

    condition: "Condition"

    def evaluate(self, context: EvalContext) -> bool:
        return not self.condition.evaluate(context)


# A compiled node. The union is closed in v1; each satisfies the `Condition` protocol.
Condition = Comparison | AllOf | AnyOf | NotOf


def _operand_of(node: Mapping[str, object], side: str) -> Operand:
    operand = node[side]
    if not isinstance(operand, Mapping) or "ref" not in operand:
        raise EngineError(f"comparison {side!r} operand is not a ref: {operand!r}")
    ref = operand["ref"]
    if not isinstance(ref, str):
        raise EngineError(f"comparison {side!r} ref must be a string, got {ref!r}")
    return compile_operand(ref)


def compile_condition(node: Mapping[str, object]) -> Condition:
    """Compile one condition node, recursing into the tree.

    The node's *shape* decides its kind — the same untagged discipline the schema uses, where
    `extra="forbid"` guarantees exactly one of these keys is present. Anything else is a
    document the engine was not built to interpret, and it says so rather than ignore it.
    """
    if "op" in node:
        op_name = node["op"]
        if not isinstance(op_name, str) or op_name not in OPERATORS:
            raise EngineError(
                f"unknown comparison operator {op_name!r}; this engine knows {sorted(OPERATORS)}"
            )
        return Comparison(
            op_name=op_name,
            _op=OPERATORS[op_name],
            left=_operand_of(node, "left"),
            right=_operand_of(node, "right"),
        )

    if "all" in node:
        return AllOf(_compile_children(node["all"], "all"))
    if "any" in node:
        return AnyOf(_compile_children(node["any"], "any"))
    if "not" in node:
        child = node["not"]
        if not isinstance(child, Mapping):
            raise EngineError(f"'not' takes one condition, got {child!r}")
        return NotOf(compile_condition(child))

    raise EngineError(f"unrecognised condition node with keys {sorted(node)}")


def _compile_children(children: object, key: str) -> tuple[Condition, ...]:
    if not isinstance(children, list) or not children:
        raise EngineError(f"{key!r} takes a non-empty list of conditions, got {children!r}")
    compiled: list[Condition] = []
    for child in children:
        if not isinstance(child, Mapping):
            raise EngineError(f"{key!r} child is not a condition: {child!r}")
        compiled.append(compile_condition(child))
    return tuple(compiled)


__all__ = [
    "OPERATORS",
    "AllOf",
    "AnyOf",
    "Comparison",
    "Condition",
    "NotOf",
    "Operand",
    "Operator",
    "compile_condition",
    "compile_operand",
]
