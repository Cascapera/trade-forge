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
import datetime as dt
from decimal import Decimal
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.costs import NoCostModel, SpreadCostModel
from tradeforge_engine.domain import Candle, Side
from tradeforge_engine.loop import run
from tradeforge_engine.metrics import compute_metrics
from tradeforge_engine.risk import PercentRiskManager
from tradeforge_engine.strategy import compile_strategy
from tradeforge_engine.testing import EURUSD, HOUR, START, bar

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


def test_golden_r_multiple_and_context() -> None:
    """Each trade's result in R and the indicator snapshot that justified its entry.

    Trade 1 risked $100 (1% of $10 000) and made +$200 ⇒ +2R; trade 2 risked $100 and lost
    it ⇒ -1R. The context is the SMA pair on the decision bar (bar 3, then bar 10)."""
    broker = BacktestBroker(
        instrument=EURUSD,
        initial_capital=Decimal(10_000),
        cost_model=NoCostModel(),
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

    win, loss = result.trades
    assert win.r_multiple == Decimal("2")
    assert loss.r_multiple == Decimal("-1")
    assert win.context == {"fast": Decimal("1.09925"), "slow": Decimal("1.09900")}
    assert loss.context == {"fast": Decimal("1.10550"), "slow": Decimal("1.10400")}


def test_golden_metrics_by_hand() -> None:
    """The §5 metrics over the golden — the spec's hand-verified acceptance criteria."""
    broker = BacktestBroker(
        instrument=EURUSD,
        initial_capital=Decimal(10_000),
        cost_model=NoCostModel(),
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
    metrics = compute_metrics(
        trades=result.trades,
        equity_curve=result.equity_curve,
        initial_capital=Decimal(10_000),
    )

    assert metrics.net_profit == Decimal("100.00")
    assert metrics.win_rate == Decimal("0.5")
    assert metrics.payoff == Decimal("2")
    assert metrics.profit_factor == Decimal("2")
    assert metrics.expectancy == Decimal("50.00")
    # Peak equity $10 200 after trade 1, down to $10 100 after trade 2's stop.
    assert metrics.max_drawdown_abs == Decimal("100.00")
    # Trade 1 lasts one bar, trade 2 stops on its entry bar ⇒ average 30 minutes.
    assert metrics.avg_trade_duration == dt.timedelta(minutes=30)
    assert metrics.cagr is None  # 15-hour span is not annualised


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


# --------------------------------------------------------------------------- #
# RSI + a literal threshold, driven through the whole engine (PR-201)           #
# --------------------------------------------------------------------------- #


def _rsi_oversold_strategy() -> dict[str, object]:
    """Go long when RSI(2) crosses below 30 — the canonical oversold trigger, and the reason the
    literal operand had to exist: `30` is a constant, not a reference to anything."""
    return {
        "schema_version": "1.0",
        "name": "RSI oversold",
        "timeframe": "H1",
        "indicators": [{"id": "rsi", "type": "RSI", "params": {"period": 2}}],
        "entry": {
            "long": {"op": "crosses_below", "left": {"ref": "rsi"}, "right": {"value": 30}},
            "short": None,
        },
        # Stop only — no target — so the entry stays open to the end of this short series and the
        # test can assert the position the RSI signal opened, not the machinery of an exit.
        "exit": {
            "stop_loss": {"type": "candle_extreme", "params": {"lookback": 2, "side": "low"}},
            "conditions": [],
        },
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def _rsi_candles() -> list[Candle]:
    """Closes 1.10000, +100, +100, +100, then -300: RSI(2) reads 100 through bar 3 and 25 at bar
    4. The wide lows on bars 3-4 put the stop far below, so the long that opens stays open."""
    return [
        bar(0, open_="1.10000", close="1.10000"),
        bar(1, open_="1.10000", close="1.10100"),
        bar(2, open_="1.10100", close="1.10200"),
        bar(3, open_="1.10200", close="1.10300", high="1.10300", low="1.09000"),
        bar(4, open_="1.10300", close="1.10000", high="1.10300", low="1.09000"),
        bar(5, open_="1.10000", close="1.10200", high="1.10200", low="1.10000"),
        bar(6, open_="1.10200", close="1.10300", high="1.10300", low="1.10200"),
    ]


def test_an_rsi_oversold_entry_runs_through_the_engine() -> None:
    """RSI(2) hits 25 at bar 4 (avg_gain 0.0005, avg_loss 0.0015, RS 1/3), so `rsi crosses_below
    30` fires there — bar 3 read 100. The decision acts at bar 5's *open*, 1.10000, never on the
    signal bar: RSI plus a literal threshold, proven anti-lookahead-clean through the real engine.
    """
    broker = BacktestBroker(
        instrument=EURUSD,
        initial_capital=Decimal(10_000),
        cost_model=NoCostModel(),
        slippage_ticks=Decimal(0),
    )
    result = run(
        candles=_rsi_candles(),
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=compile_strategy(_rsi_oversold_strategy()),
        broker=broker,
        risk=PercentRiskManager(percent=Decimal(1)),
    )

    # The signal opened exactly one long, filled at the bar *after* it — never the signal bar.
    positions = broker.positions("EURUSD")
    assert len(positions) == 1
    assert positions[0].side is Side.LONG
    assert positions[0].entry_price == Decimal("1.10000")
    assert len(result.trades) == 0  # still open: nothing closed, so no round-trip yet

    # Reconciliation: nothing closed and no costs, so the balance is exactly where it started.
    assert positions[0].entry_costs == Decimal(0)
    assert result.final_account.balance == Decimal(10_000)
