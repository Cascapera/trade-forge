"""PR-302-A's acceptance: the certified backtest, re-run through the live path.

The claim of this PR is that a paper session is the *same* engine with a different candle
source — not a similar one. A claim like that is only worth what its counter-example costs, so
this file does not invent a scenario. It takes the golden dataset — the one whose every trade
and P&L is derived by hand in `packages/engine/tests/golden/ma_cross_golden.md`, and which
`test_golden_matches_the_hand_worked_spreadsheet` pins to the cent — publishes it bar by bar
onto a Redis stream, drives `iter_run` from `CandleStream`, and demands the same numbers.

What that separates, which a smaller test would not:

* a bar lost, duplicated or reordered by the consumer;
* a field mangled on the wire (a price parsed through `float`, a spread dropped);
* the loop behaving differently when fed lazily than when fed a list;
* the equity curve being rebuilt from `BarOutcome`s in a way that does not match `RunResult`.

⚠️ This is the *path*, not the network. The client is the same double as
`test_candle_stream.py`, so the two share a blind spot and both defer to the integration test
for what a real server does.
"""

import csv
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pytest

from tradeforge_api.live import CandleStream
from tradeforge_api.live.testing import FakeRedisStreams, published
from tradeforge_collector.live import Subscription
from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.costs import BarSpreadCostModel, NoCostModel
from tradeforge_engine.domain import Candle
from tradeforge_engine.loop import iter_run, run
from tradeforge_engine.risk import PercentRiskManager
from tradeforge_engine.strategy import compile_strategy
from tradeforge_engine.testing import EURUSD, HOUR, START

EURUSD_H1 = Subscription(symbol="EURUSD", timeframe="H1")
_GOLDEN_CSV = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "engine"
    / "tests"
    / "golden"
    / "ma_cross_golden.csv"
)


def golden_candles(*, spread: int = 0) -> list[Candle]:
    """The golden dataset, optionally carrying a spread the original does not have.

    The CSV has no spread column — it predates the live path and its worksheet charges no
    costs. Stamping one on here is how the same certified bars can also exercise
    `BarSpreadCostModel`, without touching a file whose numbers other tests depend on.
    """
    with _GOLDEN_CSV.open(encoding="utf-8") as handle:
        return [
            Candle(
                time=START + int(row["index"]) * HOUR,
                open=Decimal(row["open"]),
                high=Decimal(row["high"]),
                low=Decimal(row["low"]),
                close=Decimal(row["close"]),
                spread=spread,
            )
            for row in csv.DictReader(handle)
        ]


