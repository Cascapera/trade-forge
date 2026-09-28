"""A run warms up before its window, and books only from `book_from` (ADR-0030).

The claim the ADR rests on: a run that reads bars before its window, trading on them as shadow,
takes **exactly the trades a longer run takes from `book_from` on** — the same entries, exits and
R — while its account opens at `book_from` with the initial capital. That is what makes a run's
result stop depending on the start date chosen. Held on the ledger by hand, and on whole runs over
the seeded walks that trade, alone and in a batch.
"""

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest

from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.batch import BatchMember, run_batch
from tradeforge_engine.domain import Candle, ClosedTrade, Side, SignalKind
from tradeforge_engine.loop import QuietBars, run
from tradeforge_engine.portfolio import Portfolio
from tradeforge_engine.risk import PercentRiskManager
from tradeforge_engine.setup_factory import reading_for, shared_reading
from tradeforge_engine.strategy import compile_strategy
from tradeforge_engine.testing import EURUSD, HOUR

from .test_batch import _grid
from .test_market_reading import _walk
from .test_portfolio import T0, T1, T2, fill, order

CAPITAL = Decimal(10_000)
T3 = T2 + dt.timedelta(hours=1)


def _closing(time: dt.datetime, close: str) -> Candle:
    price = Decimal(close)
    return Candle(time=time, open=price, high=price, low=price, close=price, tick_volume=1)


class TestTheLedger:
    """Worked by hand: one lot of EURUSD, $1 a tick."""

    def test_a_position_opened_before_book_from_is_never_booked(self) -> None:
        portfolio = Portfolio(initial_capital=CAPITAL, instrument=EURUSD, book_from=T2)

        portfolio.apply(fill(order(), price="1.10000", time=T1, costs="7"))
        # Open across `book_from`, 100 ticks up: worth nothing to the account.
        portfolio.mark_to_market(_closing(T2, "1.10100"))
        assert portfolio.account().balance == CAPITAL
        assert portfolio.account().equity == CAPITAL

        trade = portfolio.apply(
            fill(order(intent=SignalKind.EXIT), price="1.11000", time=T3, costs="7")
        )

        # Built and handed back — the broker's caller reads it as always — but not kept.
        assert trade is not None
        assert trade.gross_pnl == Decimal(1000)
        assert portfolio.trades == ()
        assert portfolio.account().balance == CAPITAL
        assert portfolio.account().equity == CAPITAL

    def test_a_position_opened_at_book_from_is_the_account_s(self) -> None:
        portfolio = Portfolio(initial_capital=CAPITAL, instrument=EURUSD, book_from=T1)

        portfolio.apply(fill(order(), price="1.10000", time=T1, costs="7"))
        portfolio.mark_to_market(_closing(T2, "1.10100"))
        # 100 ticks x $1 on the open lot, less the $7 the entry cost.
        assert portfolio.account().equity == CAPITAL - 7 + 100
        portfolio.apply(fill(order(intent=SignalKind.EXIT), price="1.11000", time=T3, costs="7"))

        (trade,) = portfolio.trades
        assert trade.net_pnl == Decimal(1000 - 14)
        assert portfolio.account().balance == CAPITAL + 1000 - 14

    def test_after_a_shadow_the_ledger_books_as_if_it_had_opened_then(self) -> None:
        """The property of the ledger — the trades sum to what the account made — over a warm-up
        trade followed by a booked one."""
        portfolio = Portfolio(initial_capital=CAPITAL, instrument=EURUSD, book_from=T1)
        portfolio.apply(fill(order(), price="1.10000", time=T0, costs="3"))
        portfolio.apply(fill(order(intent=SignalKind.EXIT), price="1.09000", time=T1, costs="3"))
        portfolio.apply(fill(order(Side.SHORT), price="1.09000", time=T2, costs="5"))
        portfolio.apply(
            fill(order(Side.LONG, SignalKind.EXIT), price="1.08500", time=T3, costs="5")
        )

        (trade,) = portfolio.trades
        assert trade.net_pnl == Decimal(500 - 10)
        assert sum(one.net_pnl for one in portfolio.trades) == (
            portfolio.account().equity - CAPITAL
        )

    def test_without_book_from_nothing_is_shadow(self) -> None:
        portfolio = Portfolio(initial_capital=CAPITAL, instrument=EURUSD)
        portfolio.apply(fill(order(), price="1.10000", time=T0))
        portfolio.apply(fill(order(intent=SignalKind.EXIT), price="1.10100", time=T1))

        assert len(portfolio.trades) == 1


def _broker(book_from: dt.datetime | None) -> BacktestBroker:
    return BacktestBroker(
        instrument=EURUSD, initial_capital=CAPITAL, take_profit_rr=Decimal(2), book_from=book_from
    )


