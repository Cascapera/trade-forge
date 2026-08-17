"""The compiler: a validated DSL document in, a runnable strategy out.

The end-to-end test is the one that matters — a compiled strategy driven through the real
event loop, producing a trade whose fill lands on the bar *after* the decision, because the
whole point of the DSL is to reach that same anti-lookahead loop the stub reached in PR-103.

The fixtures test is the drift guard. The engine takes the DSL as a plain mapping and does
not import the schema package, so nothing at the type level stops the two from disagreeing.
Instead, every strategy the schema package publishes as valid is compiled here: add a field
or an indicator type to the DSL and forget to teach the engine, and this test goes red.
"""

import datetime as dt
import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from tradeforge_engine.domain import Candle, SignalKind
from tradeforge_engine.errors import EngineError
from tradeforge_engine.indicators import SMA
from tradeforge_engine.loop import run
from tradeforge_engine.strategy import CompiledStrategy, StopRule, compile_strategy
from tradeforge_engine.testing import EURUSD, HOUR, START, FixedRisk, ImmediateFillBroker, bar


def _compiled(document: dict[str, object]) -> CompiledStrategy:
    """Compile a *condition* document and narrow to the concrete type.

    `compile_strategy` returns the `Strategy` protocol, because a document may instead name a
    setup and get a state machine back (ADR-0019). Everything in this module is about the tree
    walker, and asserting the type once here is also the assertion that a document with no
    `setup` block still compiles to exactly that.
    """
    strategy = compile_strategy(document)
    assert isinstance(strategy, CompiledStrategy)
    return strategy


_FIXTURES = Path(__file__).resolve().parents[2] / "schema" / "fixtures"


def _crossover_strategy() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "name": "fast crosses slow",
        "timeframe": "H1",
        "indicators": [
            {"id": "fast", "type": "SMA", "params": {"period": 2, "source": "close"}},
            {"id": "slow", "type": "SMA", "params": {"period": 3, "source": "close"}},
        ],
        "entry": {
            "long": {"op": "crosses_above", "left": {"ref": "fast"}, "right": {"ref": "slow"}},
            "short": None,
        },
        "exit": {
            "conditions": [
                {"op": "crosses_below", "left": {"ref": "fast"}, "right": {"ref": "slow"}}
            ]
        },
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def _setup_strategy() -> dict[str, object]:
    """The other document shape: one that *names* a setup instead of describing it (ADR-0019)."""
    return {
        "schema_version": "1.0",
        "name": "the pullback",
        "timeframe": "H1",
        "setup": {"type": "ponto_continuo", "params": {"side": "long", "period": 3}},
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def _series(closes: list[str]) -> list[Candle]:
    candles: list[Candle] = []
    for index, value in enumerate(closes):
        open_ = Decimal(closes[index - 1]) if index else Decimal(value)
        close = Decimal(value)
        high = max(open_, close) + Decimal("0.001")
        low = min(open_, close) - Decimal("0.001")
        candles.append(
            Candle(time=START + index * HOUR, open=open_, high=high, low=low, close=close)
        )
    return candles


# --------------------------------------------------------------------------- #
# End to end                                                                    #
# --------------------------------------------------------------------------- #


def test_a_compiled_strategy_trades_through_the_real_loop() -> None:
    """A crossover long, entered on the cross and closed on the cross back — and the entry
    fills at the *open of the next bar*, the invariant the whole engine exists to hold."""
    candles = _series(
        ["1.100", "1.090", "1.080", "1.070", "1.090", "1.110", "1.130", "1.120", "1.090", "1.070"]
    )
    strategy = _compiled(_crossover_strategy())

    result = run(
        candles=candles,
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=strategy,
        broker=ImmediateFillBroker(),
        risk=FixedRisk(),
    )

    entry, exit_ = result.fills
    assert entry.order.intent is SignalKind.ENTRY
    assert entry.order.reason == "entry.long"
    # Decided on the crossover bar (index 5), filled at the open of the next one (index 6).
    assert entry.order.decided_at == candles[5].time
    assert entry.time == candles[6].time
    assert entry.price == candles[6].open

    assert exit_.order.intent is SignalKind.EXIT
    assert exit_.order.reason == "exit.condition"
    assert len(result.trades) == 1


def test_the_compiler_reports_the_timeframe_the_loop_needs() -> None:
    strategy = _compiled(_crossover_strategy())
    assert strategy.timeframe == dt.timedelta(hours=1)
    assert strategy.name == "fast crosses slow"


def test_an_entry_only_fires_while_flat_and_an_exit_only_while_in_a_position() -> None:
    """The strategy checks exits when it holds a position and entries when it does not — the
    order the sdd.md loop prescribes, and the reason a single crossover makes one round trip,
    not a new entry on every subsequent bar it stays true."""
    candles = _series(
        ["1.100", "1.090", "1.080", "1.070", "1.090", "1.110", "1.130", "1.140", "1.150", "1.160"]
    )
    strategy = _compiled(_crossover_strategy())
    result = run(
        candles=candles,
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=strategy,
        broker=ImmediateFillBroker(),
        risk=FixedRisk(),
    )

    # Price keeps rising after the entry: fast stays above slow, so the entry condition would
    # be *true* as a level — but it is an edge, and the strategy is no longer flat. One entry.
    entries = [fill for fill in result.fills if fill.order.intent is SignalKind.ENTRY]
    assert len(entries) == 1


# --------------------------------------------------------------------------- #
# Gates and refusals                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "build", [_crossover_strategy, _setup_strategy], ids=["conditions", "setup"]
)
def test_an_unsupported_schema_version_is_refused(build: Callable[[], dict[str, object]]) -> None:
    """A saved strategy is immutable for its version; the engine refuses one it was not built
    to interpret rather than guess and reproduce a different backtest (AGENTS.md §5.5).

    Both document shapes are gated, and this is the assertion that the setup branch sits *after*
    the gate rather than in front of it. A v2.0 that redefined what `stop_buffer` measures — the
    field is a fraction of the zone's width, which is peculiar enough to be worth changing — would
    otherwise be read with this engine's meaning, put every stop somewhere else, and finish `done`
    with metrics nobody thinks to question.
    """
    document = build()
    document["schema_version"] = "2.0"
    with pytest.raises(EngineError, match="schema_version"):
        compile_strategy(document)


