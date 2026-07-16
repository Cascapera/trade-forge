"""The ledger, checked by hand.

Every number below was worked out on paper first. That is the standard for the engine's
accounting from here on: a test that merely agrees with the code proves the code agrees
with itself.

The formula under test is `(move / tick_size) * tick_value * volume`, and it is the only
one in the system. One tick of EURUSD (0.00001) on a standard lot is worth $1; one tick of
AAPL (0.01) on one share is worth one cent. Same line of code, entirely different money.
"""

import datetime as dt
from decimal import Decimal

import pytest

from tradeforge_engine.domain import Fill, InstrumentSpec, OrderRequest, Side, SignalKind
from tradeforge_engine.errors import EngineError
from tradeforge_engine.portfolio import Portfolio
from tradeforge_engine.testing import AAPL, EURUSD, bar

T0 = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
T1 = dt.datetime(2024, 1, 1, 1, tzinfo=dt.UTC)
T2 = dt.datetime(2024, 1, 1, 2, tzinfo=dt.UTC)


def order(
    side: Side = Side.LONG,
    intent: SignalKind = SignalKind.ENTRY,
    *,
    volume: str = "1",
    symbol: str = "EURUSD",
    stop: str | None = None,
) -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        side=side,
        intent=intent,
        volume=Decimal(volume),
        decided_at=T0,
        stop_loss=Decimal(stop) if stop is not None else None,
    )


def fill(request: OrderRequest, *, price: str, time: dt.datetime, costs: str = "0") -> Fill:
    return Fill(
        order=request,
        time=time,
        price=Decimal(price),
        volume=request.volume,
        costs=Decimal(costs),
    )


def a_portfolio(instrument: InstrumentSpec = EURUSD, capital: str = "10000") -> Portfolio:
    return Portfolio(initial_capital=Decimal(capital), instrument=instrument)


def test_a_long_that_gains_100_pips_on_one_lot_makes_1000_dollars() -> None:
    """Worked by hand: 1.11000 - 1.10000 = 0.01000. That is 1000 ticks of 0.00001.
    1000 ticks x $1 per tick x 1 lot = $1 000.
    """
    portfolio = a_portfolio()

    portfolio.apply(fill(order(), price="1.10000", time=T1))
    trade = portfolio.apply(fill(order(intent=SignalKind.EXIT), price="1.11000", time=T2))

    assert trade is not None
    assert trade.gross_pnl == Decimal(1000)
    assert trade.net_pnl == Decimal(1000)
    assert portfolio.account().balance == Decimal(11_000)


def test_a_short_makes_money_when_the_price_falls() -> None:
    """Written once, for a long, and flipped by the side's sign.

    A separate short branch is how a codebase ends up correct in one direction and subtly
    wrong in the other — and the wrong one is the one nobody trades until the day it ships.
    """
    portfolio = a_portfolio()

    portfolio.apply(fill(order(Side.SHORT), price="1.11000", time=T1))
    trade = portfolio.apply(fill(order(Side.SHORT, SignalKind.EXIT), price="1.10000", time=T2))

    assert trade is not None
    assert trade.gross_pnl == Decimal(1000)


def test_a_short_loses_money_when_the_price_rises() -> None:
    portfolio = a_portfolio()

    portfolio.apply(fill(order(Side.SHORT), price="1.10000", time=T1))
    trade = portfolio.apply(fill(order(Side.SHORT, SignalKind.EXIT), price="1.10500", time=T2))

    assert trade is not None
    assert trade.gross_pnl == Decimal(-500)
    assert portfolio.account().balance == Decimal(9_500)


def test_a_stock_uses_the_same_formula_and_gets_entirely_different_money() -> None:
    """100 shares of AAPL, $190.00 to $192.50. By hand: $2.50 x 100 = $250.

    Same line of code as the forex trade above. `tick_size` and `tick_value` are what make
    it come out right — which is why they are data in a table and not constants in the core.
    """
    portfolio = a_portfolio(AAPL, capital="50000")

    portfolio.apply(fill(order(volume="100", symbol="AAPL"), price="190.00", time=T1))
    trade = portfolio.apply(
        fill(order(intent=SignalKind.EXIT, volume="100", symbol="AAPL"), price="192.50", time=T2)
    )

    assert trade is not None
    assert trade.gross_pnl == Decimal(250)


def test_costs_are_subtracted_from_the_gross() -> None:
    portfolio = a_portfolio()

    portfolio.apply(fill(order(), price="1.10000", time=T1, costs="7"))
    trade = portfolio.apply(
        fill(order(intent=SignalKind.EXIT), price="1.10100", time=T2, costs="7")
    )

    assert trade is not None
    assert trade.gross_pnl == Decimal(100)
    # BOTH legs. The entry cost 7 and the exit cost 7, and the trade reports the round trip.
    # Recording only the exit is the bug this assertion exists to pin down: the balance
    # would still be right, the trade table would quietly report 93 instead of 86, and every
    # metric downstream would inherit an optimistic bias nobody could trace.
    assert trade.costs == Decimal(14)
    assert trade.net_pnl == Decimal(86)
    assert portfolio.account().balance == Decimal(10_000) + Decimal(86)


def test_equity_moves_with_an_open_position_and_balance_does_not() -> None:
    """The gap between the two is why drawdown is measured on equity.

    An account with a losing position still open has not *lost* the money yet — but it has
    drawn down. Measured on balance, the curve is a serene flat line right up to the margin
    call.
    """
    portfolio = a_portfolio()
    portfolio.apply(fill(order(), price="1.10000", time=T1))

    portfolio.mark_to_market(bar(2, open_="1.10000", close="1.09500"))

    assert portfolio.account().balance == Decimal(10_000)
    assert portfolio.account().equity == Decimal(10_000) - Decimal(500)


