"""The three detections, on Linux, with no terminal.

Each one exists because the naive reading of "how much history is there" was measured to be
wrong in a different direction, and each one can be wrong in a way that produces a *plausible*
number rather than an error. That is what puts them here rather than behind an integration mark.
"""

import datetime as dt
from collections.abc import Callable
from decimal import Decimal

import pytest

from tradeforge_collector.history import (
    FABRICATED_YEAR_THRESHOLD,
    MIN_SPREAD_SAMPLES,
    HistoryReport,
    count_answering,
    fabricated_fraction,
    is_fabricated,
    is_stamped_spread,
    last_fabricated_year,
    usable_from,
)
from tradeforge_collector.source import Candle

NOON = dt.datetime(2026, 8, 19, 12, 0, tzinfo=dt.UTC)


def real(minute: int, *, volume: int = 500) -> Candle:
    """A bar a market actually made: it has a range, and ticks happened."""
    return Candle(
        time=NOON + dt.timedelta(minutes=minute),
        open=Decimal("1.10000"),
        high=Decimal("1.10020"),
        low=Decimal("1.09990"),
        close=Decimal("1.10010"),
        tick_volume=volume,
        spread=1,
        real_volume=0,
    )


def reconstructed(minute: int) -> Candle:
    """One price stamped for the whole period: no range, one tick. Not a market."""
    price = Decimal("0.53690")
    return Candle(
        time=NOON + dt.timedelta(minutes=minute),
        open=price,
        high=price,
        low=price,
        close=price,
        tick_volume=1,
        spread=0,
        real_volume=0,
    )


def report(**patch: object) -> HistoryReport:
    fields: dict[str, object] = {
        "oldest": dt.datetime(1971, 1, 4, tzinfo=dt.UTC),
        "bar_count": 14_342,
        "terminal_maxbars": 100_000,
        "bar_count_is_a_ceiling": False,
        "last_fabricated": 1975,
        "first_measured_cost": 2009,
    }
    fields.update(patch)
    return HistoryReport(**fields)  # type: ignore[arg-type]


class TestCountingBySearch:
    """The depth search. Every assertion here is an off-by-one waiting to happen."""

    def answers_for(self, length: int) -> tuple[Callable[[int], bool], list[int]]:
        calls: list[int] = []

        def answers(position: int) -> bool:
            calls.append(position)
            return position < length

        return answers, calls

    @pytest.mark.parametrize("length", [1, 2, 3, 259, 14_342, 100_000])
    def test_it_finds_the_exact_length(self, length: int) -> None:
        answers, _ = self.answers_for(length)

        assert count_answering(answers, ceiling=10_000_000) == length

    def test_a_series_of_one_is_not_a_series_of_two(self) -> None:
        """⚠️ The boundary the doubling loop is most likely to get wrong.

        Position 0 answers and position 1 does not, so the loop never doubles at all and the
        answer comes entirely from the `low + 1` at the end.
        """
        answers, _ = self.answers_for(1)

        assert count_answering(answers, ceiling=1_000) == 1

    def test_a_symbol_with_nothing_is_zero_and_asks_once(self) -> None:
        # An ordinary state — a symbol the broker has never quoted — and it must not cost a
        # search. A loop that bisected anyway would ask 30 times to learn there is no data.
        answers, calls = self.answers_for(0)

        assert count_answering(answers, ceiling=1_000) == 0
        assert calls == [0]

    def test_the_search_is_logarithmic_and_not_linear(self) -> None:
        """⚠️ 100000 positions asked one at a time is minutes, not milliseconds.

        Measured against the real terminal, the whole search costs 34 calls. A regression to a
        linear scan would still be *correct*, and would still pass every test above — which is
        exactly why the call count is asserted rather than assumed.
        """
        answers, calls = self.answers_for(100_000)

        count_answering(answers, ceiling=10_000_000)

        assert len(calls) < 40

    def test_a_source_that_never_stops_is_bounded_by_the_ceiling(self) -> None:
        # A fake that forgot to stop, or a terminal in a strange mood. The loop must end.
        def always(_position: int) -> bool:
            return True

        assert count_answering(always, ceiling=1_024) == 1_024


