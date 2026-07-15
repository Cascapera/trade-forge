"""The compiler: a validated DSL document in, a runnable `Strategy` out.

This is the seam the whole DSL exists for. The frontend composes JSON, the schema package
validates its shape and meaning, and this turns that JSON into objects the event loop can
drive — the same loop, unchanged, that PR-103 tested with a hand-written stub.

**Why a `Mapping`, not a Pydantic model.** The engine has no dependencies, on purpose (see
the package docstring) — that is the mechanism behind determinism, and importing the schema
package would pull Pydantic into the core. So the compiler takes the validated document as a
plain mapping: the schema package is the single source of truth for *shape*, and a test
compiles every one of its fixtures to catch drift, but nothing here imports it. The document
is assumed already validated; the compiler still fails loudly, never silently, on anything
it does not recognise.

**The `schema_version` gate.** A saved strategy is immutable for its version (AGENTS.md
§5.5). The engine must be able to *refuse* a document written for a version it was not built
to interpret, rather than do its best guess and reproduce yesterday's backtest differently.
That refusal lives on the first line of `compile_strategy`.
"""

import datetime as dt
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Final

from tradeforge_engine.domain import (
    Candle,
    Context,
    EvalContext,
    Money,
    Side,
    Signal,
    SignalKind,
)
from tradeforge_engine.errors import EngineError
from tradeforge_engine.expressions import (
    AllOf,
    AnyOf,
    Comparison,
    Condition,
    compile_condition,
)
from tradeforge_engine.indicators import build_indicator
from tradeforge_engine.protocols import Indicator

SUPPORTED_SCHEMA_VERSION: Final = "1.0"

# The DSL names a timeframe; the loop needs its duration to police the lookahead ceiling
# (PR-103). Deriving one from the other here means a strategy and the engine that runs it
# never disagree about how long a bar is.
TIMEFRAME_DELTAS: Final[dict[str, dt.timedelta]] = {
    "M1": dt.timedelta(minutes=1),
    "M5": dt.timedelta(minutes=5),
    "M15": dt.timedelta(minutes=15),
    "M30": dt.timedelta(minutes=30),
    "H1": dt.timedelta(hours=1),
    "H4": dt.timedelta(hours=4),
    "D1": dt.timedelta(days=1),
    "W1": dt.timedelta(weeks=1),
}


def _max_lookback(condition: Condition) -> int:
    """The deepest closed candle any operand in this tree reaches at the current bar."""
    if isinstance(condition, Comparison):
        return max(condition.left.lookback, condition.right.lookback)
    if isinstance(condition, (AllOf, AnyOf)):
        return max((_max_lookback(child) for child in condition.conditions), default=0)
    return _max_lookback(condition.condition)  # NotOf


class CompiledStrategy:
    """A DSL strategy compiled into the engine's `Strategy` protocol.

    It owns the indicators and a rolling window of recent closed candles. Each bar, in the
    order the sdd.md §3.3.2 loop prescribes: update the indicators, build the `EvalContext`,
    then evaluate conditions. A position is open ⇒ only exit conditions are asked (an entry
    while already in the trade is not this phase's business — one position at a time). Flat ⇒
    entry conditions decide, long taking precedence over short on the rare bar both fire.

    Stops, targets and sizing are declared in the DSL but *not* resolved here: they belong to
    the broker and risk manager of PR-105, which is the seam that executes them intrabar. This
    class emits condition-driven intent and nothing else — the clean half of the boundary.
    """

    def __init__(  # noqa: PLR0913 — keyword-only; each names one compiled part of a strategy
        self,
        *,
        name: str,
        timeframe: dt.timedelta,
        indicators: Mapping[str, Indicator],
        entry_long: Condition | None,
        entry_short: Condition | None,
        exit_conditions: tuple[Condition, ...],
        history_depth: int,
    ) -> None:
        self.name = name
        self.timeframe = timeframe
        self._indicators: dict[str, Indicator] = dict(indicators)
        self._entry_long = entry_long
        self._entry_short = entry_short
        self._exit_conditions = exit_conditions
        # Newest-first, bounded: the deepest ref plus one bar for the edge operators' "previous
        # bar", plus the current bar. A window any shorter would resolve a legal ref to None.
        self._candles: deque[Candle] = deque(maxlen=history_depth)
        # Two values per indicator — this bar and last — is all an edge operator can ask for.
        self._indicator_history: dict[str, deque[Money | None]] = {
            indicator_id: deque(maxlen=2) for indicator_id in self._indicators
        }

    def on_bar(self, context: Context) -> Sequence[Signal]:
        candle = context.candle
        self._candles.appendleft(candle)

        for indicator_id, indicator in self._indicators.items():
            indicator.update(candle)
            self._indicator_history[indicator_id].appendleft(indicator.value())

        eval_context = EvalContext(
            candles=tuple(self._candles),
            indicator_values={
                indicator_id: tuple(history)
                for indicator_id, history in self._indicator_history.items()
            },
            position=context.position,
        )

        if context.position is not None:
            if any(condition.evaluate(eval_context) for condition in self._exit_conditions):
                return [
                    Signal(
                        kind=SignalKind.EXIT,
                        side=context.position.side,
                        reference_price=candle.close,
                        reason="exit.condition",
                    )
                ]
            return []

        if self._entry_long is not None and self._entry_long.evaluate(eval_context):
            return [self._entry(Side.LONG, candle)]
        if self._entry_short is not None and self._entry_short.evaluate(eval_context):
            return [self._entry(Side.SHORT, candle)]
        return []

    @staticmethod
    def _entry(side: Side, candle: Candle) -> Signal:
        return Signal(
            kind=SignalKind.ENTRY,
            side=side,
            reference_price=candle.close,
            reason=f"entry.{side.value}",
        )


