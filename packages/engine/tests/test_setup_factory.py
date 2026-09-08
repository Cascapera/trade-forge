"""Building a named setup from its DSL block.

The factory is a lookup table and a handful of parameter reads, which sounds like it needs no
tests until you notice what a wrong read costs: a `period` that silently becomes 1, a `stop_buffer`
that carries a float's binary dust into every stop price, a `breakeven_at_r: null` that arrives as
the default 2 and quietly takes winners to breakeven in a run whose whole point was to measure what
happens without that. None of those raise. All of them produce a plausible backtest.

So these assert what was *built*, not merely that something was, and they reach into the private
attributes to do it. That is deliberate: the parameters are the entire contract of this module, and
a test that only checked the class name would pass for every one of the bugs above.
"""

import datetime as dt
from decimal import Decimal

import pytest

from tradeforge_engine.average_setups import AverageEntryPoint, PatternWatch
from tradeforge_engine.bar_setups import GiftStop, GiftTrigger, IgnoredBarTrigger
from tradeforge_engine.domain import Side
from tradeforge_engine.errors import EngineError
from tradeforge_engine.setup_factory import build_setup
from tradeforge_engine.setups import (
    ChochQualifier,
    ContinuationQualifier,
    StructureStrategy,
    ZoneEntryPoint,
)
from tradeforge_engine.swing import Mme9BreakoutStrategy, PontoContinuoStrategy


def _built(kind: str, **params: object) -> object:
    return build_setup({"type": kind, "params": params})


# --------------------------------------------------------------------------- #
# The four setups the DSL can name                                              #
# --------------------------------------------------------------------------- #


def test_the_mme9_breakout_is_built_with_its_parameters() -> None:
    setup = _built(
        "mme9_breakout", side="short", period=21, stop_buffer_ticks=3, breakeven_at_r=1.5
    )
    assert isinstance(setup, Mme9BreakoutStrategy)
    assert setup._side is Side.SHORT
    assert setup._stop_buffer_ticks == Decimal(3)
    assert setup._breakeven_at_r == Decimal("1.5")


def test_the_ponto_continuo_is_built_with_its_average() -> None:
    setup = _built("ponto_continuo", side="long", period=20, average="SMA")
    assert isinstance(setup, PontoContinuoStrategy)
    assert setup._side is Side.LONG
    # The average kind is the one parameter with no numeric tell, so it is read off the object it
    # produced rather than off the argument that produced it.
    assert type(setup._average).__name__ == "SMA"


def test_the_choch_setup_is_built_on_its_qualifier() -> None:
    setup = _built("structure_choch", allow_secondary=True, stop_buffer=0.25)
    assert isinstance(setup, StructureStrategy)
    assert isinstance(setup._qualifier, ChochQualifier)
    assert setup._allow_secondary is True
    assert setup._stop_buffer == Decimal("0.25")


def test_the_continuation_setup_carries_its_cap_into_the_qualifier() -> None:
    """`max_bos` belongs to the qualifier, not to the strategy — the factory is the only place
    that knows the parameter has to be routed one level deeper."""
    setup = _built("structure_continuation", max_bos=1)
    assert isinstance(setup, StructureStrategy)
    assert isinstance(setup._qualifier, ContinuationQualifier)
    assert setup._qualifier._max_bos == 1


# --------------------------------------------------------------------------- #
# Defaults: the factory holds none, so omission must reach the class            #
# --------------------------------------------------------------------------- #


def test_an_omitted_parameter_takes_the_engine_class_default() -> None:
    """The factory passes only what the document carried. Restating a default here would be a
    third copy of the number — see `tests/test_setup_defaults.py`, which pins the other two."""
    lean = _built("ponto_continuo", side="long")
    bare = PontoContinuoStrategy(side=Side.LONG)
    assert isinstance(lean, PontoContinuoStrategy)
    assert lean._breakeven_at_r == bare._breakeven_at_r
    assert lean._stop_buffer_ticks == bare._stop_buffer_ticks
    assert type(lean._average).__name__ == type(bare._average).__name__


