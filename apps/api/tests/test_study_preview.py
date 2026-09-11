"""What a grid would produce, asked before anything is produced.

The screen cannot answer this question itself. Whether a point can run is the DSL's semantics —
`htf` coarser than the document's own timeframe and a whole number of its bars, a broker clock
required beside a filter — and those live in Python, once. So the browser asks here, and the only
thing that could make that worthless is the answer differing from what a launch would decide.
That agreement is what most of this file is about.

No database: `preview_of` takes the strategy it was handed. The route's own job is a lookup and a
404, and that is the integration suite's.
"""

from typing import Any

import pytest
from fastapi import HTTPException

from tradeforge_api.routers.studies import points_for, preview_of
from tradeforge_db.models import Strategy

# ⚠️ M15, and the number matters. `htf` must be coarser than the document's own timeframe, so on
# this base `M30` and up are legal and `M15` and below are not — which makes an axis over the raw
# list of timeframes half legal, and that is the shape of grid this endpoint exists for.
_FILTERED: dict[str, Any] = {
    "schema_version": "1.0",
    "name": "choch",
    "timeframe": "M15",
    # `stop_buffer` is spelled out rather than left to its default because `expand` refuses a
    # path the document has nothing at — an axis can only vary a key that is already written.
    "setup": {
        "type": "structure_choch",
        "params": {"htf": "H4", "htf_offset": 3, "stop_buffer": 0.1},
    },
    "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
}


def _base(document: dict[str, Any] | None = None) -> Strategy:
    """A stored strategy, in memory. Never added to a session — nothing here writes."""
    definition = dict(_FILTERED if document is None else document)
    return Strategy(name=definition["name"], definition=definition)


def test_a_grid_whose_every_point_runs_reports_no_refusals() -> None:
    preview = preview_of(_base(), {"setup.params.htf": [None, "H4", "D1"]}, "M15")

    assert preview.points == 3
    assert preview.refusals == []
    assert preview.grid_error is None


def test_every_refusal_comes_back_at_once_rather_than_the_first() -> None:
    """⚠️ **The whole reason this is not `points_for`.** A launch stops at the first bad point and
    refuses the request, which is right — nothing may be half-written. A preview that stopped
    there would hand back one problem per round trip, so fixing a three-value axis would take
    three asks, and the third would be the one that finally launched.

    `M1`, `M5` and `M15` are all finer than or equal to the document's own M15.
    """
    preview = preview_of(_base(), {"setup.params.htf": ["M1", "M5", "M15", "H4"]}, "M15")

    assert preview.points == 4
    assert [refusal.values["setup.params.htf"] for refusal in preview.refusals] == [
        "M1",
        "M5",
        "M15",
    ]
    assert all("coarser than M15" in refusal.reason for refusal in preview.refusals)


def test_a_refusal_names_the_point_in_the_words_the_rest_of_the_screen_uses() -> None:
    """The label is what the heatmap and the run log call this point. A preview that invented its
    own phrasing would make the reader match two descriptions of the same combination by eye."""
    preview = preview_of(
        _base(), {"setup.params.htf": ["M5"], "setup.params.stop_buffer": [0.2]}, "M15"
    )

    (refusal,) = preview.refusals
    # A string keeps its quotes here, the way `label_for` writes every other point in the study.
    assert refusal.label == "htf='M5', stop_buffer=0.2"
    assert refusal.values == {"setup.params.htf": "M5", "setup.params.stop_buffer": 0.2}


@pytest.mark.parametrize(
    ("grid", "because"),
    [
        ({"setup.params.nonesuch": [1, 2]}, "nonesuch"),
        ({"setup.params.htf": []}, "no values"),
    ],
)
def test_a_grid_that_cannot_be_applied_is_an_answer_rather_than_an_error(
    grid: dict[str, list[Any]], because: str
) -> None:
    """⚠️ **200, not 422, and the distinction is the endpoint's whole posture.** The caller asked
    what this grid would do. "It cannot be applied to this strategy at all" answers that question.
    Raising instead would make a client tell apart "the preview failed" from "the preview says
    your grid is bad", which are the same news arriving two ways.

    Reported separately from `refusals` because it is a different kind of no: there are no points
    to report on, rather than points that will not run.
    """
    preview = preview_of(_base(), grid, "M15")

    assert preview.points == 0
    assert preview.refusals == []
    assert preview.grid_error is not None
    assert because in preview.grid_error