def test_a_strategy_with_no_entry_side_is_refused() -> None:
    document = _crossover_strategy()
    document["entry"] = {"long": None, "short": None}
    with pytest.raises(EngineError, match="at least one side"):
        compile_strategy(document)


def test_a_short_only_strategy_enters_short() -> None:
    """The other side of the entry branch: fast crossing *below* slow opens a short."""
    document = _crossover_strategy()
    document["entry"] = {
        "long": None,
        "short": {"op": "crosses_below", "left": {"ref": "fast"}, "right": {"ref": "slow"}},
    }
    # rise to a peak, then fall — fast(2) crosses below slow(3) on the way down
    candles = _series(["1.10", "1.12", "1.14", "1.16", "1.14", "1.10", "1.06", "1.04"])
    result = run(
        candles=candles,
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=compile_strategy(document),
        broker=ImmediateFillBroker(),
        risk=FixedRisk(),
    )
    entries = [fill for fill in result.fills if fill.order.intent is SignalKind.ENTRY]
    assert entries
    assert entries[0].order.reason == "entry.short"


def test_long_takes_precedence_when_both_sides_fire_on_the_same_bar() -> None:
    """The declared tie-break: with both entry conditions true on a flat bar, the strategy
    opens long. `gt(price.high, price.low)` is always true, so both sides fire every bar —
    the bar the strategy is flat, long must win."""
    document = _crossover_strategy()
    document["indicators"] = []
    always = {"op": "gt", "left": {"ref": "price.high"}, "right": {"ref": "price.low"}}
    document["entry"] = {"long": always, "short": always}
    document["exit"] = {"conditions": []}

    result = run(
        candles=_series(["1.10", "1.11", "1.12"]),
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=compile_strategy(document),
        broker=ImmediateFillBroker(),
        risk=FixedRisk(),
    )
    entries = [fill for fill in result.fills if fill.order.intent is SignalKind.ENTRY]
    assert entries[0].order.reason == "entry.long"