def test_a_setup_block_with_no_params_at_all_is_built_on_defaults() -> None:
    """`{"type": "structure_choch"}` — the structure family needs no parameter to run, so the
    document is allowed to say nothing, and the schema gives `params` a default for that reason."""
    setup = build_setup({"type": "structure_choch"})
    assert isinstance(setup, StructureStrategy)
    assert setup._stop_buffer == StructureStrategy(qualifier=ChochQualifier())._stop_buffer


# --------------------------------------------------------------------------- #
# Present-and-null is a setting; absent is not                                  #
# --------------------------------------------------------------------------- #


def test_an_explicit_null_breakeven_switches_the_rule_off() -> None:
    """The distinction the whole `_optional_decimal` helper exists for. "What would this setup
    earn without taking winners to breakeven" is a real question, and `null` is how the document
    asks it — so it must reach the class as `None` rather than be treated as absent and defaulted
    back to 2, which would answer a different question and look right doing it.
    """
    off = _built("ponto_continuo", side="long", breakeven_at_r=None)
    assert isinstance(off, PontoContinuoStrategy)
    assert off._breakeven_at_r is None
    assert PontoContinuoStrategy(side=Side.LONG)._breakeven_at_r is not None


def test_an_explicit_null_max_bos_means_uncapped() -> None:
    setup = _built("structure_continuation", max_bos=None)
    assert isinstance(setup, StructureStrategy)
    assert isinstance(setup._qualifier, ContinuationQualifier)
    assert setup._qualifier._max_bos is None


# --------------------------------------------------------------------------- #
# Numbers arrive from JSONB, and the route matters                              #
# --------------------------------------------------------------------------- #


def test_a_fractional_parameter_does_not_inherit_a_float_s_binary_dust() -> None:
    """`0.1` as an IEEE double is 0.1000000000000000055511151231257827…, and a stop buffer built
    from it is that far off the zone width the document asked for. Going through `str` is what
    keeps the number the user typed — the same route the runner uses for the risk percent."""
    setup = _built("structure_choch", stop_buffer=0.1)
    assert isinstance(setup, StructureStrategy)
    assert setup._stop_buffer == Decimal("0.1")
    assert setup._stop_buffer != Decimal(0.1)  # noqa: RUF032 — the dust is the point


# --------------------------------------------------------------------------- #
# Loud failure: an unbuildable document raises rather than running something else#
# --------------------------------------------------------------------------- #


def test_an_unknown_setup_type_names_the_alternatives() -> None:
    with pytest.raises(EngineError, match="unknown setup type 'setup_9_4'"):
        build_setup({"type": "setup_9_4", "params": {}})


def test_a_setup_with_no_type_is_refused() -> None:
    with pytest.raises(EngineError, match="unknown setup type"):
        build_setup({"params": {"side": "long"}})


@pytest.mark.parametrize("side", ["", "buy", None, 1])
def test_a_directional_setup_demands_a_side_it_understands(side: object) -> None:
    with pytest.raises(EngineError, match="setup side must be"):
        build_setup({"type": "mme9_breakout", "params": {"side": side}})


def test_params_must_be_a_mapping() -> None:
    with pytest.raises(EngineError, match="setup params must be a mapping"):
        build_setup({"type": "structure_choch", "params": [1, 2]})


def test_an_unknown_average_is_refused_rather_than_defaulted() -> None:
    with pytest.raises(EngineError, match="setup average must be"):
        _built("ponto_continuo", side="long", average="WMA")


@pytest.mark.parametrize("value", ["20", 20.5, None])
def test_a_period_that_is_not_an_integer_is_refused(value: object) -> None:
    with pytest.raises(EngineError, match="setup period must be an integer"):
        _built("ponto_continuo", side="long", period=value)


def test_a_boolean_is_not_an_integer() -> None:
    """`True` is an `int` in Python, so `period: true` would otherwise build a period of 1 and
    run — a nonsense document producing a plausible backtest, which is the worst failure mode
    this module has."""
    with pytest.raises(EngineError, match="setup period must be an integer"):
        _built("ponto_continuo", side="long", period=True)


