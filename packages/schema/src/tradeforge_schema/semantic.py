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
from typing import Final

from tradeforge_schema.models import (
    COMPOSITE_COMPONENTS,
    AllOf,
    AnyOf,
    Between,
    Comparison,
    Condition,
    Ref,
    Strategy,
    Trend,
)

# `price` and `candle` are namespaces in the ref grammar. An indicator called
# `price` would make `price.close` ambiguous, so the name is not available.
RESERVED_IDS = frozenset({"price", "candle"})

# The fields those namespaces expose, in the order a reader expects them. Kept as a tuple rather
# than a set so an error message lists them the same way twice running.
PRICE_FIELDS: Final[tuple[str, ...]] = ("open", "high", "low", "close")


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

    Written as early returns rather than a `match`: the union is closed — six members as of
    the `between` and `rising`/`falling` nodes — so the last one is reached by elimination,
    which mypy proves, and which leaves no unreachable "nothing matched" branch for the
    coverage report to complain about.

    ⚠️ That proof is also the guard on this function. Adding a member to `Condition` without a
    branch here does not fall through to a default: it makes the final line read `not_` off a
    node that has no such attribute, and mypy refuses the file. It is how the two nodes above
    got their branches — the type checker asked for them before any test did.
    """
    if isinstance(condition, Comparison):
        # Only refs need checking against declared indicators; a constant operand references
        # nothing, so it is skipped rather than mistaken for an undeclared name.
        for operand, side in ((condition.left, "left"), (condition.right, "right")):
            if isinstance(operand, Ref):
                yield operand, f"{path}.{side}"
        return

    if isinstance(condition, Between):
        # Three operands, and every one of them can name an indicator: a band drawn between two
        # curves is the reason this node takes operands rather than two numbers.
        for operand, side in (
            (condition.value, "value"),
            (condition.low, "low"),
            (condition.high, "high"),
        ):
            if isinstance(operand, Ref):
                yield operand, f"{path}.{side}"
        return

    if isinstance(condition, Trend):
        if isinstance(condition.of, Ref):
            yield condition.of, f"{path}.of"
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


def _indicator_target(ref: Ref) -> tuple[str, str | None] | None:
    """Split a ref into `(indicator id, component)`, or `None` if it names no indicator.

    ⚠️ **Decided by namespace, not by the presence of a dot.** The earlier version answered
    "contains no `.` and no `[`", which was exactly right while the grammar's only dotted forms
    were `price.*` and `candle[-N].*`. Once `bb.upper` became well-formed, that test started
    classifying every component ref as "not an indicator" — so a typo like `bb.uppper` passed this
    layer, reached the engine, resolved to `None`, and made its condition false on every bar of
    every run. Nothing raised, nothing logged, and the backtest simply never traded.

    So the two namespaces are named, and everything else that looks like an identifier is an
    indicator reference whose component (if any) is checked against the indicator's type.
    """
    text = ref.ref
    if "[" in text:
        return None  # `candle[-N].field` — the only bracketed form the grammar has
    head, _, component = text.partition(".")
    if head in RESERVED_IDS:
        return None
    return head, component or None


def _reserved_namespace_error(ref: Ref, path: str) -> SemanticError | None:
    """Refuse `price.clsoe` — a reserved namespace with a field it does not have.

    ⚠️ **This is the only layer that can refuse it, and `REF_PATTERN` cannot help.** The component
    alternative added for `bb.upper` also matches `price.clsoe`, because `price` is a perfectly good
    identifier and `clsoe` a perfectly good component name. Narrowing it there would need a negative
    lookahead, and Pydantic compiles `pattern=` with Rust's `regex` crate, which has no look-around
    at all — the expression fails to compile at import time and takes the package with it.

    Without this check the misspelling reaches the engine, resolves through `indicator_at` — which
    has no channel by that name and answers `None` — and the condition is false on every bar of the
    run. The rule reads as declined rather than as broken, and nothing raises or logs.

    `expressions.compile_operand` refuses the same shape a second time, on the layer that actually
    executes, for documents that never came through here.
    """
    text = ref.ref
    head, _, tail = text.partition(".")
    if head not in RESERVED_IDS:
        return None
    if head == "price":
        if tail in PRICE_FIELDS:
            return None
        return SemanticError(path, f"{text!r} is not a price field; expected one of {_fields()}")
    # ⚠️ Reaching here at all means the ref is illegal, so there is nothing left to check. The legal
    # candle form carries its offset *before* the dot — `candle[-1].high` splits into a head of
    # `candle[-1]`, which is not a reserved id and left this function two lines up. So a head of
    # exactly `candle` is either the bare namespace or `candle.something`, and neither names a bar.
    # (An earlier version re-matched the legal pattern here; the branch where it succeeded was
    # unreachable, and the copied pattern was doing nothing but inviting drift.)
    return SemanticError(
        path, f"{text!r} must name a closed candle and a field, as candle[-1].close"
    )


def _fields() -> str:
    return ", ".join(f"price.{name}" for name in PRICE_FIELDS)


def _component_error(
    indicator_id: str,
    component: str | None,
    components: tuple[str, ...],
    text: str,
    path: str,
) -> SemanticError | None:
    """Whether the component half of a ref agrees with what the indicator answers to.

    Three ways it can disagree, and every one of them resolves to `None` in the engine if it gets
    through — a condition false on every bar rather than an error anybody sees.
    """
    if components and component is None:
        # No default component, deliberately. "The middle band" is a tempting default for
        # Bollinger and there is no defensible one for ADX — and a default would make `bb`
        # and `bb.middle` two spellings of one value, which is how a DSL starts to rot.
        named = ", ".join(f"{indicator_id}.{name}" for name in components)
        return SemanticError(path, f"{indicator_id!r} has several outputs; name one of {named}")
    if components and component not in components:
        return SemanticError(
            path,
            f"{indicator_id!r} has no component {component!r}; "
            f"it answers to {', '.join(components)}",
        )
    if not components and component is not None:
        return SemanticError(
            path,
            f"{indicator_id!r} answers with a single value; "
            f"reference it as {indicator_id!r}, not {text!r}",
        )
    return None


def _validate_setup_document(strategy: Strategy) -> list[SemanticError]:
    """The rules for a document that *names* a setup rather than describing one.

    A setup is a state machine that owns its own indicators, its own entry trigger and its own
    protective stop. Anything the condition half of the DSL would contribute is therefore not
    merely redundant — it is a second opinion with no arbiter, so it is refused rather than
    ignored. Silently dropping an `entry` the user wrote is how a backtest ends up answering a
    question nobody asked.

    What a setup document *does* keep is the two numbers that were never the strategy's to begin
    with: `risk.sizing` (the account's) and `exit.take_profit` (the broker's target, resolved at
    fill). Note that a target here needs **no** `exit.stop_loss` — the rule below that demands one
    does not apply, because the setup places the stop itself, from the reference bar. Applying it
    would reject every setup document that names a target, which is all of the interesting ones.
    """
    errors: list[SemanticError] = []
    if strategy.indicators:
        errors.append(
            SemanticError(
                "indicators",
                "a setup declares its own indicators; remove these or drop the setup",
            ),
        )
    if strategy.entry.long is not None or strategy.entry.short is not None:
        errors.append(
            SemanticError(
                "entry",
                "a setup is the entry; it cannot be combined with entry conditions",
            ),
        )
    if strategy.exit.stop_loss is not None:
        errors.append(
            SemanticError(
                "exit.stop_loss",
                "a setup places its own stop from the bar it entered on",
            ),
        )
    if strategy.exit.conditions:
        errors.append(
            SemanticError(
                "exit.conditions",
                "a setup conducts its own exit; the position leaves at a level, never on a rule",
            ),
        )
    return errors


def validate_semantics(strategy: Strategy) -> list[SemanticError]:
    """Return every reason the strategy cannot run. Empty list means it can."""
    if strategy.setup is not None:
        return _validate_setup_document(strategy)

    errors: list[SemanticError] = []

    # id -> the component names it answers to, empty for a single-valued indicator.
    declared: dict[str, tuple[str, ...]] = {}
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
        declared[indicator.id] = COMPOSITE_COMPONENTS.get(indicator.type, ())

    for ref, path in _strategy_refs(strategy):
        reserved = _reserved_namespace_error(ref, path)
        if reserved is not None:
            errors.append(reserved)
            continue
        target = _indicator_target(ref)
        if target is None:
            continue
        indicator_id, component = target
        if indicator_id not in declared:
            errors.append(
                SemanticError(path, f"reference to undeclared indicator {indicator_id!r}"),
            )
            continue
        mismatch = _component_error(indicator_id, component, declared[indicator_id], ref.ref, path)
        if mismatch is not None:
            errors.append(mismatch)

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
