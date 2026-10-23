"""Cutting an out-of-sample run into years and blocks — every number here worked by hand."""

import datetime as dt
from decimal import Decimal

import pytest

from tradeforge_api.slices import ClosedTrade, Slice, by_blocks, by_year, verdict


def at(year: int, month: int = 6, day: int = 1) -> dt.datetime:
    return dt.datetime(year, month, day, tzinfo=dt.UTC)


def trade(when: dt.datetime, r: str | None) -> ClosedTrade:
    return ClosedTrade(entry_time=when, r=None if r is None else Decimal(r))


class TestByYear:
    def test_every_year_of_the_window_is_a_slice_the_empty_ones_shown_and_not_counted(
        self,
    ) -> None:
        """2021 had no trade. It is still a row — the reader must see the gap — but "no
        evidence" is not a losing year, so it stays out of the verdict."""
        trades = [trade(at(2020, 3), "2"), trade(at(2020, 9), "-1"), trade(at(2022), "-0.5")]

        slices = by_year(trades, at(2020, 1, 1), at(2023, 1, 1))

        assert [(one.label, one.trades, one.net_r, one.counted) for one in slices] == [
            ("2020", 2, Decimal("1"), True),
            ("2021", 0, Decimal("0"), False),
            ("2022", 1, Decimal("-0.5"), True),
            ("2023", 0, Decimal("0"), False),
        ]

    def test_a_trade_belongs_to_the_year_it_was_entered(self) -> None:
        # Entered on 30 December, closed in January: the decision was 2020's.
        slices = by_year([trade(at(2020, 12, 30), "3")], at(2020, 1, 1), at(2021, 12, 31))

        assert [(one.label, one.net_r) for one in slices] == [
            ("2020", Decimal("3")),
            ("2021", Decimal("0")),
        ]

    def test_the_first_and_last_years_are_clipped_to_the_window(self) -> None:
        slices = by_year([], at(2020, 3, 1), at(2021, 7, 1))

        assert (slices[0].date_from, slices[0].date_to) == (at(2020, 3, 1), at(2021, 1, 1))
        assert (slices[1].date_from, slices[1].date_to) == (at(2021, 1, 1), at(2021, 7, 1))

    def test_a_trade_with_no_stop_has_no_r_and_is_left_out(self) -> None:
        slices = by_year(
            [trade(at(2020), None), trade(at(2020), "1")], at(2020, 1, 1), at(2020, 12, 31)
        )

        assert (slices[0].trades, slices[0].net_r) == (1, Decimal("1"))


class TestByBlocks:
    def test_consecutive_blocks_in_entry_order_and_the_short_tail_is_not_counted(self) -> None:
        """Seven trades in blocks of three: 1-3, 4-6 and a tail of one. Handed over out of
        order, because the database gives no promise and the cut must not depend on one."""
        rs = ["1", "1", "-1", "-1", "-1", "2", "5"]
        trades = [trade(at(2020, 1, 1) + dt.timedelta(days=i), r) for i, r in enumerate(rs)]

        slices = by_blocks(list(reversed(trades)), 3)

        assert [(one.label, one.trades, one.net_r, one.counted) for one in slices] == [
            ("1-3", 3, Decimal("1"), True),
            ("4-6", 3, Decimal("0"), True),
            ("7-7", 1, Decimal("5"), False),
        ]
        assert slices[0].date_from == at(2020, 1, 1)
        assert slices[0].date_to == at(2020, 1, 3)

    def test_a_trade_with_no_r_does_not_take_a_place_in_a_block(self) -> None:
        trades = [
            trade(at(2020, 1, 1), "1"),
            trade(at(2020, 1, 2), None),
            trade(at(2020, 1, 3), "1"),
        ]

        assert [one.trades for one in by_blocks(trades, 2)] == [2]

    def test_no_trades_is_no_blocks(self) -> None:
        assert by_blocks([], 30) == []

    def test_a_block_of_nothing_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one trade"):
            by_blocks([], 0)


def piece(net_r: str, *, counted: bool = True) -> Slice:
    return Slice(
        label="x",
        date_from=at(2020),
        date_to=at(2021),
        trades=1,
        net_r=Decimal(net_r),
        counted=counted,
    )


class TestVerdict:
    def test_the_share_is_of_counted_slices_only(self) -> None:
        # 3 counted: +2, -1, +0.5 → 2 of 3. The uncounted +9 must not move it.
        result = verdict(
            [piece("2"), piece("-1"), piece("0.5"), piece("9", counted=False)], Decimal("0.6")
        )

        assert (result.counted, result.positive) == (3, 2)
        assert result.share == Decimal(2) / Decimal(3)
        assert result.passed

    def test_below_the_bar_does_not_pass(self) -> None:
        assert not verdict([piece("2"), piece("-1"), piece("0.5")], Decimal("0.7")).passed

    def test_break_even_is_not_positive(self) -> None:
        result = verdict([piece("0"), piece("1")], Decimal("0.5"))

        assert result.positive == 1

    def test_one_slice_never_passes_however_good(self) -> None:
        """One draw at 100% says nothing the whole-window number did not."""
        result = verdict([piece("10")], Decimal("0.5"))

        assert result.share == Decimal(1)
        assert not result.passed

    def test_nothing_counted_is_no_share_not_zero(self) -> None:
        result = verdict([piece("3", counted=False)], Decimal("0.5"))

        assert result.share is None
        assert not result.passed
