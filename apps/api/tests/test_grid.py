"""Expanding a grid of parameter ranges into the documents a study will run.

No database and no HTTP: this is the arithmetic, and it is the half of a study that can be
wrong without anything crashing. A grid that expands to the wrong documents still produces
runs, still fills a heatmap, and still reads as a finding about the market.
"""

import re
from collections.abc import Sequence
from typing import Any

import pytest

from tradeforge_api.grid import GridError, coordinates, expand, named, read_point, size_of

# A setup document, because every strategy in this project's database is one of these. The
# nesting is what matters: the axes reach two levels down, into a dict shared by every point
# unless it is copied properly.
_BASE: dict[str, Any] = {
    "schema_version": "1.0",
    "name": "MME9 breakout",
    "timeframe": "H1",
    "setup": {
        "type": "mme9_breakout",
        "params": {"side": "long", "period": 9, "take_profit_rr": 5},
    },
    "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
}

# A DSL document, whose indicators are a *list* — the only shape that exercises a numeric
# segment. Kept beside the setup one because the two reach the parameter by different paths.
_DSL: dict[str, Any] = {
    "schema_version": "1.0",
    "name": "MA cross",
    "timeframe": "H1",
    "indicators": [
        {"id": "fast", "type": "SMA", "params": {"period": 2}},
        {"id": "slow", "type": "SMA", "params": {"period": 3}},
    ],
}