def test_a_flag_that_is_not_a_boolean_is_refused() -> None:
    with pytest.raises(EngineError, match="setup allow_secondary must be true or false"):
        _built("structure_choch", allow_secondary="yes")


@pytest.mark.parametrize("value", [{"a": 1}, [1], True])
def test_a_numeric_parameter_that_is_not_a_number_is_refused(value: object) -> None:
    with pytest.raises(EngineError, match="setup stop_buffer must be a number"):
        _built("structure_choch", stop_buffer=value)


def test_a_parameter_the_engine_class_rejects_still_raises() -> None:
    """The factory does not re-implement the classes' own validation, and does not need to: a
    period of zero reaches `PontoContinuoStrategy` and is refused there, with its message."""
    with pytest.raises(ValueError, match="average period must be >= 1"):
        _built("ponto_continuo", side="long", period=0)


def test_an_unknown_entry_point_is_refused_rather_than_defaulted() -> None:
    """⚠️ A document naming an entry point this engine does not have must not run at the edge.

    `build_setup` takes a raw `Mapping`, not the Pydantic model — a strategy stored in JSONB
    reaches it unvalidated. `"body"` is the likeliest wrong value of all, because the author's own
    book lists it as model 2 and the engine deliberately does not carry it. Defaulting would run
    that document at the widest stop of the two and report the result as the method the document
    asked for.
    """
    for kind in ("structure_choch", "structure_continuation"):
        with pytest.raises(EngineError, match="setup entry_point must be"):
            build_setup({"type": kind, "params": {"entry_point": "body"}})


def test_the_return_pass_entry_point_reaches_the_engine() -> None:
    """The value the DSL gained for 11.5, carried the whole way from a raw document to the enum.

    ⚠️ The refusal above and this acceptance are one mechanism read twice, and neither alone says
    it works: the allowed set is derived from `ZoneEntryPoint`, so a value missing from the enum
    is refused with the same message an unknown one gets. `"body"` proves the gate closes, and
    only a value that must pass proves it opens — on the field that is *not* the default, or the
    assertion would hold for a factory that dropped it.
    """
    for kind in ("structure_choch", "structure_continuation"):
        setup = _built(kind, entry_point="return_pass")
        assert isinstance(setup, StructureStrategy)
        assert setup._entry_point is ZoneEntryPoint.RETURN_PASS


def test_the_gift_s_stop_and_the_volume_filter_reach_the_trigger() -> None:
    """The two parameters the gift added, carried from a raw document to the object that reads
    them — and read off the trigger the activation was built with, because `StructureStrategy`
    keeps neither as a field of its own.

    ⚠️ Both on their non-default value. `gift_stop="gift"` and `volume_filter=False` are what
    the class does when nothing arrives, so asserting them would hold for a factory that dropped
    both keys on the floor.
    """
    for kind in ("structure_choch", "structure_continuation"):
        setup = _built(kind, entry_point="gift", gift_stop="forca", volume_filter=True)
        assert isinstance(setup, StructureStrategy)
        trigger = setup._activation.trigger  # type: ignore[attr-defined]
        assert isinstance(trigger, GiftTrigger)
        assert trigger.stop_at is GiftStop.FORCA
        assert trigger.volume_fraction == Decimal("0.70")


def test_the_ignored_bar_reads_the_filter_and_has_no_stop_to_choose() -> None:
    setup = _built("structure_choch", entry_point="barra_ignorada", volume_filter=True)
    assert isinstance(setup, StructureStrategy)
    trigger = setup._activation.trigger  # type: ignore[attr-defined]
    assert isinstance(trigger, IgnoredBarTrigger)
    assert trigger.volume_fraction == Decimal("0.70")

    quiet = _built("structure_choch", entry_point="barra_ignorada")
    assert quiet._activation.trigger.volume_fraction is None  # type: ignore[attr-defined]