def _require_mapping(value: object, what: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EngineError(f"{what} must be an object, got {value!r}")
    return value


def _compile_side(entry: Mapping[str, object], side: str) -> Condition | None:
    node = entry.get(side)
    if node is None:
        return None
    return compile_condition(_require_mapping(node, f"entry.{side}"))


def compile_strategy(document: Mapping[str, object]) -> CompiledStrategy:
    """Compile a validated DSL document into a runnable strategy.

    Assumes `document` has already passed the schema package's shape and semantic checks.
    Refuses an unsupported `schema_version` outright, and raises `EngineError` — never a bare
    `KeyError` — on anything structural it cannot interpret, so a malformed document fails
    with a sentence instead of a traceback.
    """
    version = document.get("schema_version")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise EngineError(
            f"strategy schema_version {version!r} is not supported; "
            f"this engine interprets {SUPPORTED_SCHEMA_VERSION!r}"
        )

    name = document.get("name")
    if not isinstance(name, str):
        raise EngineError(f"strategy name must be a string, got {name!r}")

    timeframe_key = document.get("timeframe")
    timeframe = TIMEFRAME_DELTAS.get(str(timeframe_key))
    if timeframe is None:
        raise EngineError(
            f"unknown timeframe {timeframe_key!r}; this engine knows {sorted(TIMEFRAME_DELTAS)}"
        )

    indicators: dict[str, Indicator] = {}
    raw_indicators = document.get("indicators", [])
    if not isinstance(raw_indicators, list):
        raise EngineError(f"indicators must be a list, got {raw_indicators!r}")
    for spec in raw_indicators:
        indicator_id, indicator = build_indicator(_require_mapping(spec, "indicator"))
        if indicator_id in indicators:
            raise EngineError(f"duplicate indicator id {indicator_id!r}")
        indicators[indicator_id] = indicator

    entry = _require_mapping(document.get("entry"), "entry")
    entry_long = _compile_side(entry, "long")
    entry_short = _compile_side(entry, "short")
    if entry_long is None and entry_short is None:
        raise EngineError("a strategy must define entry conditions for at least one side")

    exit_block = _require_mapping(document.get("exit", {}), "exit")
    raw_exit_conditions = exit_block.get("conditions", [])
    if not isinstance(raw_exit_conditions, list):
        raise EngineError(f"exit.conditions must be a list, got {raw_exit_conditions!r}")
    exit_conditions = tuple(
        compile_condition(_require_mapping(node, "exit condition")) for node in raw_exit_conditions
    )

    trees = [tree for tree in (entry_long, entry_short, *exit_conditions) if tree is not None]
    # +2: the current bar (index 0) and the one bar back every edge operator can reach.
    history_depth = max((_max_lookback(tree) for tree in trees), default=0) + 2

    return CompiledStrategy(
        name=name,
        timeframe=timeframe,
        indicators=indicators,
        entry_long=entry_long,
        entry_short=entry_short,
        exit_conditions=exit_conditions,
        history_depth=history_depth,
    )


__all__ = [
    "SUPPORTED_SCHEMA_VERSION",
    "TIMEFRAME_DELTAS",
    "CompiledStrategy",
    "compile_strategy",
]
