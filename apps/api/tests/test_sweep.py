"""Expanding a sweep — the arithmetic, with no database anywhere.

A sweep is the product a study and a basket each refuse to take, plus the timeframe. What can be
wrong here is what every point becomes: which document, under which name, at which chart. The
router's job is writing those and is the integration suite's.
"""

import re
from typing import Any

import pytest

from tradeforge_api.sweep import (
    MAX_SWEEP_RUNS,
    SECONDS_PER_BACKTEST,
    SweepDocument,
    SweepError,
    documents_for,
    points_in,
    size_refusal,
)


def a_document(timeframe: str = "M15") -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "name": "MME9-20260910-172055",
        "timeframe": timeframe,
        "setup": {"type": "mme9_breakout", "params": {"period": 9, "breakeven_at_r": 2.0}},
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def a_filtered_document() -> dict[str, Any]:
    """His structure entry under an H4 filter — the shape PR-238's equality rule governs."""
    return {
        "schema_version": "1.0",
        "name": "CHoCH under H4",
        "timeframe": "M15",
        "setup": {"type": "structure_choch", "params": {"htf": "H4", "htf_offset": 3}},
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def expand_one(
    definition: dict[str, Any],
    grid: dict[str, list[Any]],
    timeframes: list[str],
    name: str = "9.1 sem filtro",
) -> list[SweepDocument]:
    return documents_for(
        entry_id="e1", entry_name=name, definition=definition, grid=grid, timeframes=timeframes
    )


class TestTheTimeframeIsWrittenIntoTheDocument:
    def test_every_document_carries_the_timeframe_it_will_run_at(self) -> None:
        # ⚠️ The whole reason this module writes documents rather than only runs. Since PR-238 a
        # document and its run must agree under a higher-timeframe filter, because the filter's
        # bars are assembled from the document's own width. A sweep that set only the run's
        # timeframe would produce exactly the disagreement that rule refuses.
        out = expand_one(a_document(), {}, ["M15", "H1", "H4"])

        assert [doc.document["timeframe"] for doc in out] == ["M15", "H1", "H4"]
        # And carried out to the caller too, because the run needs it as a column — reading it
        # back out of the document would be parsing what we just wrote.
        assert [doc.timeframe for doc in out] == ["M15", "H1", "H4"]

    def test_the_base_documents_own_timeframe_is_replaced_not_kept(self) -> None:
        # A document saved at M15, swept at H1 only: nothing anywhere should still say M15.
        out = expand_one(a_document("M15"), {}, ["H1"])

        assert len(out) == 1
        assert out[0].document["timeframe"] == "H1"

    def test_a_filtered_document_keeps_its_filter_while_the_chart_moves(self) -> None:
        # The `htf` is a parameter of the setup and is **not** the sweep's axis. Moving the chart
        # under a filter is exactly what PR-238 governs, and the point of writing the timeframe
        # in is that the launch can then judge each document on its own.
        out = expand_one(a_filtered_document(), {}, ["M15", "H1"], name="CHoCH")

        assert [doc.document["setup"]["params"]["htf"] for doc in out] == ["H4", "H4"]
        assert [doc.document["timeframe"] for doc in out] == ["M15", "H1"]


class TestTheNameCarriesTheTimeframe:
    def test_two_timeframes_produce_two_different_names(self) -> None:
        # ⚠️ Without the timeframe in the name, the two documents differ in content and share a
        # name — and (name, version) is unique, so the second would collide rather than insert.
        out = expand_one(a_document(), {"setup.params.period": [9]}, ["M15", "H1"])

        names = [doc.document["name"] for doc in out]
        assert len(set(names)) == 2
        assert all("9.1 sem filtro" in name for name in names)
        assert any("M15" in name for name in names)
        assert any("H1" in name for name in names)

    def test_an_entry_with_no_grid_is_named_by_its_timeframe_alone(self) -> None:
        # Not `[M15 · ]` with a dangling separator: there is nothing on the other side of it.
        out = expand_one(a_document(), {}, ["M15"])

        assert out[0].label == "M15"
        assert out[0].document["name"] == "9.1 sem filtro [M15]"

    def test_a_grid_point_is_named_by_both(self) -> None:
        out = expand_one(a_document(), {"setup.params.period": [5]}, ["H1"])

        assert out[0].label == "H1 · period=5"


class TestWhatEachEntryBecomes:
    def test_an_entry_with_no_grid_is_one_document_per_timeframe(self) -> None:
        # ⚠️ One, not zero. The empty product is 1, and an entry that varies nothing is still a
        # backtest — the same arithmetic the catalogue reports beside a plain entry.
        out = expand_one(a_document(), {}, ["M15", "H1"])

        assert len(out) == 2

    def test_a_grid_multiplies_within_each_timeframe(self) -> None:
        out = expand_one(
            a_document(),
            {"setup.params.period": [5, 9, 21], "setup.params.breakeven_at_r": [1.5, 2.0]},
            ["M15", "H1"],
        )

        # Three by two by two, and the values really are substituted rather than merely counted.
        assert len(out) == 12
        periods = {doc.document["setup"]["params"]["period"] for doc in out}
        assert periods == {5, 9, 21}

    def test_the_values_carry_the_timeframe_beside_the_axes(self) -> None:
        # ⚠️ Coordinates, not a caption. A heatmap cell placed by splitting `label` works until
        # a value contains a separator; these are keyed and typed.
        out = expand_one(a_document(), {"setup.params.period": [5]}, ["H1"])

        assert out[0].values == {"timeframe": "H1", "setup.params.period": 5}

    def test_an_axis_the_document_cannot_reach_is_refused_with_the_entry_named(self) -> None:
        # The entry's name is in the message because a sweep holds several, and "an axis leads
        # nowhere" without saying whose is a message that sends a reader through all of them.
        with pytest.raises(SweepError, match=re.escape("9.1 sem filtro")):
            expand_one(a_document(), {"setup.params.nonesuch": [1, 2]}, ["M15"])

    def test_no_timeframes_is_refused_rather_than_silently_empty(self) -> None:
        # An empty list is a question nobody asked, and returning `[]` would make a sweep that
        # enqueues nothing look like a sweep that ran.
        with pytest.raises(SweepError, match="timeframe"):
            expand_one(a_document(), {}, [])


class TestTheSize:
    """⚠️ **The sum-vs-product arithmetic is proved on the router, not here.**
    `test_entries_add_while_timeframes_and_markets_multiply` launches two entries of three and
    one points over two charts and three markets and demands 24 runs — an implementation that
    multiplied the entries would give 18, so that fixture separates the two. A pure function
    restating the same arithmetic beside it was a second answer that nothing consulted; it was
    deleted, and this class keeps only what the preview itself asks.
    """

    def test_an_entry_that_varies_nothing_still_counts_as_one(self) -> None:
        # The empty product spelled as a number, and the preview reports it per entry.
        assert points_in({}) == 1

    def test_the_count_matches_what_the_expansion_produces(self) -> None:
        # The property that makes the preview worth having, between the two functions that
        # actually run: `points_in` is the number the preview shows for an entry, and
        # `documents_for` builds what the launch writes. Without this pair each could drift
        # alone, and the one a person reads is the one that would be wrong.
        grid: dict[str, list[Any]] = {"setup.params.period": [5, 9, 21]}
        timeframes = ["M15", "H1"]

        produced = len(expand_one(a_document(), grid, timeframes))
        promised = points_in(grid) * len(timeframes)

        assert produced == promised == 6

    def test_the_cap_admits_the_sweep_this_feature_was_asked_for(self) -> None:
        # ⚠️ This caught a real one. Three catalogue entries of fifty points each, over five
        # markets and three timeframes, is 2 250 runs — and the first cap was 2 000, so it
        # refused the exact shape the feature exists for. A cap that refuses the feature is not
        # a cap.
        #
        # Written as the shape rather than as `2250`, so a reader sees which axis is which:
        # the three entries **add**, the charts and the markets multiply.
        asked_for = (50 + 50 + 50) * 3 * 5

        assert asked_for == 2250
        assert size_refusal(asked_for) is None

    def test_the_cap_is_a_time_budget_a_person_can_sit_through(self) -> None:
        # The other side, and the reason the cap is not simply enormous. Asserted against the
        # measured cost rather than against the number itself, so raising one without the other
        # fails here — which is what makes this a budget rather than a preference.
        minutes = MAX_SWEEP_RUNS * SECONDS_PER_BACKTEST / 60

        assert minutes < 12
        # Even if every run were the slowest ever measured on this project (2.17 s).
        assert MAX_SWEEP_RUNS * 2.17 / 60 < 120


class TestWhatTheSizeRefuses:
    """`size_refusal` is the one sentence both endpoints say, so it is tested once here."""

    def test_a_sweep_inside_the_budget_is_not_refused(self) -> None:
        assert size_refusal(1) is None
        assert size_refusal(MAX_SWEEP_RUNS) is None

    def test_the_cap_is_exclusive_at_the_edge(self) -> None:
        # ⚠️ The boundary in both directions, in one test. `>` and `>=` are one character
        # apart and a test at only one side of the edge does not separate them — it would pass
        # for a cap that silently refuses the largest sweep a person was told they could run.
        assert size_refusal(MAX_SWEEP_RUNS) is None
        assert size_refusal(MAX_SWEEP_RUNS + 1) is not None

    def test_the_refusal_says_the_sweep_own_size_and_the_cap(self) -> None:
        # Both numbers, because "too big" without either is a message that sends a person back
        # to count their own grid.
        message = size_refusal(3600)

        assert message is not None
        assert "3600" in message
        assert str(MAX_SWEEP_RUNS) in message

    def test_zero_is_a_refusal_and_not_a_small_sweep(self) -> None:
        # ⚠️ Separated from the cap on purpose: both return a string, and a helper that
        # answered "nothing runs" with the cap's sentence would read as plausible and be absurd.
        message = size_refusal(0)

        assert message is not None
        assert str(MAX_SWEEP_RUNS) not in message
