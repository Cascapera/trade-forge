"""The contract between the schema package and the engine, pinned where both can be imported.

`packages/schema` and `packages/engine` deliberately do not import each other: the schema is the
DSL's shape and meaning, the engine is its execution, and the direction of a dependency between
them would put one in charge of the other. `apps/api` depends on both, so this is the only place
in the tree that can compare them — and the comparison is worth making because the two facts below
are written down twice.

⚠️ **Both kinds of drift fail silently.** An indicator the schema publishes and the engine cannot
build is caught loudly, by `EngineError` at compile time. But a *component* the schema accepts and
the engine does not open a channel for resolves to `None` at evaluation, and a comparison against
`None` is false — so the strategy runs, reports, and never trades, which looks exactly like a rule
that was evaluated and declined.
"""

from decimal import Decimal, localcontext

from tradeforge_engine.domain import Candle
from tradeforge_engine.indicators import COMPOSITE_COMPONENTS as ENGINE_COMPONENTS
from tradeforge_engine.indicators import INDICATOR_BUILDERS
from tradeforge_engine.loop import ENGINE_CONTEXT
from tradeforge_engine.testing import HOUR, START
from tradeforge_schema.models import COMPOSITE_COMPONENTS as SCHEMA_COMPONENTS
from tradeforge_schema.models import Indicator


def _published_types() -> set[str]:
    """Every `type` literal the DSL's discriminated indicator union admits."""
    published: set[str] = set()
    for member in Indicator.__value__.__args__[0].__args__:
        annotation = member.model_fields["type"].annotation
        published.update(annotation.__args__)
    return published


def test_the_union_really_publishes_the_indicators_this_module_thinks_it_does() -> None:
    """⚠️ Read first, because every other test here compares two sets.

    `_published_types` walks Pydantic internals, and an empty result would make
    `published <= builders` pass vacuously — a green suite asserting nothing at all about the DSL.
    """
    assert len(_published_types()) == len(INDICATOR_BUILDERS) > 6


def test_every_indicator_the_dsl_publishes_can_be_built_by_the_engine() -> None:
    """A document the schema calls valid must be runnable, or the API accepts what it cannot run."""
    assert _published_types() <= set(INDICATOR_BUILDERS)


def test_the_engine_builds_nothing_the_dsl_cannot_express() -> None:
    """The other direction: a builder nobody can reach is dead weight that reads as a feature."""
    assert set(INDICATOR_BUILDERS) <= _published_types()


def test_the_two_packages_agree_on_which_indicators_have_components() -> None:
    """The set of multi-output types, both spellings.

    An indicator listed here by the schema but not by the engine would have its components
    accepted and then resolve to nothing. One listed by the engine but not the schema is the
    mirror: the schema would demand a bare reference for something the compiler opens three
    channels for, so the only reference that validates is the one that resolves to nothing.
    """
    assert set(SCHEMA_COMPONENTS) == set(ENGINE_COMPONENTS)


def test_the_two_packages_agree_on_the_component_names_themselves() -> None:
    """⚠️ The one a typo hides in. `bb.uppper` refused by validation is a usable error message;
    `bb.upper` accepted by validation and unknown to the engine is a backtest of nothing."""
    assert SCHEMA_COMPONENTS == ENGINE_COMPONENTS


def test_a_composite_reports_exactly_the_components_it_declares_in_order() -> None:
    """And the third place the names could drift: the running object.

    `COMPOSITE_COMPONENTS` in the engine is a mapping the compiler reads to size its channels, but
    what a bar actually writes comes from `components()` on the instance. A class whose mapping and
    whose declared tuple disagreed would open channels nothing ever fills — `None` for ever, on a
    ref that validated.

    ⚠️ **Compared as a tuple, not as a set, and the difference is the whole point.** The compiler
    builds a composite's channels by iterating `components()` — not by reading `COMPONENTS` — and
    that order survives all the way to the chart, where `toCurves` draws the first component solid
    and thick as the subject of the family. So `COMPONENTS` being ordered primary-first is only
    worth anything if the running object agrees: reorder the literal inside `components()` and the
    upper band gets drawn as the subject of its own average. Nothing about the *numbers* changes,
    which is exactly why a set comparison let it through.

    ⚠️ **And the indicator has to be fed first, which a mutation run is what caught.** A composite
    returns a different literal while warming up — all names, all `None` — so a version of this test
    that asked a freshly built object was reading the wrong mapping: reordering the *populated*
    return survived it untouched. Two probes now, warm and warmed-up, because the answer has to be
    the same shape either side of the warm-up and only one of them is the one a bar writes.
    """
    for kind, names in ENGINE_COMPONENTS.items():
        params = {"period": 2} if kind == "ADX" else {"period": 2, "deviations": 2}
        builder = INDICATOR_BUILDERS[kind]
        indicator = builder({"id": "probe", "type": kind, "params": params})
        assert tuple(indicator.components()) == tuple(names), f"{kind}, warming up"  # type: ignore[union-attr]

        with localcontext(ENGINE_CONTEXT):
            for index in range(6):
                price = Decimal(100) + index
                indicator.update(
                    Candle(
                        time=START + index * HOUR,
                        open=price,
                        high=price + 1,
                        low=price - 1,
                        close=price,
                    )
                )
        component = indicator.components()  # type: ignore[union-attr]
        assert tuple(component) == tuple(names), f"{kind}, warmed up"
        assert all(value is not None for value in component.values()), kind
