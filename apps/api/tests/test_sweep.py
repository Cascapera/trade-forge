"""Expanding a sweep — the arithmetic, with no database anywhere.

A sweep is the product a study and a basket each refuse to take, plus the timeframe. What can be
wrong here is what every point becomes: which document, under which name, at which chart. The
router's job is writing those and is the integration suite's.
"""

import copy
import re
from typing import Any

import pytest

from tradeforge_api.sweep import (
    SweepDocument,
    SweepError,
    documents_for,
    points_in,
    shared,
    size_refusal,
)
from tradeforge_engine.setup_factory import unread_params


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

    def test_the_sweep_he_was_refused_is_admitted(self) -> None:
        # ⚠️ The real one, 18/09: "this sweep expands to 3168 backtests, over the 3000". No cap
        # since (his decision) — hours of queue are his to spend. Written as a size far past the
        # old cap too, so a cap reintroduced at any plausible number fails here.
        assert size_refusal(3168) is None
        assert size_refusal(1_000_000) is None


class TestWhatTheSizeRefuses:
    """`size_refusal` is the one sentence both endpoints say, so it is tested once here — and
    since 18/09 the only size it refuses is none at all."""

    def test_any_sweep_with_something_to_run_is_not_refused(self) -> None:
        assert size_refusal(1) is None
        assert size_refusal(3001) is None

    def test_zero_is_a_refusal_and_not_a_small_sweep(self) -> None:
        # The one refusal left: nothing to run is a question that cannot be asked, and `0 runs`
        # beside a live button invites pressing it.
        message = size_refusal(0)

        assert message == "no combination in this sweep can run"


# His template's shape: a setup that brings a 5 R target with it.
BASE: dict[str, Any] = {
    **a_document(),
    "exit": {
        "stop_loss": None,
        "take_profit": {"type": "risk_multiple", "params": {"rr": 5}},
        "conditions": [],
    },
}


class TestNoTargetUnlessTheGridNamesOne:
    """His call of 23/09: a sweep runs without a target and scores every target from how far the
    trades went. Only a grid that names the target axis runs with the targets it names."""

    def test_a_grid_without_the_target_axis_runs_every_point_without_a_target(self) -> None:
        docs = documents_for(
            entry_id="e1",
            entry_name="9.1",
            definition=BASE,
            grid={"setup.params.period": [9, 21]},
            timeframes=["M15"],
        )
        assert [doc.document["exit"]["take_profit"] for doc in docs] == [None, None]
        # The rest of the exit block is the saved one.
        assert all(doc.document["exit"]["conditions"] == [] for doc in docs)

    def test_an_entry_with_no_grid_runs_without_a_target_too(self) -> None:
        [doc] = documents_for(
            entry_id="e1", entry_name="9.1", definition=BASE, grid={}, timeframes=["M15"]
        )
        assert doc.document["exit"]["take_profit"] is None

    def test_a_grid_that_names_the_target_runs_with_exactly_those(self) -> None:
        docs = documents_for(
            entry_id="e1",
            entry_name="9.1",
            definition=BASE,
            grid={"exit.take_profit.params.rr": [2, None]},
            timeframes=["M15"],
        )
        assert [doc.document["exit"]["take_profit"] for doc in docs] == [
            {"type": "risk_multiple", "params": {"rr": 2}},
            None,
        ]

    def test_the_saved_document_is_left_as_it_was(self) -> None:
        before = copy.deepcopy(BASE)
        documents_for(entry_id="e1", entry_name="9.1", definition=BASE, grid={}, timeframes=["H1"])
        assert before == BASE


def a_zone_document() -> dict[str, Any]:
    """His CHOCH entry, the setup whose entry points leave some dials unread."""
    return {
        "schema_version": "1.0",
        "name": "CHOCH COMPLETO",
        "timeframe": "M15",
        "setup": {
            "type": "structure_choch",
            "params": {
                "entry_point": "edge",
                "stop_buffer": 0.1,
                "gift_stop": "gift",
                "volume_filter": False,
            },
        },
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


class TestPointsThatRunTheSameShareOneRun:
    """His answer, 24/09: a point that differs from another only in a parameter its entry point
    never reads is answered by that one's run."""

    GRID: dict[str, list[Any]] = {  # noqa: RUF012 — read-only, a class-level fixture
        "setup.params.entry_point": ["edge", "martelo"],
        "setup.params.stop_buffer": [0, 0.1, 0.2],
    }

    def test_the_buffer_is_shared_where_the_entry_does_not_read_it(self) -> None:
        docs = expand_one(a_zone_document(), self.GRID, ["M15"], name="CHOCH")

        answered = shared(docs, unread_params)

        by_label = {doc.label: doc for doc in docs}
        followers = {by_label[doc.label].label for doc in docs if id(doc) in answered}
        # Every martelo point but the first is answered; no edge point is: edge reads the buffer.
        martelo = [doc for doc in docs if doc.values["setup.params.entry_point"] == "martelo"]
        assert followers == {doc.label for doc in martelo[1:]}
        assert all(answered[id(doc)] is martelo[0] for doc in martelo[1:])

    def test_the_first_point_in_launch_order_owns_the_run(self) -> None:
        docs = expand_one(a_zone_document(), self.GRID, ["M15"])
        order = [id(doc) for doc in docs]

        answered = shared(docs, unread_params)

        assert answered
        for follower, owner in answered.items():
            assert order.index(id(owner)) < order.index(follower)
            assert id(owner) not in answered

    def test_never_across_charts(self) -> None:
        grid: dict[str, list[Any]] = {
            "setup.params.entry_point": ["martelo"],
            "setup.params.stop_buffer": [0, 0.1],
        }
        docs = expand_one(a_zone_document(), grid, ["M15", "H1"])

        answered = shared(docs, unread_params)

        assert len(answered) == 2
        assert all(
            doc.timeframe == answered[id(doc)].timeframe for doc in docs if id(doc) in answered
        )

    def test_never_across_entries(self) -> None:
        """Two entries can hold the same document; a point answered by the other's run would lose
        the entry it belongs to on every screen that groups by entry."""
        grid = {"setup.params.entry_point": ["martelo"]}
        mine = expand_one(a_zone_document(), grid, ["M15"], name="A")
        theirs = documents_for(
            entry_id="e2",
            entry_name="B",
            definition=a_zone_document(),
            grid=grid,
            timeframes=["M15"],
        )

        assert shared([*mine, *theirs], unread_params) == {}

    def test_nothing_is_shared_that_the_setup_reads(self) -> None:
        grid: dict[str, list[Any]] = {
            "setup.params.entry_point": ["edge"],
            "setup.params.stop_buffer": [0, 0.1, 0.2],
        }
        docs = expand_one(a_zone_document(), grid, ["M15"])

        assert shared(docs, unread_params) == {}

    def test_nothing_is_shared_for_a_setup_the_engine_vouches_nothing_for(self) -> None:
        grid = {"setup.params.period": [5, 9]}
        docs = expand_one(a_document(), grid, ["M15"])

        assert shared(docs, unread_params) == {}
        assert shared(docs, lambda _setup: frozenset({"period"})) != {}