def test_the_history_window_counts_lookback_through_a_not() -> None:
    """`_max_lookback` must recurse into `not`: a `candle[-4]` buried under a negation still
    needs four bars of history plus the current and edge bars."""
    document = _crossover_strategy()
    document["entry"] = {
        "long": {
            "not": {"op": "gt", "left": {"ref": "price.high"}, "right": {"ref": "candle[-4].high"}}
        },
        "short": None,
    }
    strategy = _compiled(document)
    assert strategy._candles.maxlen == 6  # 4 (deepest ref, under the not) + 1 + 1


def test_entry_that_is_not_an_object_is_refused() -> None:
    document = _crossover_strategy()
    document["entry"] = "long only"
    with pytest.raises(EngineError, match="entry must be an object"):
        compile_strategy(document)


def test_a_name_that_is_not_a_string_is_refused() -> None:
    document = _crossover_strategy()
    document["name"] = 123
    with pytest.raises(EngineError, match="name must be a string"):
        compile_strategy(document)


def test_an_unknown_timeframe_is_refused() -> None:
    document = _crossover_strategy()
    document["timeframe"] = "M2"
    with pytest.raises(EngineError, match="unknown timeframe"):
        compile_strategy(document)


def test_indicators_that_are_not_a_list_is_refused() -> None:
    document = _crossover_strategy()
    document["indicators"] = {"fast": "SMA"}
    with pytest.raises(EngineError, match="indicators must be a list"):
        compile_strategy(document)


def test_exit_conditions_that_are_not_a_list_is_refused() -> None:
    document = _crossover_strategy()
    document["exit"] = {"conditions": {"op": "gt"}}
    with pytest.raises(EngineError, match=r"exit\.conditions must be a list"):
        compile_strategy(document)


# --------------------------------------------------------------------------- #
# candle_extreme stop compilation                                               #
# --------------------------------------------------------------------------- #


def _with_stop(**params: object) -> dict[str, object]:
    document = _crossover_strategy()
    document["exit"] = {"stop_loss": {"type": "candle_extreme", "params": params}, "conditions": []}
    return document


def test_the_stop_is_compiled_and_sizes_the_window() -> None:
    strategy = _compiled(_with_stop(lookback=5, side="low"))
    assert strategy._stop_rule == StopRule(lookback=5, side="low")
    assert strategy._candles.maxlen == 7  # max(condition lookback 0, stop 5) + 2


def test_an_unsupported_stop_type_is_refused() -> None:
    document = _crossover_strategy()
    document["exit"] = {"stop_loss": {"type": "trailing", "params": {}}, "conditions": []}
    with pytest.raises(EngineError, match="unsupported stop type"):
        compile_strategy(document)


def test_a_bad_stop_lookback_is_refused() -> None:
    with pytest.raises(EngineError, match="lookback must be a positive int"):
        compile_strategy(_with_stop(lookback=0, side="low"))


def test_a_bad_stop_side_is_refused() -> None:
    with pytest.raises(EngineError, match="side must be 'low' or 'high'"):
        compile_strategy(_with_stop(lookback=2, side="middle"))


def test_stop_rule_level_over_low_and_high() -> None:
    """The extreme over the last N bars, newest-first — and `None` before there are N."""
    candles = [
        bar(2, open_="1.10", close="1.11", high="1.12", low="1.09"),
        bar(1, open_="1.09", close="1.10", high="1.11", low="1.07"),
        bar(0, open_="1.08", close="1.09", high="1.10", low="1.06"),
    ]
    assert StopRule(lookback=3, side="low").level(candles) == Decimal("1.06")
    assert StopRule(lookback=2, side="high").level(candles) == Decimal("1.12")
    assert StopRule(lookback=5, side="low").level(candles) is None  # not enough history yet


def test_a_duplicate_indicator_id_is_refused() -> None:
    document = _crossover_strategy()
    document["indicators"] = [
        {"id": "dup", "type": "SMA", "params": {"period": 2}},
        {"id": "dup", "type": "EMA", "params": {"period": 3}},
    ]
    with pytest.raises(EngineError, match="duplicate indicator id"):
        compile_strategy(document)