def test_an_unknown_gift_stop_is_refused_rather_than_defaulted() -> None:
    """`"region"` is the likeliest wrong value — it is where the hammer's stop sat before he
    moved it onto the bar — and defaulting it would run the document at the gift's own low."""
    with pytest.raises(EngineError, match="setup gift_stop must be one of 'gift', 'forca'"):
        _built("structure_choch", entry_point="gift", gift_stop="region")


def test_the_mme9_s_entry_point_and_the_gift_dials_reach_the_watch() -> None:
    """The three parameters the average host gained, carried to the object that reads them, on
    their non-default values. The classic entry keeps no watch at all, which is the other half."""
    setup = _built(
        "mme9_breakout", side="long", entry_point="gift", gift_stop="forca", volume_filter=True
    )
    assert isinstance(setup, Mme9BreakoutStrategy)
    watch = setup._watch
    assert isinstance(watch, PatternWatch)
    assert watch.entry_point is AverageEntryPoint.GIFT
    assert watch.gift_stop is GiftStop.FORCA
    assert watch.volume_filter is True

    classic = _built("mme9_breakout", side="long")
    assert classic._watch is None  # type: ignore[attr-defined]


def test_the_ponto_continuo_takes_the_same_three_dials() -> None:
    """The second host of the same clock, and the assertion is that the *watch* was built — not
    merely that the constructor accepted the words."""
    setup = _built(
        "ponto_continuo",
        side="long",
        entry_point="gift",
        gift_stop="forca",
        volume_filter=True,
    )
    assert isinstance(setup, PontoContinuoStrategy)
    watch = setup._watch
    assert isinstance(watch, PatternWatch)
    assert watch.entry_point is AverageEntryPoint.GIFT
    # ⚠️ **The line the first version of this test dropped**, and the root sweep cannot cover it:
    # `test_setup_defaults` swaps the strategy for a recorder, so it proves the keyword reached
    # `__init__` and never that `__init__` passed it on. A constructor pinning `GiftStop.GIFT` here
    # survives both suites, and the document that asked for the force bar's stop is run with the
    # gift's — tighter, so the position is larger and the backtest answers a question nobody put.
    assert watch.gift_stop is GiftStop.FORCA
    assert watch.volume_filter is True

    ignored = _built("ponto_continuo", side="long", entry_point="barra_ignorada")
    assert isinstance(ignored, PontoContinuoStrategy)
    assert ignored._watch is not None
    assert ignored._watch.entry_point is AverageEntryPoint.BARRA_IGNORADA

    classic = _built("ponto_continuo", side="long")
    assert classic._watch is None  # type: ignore[attr-defined]


def test_an_unknown_ponto_continuo_entry_point_is_refused_rather_than_defaulted() -> None:
    """`"midpoint"` is a real entry point on the structure setups and means nothing here."""
    with pytest.raises(EngineError, match="setup entry_point must be one of 'classic', 'martelo'"):
        _built("ponto_continuo", side="long", entry_point="midpoint")


def test_an_unknown_mme9_entry_point_is_refused_rather_than_defaulted() -> None:
    """`"edge"` is a real entry point one setup over, and the likeliest wrong value here."""
    with pytest.raises(EngineError, match="setup entry_point must be one of 'classic', 'martelo'"):
        _built("mme9_breakout", side="long", entry_point="edge")


def test_the_volume_filter_must_be_a_boolean() -> None:
    with pytest.raises(EngineError, match="setup volume_filter must be true or false"):
        _built("structure_choch", entry_point="gift", volume_filter="on")


# --------------------------------------------------------------------------- #
# The timeframe above (2026-09-08)                                              #
# --------------------------------------------------------------------------- #


def test_the_higher_timeframe_reaches_both_structure_setups_as_a_duration() -> None:
    """The document names a bar; the class takes how long one lasts. Both setups of the family
    take it, because the filter is the family's (his answer 7), and `structure_continuation`
    is the one a test written against the choch alone would leave unproven."""
    for kind in ("structure_choch", "structure_continuation"):
        setup = build_setup(
            {"type": kind, "params": {"htf": "H4", "htf_offset": 3}},
            timeframe=dt.timedelta(minutes=15),
        )
        assert isinstance(setup, StructureStrategy)
        assert setup._gate is not None
        assert setup._gate.timeframe == dt.timedelta(hours=4)


