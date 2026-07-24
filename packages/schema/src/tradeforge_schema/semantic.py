"""Semantic validation — the half of the contract a JSON Schema cannot express.

A schema checks *shape*. It will happily accept a strategy whose entry condition
compares against an indicator that was never declared, or that asks for a 2:1
take-profit with no stop-loss to measure the 1 against. Both documents are
well-formed. Both are unrunnable.

Catching that here, at the door, is the difference between an error the user reads
in the builder and an error the user discovers forty minutes into a backtest.

This layer is Python-only, and that asymmetry is deliberate: the frontend gets the
schema (shape) for instant feedback while typing, and the API is the authority on
meaning. Never assume a document that passed in the browser is executable.
"""

from collections.abc import Iterator
from dataclasses import dataclass

from tradeforge_schema.models import (
    AllOf,
    AnyOf,
    Comparison,
    Condition,
    Ref,
    Strategy,
)

# `price` and `candle` are namespaces in the ref grammar. An indicator called
# `price` would make `price.close` ambiguous, so the name is not available.
RESERVED_IDS = frozenset({"price", "candle"})


@dataclass(frozen=True, slots=True)
class SemanticError:
    """One reason a well-formed strategy still cannot run."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class SemanticValidationError(ValueError):
    """Raised when a schema-valid strategy is not executable."""

    def __init__(self, errors: list[SemanticError]) -> None:
        self.errors = errors
        super().__init__("; ".join(str(error) for error in errors))


def _iter_refs(condition: Condition, path: str) -> Iterator[tuple[Ref, str]]:
    """Walk the expression tree, yielding every operand with the path that reached it.

    Written as early returns rather than a `match`: the union has exactly four members,
    so the last one is reached by elimination — which mypy proves, and which leaves no
    unreachable "nothing matched" branch for the coverage report to complain about.
    """
    if isinstance(condition, Comparison):
        # Only refs need checking against declared indicators; a constant operand references
        # nothing, so it is skipped rather than mistaken for an undeclared name.
        for operand, side in ((condition.left, "left"), (condition.right, "right")):
            if isinstance(operand, Ref):
                yield operand, f"{path}.{side}"
        return

    if isinstance(condition, AllOf):
        for index, child in enumerate(condition.all):
            yield from _iter_refs(child, f"{path}.all[{index}]")
        return

    if isinstance(condition, AnyOf):
        for index, child in enumerate(condition.any):
            yield from _iter_refs(child, f"{path}.any[{index}]")
        return

    yield from _iter_refs(condition.not_, f"{path}.not")


def _strategy_refs(strategy: Strategy) -> Iterator[tuple[Ref, str]]:
    if strategy.entry.long is not None:
        yield from _iter_refs(strategy.entry.long, "entry.long")
    if strategy.entry.short is not None:
        yield from _iter_refs(strategy.entry.short, "entry.short")
    for index, condition in enumerate(strategy.exit.conditions):
        yield from _iter_refs(condition, f"exit.conditions[{index}]")


def _is_indicator_ref(ref: Ref) -> bool:
    """`price.close` and `candle[-1].high` resolve without an indicator; a bare name does not."""
    return "." not in ref.ref and "[" not in ref.ref


def validate_semantics(strategy: Strategy) -> list[SemanticError]:
    """Return every reason the strategy cannot run. Empty list means it can."""
    errors: list[SemanticError] = []

    declared: set[str] = set()
    for index, indicator in enumerate(strategy.indicators):
        path = f"indicators[{index}]"
        if indicator.id in declared:
            errors.append(
                SemanticError(path, f"duplicate indicator id {indicator.id!r}"),
            )
        if indicator.id in RESERVED_IDS:
            errors.append(
                SemanticError(
                    path,
                    f"{indicator.id!r} is a reserved namespace and cannot name an indicator",
                ),
            )
        declared.add(indicator.id)

    for ref, path in _strategy_refs(strategy):
        if _is_indicator_ref(ref) and ref.ref not in declared:
            errors.append(
                SemanticError(path, f"reference to undeclared indicator {ref.ref!r}"),
            )

    if strategy.entry.long is None and strategy.entry.short is None:
        errors.append(
            SemanticError("entry", "a strategy must define entry conditions for at least one side"),
        )

    if strategy.exit.take_profit is not None and strategy.exit.stop_loss is None:
        errors.append(
            SemanticError(
                "exit.take_profit",
                "a risk-multiple target needs a stop_loss — there is no risk to multiply",
            ),
        )

    return errors


def assert_executable(strategy: Strategy) -> Strategy:
    """Raise unless the strategy is semantically sound. Returns it for chaining."""
    errors = validate_semantics(strategy)
    if errors:
        raise SemanticValidationError(errors)
    return strategy