def _params(document: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = document["setup"]["params"]
    return params


def _period(document: dict[str, Any], which: int) -> int:
    """The `which`-th indicator's period, out of a DSL document's list."""
    indicators: list[dict[str, Any]] = document["indicators"]
    period: int = indicators[which]["params"]["period"]
    return period


def test_a_grid_expands_to_every_combination_last_axis_fastest() -> None:
    """The golden: 3 periods by 2 targets is 6 documents, in the order a nested loop produces.

    ⚠️ **Three by two, not two by two.** A square grid is the one shape where several wrong
    implementations agree with the right one: `zip` instead of `product` gives 2 points against
    6 here but 2 against 4 there, and a `sum` where the size is a product gives 4 either way on
    a 2x2. The asymmetry is what makes the count mean something.

    The order is asserted, not just the set, because it becomes the order of the study's runs —
    and a heatmap laid out row by row is reading exactly that.
    """
    points = expand(
        _BASE, {"setup.params.period": [5, 9, 20], "setup.params.take_profit_rr": [2, 3]}
    )

    assert [
        (_params(p.document)["period"], _params(p.document)["take_profit_rr"]) for p in points
    ] == [
        (5, 2),
        (5, 3),
        (9, 2),
        (9, 3),
        (20, 2),
        (20, 3),
    ]


def test_expanding_leaves_the_base_document_untouched() -> None:
    """The caller's document is a stored strategy, and a study must not edit the thing it varies.

    Mutating it in place would rewrite the base strategy's own parameters to whatever the last
    grid point happened to be — and the row it came from is immutable by invariant, so the
    damage would only ever show up as a later run of the *base* strategy behaving oddly.
    """
    expand(_BASE, {"setup.params.period": [5, 20]})

    assert _params(_BASE)["period"] == 9


def test_each_point_gets_its_own_nested_parameters() -> None:
    """⚠️ The test that separates a deep copy from a shallow one, and the failure is total.

    A shallow copy shares the nested `params` dict across every point. Each substitution then
    writes into the *same* dictionary, so the last one wins and all N documents come out
    identical — N runs of one configuration, presented as a search of the space.

    Asserted on the objects rather than on the values, because the values are already pinned by
    the golden above; what this states is *why* they can differ at all.
    """
    points = expand(_BASE, {"setup.params.period": [5, 9, 20]})

    holders = [id(_params(point.document)) for point in points]
    assert len(set(holders)) == len(points)


def test_a_numeric_segment_reaches_into_a_list() -> None:
    """`indicators.0.params.period` — the DSL's shape, where the container is a list.

    Both indicators are varied at once so the test cannot pass by indexing the wrong element:
    a path that always resolved to `indicators[0]` would give every point the same slow period.
    """
    points = expand(
        _DSL, {"indicators.0.params.period": [2, 4], "indicators.1.params.period": [10]}
    )

    assert [(_period(p.document, 0), _period(p.document, 1)) for p in points] == [
        (2, 10),
        (4, 10),
    ]


def test_a_path_the_document_has_nothing_at_is_refused() -> None:
    """⚠️ The refusal that matters most, because its absence is silent.

    `periodd` is what a typo looks like. Substituting there would *add* a key, and nothing reads
    it — the setup factory takes the parameters it knows and ignores the rest. The study would
    run N identical backtests, report N identical results, and draw a flat heatmap that reads as
    a finding about the market rather than as a misspelling.
    """
    with pytest.raises(GridError, match=re.escape("nothing at 'setup.params.periodd'")):
        expand(_BASE, {"setup.params.periodd": [5, 9]})


def test_a_path_running_through_a_scalar_is_refused() -> None:
    """The other way a path can fail, and it fails one segment earlier.

    `setup.type` is the string `mme9_breakout`. A walk that stopped at the second-to-last
    segment would hand back that string as the container to write into — and the refusal would
    come later, from Python, as a `TypeError` in the middle of writing a study to the database.
    """
    with pytest.raises(GridError, match=re.escape("nothing at 'setup.type.period'")):
        expand(_BASE, {"setup.type.period": [5, 9]})


def test_an_index_past_the_end_of_a_list_is_refused() -> None:
    """The list-shaped version of the same mistake — and it names the length, so the message
    says what the document actually holds rather than only that something was wrong."""
    with pytest.raises(GridError, match="is a list of 2"):
        expand(_DSL, {"indicators.7.params.period": [2, 4]})


def test_an_axis_with_no_values_is_refused() -> None:
    """An empty axis makes the product zero: the study would be accepted, expand to no points,
    and sit there as a study that ran nothing. Refusing names the axis that was left blank."""
    with pytest.raises(GridError, match="has no values"):
        expand(_BASE, {"setup.params.period": [5, 9], "setup.params.take_profit_rr": []})


def test_an_axis_that_repeats_a_value_is_refused() -> None:
    """Two identical points are two identical backtests, and on a heatmap they are one cell
    drawn twice. Cheap to catch here; indistinguishable from a real result afterwards."""
    with pytest.raises(GridError, match="repeats a value"):
        expand(_BASE, {"setup.params.period": [5, 9, 5]})


def test_a_grid_with_no_axes_is_refused() -> None:
    """A study that varies nothing is a backtest, and `POST /backtests` already exists."""
    with pytest.raises(GridError, match="at least one parameter"):
        expand(_BASE, {})


def test_a_grid_over_the_limit_is_refused_with_its_own_size() -> None:
    """Refused whole, never trimmed — and the message carries the count.

    Half a grid drawn as a heatmap is a picture of a space that was never searched, and it looks
    exactly like a picture of one that was. The size is in the message because the product grows
    multiplicatively: the caller who typed a fourth axis did not add five points, they multiplied
    by five, and the number is the only thing that says so.
    """
    with pytest.raises(GridError, match="expands to 27 combinations, over the 8"):
        expand(
            _BASE,
            {
                "setup.params.period": [5, 9, 20],
                "setup.params.take_profit_rr": [2, 3, 4],
                "setup.params.side": ["long", "short", "both"],
            },
            limit=8,
        )


def test_a_grid_exactly_at_the_limit_is_accepted() -> None:
    """The boundary belongs to the accepted side: a limit of N means N points may run.

    Its own test because `>` and `>=` are indistinguishable everywhere else — every other
    scenario here sits well clear of the cap.
    """
    points = expand(_BASE, {"setup.params.period": [5, 9, 20]}, limit=3)

    assert len(points) == 3


def test_the_size_is_the_product_of_the_axes_not_their_total() -> None:
    """Said separately from `expand` because a caller needs the number *before* committing.

    ⚠️ Three by two by two: 12 as a product, 7 as a sum. Two axes of two would be 4 either way,
    and the whole point of reporting this is that people underestimate it.
    """
    grid: dict[str, Sequence[Any]] = {
        "setup.params.period": [5, 9, 20],
        "setup.params.take_profit_rr": [2, 3],
        "setup.params.side": ["long", "short"],
    }

    assert size_of(grid) == 12


def test_a_point_is_named_by_what_makes_it_different() -> None:
    """The stored name is what the run log shows, and the run log is where a study is read.

    Only the leaf of each path appears: the `setup.params.` prefix is identical across every
    point of a study, so repeating it would push the part that differs off the end of the column.
    """
    [point] = expand(_BASE, {"setup.params.period": [9]})

    assert named("MME9 breakout", point) == "MME9 breakout [period=9]"


def test_an_axis_can_replace_a_whole_list_element() -> None:
    """The last segment being a list index, which no other test reaches.

    `indicators.0.params.period` walks *through* a list and lands in a dict; `indicators.0`
    lands on the list itself, which is the only shape that exercises writing and reading by
    position. A grid over it swaps one indicator for another wholesale — coarse, but the DSL
    permits it and the substitution has to be right or a study would run documents nobody asked
    for.
    """
    points = expand(
        _DSL,
        {"indicators.0": [{"id": "fast", "type": "EMA", "params": {"period": 2}}]},
    )

    [point] = points
    assert point.document["indicators"][0]["type"] == "EMA"
    # And the sibling is untouched, so the write went to the named index rather than to the list.
    assert point.document["indicators"][1]["type"] == "SMA"


@pytest.mark.parametrize("path", ["", ".", "setup..params", "setup.params."])
def test_a_malformed_path_is_refused_before_anything_is_walked(path: str) -> None:
    """The grid's keys are raw strings from a client, so the empty segment is reachable input.

    Untested until now, and the failure without it is not a refusal: an empty segment would be
    looked up as a key, miss, and produce a message about the strategy having nothing at `''` —
    which reads as a fact about the strategy rather than as a malformed request.
    """
    with pytest.raises(GridError, match="not a path into a strategy document"):
        expand(_BASE, {path: [1, 2]})


def test_an_axis_the_documents_do_not_have_reads_as_nothing_rather_than_raising() -> None:
    """⚠️ The read is deliberately forgiving where the expansion is deliberately loud.

    A grid is a stored column somebody can edit, and a study whose grid names a parameter its
    documents lack is a study that can still be *read*: every other axis places, and the odd one
    out sorts last. Raising here would 500 a reporting endpoint over a value that is merely
    surprising — the heatmap, the fold's chosen label and the run ordering all go through this.

    Expanding the same grid still refuses, and the asymmetry is the point: writing a document
    from a path nothing reads would create a parameter the engine ignores.
    """
    document = {"setup": {"type": "mme9_breakout", "params": {"period": 9}}}
    grid = {"setup.params.period": [9], "setup.params.nothing_reads_this": [1, 2]}

    assert read_point(document, grid) == {
        "setup.params.period": 9,
        "setup.params.nothing_reads_this": None,
    }
    # Unplaceable sorts last: `len(axis)` is one past the final index, whatever the axis holds.
    assert coordinates(grid, read_point(document, grid)) == (0, 2)

    with pytest.raises(GridError, match=re.escape("nothing at 'setup.params.nothing_reads_this'")):
        expand(document, grid)