def test_the_preview_refuses_exactly_what_a_launch_refuses() -> None:
    """⚠️ **The only property that makes this endpoint worth having.** A preview that answered
    about *nearly* the documents a launch would run agrees with the right answer and with the
    wrong one — and it would be believed, because it is the thing on screen.

    Both halves are driven from the same grid: `points_for` is what `POST /studies` calls, and it
    is asked here for its verdict rather than for its documents.
    """
    clean = {"setup.params.htf": [None, "H4"]}
    dirty = {"setup.params.htf": ["M5", "H4"]}

    assert preview_of(_base(), clean, "M15").refusals == []
    assert len(points_for(_base(), clean, "M15")) == 2

    assert [
        refusal.values["setup.params.htf"] for refusal in preview_of(_base(), dirty, "M15").refusals
    ] == ["M5"]
    with pytest.raises(HTTPException) as refused:
        points_for(_base(), dirty, "M15")
    assert refused.value.status_code == 422


def test_the_preview_asks_about_the_named_documents_a_launch_would_write() -> None:
    """⚠️ **Naming is part of preparing a point, not a flourish after it.** `strategies.name` is a
    generated column (`definition ->> 'name'`), so the name is written **into** the document — and
    it is a field the DSL checks. A preview validating the point before it is named would be
    asking about a document one field short of the one that gets written, which is the class of
    divergence that agrees with the right answer and the wrong one alike.

    Made observable by a base name near the DSL's 120-character ceiling: the suffix each point
    carries is what pushes it over. Drop the naming from `_prepared` and this grid comes back
    clean — which is also the bug a person would otherwise meet as a 422 after clicking launch.
    """
    long_name = "c" * 110
    base = _base({**_FILTERED, "name": long_name})

    preview = preview_of(base, {"setup.params.htf": ["H4", "D1"]}, "M15")

    assert preview.points == 2
    assert [refusal.label for refusal in preview.refusals] == ["htf='H4'", "htf='D1'"]
    assert all("at most 120 characters" in refusal.reason for refusal in preview.refusals)


def test_a_point_wrong_in_two_places_says_both() -> None:
    """⚠️ **The same completeness one level down.** The endpoint walks every point rather than
    stopping at the first; a reason that stopped at the first *field* would put the round trip
    straight back, just inside one combination instead of across several.

    Both bad values ride on one point, so a fixture failing for a single reason cannot tell a
    complete message from a truncated one.
    """
    base = _base(
        {
            **_FILTERED,
            "setup": {
                "type": "structure_choch",
                # `breakeven_at_r` is written out because an axis can only vary a key the
                # document already has; `stop_buffer` is bad here and stays bad on every point.
                "params": {
                    "htf": None,
                    "htf_offset": None,
                    "stop_buffer": -5,
                    "breakeven_at_r": 2.0,
                },
            },
        }
    )

    (refusal,) = preview_of(base, {"setup.params.breakeven_at_r": [-1]}, "M15").refusals

    assert refusal.reason == (
        "stop_buffer: Input should be greater than or equal to 0; "
        "breakeven_at_r: Input should be greater than 0"
    )