class TestWhatTheBarsCanProve:
    """The fabrication census, and the honest edge of what it can say."""

    def test_a_bar_nothing_traded_is_fabricated(self) -> None:
        assert is_fabricated(reconstructed(0)) is True

    def test_a_ranged_bar_with_one_tick_is_still_a_market(self) -> None:
        """Both halves of the signature are required, and this is one separating case.

        ⚠️ A test whose fabricated bars differed in *both* fields could not tell `and` from `or`.
        A bar with a real high and low that happened to print once is thin, not invented.
        """
        assert is_fabricated(real(0, volume=1)) is False

    def test_a_flat_bar_that_actually_traded_is_still_a_market(self) -> None:
        # The other half: no range, but hundreds of ticks. A pegged currency does this.
        price = Decimal("7.75000")
        flat_but_busy = Candle(
            time=NOON,
            open=price,
            high=price,
            low=price,
            close=price,
            tick_volume=500,
            spread=1,
            real_volume=0,
        )

        assert is_fabricated(flat_but_busy) is False

    @pytest.mark.parametrize("volume", [0, 1])
    def test_a_fabrication_may_report_no_ticks_at_all(self, volume: int) -> None:
        # Some vendors write 0 and some write 1 for the same made-up bar. A threshold of `== 1`
        # would miss half the data this exists to catch.
        price = Decimal("0.53690")
        bar = Candle(
            time=NOON,
            open=price,
            high=price,
            low=price,
            close=price,
            tick_volume=volume,
            spread=0,
            real_volume=0,
        )

        assert is_fabricated(bar) is True

    def test_a_years_fraction_is_counted_not_guessed(self) -> None:
        bars = [reconstructed(0), reconstructed(1), real(2), real(3)]

        assert fabricated_fraction(bars) == 0.5

    def test_a_year_with_no_bars_is_not_fabricated(self) -> None:
        # `0.0`, not a division by zero and not `1.0`: an absent year makes no claim.
        assert fabricated_fraction([]) == 0.0

    def test_the_last_filled_year_is_the_boundary(self) -> None:
        """Measured on EURUSD D1: 100% of 1971, 69% of 1972, 21% of 1973, 0% by the 1990s."""
        fractions = {1971: 1.0, 1972: 0.69, 1973: 0.21, 1974: 0.12, 1996: 0.0}

        assert last_fabricated_year(fractions, threshold=FABRICATED_YEAR_THRESHOLD) == 1972

    def test_a_quiet_year_in_real_data_does_not_reopen_the_boundary(self) -> None:
        """⚠️ A handful of dead days must not drag the floor forward by decades.

        The threshold is what separates "this year was filled in" from "this year had some very
        quiet sessions", and a rule that fired on any fabricated bar at all would put the floor
        wherever the market last took a holiday.
        """
        fractions = {1971: 1.0, 1972: 0.69, 2015: 0.02, 2024: 0.0}

        assert last_fabricated_year(fractions, threshold=FABRICATED_YEAR_THRESHOLD) == 1972

    def test_a_series_with_nothing_fabricated_has_no_boundary(self) -> None:
        # BTCUSD measured exactly this: every year real, back to 2022.
        assert last_fabricated_year({2022: 0.0, 2023: 0.0}, threshold=0.5) is None

    def test_the_threshold_is_inclusive(self) -> None:
        # Half-and-half counts as filled. Asserted because an off-by-a-hair here moves the
        # boundary of every symbol whose transition year sits near the middle.
        assert last_fabricated_year({1973: 0.5}, threshold=0.5) == 1973
        assert last_fabricated_year({1973: 0.49}, threshold=0.5) is None


class TestWhetherTheSpreadWasMeasured:
    def test_a_year_that_never_moved_was_typed(self) -> None:
        # Measured: 2006 through 2009 on this broker, one number per year, min equal to max.
        assert is_stamped_spread([20] * MIN_SPREAD_SAMPLES, floating=True) is True

    def test_a_year_that_moved_was_measured(self) -> None:
        # Measured: 2010 varied 8..20 on the same instrument.
        spreads = [8, 11, 20] * MIN_SPREAD_SAMPLES

        assert is_stamped_spread(spreads, floating=True) is False

    def test_a_fixed_spread_instrument_cannot_be_judged(self) -> None:
        """⚠️ The false positive that would have shipped, and it lands on the best data.

        `spread_float` false means the broker genuinely charges the same spread all the time —
        this project's previous broker quoted AAPL exactly that way. A flat year there is the
        truth, not an invention, and calling it stamped would put a warning on the instrument
        whose costs are the most trustworthy in the catalogue.
        """
        assert is_stamped_spread([12] * MIN_SPREAD_SAMPLES, floating=False) is None

    def test_too_few_bars_is_unknown_rather_than_constant(self) -> None:
        """A year with three bars is constant by arithmetic, not by evidence.

        ⚠️ `None` and not `True`: a thin year would otherwise be reported as invented costs,
        which is a claim about the broker made from having almost no data.
        """
        assert is_stamped_spread([20, 20, 20], floating=True) is None

    def test_the_threshold_is_a_floor_and_not_a_range(self) -> None:
        # Exactly at the minimum is enough; one below is not. Asserted because an off-by-one
        # here changes the verdict of every thin year at once.
        assert is_stamped_spread([20] * MIN_SPREAD_SAMPLES, floating=True) is True
        assert is_stamped_spread([20] * (MIN_SPREAD_SAMPLES - 1), floating=True) is None

    def test_no_bars_at_all_is_unknown(self) -> None:
        assert is_stamped_spread([], floating=True) is None


