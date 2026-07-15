"""The golden backtest: the engine, driven end to end, checked against numbers worked by hand.

`golden/ma_cross_golden.csv` is the dataset; `golden/ma_cross_golden.md` derives every trade
and P&L by hand. This test asserts the engine reproduces that worksheet to the cent — the
single most important test in the package, because it is the one that would catch a fill, a
stop, a size or a cost being subtly wrong in a way no unit test isolates.

It also states the reconciliation property — `sum(net_pnl) == equity change` — as both a
concrete assertion on the golden (which ends flat) and a property-based one over random data
(which holds open-position or not, because a still-open entry cost is accounted for).
"""

import csv
from decimal import Decimal
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.costs import NoCostModel, SpreadCostModel
from tradeforge_engine.domain import Candle, Side
from tradeforge_engine.loop import run
from tradeforge_engine.risk import PercentRiskManager
from tradeforge_engine.strategy import compile_strategy
from tradeforge_engine.testing import EURUSD, HOUR, START

_GOLDEN_CSV = Path(__file__).resolve().parent / "golden" / "ma_cross_golden.csv"


def _golden_candles() -> list[Candle]:
    candles: list[Candle] = []
    with _GOLDEN_CSV.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            candles.append(
                Candle(
                    time=START + int(row["index"]) * HOUR,
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                )
            )
    return candles


def _ma_cross_strategy() -> dict[str, object]:
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


def test_golden_matches_the_hand_worked_spreadsheet() -> None:
    """Two trades, every field derived in `ma_cross_golden.md`."""
    broker = BacktestBroker(
        instrument=EURUSD,
        initial_capital=Decimal(10_000),
        cost_model=NoCostModel(),
        slippage_ticks=Decimal(0),
        take_profit_rr=Decimal(2),
    )
    result = run(
        candles=_golden_candles(),
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=compile_strategy(_ma_cross_strategy()),
        broker=broker,
        risk=PercentRiskManager(percent=Decimal(1)),
    )

    assert len(result.trades) == 2

    # Trade 1 — target
    win = result.trades[0]
    assert win.side is Side.LONG
    assert win.entry_price == Decimal("1.10100")
    assert win.volume == Decimal("0.25")
    assert win.exit_price == Decimal("1.10900")
    assert win.reason == "tp"
    assert win.net_pnl == Decimal("200.00")

    # Trade 2 — stop, on its own entry bar, sized against the grown equity
    loss = result.trades[1]
    assert loss.entry_price == Decimal("1.10600")
    assert loss.volume == Decimal("0.20")  # 0.204 raw, floored to the 0.01 lot step
    assert loss.exit_price == Decimal("1.10100")
    assert loss.reason == "sl"
    assert loss.net_pnl == Decimal("-100.00")

    assert result.final_account.equity == Decimal("10100.00")
    # Reconciliation: the run ends flat, so the trades account for the whole equity change.
    assert sum((trade.net_pnl for trade in result.trades), Decimal(0)) == Decimal("100.00")
    assert broker.positions("EURUSD") == ()


def test_costs_come_straight_off_the_bottom_line() -> None:
    """The same run with a 10-point spread: each trade pays $5 in and $5 out, times its lots —
    and the reconciliation still holds exactly, costs on both legs."""
    broker = BacktestBroker(
        instrument=EURUSD,
        initial_capital=Decimal(10_000),
        cost_model=SpreadCostModel(spread_points=Decimal(10)),
        take_profit_rr=Decimal(2),
    )
    result = run(
        candles=_golden_candles(),
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=compile_strategy(_ma_cross_strategy()),
        broker=broker,
        risk=PercentRiskManager(percent=Decimal(1)),
    )

    reconciled = sum((trade.net_pnl for trade in result.trades), Decimal(0))
    assert reconciled == result.final_account.equity - Decimal(10_000)
    # Costs made every trade worse than its no-cost twin.
    assert all(trade.costs > Decimal(0) for trade in result.trades)


def _candles_from_closes(closes: list[Decimal]) -> list[Candle]:
    candles: list[Candle] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        open_ = previous
        high = max(open_, close) + Decimal("0.00050")
        low = min(open_, close) - Decimal("0.00050")
        candles.append(
            Candle(time=START + index * HOUR, open=open_, high=high, low=low, close=close)
        )
        previous = close
    return candles


@given(
    steps=st.lists(
        st.integers(min_value=-40, max_value=40),
        min_size=4,
        max_size=40,
    )
)
def test_reconciliation_holds_over_random_walks(steps: list[int]) -> None:
    """`sum(net_pnl) == balance change`, adjusted for a position still open at the end.

    Balance moves by -entry_cost when a position opens and by +gross-exit_cost when it closes,
    so over a closed trade it moves by exactly `net_pnl`. A position still open at the end has
    only paid its entry cost, so the identity carries that one term — and with it the property
    holds for *every* run, not only the ones that happen to end flat.
    """
    price = Decimal("1.20000")
    closes: list[Decimal] = []
    for step in steps:
        price += Decimal(step) * Decimal("0.00010")
        closes.append(price)

    broker = BacktestBroker(
        instrument=EURUSD,
        initial_capital=Decimal(10_000),
        cost_model=SpreadCostModel(spread_points=Decimal(4)),
        take_profit_rr=Decimal(2),
    )
    result = run(
        candles=_candles_from_closes(closes),
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=compile_strategy(_ma_cross_strategy()),
        broker=broker,
        risk=PercentRiskManager(percent=Decimal(1)),
    )

    open_positions = broker.positions("EURUSD")
    open_entry_cost = open_positions[0].entry_costs if open_positions else Decimal(0)
    realised = sum((trade.net_pnl for trade in result.trades), Decimal(0))
    assert realised == result.final_account.balance - Decimal(10_000) + open_entry_cost