def ma_cross_strategy() -> dict[str, object]:
    """The golden's own document, verbatim. Kept here rather than imported from the engine's
    test package because a test package is not an importable contract — and copied in full
    rather than trimmed, so a drift between the two shows up as a different result, loudly,
    instead of as a different strategy quietly agreeing."""
    return {
        "schema_version": "1.0",
        "name": "MA cross golden",
        "timeframe": "H1",
        "indicators": [
            {"id": "fast", "type": "SMA", "params": {"period": 2}},
            {"id": "slow", "type": "SMA", "params": {"period": 3}},
        ],
        "entry": {
            "long": {"op": "crosses_above", "left": {"ref": "fast"}, "right": {"ref": "slow"}},
            "short": None,
        },
        "exit": {
            "stop_loss": {"type": "candle_extreme", "params": {"lookback": 2, "side": "low"}},
            "take_profit": {"type": "risk_multiple", "params": {"rr": 2.0}},
            "conditions": [],
        },
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def a_broker(cost_model: NoCostModel | BarSpreadCostModel) -> BacktestBroker:
    return BacktestBroker(
        instrument=EURUSD,
        initial_capital=Decimal(10_000),
        cost_model=cost_model,
        slippage_ticks=Decimal(0),
        take_profit_rr=Decimal(2),
    )


def live_stream(candles: list[Candle], *, group: str) -> CandleStream:
    """The candles on a stream, encoded by the collector's own publisher, ready to be read
    back. `start_id="0"` because the bars are seeded before the session starts — in production
    the collector publishes while the session is already listening."""
    client = FakeRedisStreams(EURUSD_H1, published(EURUSD_H1, *candles))
    return CandleStream(client, EURUSD_H1, group=group, block_ms=10, start_id="0")


def test_the_golden_backtest_produces_the_same_result_through_the_live_path() -> None:
    """The acceptance. Same strategy object, same broker configuration, same bars — one fed as
    a list, the other read off a stream one bar at a time — and the trades, the fills and the
    equity curve have to match field for field."""
    backtest = run(
        candles=golden_candles(),
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=compile_strategy(ma_cross_strategy()),
        broker=a_broker(NoCostModel()),
        risk=PercentRiskManager(percent=Decimal(1)),
    )

    live_broker = a_broker(NoCostModel())
    stream = live_stream(golden_candles(), group="acceptance")
    outcomes = []
    for outcome in iter_run(
        candles=stream.candles(),
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=compile_strategy(ma_cross_strategy()),
        broker=live_broker,
        risk=PercentRiskManager(percent=Decimal(1)),
    ):
        outcomes.append(outcome)
        # The stream never ends — in production that is the point, and here it means the test
        # has to decide when it has seen the run. The golden's length is the honest boundary.
        if len(outcomes) == backtest.candles_processed:
            break

    assert [asdict(t) for t in live_broker.trades()] == [asdict(t) for t in backtest.trades]
    assert [asdict(f) for o in outcomes for f in o.fills] == [asdict(f) for f in backtest.fills]
    assert [asdict(o.equity) for o in outcomes] == [asdict(p) for p in backtest.equity_curve]
    assert live_broker.account().equity == backtest.final_account.equity == Decimal("10100.00")


def test_the_certified_numbers_survive_the_wire() -> None:
    """Stated against the worksheet rather than against the other run, so that a bug affecting
    *both* paths identically cannot hide behind their agreement."""
    live_broker = a_broker(NoCostModel())
    stream = live_stream(golden_candles(), group="worksheet")
    for index, _outcome in enumerate(
        iter_run(
            candles=stream.candles(),
            timeframe=HOUR,
            instrument=EURUSD,
            strategy=compile_strategy(ma_cross_strategy()),
            broker=live_broker,
            risk=PercentRiskManager(percent=Decimal(1)),
        )
    ):
        if index + 1 == len(golden_candles()):
            break

    trades = live_broker.trades()
    assert len(trades) == 2
    assert trades[0].entry_price == Decimal("1.10100")
    assert trades[0].exit_price == Decimal("1.10900")
    assert trades[0].reason == "tp"
    assert trades[0].net_pnl == Decimal("200.00")
    assert trades[1].entry_price == Decimal("1.10600")
    assert trades[1].exit_price == Decimal("1.10100")
    assert trades[1].reason == "sl"
    assert trades[1].net_pnl == Decimal("-100.00")


def test_a_paper_session_is_charged_the_spread_the_bars_carried() -> None:
    """The other half of the PR: the same run, priced by the bar instead of by a constant.

    Costs are asserted against the *fixed*-spread arithmetic on the same bars, so the test says
    what the model does rather than restating its formula. Four legs — two trades, entry and
    exit — at half of 20 points on EURUSD (tick_value 1) is $10 a leg for a full lot, scaled by
    the volume the risk manager chose.
    """
    live_broker = a_broker(BarSpreadCostModel())
    stream = live_stream(golden_candles(spread=20), group="charged")
    for index, _outcome in enumerate(
        iter_run(
            candles=stream.candles(),
            timeframe=HOUR,
            instrument=EURUSD,
            strategy=compile_strategy(ma_cross_strategy()),
            broker=live_broker,
            risk=PercentRiskManager(percent=Decimal(1)),
        )
    ):
        if index + 1 == len(golden_candles()):
            break

    trades = live_broker.trades()
    assert len(trades) == 2, "the spread changed which trades were taken, not just their cost"

    for trade in trades:
        assert trade.costs == Decimal(10) * trade.volume * 2, "not half a 20-point spread a leg"

    # And it actually cost something — a model charging zero would satisfy an assertion written
    # only as "costs are consistent".
    assert sum((t.costs for t in trades), Decimal(0)) > Decimal(0)
    assert live_broker.account().equity < Decimal("10100.00"), "the free-of-charge result"


def test_the_spread_reaches_the_engine_through_the_wire_not_from_the_fixture() -> None:
    """A guard on the previous test's premise: had the spread been dropped in encoding, the
    bars arriving at the broker would carry `0` and `BarSpreadCostModel` would refuse them —
    but only if something actually reads them. This checks the field survives the round trip
    before any of that."""
    stream = live_stream(golden_candles(spread=37), group="wire")
    candles = stream.candles()

    first = next(candles)
    assert first.spread == 37
    assert first.time == START
    assert first.close == golden_candles()[0].close


def test_a_bar_with_no_spread_stops_a_paper_session() -> None:
    """The golden CSV carries no spread, and a paper session fed it must not report a
    cost-free edge — the refusal arrives at the first fill, which is where it can still be
    acted on."""
    live_broker = a_broker(BarSpreadCostModel())
    stream = live_stream(golden_candles(), group="silent")

    with pytest.raises(ValueError, match="carries no spread"):
        for _ in iter_run(
            candles=stream.candles(),
            timeframe=HOUR,
            instrument=EURUSD,
            strategy=compile_strategy(ma_cross_strategy()),
            broker=live_broker,
            risk=PercentRiskManager(percent=Decimal(1)),
        ):
            pass
