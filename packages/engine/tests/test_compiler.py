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
from decimal import Decimal
from pathlib import Path

import pytest

from tradeforge_engine.domain import Candle, SignalKind
from tradeforge_engine.errors import EngineError
from tradeforge_engine.indicators import SMA
from tradeforge_engine.loop import run
from tradeforge_engine.strategy import StopRule, compile_strategy
from tradeforge_engine.testing import EURUSD, HOUR, START, FixedRisk, ImmediateFillBroker, bar

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
    strategy = compile_strategy(_crossover_strategy())

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
    strategy = compile_strategy(_crossover_strategy())
    assert strategy.timeframe == dt.timedelta(hours=1)
    assert strategy.name == "fast crosses slow"


def test_an_entry_only_fires_while_flat_and_an_exit_only_while_in_a_position() -> None:
    """The strategy checks exits when it holds a position and entries when it does not — the
    order the sdd.md loop prescribes, and the reason a single crossover makes one round trip,
    not a new entry on every subsequent bar it stays true."""
    candles = _series(
        ["1.100", "1.090", "1.080", "1.070", "1.090", "1.110", "1.130", "1.140", "1.150", "1.160"]
    )
    strategy = compile_strategy(_crossover_strategy())
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


def test_an_unsupported_schema_version_is_refused() -> None:
    """A saved strategy is immutable for its version; the engine refuses one it was not built
    to interpret rather than guess and reproduce a different backtest (AGENTS.md §5.5)."""
    document = _crossover_strategy()
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
    strategy = compile_strategy(document)
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
    strategy = compile_strategy(_with_stop(lookback=5, side="low"))
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
    strategy = compile_strategy(document)
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
    """Each strategy the schema package publishes as valid must compile. This is the seam that
    keeps the engine and the DSL from drifting without a code dependency between them."""
    document = json.loads(fixture.read_text(encoding="utf-8"))
    strategy = compile_strategy(document)
    assert strategy.name


def test_the_canonical_fixture_builds_its_indicators() -> None:
    document = json.loads(
        (_FIXTURES / "valid" / "ma_cross_breakout.json").read_text(encoding="utf-8")
    )
    strategy = compile_strategy(document)
    assert set(strategy._indicators) == {"sma_fast", "sma_slow"}
    assert all(isinstance(indicator, SMA) for indicator in strategy._indicators.values())
    assert strategy.timeframe == dt.timedelta(hours=1)
