"""Swap: the nights a position is charged for, and the signed amount it lands on the trade.

His account (24/09): GBPUSD pays -5 USD per standard lot per night on both sides, the Wednesday
rollover counts three nights, and the weekend none. The rollover is 17:00 New York — his broker's
server midnight — so the instants below are written in New York time and the tests hold across the
daylight-saving shift.
"""

import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from tradeforge_engine.domain import Fill, OrderRequest, Side, SignalKind
from tradeforge_engine.portfolio import Portfolio
from tradeforge_engine.swap import SwapRates, nights_held
from tradeforge_engine.testing import EURUSD

NY = ZoneInfo("America/New_York")


def ny(day: int, hour: int, month: int = 9) -> dt.datetime:
    """An instant in New York, September 2026: Monday the 21st to Sunday the 27th."""
    return dt.datetime(2026, month, day, hour, tzinfo=NY)


class TestNightsHeld:
    @pytest.mark.parametrize(
        ("entry", "exit_", "nights"),
        [
            (ny(21, 10), ny(21, 16), 0),  # Monday, closed before the rollover
            (ny(21, 10), ny(22, 10), 1),  # Monday night
            (ny(23, 10), ny(24, 10), 3),  # Wednesday night carries the weekend
            (ny(24, 10), ny(25, 10), 1),  # Thursday night
            (
                ny(25, 10),
                ny(28, 10),
                1,
            ),  # Friday to Monday: one rollover, the weekend is Wednesday's
            (ny(21, 10), ny(28, 10), 7),  # a week held whole pays seven nights
            (ny(27, 18), ny(28, 10), 0),  # opened at Sunday's open, closed before Monday's rollover
        ],
    )
    def test_a_week_rolls_mon_tue_wed_triple_thu_fri(
        self, entry: dt.datetime, exit_: dt.datetime, nights: int
    ) -> None:
        assert nights_held(entry, exit_) == nights

    def test_a_position_opened_or_closed_at_the_rollover_was_not_held_through_it(self) -> None:
        assert nights_held(ny(21, 17), ny(22, 16)) == 0
        assert nights_held(ny(21, 10), ny(21, 17)) == 0

    def test_the_rollover_follows_new_york_across_daylight_saving(self) -> None:
        """17:00 New York is 21:00 UTC in summer and 22:00 UTC in winter. A position closed at
        21:30 UTC was held through summer's rollover and not through winter's."""
        summer = dt.datetime(2026, 9, 21, 21, 30, tzinfo=dt.UTC)
        winter = dt.datetime(2026, 1, 12, 21, 30, tzinfo=dt.UTC)

        assert nights_held(summer - dt.timedelta(hours=2), summer) == 1
        assert nights_held(winter - dt.timedelta(hours=2), winter) == 0

    def test_nothing_is_held_backwards(self) -> None:
        assert nights_held(ny(22, 10), ny(21, 10)) == 0


class TestSwapRates:
    def test_signed_as_the_broker_quotes_it_per_lot_per_night_per_side(self) -> None:
        rates = SwapRates(long_per_lot=Decimal(-5), short_per_lot=Decimal("1.2"))

        # Half a lot over Wednesday night: three nights.
        assert rates.on(Side.LONG, Decimal("0.5"), ny(23, 10), ny(24, 10)) == Decimal("-7.5")
        assert rates.on(Side.SHORT, Decimal("0.5"), ny(23, 10), ny(24, 10)) == Decimal("1.80")

    def test_no_rates_is_no_swap(self) -> None:
        assert SwapRates().on(Side.LONG, Decimal(1), ny(21, 10), ny(28, 10)) == 0


def _fill(side: Side, intent: SignalKind, price: str, when: dt.datetime, costs: str) -> Fill:
    order = OrderRequest(
        symbol="EURUSD",
        side=side,
        intent=intent,
        volume=Decimal(1),
        decided_at=when,
        stop_loss=Decimal("1.09000") if intent is SignalKind.ENTRY else None,
    )
    return Fill(
        order=order, time=when, price=Decimal(price), volume=Decimal(1), costs=Decimal(costs)
    )


class TestTheTradeCarriesIt:
    def test_the_swap_lands_on_the_trade_and_the_balance_alike(self) -> None:
        """The reconciliation the whole ledger rests on — `sum(net_pnl) == final - initial` — holds
        with the swap in: it moves the trade's net and the balance by the same signed amount."""
        portfolio = Portfolio(
            initial_capital=Decimal(10_000),
            instrument=EURUSD,
            swap=SwapRates(long_per_lot=Decimal(-5), short_per_lot=Decimal(-5)),
        )
        entered = ny(23, 10).astimezone(dt.UTC)
        exited = ny(24, 10).astimezone(dt.UTC)
        portfolio.apply(_fill(Side.LONG, SignalKind.ENTRY, "1.10000", entered, "4"))
        trade = portfolio.apply(_fill(Side.SHORT, SignalKind.EXIT, "1.10100", exited, "4"))

        assert trade is not None
        assert trade.swap == Decimal(-15)
        assert trade.net_pnl == trade.gross_pnl - trade.costs + trade.swap
        assert portfolio.account().balance - Decimal(10_000) == trade.net_pnl

    def test_a_portfolio_without_rates_charges_no_swap(self) -> None:
        portfolio = Portfolio(initial_capital=Decimal(10_000), instrument=EURUSD)
        portfolio.apply(_fill(Side.LONG, SignalKind.ENTRY, "1.10000", ny(23, 10), "0"))
        trade = portfolio.apply(_fill(Side.SHORT, SignalKind.EXIT, "1.10100", ny(24, 10), "0"))

        assert trade is not None
        assert trade.swap == 0
        assert trade.net_pnl == trade.gross_pnl