def test_an_unknown_higher_timeframe_is_refused_with_the_alternatives() -> None:
    with pytest.raises(EngineError, match=r"setup htf must be one of .*'H4'.*got 'H3'"):
        build_setup(
            {"type": "structure_choch", "params": {"htf": "H3"}}, timeframe=dt.timedelta(minutes=15)
        )


def test_a_null_higher_timeframe_is_the_filter_off() -> None:
    setup = build_setup({"type": "structure_choch", "params": {"htf": None}})
    assert isinstance(setup, StructureStrategy)
    assert setup._gate is None


def test_a_higher_timeframe_without_the_document_s_own_is_refused_by_the_class() -> None:
    """`build_setup` alone cannot know the base bar; the class says so rather than guessing."""
    with pytest.raises(ValueError, match="own timeframe"):
        _built("structure_choch", htf="H4")


def test_a_higher_timeframe_that_is_not_higher_is_refused_by_the_class() -> None:
    with pytest.raises(ValueError, match="coarser"):
        build_setup(
            {"type": "structure_choch", "params": {"htf": "M15", "htf_offset": 0}},
            timeframe=dt.timedelta(hours=1),
        )


def test_the_broker_s_clock_reaches_the_engine_as_a_duration_half_hours_included() -> None:
    """The document says hours ahead of UTC; the engine reasons in durations. Half hours are real
    timezones, so the probe is one — and it is not zero, or the assertion would hold for a factory
    that dropped the field and let the aggregator's own arithmetic answer."""
    setup = build_setup(
        {"type": "structure_choch", "params": {"htf": "H4", "htf_offset": -5.5}},
        timeframe=dt.timedelta(minutes=15),
    )
    assert isinstance(setup, StructureStrategy)
    assert setup._gate is not None
    assert setup._gate.offset == dt.timedelta(hours=-5, minutes=-30)


def test_a_clock_that_is_not_a_number_of_hours_is_refused() -> None:
    with pytest.raises(EngineError, match="setup htf_offset must be hours ahead of UTC"):
        build_setup(
            {"type": "structure_choch", "params": {"htf": "H4", "htf_offset": True}},
            timeframe=dt.timedelta(minutes=15),
        )


# --------------------------------------------------------------------------- #
# The long-average direction filter (2026-09-09)                                #
# --------------------------------------------------------------------------- #


def test_the_long_average_period_reaches_both_swing_setups() -> None:
    """Both hosts take the filter — his answer 5 — and `_optional_int` is what routes it. The
    probe is a period that is not either setup's own, so a factory handing the setup's `period`
    through by mistake would fail here rather than agree by coincidence."""
    for kind, cls in (
        ("mme9_breakout", Mme9BreakoutStrategy),
        ("ponto_continuo", PontoContinuoStrategy),
    ):
        setup = _built(kind, side="long", long_average_period=200)
        assert isinstance(setup, cls)
        assert setup._long is not None
        assert setup._long.label == "long EMA 200"


def test_a_null_long_average_period_is_the_filter_off() -> None:
    """Present-and-null is the setting *off*, and it must not be read as absent — which happens
    to mean the same thing today, and would stop meaning it the day a default is given."""
    setup = _built("mme9_breakout", side="long", long_average_period=None)
    assert isinstance(setup, Mme9BreakoutStrategy)
    assert setup._long is None


def test_a_long_average_period_that_is_not_an_integer_is_refused() -> None:
    with pytest.raises(EngineError, match="setup long_average_period must be an integer"):
        _built("ponto_continuo", side="long", long_average_period="200")


def test_a_long_average_period_the_class_rejects_still_raises() -> None:
    with pytest.raises(ValueError, match="long average period must be >= 1"):
        _built("mme9_breakout", side="long", long_average_period=0)