def test_the_history_window_is_sized_to_the_deepest_ref() -> None:
    """A ref reaching three bars back is useless if only two are kept; the buffer is the
    deepest lookback plus the current bar plus one for the edge operators' previous bar."""
    document = _crossover_strategy()
    document["entry"] = {
        "long": {
            "op": "breaks_above",
            "left": {"ref": "price.high"},
            "right": {"ref": "candle[-3].high"},
        },
        "short": None,
    }
    strategy = _compiled(document)
    assert strategy._candles.maxlen == 5  # 3 (deepest ref) + 1 (current) + 1 (edge shift)


# --------------------------------------------------------------------------- #
# Drift guard against the schema package's own fixtures                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "fixture",
    sorted((_FIXTURES / "valid").glob("*.json")),
    ids=lambda path: path.stem,
)
def test_every_valid_schema_fixture_compiles(fixture: Path) -> None:
    """Each strategy the schema package publishes as valid must compile into something the loop
    can drive. This is the seam that keeps the engine and the DSL from drifting without a code
    dependency between them.

    The assertion is `on_bar`, not a concrete type, and that is the whole point since ADR-0019: a
    valid document compiles either into the tree walker or into a setup's state machine, and what
    the two owe in common is exactly the one method `run()` calls. Asserting `CompiledStrategy`
    here would make every setup fixture a failure — which is how this test first told us the
    fixtures had grown a second shape.
    """
    document = json.loads(fixture.read_text(encoding="utf-8"))
    strategy = compile_strategy(document)
    assert callable(strategy.on_bar)


def test_the_canonical_fixture_builds_its_indicators() -> None:
    document = json.loads(
        (_FIXTURES / "valid" / "ma_cross_breakout.json").read_text(encoding="utf-8")
    )
    strategy = _compiled(document)
    assert set(strategy._indicators) == {"sma_fast", "sma_slow"}
    assert all(isinstance(indicator, SMA) for indicator in strategy._indicators.values())
    assert strategy.timeframe == dt.timedelta(hours=1)


# --------------------------------------------------------------------------- #
# How deep a tree reaches, and the buffers that have to be sized for it         #
# --------------------------------------------------------------------------- #


