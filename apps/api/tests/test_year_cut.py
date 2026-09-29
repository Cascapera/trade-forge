"""Cutting a run to whole years in R, and refusing the cuts a later run would not reproduce — every
number worked by hand."""

import datetime as dt
from decimal import Decimal

import pytest

from tradeforge_api.year_cut import NoCut, cut_years, sizing_document, sizing_of
from tradeforge_engine.domain import (
    ClosedTrade,
    EquityPoint,
    Refusal,
    RefusedBy,
    Side,
    SignalKind,
)

CAPITAL = Decimal("10000")
FROM = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
TO = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)


def on(year: int, month: int = 6) -> dt.datetime:
    return dt.datetime(year, month, 1, tzinfo=dt.UTC)


def trade(entered: dt.datetime, volume: str) -> ClosedTrade:
    return ClosedTrade(
        symbol="AUDUSD",
        side=Side.LONG,
        volume=Decimal(volume),
        entry_time=entered,
        entry_price=Decimal("0.7"),
        exit_time=entered + dt.timedelta(hours=1),
        exit_price=Decimal("0.7"),
        gross_pnl=Decimal(0),
        costs=Decimal(0),
        net_pnl=Decimal(0),
    )


def point(when: dt.datetime, equity: str) -> EquityPoint:
    return EquityPoint(time=when, equity=Decimal(equity))


def refused(by: RefusedBy) -> Refusal:
    return Refusal(client_id=None, intent=SignalKind.ENTRY, refused_by=by)


# --- sizing_of --------------------------------------------------------------------------------


def test_each_year_opens_on_the_equity_of_the_last_bar_before_it() -> None:
    curve = [
        point(on(2020, 1), "10000"),
        point(on(2020, 12), "8000"),
        point(on(2021, 3), "9000"),
        point(on(2021, 12), "12000"),
    ]
    sizing = sizing_of(trades=[], equity_curve=curve, refusals=[], initial_capital=CAPITAL)

    assert {year: s.equity_at_start for year, s in sizing.by_years.items()} == {
        2020: CAPITAL,
        2021: Decimal("8000"),
    }


def test_a_year_with_no_bar_opens_on_what_the_run_held_when_it_was_skipped() -> None:
    """A W1 run whose bars jump a year boundary still has the year it jumped into, and a gap of
    whole years (no bar at all in 2021) opens each of them on the same equity."""
    curve = [point(on(2020, 12), "7000"), point(on(2022, 2), "7500")]
    sizing = sizing_of(trades=[], equity_curve=curve, refusals=[], initial_capital=CAPITAL)

    assert {year: s.equity_at_start for year, s in sizing.by_years.items()} == {
        2020: CAPITAL,
        2021: Decimal("7000"),
        2022: Decimal("7000"),
    }


def test_the_smallest_lot_is_per_year_of_entry_and_absent_in_a_year_with_none() -> None:
    curve = [point(on(2020, 1), "10000"), point(on(2021, 1), "10000")]
    trades = [trade(on(2020, 2), "0.30"), trade(on(2020, 9), "0.12"), trade(on(2020, 10), "0.5")]
    sizing = sizing_of(trades=trades, equity_curve=curve, refusals=[], initial_capital=CAPITAL)

    assert sizing.by_years[2020].smallest_volume == Decimal("0.12")
    assert sizing.by_years[2021].smallest_volume is None


def test_only_the_refusals_for_a_lot_of_zero_are_counted() -> None:
    """The broker turning a duplicate away, or a market that ran past a resting order, happens to
    a later run as it happened to this one: only sizing depends on the equity."""
    refusals = [refused(RefusedBy.SIZING), refused(RefusedBy.BROKER), refused(RefusedBy.MARKET)]
    sizing = sizing_of(trades=[], equity_curve=[], refusals=refusals, initial_capital=CAPITAL)

    assert sizing.refusals == 1


def test_the_document_keeps_decimals_as_strings_and_an_empty_year_as_null() -> None:
    curve = [point(on(2020, 1), "10000"), point(on(2021, 1), "9500.5")]
    trades = [trade(on(2020, 3), "0.07")]
    sizing = sizing_of(trades=trades, equity_curve=curve, refusals=[], initial_capital=CAPITAL)

    assert sizing_document(sizing) == {
        "2020": {"equity_at_start": "10000", "smallest_volume": "0.07"},
        "2021": {"equity_at_start": "10000", "smallest_volume": None},
    }


# --- cut_years --------------------------------------------------------------------------------

R_BY_YEARS = {
    "2020": {"2020": "5", "2021": "-1"},
    "2021": {"2021": "2"},
    "2022": {"2022": "-3", "2023": "4"},
    "2023": {"2023": "1.5"},
    "2024": {"2024": "10", "2025": "-2"},
}