def test_equity_returns_to_balance_once_the_position_is_closed() -> None:
    portfolio = a_portfolio()
    portfolio.apply(fill(order(), price="1.10000", time=T1))
    portfolio.mark_to_market(bar(2, open_="1.10000", close="1.09500"))

    portfolio.apply(fill(order(intent=SignalKind.EXIT), price="1.09500", time=T2))

    account = portfolio.account()
    assert account.balance == account.equity == Decimal(9_500)


def test_a_flat_account_marks_to_market_at_its_balance() -> None:
    portfolio = a_portfolio()

    portfolio.mark_to_market(bar(1, open_="1.10000", close="9.99999"))

    assert portfolio.account().equity == Decimal(10_000)


def test_r_multiple_measures_the_result_against_the_risk_at_the_stop() -> None:
    """Entry 1.10000, stop 1.09500 ⇒ risk 50 pips = $500 on one lot. A +100-pip win of $1 000
    is +2R; the R-multiple is what makes that comparable across sizes and stops."""
    portfolio = a_portfolio()
    portfolio.apply(fill(order(stop="1.09500"), price="1.10000", time=T1))
    trade = portfolio.apply(fill(order(intent=SignalKind.EXIT), price="1.11000", time=T2))
    assert trade is not None
    assert trade.net_pnl == Decimal("1000")
    assert trade.r_multiple == Decimal("2")


def test_r_multiple_is_undefined_without_a_stop_or_with_zero_risk() -> None:
    """No stop, or a stop sitting on the entry, leaves no risk to divide by — `None`, not a
    division by zero."""
    no_stop = a_portfolio()
    no_stop.apply(fill(order(), price="1.10000", time=T1))
    trade = no_stop.apply(fill(order(intent=SignalKind.EXIT), price="1.11000", time=T2))
    assert trade is not None
    assert trade.r_multiple is None

    zero_risk = a_portfolio()
    zero_risk.apply(fill(order(stop="1.10000"), price="1.10000", time=T1))
    trade = zero_risk.apply(fill(order(intent=SignalKind.EXIT), price="1.11000", time=T2))
    assert trade is not None
    assert trade.r_multiple is None


def test_opening_a_second_position_is_refused() -> None:
    """Phase 1 holds one position at a time. It says so rather than quietly overwriting.

    A silent overwrite would lose the first position's entry price — and with it, its P&L.
    """
    portfolio = a_portfolio()
    portfolio.apply(fill(order(), price="1.10000", time=T1))

    with pytest.raises(EngineError, match="already open"):
        portfolio.apply(fill(order(), price="1.10100", time=T2))


def test_closing_nothing_is_refused() -> None:
    with pytest.raises(EngineError, match="not open"):
        a_portfolio().apply(fill(order(intent=SignalKind.EXIT), price="1.10000", time=T1))


def test_the_trade_records_both_ends_of_the_round_trip() -> None:
    portfolio = a_portfolio()

    portfolio.apply(fill(order(), price="1.10000", time=T1))
    trade = portfolio.apply(fill(order(intent=SignalKind.EXIT), price="1.10500", time=T2))

    assert trade is not None
    assert trade.entry_time == T1
    assert trade.entry_price == Decimal("1.10000")
    assert trade.exit_time == T2
    assert trade.exit_price == Decimal("1.10500")
    assert trade.side is Side.LONG
    assert portfolio.trades == (trade,)


def test_an_account_cannot_start_at_zero() -> None:
    """Percent-risk sizing on a zero balance divides by zero. Refuse at the front door."""
    with pytest.raises(ValueError, match="must be positive"):
        Portfolio(initial_capital=Decimal(0), instrument=EURUSD)


def test_the_open_position_is_visible_and_carries_its_stop() -> None:
    portfolio = a_portfolio()
    request = OrderRequest(
        symbol="EURUSD",
        side=Side.LONG,
        intent=SignalKind.ENTRY,
        volume=Decimal(1),
        decided_at=T0,
        stop_loss=Decimal("1.09500"),
        take_profit=Decimal("1.11000"),
    )

    portfolio.apply(fill(request, price="1.10000", time=T1))
    position = portfolio.position

    assert position is not None
    assert position.stop_loss == Decimal("1.09500")
    assert position.take_profit == Decimal("1.11000")
    assert position.entry_time == T1


def test_a_partial_close_is_refused_rather_than_silently_inflating_the_pnl() -> None:
    """The bug this catches: a one-lot fill closing a ten-lot position.

    Without the check, `_close` priced the *whole* position at this fill — ten lots of profit
    booked on a one-lot exit — and set the position to None, so nine lots of live exposure
    simply vanished from the ledger while they were still open at the broker. Nothing raised.

    Partial fills are normal MT5 behaviour in a thin market. Phase 1 does not support them.
    It says so.
    """
    portfolio = a_portfolio()
    portfolio.apply(fill(order(volume="10"), price="1.10000", time=T1))

    partial = Fill(
        order=order(intent=SignalKind.EXIT, volume="10"),
        time=T2,
        price=Decimal("1.11000"),
        volume=Decimal(1),  # only one of the ten lots
        costs=Decimal(0),
    )

    with pytest.raises(EngineError, match="partial close"):
        portfolio.apply(partial)


def test_a_partially_filled_entry_is_refused() -> None:
    """The symmetric hole: a broker filling one lot on an order for ten, or ten on one."""
    portfolio = a_portfolio()

    partial = Fill(
        order=order(volume="10"),
        time=T1,
        price=Decimal("1.10000"),
        volume=Decimal(1),
        costs=Decimal(0),
    )

    with pytest.raises(EngineError, match="partial fill"):
        portfolio.apply(partial)
