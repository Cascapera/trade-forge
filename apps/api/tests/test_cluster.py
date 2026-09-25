"""Replaying members' trades on one account — every balance worked by hand."""

import datetime as dt
from decimal import Decimal

from tradeforge_api.cluster import (
    Limits,
    Mark,
    MemberTrade,
    Point,
    Skip,
    daily,
    replay,
    yearly_returns,
)

ONE_PCT = Decimal("0.01")
LOOSE = Limits(max_open_positions=10, max_open_risk_pct=Decimal(1))


def at(day: int, hour: int = 0) -> dt.datetime:
    return dt.datetime(2020, 1, 1, tzinfo=dt.UTC) + dt.timedelta(days=day, hours=hour)


def trade(  # noqa: PLR0913 — a fixture naming every field a trade has
    member: int,
    enter: dt.datetime,
    leave: dt.datetime,
    r: str | None,
    *,
    long: bool = True,
    entry: str = "1.1000",
    stop: str | None = "1.0900",
) -> MemberTrade:
    return MemberTrade(
        member=member,
        entry_time=enter,
        exit_time=leave,
        long=long,
        entry_price=Decimal(entry),
        stop=None if stop is None else Decimal(stop),
        r=None if r is None else Decimal(r),
    )


def test_each_trade_is_sized_on_the_balance_it_meets() -> None:
    """10 000 at 1%: +2 R makes 200 → 10 200; the next 1% is 102, and -1 R takes it → 10 098."""
    done = replay(
        initial_capital=Decimal(10000),
        risk_pcts=[ONE_PCT],
        trades=[trade(0, at(0), at(1), "2"), trade(0, at(2), at(3), "-1")],
        marks=[],
        limits=LOOSE,
    )

    assert done.final_balance == Decimal(10098)
    assert (done.members[0].taken, done.members[0].net_pnl) == (2, Decimal(98))


def test_members_share_the_balance_and_their_own_risk() -> None:
    """Member 0 at 1% and member 1 at 2%, both open at once on 10 000: 100 and 200 at risk. Member
    1's trade closes first at +1 R (+200) and member 0's at -1 R (-100): 10 100."""
    done = replay(
        initial_capital=Decimal(10000),
        risk_pcts=[ONE_PCT, Decimal("0.02")],
        trades=[trade(0, at(0), at(3), "-1"), trade(1, at(1), at(2), "1")],
        marks=[],
        limits=LOOSE,
    )

    assert done.final_balance == Decimal(10100)
    assert done.most_open == 2
    assert [one.net_pnl for one in done.members] == [Decimal(-100), Decimal(200)]


def test_the_position_limit_skips_and_counts() -> None:
    done = replay(
        initial_capital=Decimal(10000),
        risk_pcts=[ONE_PCT, ONE_PCT],
        trades=[trade(0, at(0), at(5), "1"), trade(1, at(1), at(2), "3")],
        marks=[],
        limits=Limits(max_open_positions=1, max_open_risk_pct=Decimal(1)),
    )

    assert done.members[1].skipped == {Skip.POSITIONS.value: 1}
    assert done.members[1].taken == 0
    assert done.final_balance == Decimal(10100)


def test_the_risk_limit_counts_what_is_already_open() -> None:
    """3% cap, members at 2%: the first takes 200; a second 200 would make 400 > 300."""
    done = replay(
        initial_capital=Decimal(10000),
        risk_pcts=[Decimal("0.02"), Decimal("0.02")],
        trades=[trade(0, at(0), at(5), "1"), trade(1, at(1), at(2), "1")],
        marks=[],
        limits=Limits(max_open_positions=10, max_open_risk_pct=Decimal("0.03")),
    )

    assert done.members[1].skipped == {Skip.RISK.value: 1}


def test_an_exit_at_the_same_instant_frees_its_slot_for_an_entry() -> None:
    done = replay(
        initial_capital=Decimal(10000),
        risk_pcts=[ONE_PCT, ONE_PCT],
        trades=[trade(0, at(0), at(2), "1"), trade(1, at(2), at(3), "1")],
        marks=[],
        limits=Limits(max_open_positions=1, max_open_risk_pct=Decimal(1)),
    )

    assert done.members[1].taken == 1
    # Member 1 is sized after member 0's +100 landed: 1% of 10 100.
    assert done.members[1].net_pnl == Decimal(101)


def test_a_trade_with_no_stop_cannot_be_sized_and_is_skipped() -> None:
    done = replay(
        initial_capital=Decimal(10000),
        risk_pcts=[ONE_PCT],
        trades=[trade(0, at(0), at(1), None, stop=None)],
        marks=[],
        limits=LOOSE,
    )

    assert done.members[0].skipped == {Skip.NO_STOP.value: 1}


def test_open_positions_are_marked_so_a_combined_fall_is_seen_before_it_is_booked() -> None:
    """Two longs from 1.1000 with a 0.0100 stop, 100 at risk each. Marked at 1.0950 both are down
    half an R (-50 each): equity 9 900 while the balance is still 10 000. Then both recover and
    close at +1 R. Booked only, the curve would never have fallen at all."""
    done = replay(
        initial_capital=Decimal(10000),
        risk_pcts=[ONE_PCT, ONE_PCT],
        trades=[trade(0, at(0), at(3), "1"), trade(1, at(0), at(3), "1")],
        marks=[
            Mark(member=0, time=at(1), close=Decimal("1.0950")),
            Mark(member=1, time=at(1), close=Decimal("1.0950")),
        ],
        limits=LOOSE,
    )

    assert done.final_balance == Decimal(10200)
    lowest = min(point.equity for point in done.curve)
    assert lowest == Decimal(9900)
    assert done.max_drawdown_abs == Decimal(100)
    assert done.max_drawdown_pct == Decimal(100) / Decimal(10000)


def test_a_short_gains_as_the_price_falls() -> None:
    done = replay(
        initial_capital=Decimal(10000),
        risk_pcts=[ONE_PCT],
        trades=[trade(0, at(0), at(2), "1", long=False, stop="1.1100")],
        marks=[Mark(member=0, time=at(1), close=Decimal("1.0950"))],
        limits=LOOSE,
    )

    # Down 0.0050 against a 0.0100 stop: +0.5 R marked, +50.
    assert max(point.equity for point in done.curve) == Decimal(10100)
    assert any(point.equity == Decimal(10050) for point in done.curve)


def test_an_empty_account_takes_nothing_more() -> None:
    done = replay(
        initial_capital=Decimal(100),
        risk_pcts=[Decimal(1)],
        trades=[trade(0, at(0), at(1), "-1"), trade(0, at(2), at(3), "5")],
        marks=[],
        limits=LOOSE,
    )

    assert done.final_balance == Decimal(0)
    assert done.members[0].skipped == {Skip.EMPTY.value: 1}


def test_the_curve_is_thinned_to_the_last_point_of_each_day() -> None:
    curve = [
        Point(at(0, 1), Decimal(1), Decimal(1)),
        Point(at(0, 5), Decimal(2), Decimal(2)),
        Point(at(1, 3), Decimal(3), Decimal(3)),
    ]

    assert [point.equity for point in daily(curve)] == [Decimal(2), Decimal(3)]


def test_each_year_returns_on_what_it_started_with() -> None:
    first = dt.datetime(2020, 6, 1, tzinfo=dt.UTC)
    curve = [
        Point(first, Decimal(11000), Decimal(11000)),
        Point(first.replace(year=2021), Decimal(9900), Decimal(9900)),
    ]

    assert yearly_returns(curve, Decimal(10000)) == {
        2020: Decimal("0.1"),
        2021: Decimal("-0.1"),
    }