def healthy() -> dict[str, dict[str, str | None]]:
    """A run that grew 10 000 a year: a later run sizes everything smaller, still well above the
    step."""
    return {
        str(year): {"equity_at_start": str(10000 * (year - 2019)), "smallest_volume": "0.40"}
        for year in range(2020, 2025)
    }


def cut(first: int, last: int, **overrides: object) -> Decimal:
    kwargs: dict[str, object] = {
        "r_by_years": R_BY_YEARS,
        "sizing_by_years": healthy(),
        "sizing_refusals": 0,
        "initial_capital": CAPITAL,
        "date_from": FROM,
        "date_to": TO,
        "first_year": first,
        "last_year": last,
    }
    kwargs.update(overrides)
    return cut_years(**kwargs).net_r  # type: ignore[arg-type]


def test_the_cut_keeps_what_entered_from_its_first_year_and_left_by_its_last() -> None:
    """2021-2022: 2021's 2, and 2022's -3 — its 4 left in 2023, after the cut closed."""
    found = cut_years(
        r_by_years=R_BY_YEARS,
        sizing_by_years=healthy(),
        sizing_refusals=0,
        initial_capital=CAPITAL,
        date_from=FROM,
        date_to=TO,
        first_year=2021,
        last_year=2022,
    )

    assert found.net_r == Decimal("-1")
    assert found.yearly_r == {2021: Decimal("2"), 2022: Decimal("-3")}


def test_a_trade_that_entered_before_the_cut_is_not_the_cuts_even_if_it_left_inside() -> None:
    """2020's -1 left in 2021: the later run held it as warm-up shadow and never booked it."""
    assert cut(2021, 2021) == Decimal("2")


def test_the_whole_run_is_a_cut_too_but_the_trade_open_at_its_end_is_not() -> None:
    """Every cell but 2024's -2, which left in 2025 — past the run's own end."""
    assert cut(2020, 2024) == Decimal("5") - 1 + 2 - 3 + 4 + Decimal("1.5") + 10


def test_a_cut_outside_the_runs_window_is_refused() -> None:
    with pytest.raises(NoCut, match="starts on 2020-01-01, after 1 January 2019"):
        cut(2019, 2021)
    with pytest.raises(NoCut, match="before the end of 2025"):
        cut(2022, 2025)


def test_a_run_that_started_mid_year_cannot_answer_that_year() -> None:
    with pytest.raises(NoCut, match="after 1 January 2020"):
        cut(2020, 2021, date_from=on(2020, 3))


def test_a_cut_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(NoCut, match="starts in 2023, after it ends in 2022"):
        cut(2023, 2022)


def test_a_run_recorded_before_its_sizing_was_kept_is_refused() -> None:
    with pytest.raises(NoCut, match="recorded before"):
        cut(2021, 2022, sizing_by_years=None, sizing_refusals=None)


def test_one_signal_turned_away_for_a_lot_of_zero_refuses_every_cut() -> None:
    """From that signal on the run held a slot a run started later would have filled — measured
    29/09 as hundreds of trades only in one run or the other."""
    with pytest.raises(NoCut, match="turned 1 signal"):
        cut(2021, 2022, sizing_refusals=1)


def test_a_later_run_that_would_size_under_two_steps_is_refused() -> None:
    """The run held 40 000 when 2023 opened; a run started then holds 10 000, a quarter. Its
    smallest lot of 2024 was 0.07 → 0.0175 there, under two steps of 0.01."""
    sizing = healthy()
    sizing["2024"]["smallest_volume"] = "0.07"

    with pytest.raises(NoCut, match=r"trade of 2024 at about 0\.0175 lots"):
        cut(2023, 2024, sizing_by_years=sizing)


def test_exactly_two_steps_is_enough() -> None:
    """0.08 at a quarter is 0.02, the edge the rule allows."""
    sizing = healthy()
    sizing["2024"]["smallest_volume"] = "0.08"

    assert cut(2023, 2024, sizing_by_years=sizing) == Decimal("1.5") + 10


def test_a_run_that_lost_scales_a_later_run_up_not_down() -> None:
    """Held 2 500 when 2022 opened: a run started then holds four times as much, and 0.01 there
    is 0.04 — no signal of it could have been turned away."""
    sizing = healthy()
    sizing["2022"] = {"equity_at_start": "2500", "smallest_volume": "0.01"}

    assert cut(2022, 2022, sizing_by_years=sizing) == Decimal("-3")


def test_a_small_lot_outside_the_cut_does_not_refuse_it() -> None:
    sizing = healthy()
    sizing["2024"]["smallest_volume"] = "0.01"

    assert cut(2021, 2022, sizing_by_years=sizing) == Decimal("-1")


def test_a_year_with_no_trade_is_not_checked() -> None:
    sizing = healthy()
    sizing["2022"]["smallest_volume"] = None

    assert cut(2022, 2023, sizing_by_years=sizing) == Decimal("-3") + 4 + Decimal("1.5")
