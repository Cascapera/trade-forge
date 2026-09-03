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

from decimal import Decimal

import pytest

from tradeforge_engine.domain import Side
from tradeforge_engine.errors import EngineError
from tradeforge_engine.setup_factory import build_setup
from tradeforge_engine.setups import ChochQualifier, ContinuationQualifier, StructureStrategy
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