def test_a_refusal_names_the_field_without_the_model_it_lives_in() -> None:
    """⚠️ Pydantic's `loc` walks the union it took — `setup.structure_choch.params.stop_buffer` —
    and `structure_choch` is the name of an internal model. The web already strips it from the
    other place these errors reach a screen (`settings.ts`, `reasonOf`), so publishing it here
    would give one screen two formats for the same failure.
    """
    base = _base(
        {
            **_FILTERED,
            "setup": {
                "type": "structure_choch",
                # `breakeven_at_r` is written out because an axis can only vary a key the
                # document already has; `stop_buffer` is bad here and stays bad on every point.
                "params": {
                    "htf": None,
                    "htf_offset": None,
                    "stop_buffer": -5,
                    "breakeven_at_r": 2.0,
                },
            },
        }
    )

    (refusal,) = preview_of(base, {"setup.params.breakeven_at_r": [2.0]}, "M15").refusals

    assert refusal.reason.startswith("stop_buffer: ")
    assert "structure_choch" not in refusal.reason


def test_a_refusal_reads_as_one_sentence_rather_than_a_model_dump() -> None:
    """The reason sits beside an axis value on screen. Pydantic's own `str(exc)` is several lines
    of model paths and input values, which is the right body for a strategy form field by field
    and the wrong one for a caption on a grid."""
    long_name = "c" * 110
    (refusal,) = preview_of(
        _base({**_FILTERED, "name": long_name}), {"setup.params.htf": ["H4"]}, "M15"
    ).refusals

    assert "\n" not in refusal.reason
    assert refusal.reason.startswith("name: ")


class TestTheStudysTimeframeDecidesToo:
    """⚠️ The axis that is not in the grid.

    `_FILTERED` is saved at M15 under an H4 filter, which is legal. Launch the same grid at H4
    and the filter stops being coarser than the chart — and the engine says nothing: it assembles
    one "H4" bar per H4 bar, a bar late, so the run reports a filtered strategy that filtered
    nothing. See `runner.timeframe_refusal` for the measurement.
    """

    def test_a_grid_that_runs_at_m15_can_be_refused_whole_at_h4(self) -> None:
        grid = {"setup.params.stop_buffer": [0.1, 0.2]}

        assert preview_of(_base(), grid, "M15").refusals == []
        refused = preview_of(_base(), grid, "H4").refusals

        # Every point, because the timeframe is not on an axis: it is the same wrongness
        # repeated, and reporting one of two would suggest the other is fine.
        assert len(refused) == 2
        assert all("htf" in refusal.reason for refusal in refused)

    def test_the_launch_agrees_with_the_preview(self) -> None:
        # The property this whole module exists for, now across the new argument. A preview that
        # said yes where the launch says no is worse than no preview: it is a promise.
        grid = {"setup.params.stop_buffer": [0.1, 0.2]}

        assert len(points_for(_base(), grid, "M15")) == 2
        with pytest.raises(HTTPException) as refused:
            points_for(_base(), grid, "H4")
        assert refused.value.status_code == 422

    def test_a_filterless_document_runs_at_every_timeframe(self) -> None:
        # ⚠️ The half a "refuse any disagreement" guard would break. Without `htf` the document's
        # timeframe reaches no setup at all, so a grid over a filterless strategy is as legal at
        # H4 as at M15 — and studies over these are most of what this endpoint serves.
        plain = dict(_FILTERED)
        plain["setup"] = {"type": "structure_choch", "params": {"stop_buffer": 0.1}}
        grid = {"setup.params.stop_buffer": [0.1, 0.2]}

        for timeframe in ("M5", "M15", "H1", "H4", "D1"):
            assert preview_of(_base(plain), grid, timeframe).refusals == []

    def test_the_axis_and_the_timeframe_are_judged_together(self) -> None:
        # An axis over `htf` that is half legal at M15 becomes wholly illegal at H4: `H4` is no
        # longer coarser than the chart and `M30` never was. A check that looked at one or the
        # other would get this pair wrong in opposite directions.
        grid = {"setup.params.htf": ["M30", "H4"]}

        at_m15 = {
            refusal.values["setup.params.htf"]
            for refusal in preview_of(_base(), grid, "M15").refusals
        }
        at_h4 = {
            refusal.values["setup.params.htf"]
            for refusal in preview_of(_base(), grid, "H4").refusals
        }

        assert at_m15 == set()
        assert at_h4 == {"M30", "H4"}