class TestTheTerminalCeiling:
    def test_a_series_at_the_ceiling_is_the_machine_talking(self) -> None:
        # Measured: 100000 bars on M1, M5, M15 and H1 alike — the same round number four times
        # is a setting, not a broker.
        assert report(bar_count=100_000, terminal_maxbars=100_000).capped_by_terminal is True

    def test_a_series_past_the_ceiling_is_still_the_machine_talking(self) -> None:
        """⚠️ The case that separates `>=` from `==`, and the most capped series there is.

        `bar_count` counts positions, including the bar still forming; `maxbars` is a count of
        the series the terminal keeps. A terminal holding `maxbars` closed bars plus the
        forming one answers one position more than its own ceiling — and `==` would call that
        uncapped, which is the exact series the warning exists for.
        """
        assert report(bar_count=100_001, terminal_maxbars=100_000).capped_by_terminal is True

    def test_a_series_under_the_ceiling_is_the_broker_talking(self) -> None:
        assert report(bar_count=14_342, terminal_maxbars=100_000).capped_by_terminal is False

    def test_an_unknown_ceiling_never_reports_a_cap(self) -> None:
        """⚠️ Zero would otherwise make every series look capped.

        `bar_count >= 0` is true of everything, so a terminal that did not report its ceiling
        would put a "raise your setting" warning on data that has no such limit.
        """
        assert report(bar_count=14_342, terminal_maxbars=0).capped_by_terminal is False


class TestTheUsableStart:
    def test_the_later_floor_wins(self) -> None:
        """Filler stops in 1975 and typed costs in 2009, so the window starts in 2009.

        A run is only as trustworthy as its weaker half, and picking the earlier floor would
        hand somebody thirty-four years of prices costed with a number that was typed.
        """
        assert usable_from(report()) == dt.datetime(2009, 1, 1, tzinfo=dt.UTC)

    def test_the_filler_floor_can_be_the_later_one_too(self) -> None:
        # A symbol filled in until recently, on a broker that has measured costs for years.
        # Neither floor is always the binding one.
        found = usable_from(report(last_fabricated=2015, first_measured_cost=2009))

        assert found == dt.datetime(2016, 1, 1, tzinfo=dt.UTC)

    def test_the_year_after_the_filler_is_the_first_usable_one(self) -> None:
        """⚠️ Off by one year here is a year of made-up bars inside every default window.

        1975 was more than half filler, so the earliest honest start is January 1976 — not
        January 1975, which is the year the filler was found *in*.
        """
        found = usable_from(report(last_fabricated=1975, first_measured_cost=None))

        assert found == dt.datetime(1976, 1, 1, tzinfo=dt.UTC)

    def test_a_floor_older_than_the_data_does_not_invent_history(self) -> None:
        """⚠️ The floor is a *lower* bound on trust, never a claim that bars exist.

        A symbol whose history starts in 2022 with costs measured since 2009 has 2022 of usable
        data, not 2009. Returning the floor would name a date the terminal cannot reach.
        """
        found = usable_from(
            report(
                oldest=dt.datetime(2022, 5, 10, tzinfo=dt.UTC),
                last_fabricated=None,
                first_measured_cost=2009,
            )
        )

        assert found == dt.datetime(2022, 5, 10, tzinfo=dt.UTC)

    def test_with_neither_floor_known_it_falls_back_to_the_oldest_bar(self) -> None:
        found = usable_from(report(last_fabricated=None, first_measured_cost=None))

        assert found == dt.datetime(1971, 1, 4, tzinfo=dt.UTC)

    def test_a_symbol_with_no_bars_has_no_usable_start(self) -> None:
        # `None` must not be read as "since forever" by whatever renders it.
        found = usable_from(
            report(oldest=None, bar_count=0, last_fabricated=None, first_measured_cost=None)
        )

        assert found is None


def test_the_report_keeps_the_bounds_apart() -> None:
    """⚠️ They are independent, and a reader can only act on the one that binds them.

    The terminal's cap is fixed in a settings dialog, the filler floor by starting later, the
    cost floor by not trusting old costs at all. Collapsing them into one date would leave every
    one of those actions unavailable.
    """
    found = report(bar_count=100_000)

    assert found.capped_by_terminal is True
    assert found.last_fabricated == 1975
    assert found.first_measured_cost == 2009
    assert usable_from(found) == dt.datetime(2009, 1, 1, tzinfo=dt.UTC)


def test_a_count_that_is_really_the_probes_own_bound_says_so() -> None:
    """⚠️ Seen for real: EURUSD M1 came back as exactly 10,000,000, which is the search ceiling.

    Without the flag that number is indistinguishable from a measurement — and it is the one
    number somebody would size a backtest window from.
    """
    found = report(bar_count=10_000_000, bar_count_is_a_ceiling=True)

    assert found.bar_count_is_a_ceiling is True
