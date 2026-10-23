"""The component names are published by the classes, and read back from them.

Before this, `COMPOSITE_COMPONENTS` was a literal map written beside the classes, and the only
place the names reached the generated JSON Schema was inside a docstring — as prose. A consumer
without a Python runtime (the web builder) therefore could not offer `bb.upper` without writing
a third copy of the list by hand, and a hand-written third copy is the one that goes stale.

So the names now live on the class as `json_schema_extra`, the map is derived from the union, and
these tests pin both halves of that: what the derivation produces, and what the schema carries.
"""

import pytest
from pydantic import ConfigDict

from tradeforge_schema.generate import build_schema
from tradeforge_schema.models import (
    COMPOSITE_COMPONENTS,
    Indicator,
    # Private on purpose, and imported anyway: its refusals are only reachable from here, and
    # `readSetups` states the rule this follows — a guard nothing exercises is a guess.
    _declared_components,
    _Node,
)


def test_the_map_names_exactly_the_two_composites_in_primary_first_order() -> None:
    """Order is load-bearing: the chart draws the first component as the subject of its family."""
    assert COMPOSITE_COMPONENTS == {
        "BOLLINGER": ("middle", "upper", "lower"),
        "ADX": ("adx", "plus_di", "minus_di"),
    }


def test_a_single_valued_indicator_declares_no_components() -> None:
    """The negative half. An SMA that reported components would make `sma` alone a reference
    error, and every strategy already saved uses exactly that spelling."""
    sma = next(
        member for member in Indicator.__value__.__args__[0].__args__ if member.__name__ == "SMA"
    )
    assert _declared_components(sma) == ()


def test_every_composite_publishes_its_components_into_the_generated_schema() -> None:
    """⚠️ The half that a Python-only test would miss.

    The map above could be perfect and the schema still carry nothing — which is precisely the
    state this change was made to fix, and the state a consumer without a Python runtime cannot
    tell apart from "these indicators have no components".
    """
    defs = build_schema()["$defs"]
    assert defs["Bollinger"]["components"] == ["middle", "upper", "lower"]
    assert defs["ADX"]["components"] == ["adx", "plus_di", "minus_di"]
    # And the negative, in the same document: a single-valued indicator carries no such key, so a
    # reader can use its absence to mean "referenced by bare id".
    assert "components" not in defs["SMA"]


def test_a_callable_schema_extra_is_refused_rather_than_read_as_no_components() -> None:
    """⚠️ The failure this guard exists for is silent, not loud.

    Pydantic accepts `json_schema_extra` as a *callable* that mutates the schema in place. A
    reader that only handled the mapping form would answer `()` for such a class — which does not
    look like an error, it looks like a single-valued indicator, and every `bb.upper` in every
    saved strategy would start coming back as "reference to a component that does not exist".
    """

    class Callable(_Node):
        model_config = ConfigDict(json_schema_extra=lambda schema: schema)

    with pytest.raises(TypeError, match="no reader can inspect"):
        _declared_components(Callable)


def test_components_that_are_not_names_are_refused() -> None:
    class NotNames(_Node):
        model_config = ConfigDict(json_schema_extra={"components": ["upper", 3]})

    with pytest.raises(TypeError, match="not a name"):
        _declared_components(NotNames)


def test_components_that_are_not_a_list_are_refused() -> None:
    class NotAList(_Node):
        model_config = ConfigDict(json_schema_extra={"components": "upper"})

    with pytest.raises(TypeError, match="not a list"):
        _declared_components(NotAList)