def _run(document: dict[str, Any], candles: list[Candle], book_from: dt.datetime | None) -> Any:
    return run(
        candles=candles,
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=compile_strategy(document),
        broker=_broker(book_from),
        risk=PercentRiskManager(percent=Decimal(1)),
        record_snapshots=False,
    )


def _course(trades: tuple[ClosedTrade, ...]) -> list[tuple[Any, ...]]:
    """What a trade did, whatever the account it was sized on: when, which way, where, in R.

    Not the money: a longer run sizes its later trades on the balance its earlier ones left, and
    the run that warms up sizes them on the initial capital. With no costs, R does not depend on
    the size (the lot cancels out of result over risk).
    """
    return [
        (t.entry_time, t.exit_time, t.side, t.entry_price, t.exit_price, t.reason, t.r_multiple)
        for t in trades
    ]


@pytest.mark.parametrize(("seed", "htf"), [(0, True), (4, True), (1, False)])
def test_a_warmed_run_takes_the_trades_a_longer_run_takes_from_book_from(
    seed: int, htf: bool
) -> None:
    candles = _walk(seed)
    book_from = candles[len(candles) // 2].time
    booked = 0
    for document in _grid(htf=htf):
        longer = _run(document, candles, None)
        warmed = _run(document, candles, book_from)

        assert _course(warmed.trades) == _course(
            tuple(t for t in longer.trades if t.entry_time >= book_from)
        )
        # The account opens at `book_from`, with its capital, and books only from there.
        assert warmed.equity_curve[0].time == book_from
        assert warmed.equity_curve[0].equity == CAPITAL or warmed.trades
        assert warmed.warmed == sum(1 for candle in candles if candle.time < book_from)
        assert warmed.candles_processed == len(candles) - warmed.warmed
        assert all(fill.time >= book_from for fill in warmed.fills)
        assert sum(t.net_pnl for t in warmed.trades) == warmed.final_account.equity - CAPITAL or (
            # A booked position still open at the end is in the equity and not in the trades.
            warmed.final_account.balance != warmed.final_account.equity
        )
        booked += len(warmed.trades)
    # The walks have to have traded after `book_from`, or the equality is between empty lists.
    assert booked > 0


def _averaged(long_average: int) -> dict[str, Any]:
    """His MME9 breakout under a long average: a setup that cannot trade until the average is
    warm — the family where the ADR measured the first year differing most."""
    return {
        "schema_version": "1.0",
        "name": "averaged",
        "timeframe": "H1",
        "setup": {
            "type": "mme9_breakout",
            "params": {"side": "both", "period": 9, "long_average_period": long_average},
        },
        "exit": {"take_profit": {"type": "risk_multiple", "params": {"rr": 2}}},
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def test_a_run_s_first_trades_depend_on_the_bars_before_it_and_warming_removes_that() -> None:
    """The defect the ADR measured, held here in small: over the window alone a run can take other
    trades than the same run warmed on the bars before it; warmed, it takes the longer run's."""
    differed = 0
    for seed in (0, 4, 10):
        candles = _walk(seed)
        book_from = candles[len(candles) // 2].time
        window = [candle for candle in candles if candle.time >= book_from]
        for long_average in (50, 200):
            document = _averaged(long_average)
            cold = _run(document, window, None)
            warmed = _run(document, candles, book_from)
            longer = _run(document, candles, None)
            in_window = tuple(t for t in longer.trades if t.entry_time >= book_from)
            assert _course(warmed.trades) == _course(in_window)
            differed += _course(cold.trades) != _course(in_window)
    assert differed > 0, "no cold start differed: the walks no longer show the defect"


@pytest.mark.parametrize("seed", [0, 4])
def test_a_batch_warms_up_as_run_does(seed: int) -> None:
    candles = _walk(seed)
    book_from = candles[len(candles) // 2].time
    documents = _grid(htf=True)
    (key,) = {shared_reading(document["setup"], timeframe=HOUR) for document in documents}
    assert key is not None
    reading = reading_for(key, timeframe=HOUR)
    members = [
        BatchMember(
            strategy=compile_strategy(document, reading=reading),
            broker=_broker(book_from),
            risk=PercentRiskManager(percent=Decimal(1)),
        )
        for document in documents
    ]

    outcomes = run_batch(
        candles=candles,
        timeframe=HOUR,
        instrument=EURUSD,
        reading=reading,
        members=members,
        record_snapshots=False,
        quiet=QuietBars.SKIP,
    )

    assert [outcome.result for outcome in outcomes] == [
        _run(document, candles, book_from) for document in documents
    ]
