"""The property every backtest must satisfy, or none of its numbers mean anything:

    sum(trade.net_pnl for trade in trades) == final equity - initial capital

If it does not hold, the trade table and the equity curve are two different accounts of the
same run — and every metric in PR-106 (expectancy, profit factor, R-multiple) is computed
from whichever of them happens to be wrong.

This is where the engine's original accounting bug lived, and it is worth being precise
about how it hid: the entry's cost was debited from the balance but never recorded on the
trade. The balance was right. The trades were wrong. Both were self-consistent, every
example-based test passed, and the trade table quietly reported more profit than the account
had ever held — systematically, since in forex the entry spread is usually the larger cost.

Property-based, because the statement is universally quantified. An example proves an
example (AGENTS.md §6).
"""

import datetime as dt
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from tradeforge_engine.domain import Fill, OrderRequest, Side, SignalKind
from tradeforge_engine.loop import run
from tradeforge_engine.portfolio import Portfolio
from tradeforge_engine.testing import (
    EURUSD,
    HOUR,
    FixedRisk,
    ImmediateFillBroker,
    ScriptedStrategy,
    close_out,
    entry,
    falling,
    rising,
)

T0 = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)

prices = st.decimals(
    min_value=Decimal("1.00000"),
    max_value=Decimal("1.50000"),
    places=5,
    allow_nan=False,
    allow_infinity=False,
)
costs = st.decimals(min_value=Decimal(0), max_value=Decimal(50), places=2)
volumes = st.decimals(min_value=Decimal("0.01"), max_value=Decimal(10), places=2)
sides = st.sampled_from([Side.LONG, Side.SHORT])


def round_trip(  # noqa: PLR0913 — keyword-only; a round trip simply has this many facts
    portfolio: Portfolio,
    *,
    side: Side,
    volume: Decimal,
    entry_price: Decimal,
    exit_price: Decimal,
    entry_costs: Decimal,
    exit_costs: Decimal,
    index: int,
) -> None:
    open_order = OrderRequest(
        symbol="EURUSD",
        side=side,
        intent=SignalKind.ENTRY,
        volume=volume,
        decided_at=T0 + index * 2 * HOUR,
    )
    close_order = OrderRequest(
        symbol="EURUSD",
        side=side,
        intent=SignalKind.EXIT,
        volume=volume,
        decided_at=T0 + (index * 2 + 1) * HOUR,
    )

    portfolio.apply(
        Fill(
            order=open_order,
            time=open_order.decided_at + HOUR,
            price=entry_price,
            volume=volume,
            costs=entry_costs,
        )
    )
    portfolio.apply(
        Fill(
            order=close_order,
            time=close_order.decided_at + HOUR,
            price=exit_price,
            volume=volume,
            costs=exit_costs,
        )
    )


@given(
    trades=st.lists(
        st.tuples(sides, volumes, prices, prices, costs, costs),
        min_size=1,
        max_size=12,
    )
)
@settings(max_examples=200)
def test_the_trades_always_reconcile_with_the_account(
    trades: list[tuple[Side, Decimal, Decimal, Decimal, Decimal, Decimal]],
) -> None:
    """Any sequence of round trips, any sides, any prices, costs on **both** legs."""
    initial = Decimal(1_000_000)
    portfolio = Portfolio(initial_capital=initial, instrument=EURUSD)

    for index, (side, volume, entry_price, exit_price, entry_costs, exit_costs) in enumerate(
        trades
    ):
        round_trip(
            portfolio,
            side=side,
            volume=volume,
            entry_price=entry_price,
            exit_price=exit_price,
            entry_costs=entry_costs,
            exit_costs=exit_costs,
            index=index,
        )

    recorded = sum((trade.net_pnl for trade in portfolio.trades), Decimal(0))
    moved = portfolio.account().balance - initial

    assert recorded == moved


@given(entry_costs=costs, exit_costs=costs)
def test_a_trade_reports_the_cost_of_both_legs(entry_costs: Decimal, exit_costs: Decimal) -> None:
    """The bug, stated directly. `costs` is the round trip, not the exit."""
    portfolio = Portfolio(initial_capital=Decimal(100_000), instrument=EURUSD)

    round_trip(
        portfolio,
        side=Side.LONG,
        volume=Decimal(1),
        entry_price=Decimal("1.10000"),
        exit_price=Decimal("1.10100"),
        entry_costs=entry_costs,
        exit_costs=exit_costs,
        index=0,
    )

    [trade] = portfolio.trades
    assert trade.costs == entry_costs + exit_costs
    assert trade.net_pnl == trade.gross_pnl - trade.costs


def test_the_whole_loop_reconciles_with_costs_on_both_legs() -> None:
    """End to end, through the engine, with a broker that charges for entering and exiting."""
    result = run(
        candles=rising(20),
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=ScriptedStrategy(
            script={2: [entry()], 7: [close_out()], 10: [entry()], 16: [close_out()]}
        ),
        broker=ImmediateFillBroker(costs=Decimal(7)),
        risk=FixedRisk(),
    )

    recorded = sum((trade.net_pnl for trade in result.trades), Decimal(0))
    moved = result.final_account.balance - Decimal(10_000)

    assert len(result.trades) == 2
    assert all(trade.costs == Decimal(14) for trade in result.trades)
    assert recorded == moved


def test_a_short_through_the_whole_loop_makes_money_when_price_falls() -> None:
    """The short path, end to end. Until now it was only exercised inside the Portfolio.

    A codebase correct in one direction and wrong in the other is the normal outcome, and
    the wrong direction is the one nobody trades until the day it ships.
    """
    result = run(
        candles=falling(12),
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=ScriptedStrategy(script={1: [entry(Side.SHORT)], 8: [close_out(Side.SHORT)]}),
        broker=ImmediateFillBroker(),
        risk=FixedRisk(),
    )

    [trade] = result.trades
    assert trade.side is Side.SHORT
    assert trade.net_pnl > 0
    assert result.final_account.balance > Decimal(10_000)