def _trend_strategy(bars: int) -> dict[str, object]:
    """Enter when a moving average has climbed on each of the last `bars` bars."""
    return {
        "schema_version": "1.0",
        "name": "buy a steady climb",
        "timeframe": "H1",
        "indicators": [{"id": "line", "type": "SMA", "params": {"period": 2, "source": "close"}}],
        "entry": {
            "long": {"op": "rising", "of": {"ref": "line"}, "bars": bars},
            "short": None,
        },
        "exit": {"conditions": []},
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def test_a_deep_trend_over_an_indicator_actually_fires() -> None:
    """⚠️ **The regression test for a bug whose symptom was silence.**

    An indicator's history used to be a `deque(maxlen=2)` — this bar and the previous one, which
    was every shift any operator could ask for while the deepest was `crosses_above`. A `rising`
    with `bars: 5` asks for five bars back, gets `None`, and `None` reads as "the indicator is
    still warming up", so the condition is false. On every bar. For ever.

    Nothing raises and nothing logs: the run completes, the report renders, and the strategy
    simply never traded — which is indistinguishable from a rule that was evaluated and declined.

    Eight rising closes with a 2-bar average leaves the average rising on every bar from the
    third, so five consecutive rises exist here and this must produce an entry.
    """
    candles = _series(["1.10", "1.11", "1.12", "1.13", "1.14", "1.15", "1.16", "1.17"])

    result = run(
        candles=candles,
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=compile_strategy(_trend_strategy(bars=5)),
        broker=ImmediateFillBroker(),
        risk=FixedRisk(),
    )

    entries = [fill for fill in result.fills if fill.order.intent is SignalKind.ENTRY]
    assert entries, "a rising(bars=5) over an indicator never fired — its history is too short"


def test_the_candle_window_is_sized_for_the_shift_a_trend_applies() -> None:
    """The same hole one layer over, on candle refs rather than on indicator history.

    `rising` over `candle[-2].close` with `bars: 3` reaches five closed bars back, and the
    window used to be sized as "deepest ref + 2". A ref that resolves to `None` makes the
    condition false, so this fails exactly the way the one above does — quietly.
    """
    document = _trend_strategy(bars=3)
    document["indicators"] = []
    document["entry"] = {
        "long": {"op": "rising", "of": {"ref": "candle[-2].close"}, "bars": 3},
        "short": None,
    }
    candles = _series(["1.10", "1.11", "1.12", "1.13", "1.14", "1.15", "1.16", "1.17"])

    result = run(
        candles=candles,
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=compile_strategy(document),
        broker=ImmediateFillBroker(),
        risk=FixedRisk(),
    )

    entries = [fill for fill in result.fills if fill.order.intent is SignalKind.ENTRY]
    assert entries, "a trend over a shifted candle ref never fired — the window is too short"


def test_a_document_with_only_comparisons_keeps_the_window_it_always_had() -> None:
    """The generalisation must not quietly grow every strategy's memory.

    A tree of comparisons shifts by one, so the window is the deepest ref plus two — the number
    that was hard-coded before. Asserted because "make it bigger to be safe" is the easy fix
    here, and it costs memory on every strategy for a case only one node needs.
    """
    strategy = _compiled(_crossover_strategy())

    assert strategy._candles.maxlen == 2
    assert all(history.maxlen == 2 for history in strategy._indicator_history.values())


def test_a_trend_grows_only_the_buffers_it_needs() -> None:
    strategy = _compiled(_trend_strategy(bars=5))

    assert strategy._candles.maxlen == 6
    assert all(history.maxlen == 6 for history in strategy._indicator_history.values())


def test_the_delivered_fixture_is_sized_for_the_tree_it_actually_has() -> None:
    """⚠️ **The shape the tests were missing, and it is the shape of the example we ship.**

    Every buffer test above puts the trend at the *root* of `entry.long`. The fixture does not:
    it has `all[breaks_above, rising(atr, 3), between]` on entry and `falling(canal, 2)` on
    exit. Three separate mistakes survive a suite that only ever compiles the root form —
    `_max_shift` not recursing through `all`/`any`/`not`, the depth being measured on the first
    tree only, and `_max_lookback` reading nothing from a `Between` — and each of them fails the
    same silent way:

    * nested: the `atr` history stays two long, `rising` resolves shifts 2 and 3 to `None`, and
      the entry is false on every bar of the run — no trades, no errors, a clean report;
    * on exit only: the *entry* sets the depth, so the exit condition is false for ever and a
      position is never closed by its own rule. Worse than not trading: the trade rides to its
      stop or to the end of the run, and the report looks like a strategy that holds.

    Compiling the shipped document is what ties the example to the sizing, so the two cannot
    drift apart without this going red.
    """
    document = json.loads(
        (_FIXTURES / "valid" / "channel_breakout_with_atr_filter.json").read_text(encoding="utf-8")
    )
    strategy = _compiled(document)

    # Deepest shift in the tree is `rising(atr, bars=3)`; deepest candle ref is 0; the stop
    # reaches 3 closed bars. So the window is max(0, 3) + 3 + 1.
    assert strategy._candles.maxlen == 7
    assert all(history.maxlen == 4 for history in strategy._indicator_history.values())


def test_a_trend_that_lives_only_in_the_exit_still_sizes_the_buffers() -> None:
    """The entry is a plain comparison and the exit is a deep trend — the asymmetric case.

    Split out from the fixture test because it is the one a reader would not think to write:
    the depth has to be taken over *every* tree, and an implementation that stops at the entry
    produces a strategy that opens positions and never closes them.
    """
    document = _trend_strategy(bars=1)
    document["entry"] = {
        "long": {"op": "gt", "left": {"ref": "price.high"}, "right": {"ref": "price.low"}},
        "short": None,
    }
    document["exit"] = {"conditions": [{"op": "falling", "of": {"ref": "line"}, "bars": 4}]}

    strategy = _compiled(document)

    assert strategy._candles.maxlen == 5
    assert all(history.maxlen == 5 for history in strategy._indicator_history.values())


def _box_then_breakout() -> list[Candle]:
    """Thirty bars going nowhere, then thirty of an accelerating rally."""
    candles: list[Candle] = []
    price = Decimal("1.10000")
    for index in range(30):
        close = price + (Decimal("0.00100") if index % 2 else Decimal("-0.00090"))
        candles.append(
            Candle(
                time=START + index * HOUR,
                open=price,
                high=max(price, close) + Decimal("0.00005"),
                low=min(price, close) - Decimal("0.00020"),
                close=close,
            )
        )
        price = close
    for index in range(30, 60):
        close = price + Decimal("0.00050") * (index - 29)
        candles.append(
            Candle(
                time=START + index * HOUR,
                open=price,
                high=close + Decimal("0.00020"),
                low=price - Decimal("0.00020"),
                close=close,
            )
        )
        price = close
    return candles


def test_the_delivered_fixture_can_actually_take_a_trade() -> None:
    """⚠️ **A document that compiles is not a document that can trade, and this one could not.**

    The first version of this fixture broke out against a channel whose window *included the
    current bar* — so `HIGHEST(20) >= high` held by construction and the entry was false on
    every bar of every market. Measured over this very series: zero entries, and the largest
    `high - channel` over the whole run was exactly `0.0000`. It compiled, it validated, it ran,
    and it was a strategy with no reachable entry.

    ⚠️ The second version still took none, for a different reason worth keeping: the channel
    test was `breaks_above`, an **edge**, and edges do not survive being ANDed with slow
    filters. The edge fired five times inside the box and never again once the real rally began
    — because price then stayed above the channel continuously — while `rising(atr, 3)` only
    came true three bars *into* the rally. Two correct conditions that are never true together.
    A level (`gt`) is what composes with a filter; the edge form is covered by the MA-cross
    fixture, which ANDs it with nothing slow.

    So the assertion is deliberately weak — *at least one* entry — because the point is
    reachability, not a P&L. A stronger number here would pin this test to the shape of a
    market invented to satisfy it.
    """
    document = json.loads(
        (_FIXTURES / "valid" / "channel_breakout_with_atr_filter.json").read_text(encoding="utf-8")
    )

    result = run(
        candles=_box_then_breakout(),
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=compile_strategy(document),
        broker=ImmediateFillBroker(),
        risk=FixedRisk(),
    )

    entries = [fill for fill in result.fills if fill.order.intent is SignalKind.ENTRY]
    assert entries, "the shipped fixture has no reachable entry"


def test_a_trend_buried_under_a_not_still_sizes_the_buffers() -> None:
    """The third way to nest a node, and the one no other test reaches.

    `all` and `any` recurse over a list; `not` recurses over a single child, which is a separate
    line and the only one with no coverage of its own. The recursion is generic — there is no
    per-node branch inside it — so the risk is low, and it is one test: "enter while the average
    is *not* falling for four bars" is an ordinary rule, and under a two-bar history it would be
    `False` for ever, which `not` turns into an entry on **every** bar. That failure is louder
    than silence and still wrong.
    """
    document = _trend_strategy(bars=1)
    document["entry"] = {
        "long": {"not": {"op": "falling", "of": {"ref": "line"}, "bars": 4}},
        "short": None,
    }

    strategy = _compiled(document)

    assert strategy._candles.maxlen == 5
    assert all(history.maxlen == 5 for history in strategy._indicator_history.values())


def test_a_between_reaching_back_over_candles_sizes_the_window() -> None:
    """`_max_lookback` has to read all three operands of a `Between`, bounds included.

    A band drawn between two shifted candle refs is the case: read only `value`, and the window
    is sized for the middle while the bounds resolve to `None` — the condition false for ever
    over refs that are perfectly legal.
    """
    document = _trend_strategy(bars=1)
    document["indicators"] = []
    document["entry"] = {
        "long": {
            "op": "between",
            "value": {"ref": "price.close"},
            "low": {"ref": "candle[-4].low"},
            "high": {"ref": "candle[-2].high"},
        },
        "short": None,
    }

    strategy = _compiled(document)

    assert strategy._candles.maxlen == 6
