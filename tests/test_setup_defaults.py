"""The setup parameters, asserted to survive the trip from the document to the engine class.

A setup parameter's default exists twice on purpose. The engine class owns it, because that is
where the rule runs and where a `PontoContinuoStrategy()` built by hand must already be the
author's method. The Pydantic model repeats it, because a default that lives only in Python is
invisible to the builder, to the generated TypeScript, and (phase 3) to the LLM writing documents
— a form that cannot show "20" has to make the user guess it.

`setup_factory` deliberately holds no third copy: a parameter the document omits is not passed, so
the class default applies. That leaves exactly two numbers to keep in agreement, and this file is
what keeps them. Change the period of the Ponto Contínuo in one place and this goes red with both
values in the message.

Agreeing on the default is only half of it, and the quieter half is the other one: a parameter the
factory never *routes* is a number the user typed into the builder and the engine never saw. It
raises nothing, because the class default is a perfectly good number — the run simply answers a
different question than the one asked. So every field is also driven through the factory carrying a
value that is **not** its default, which is the only way to tell "the parameter arrived" apart from
"the default happened to match". Fixtures written with default values cannot make that distinction,
and that is exactly how the gap got here.

Both sweeps derive their field list from `model_fields` rather than repeating it. A parameter added
to the DSL and forgotten in the factory has to fail here, and a hand-written list is precisely the
thing that would let it through.

This is the only test in the repo that imports the engine and the schema package together, and it
is allowed to: it is *about* the boundary between them. Neither package imports the other, which
is the invariant `tests/test_architecture.py` enforces — and precisely why nothing at the type
level would otherwise notice the drift.
"""

import inspect
from decimal import Decimal
from typing import Any, get_args

import pytest

from tradeforge_engine import setup_factory
from tradeforge_engine.domain import Side
from tradeforge_engine.setup_factory import _BUILDERS, build_setup
from tradeforge_engine.setups import (
    ChochQualifier,
    ContinuationQualifier,
    StructureStrategy,
    ZoneEntryPoint,
)
from tradeforge_engine.swing import Mme9BreakoutStrategy, PontoContinuoStrategy
from tradeforge_schema.models import (
    ContinuationParams,
    Mme9BreakoutParams,
    PontoContinuoParams,
    Setup,
    StructureParams,
)


def _owned_by(model: type[Any], target: Any, **elsewhere: Any) -> dict[str, Any]:
    """Every field of `model`, mapped to the engine callable whose signature owns its default.

    Derived, not listed: a field added to the model joins this map on its own and is compared from
    then on. If it belongs to some other constructor — the way `max_bos` belongs to the qualifier
    rather than to the strategy — the comparison fails naming a parameter the target does not have,
    which is the correct failure: the factory has to route it somewhere, and someone has to say
    where.
    """
    return {name: elsewhere.get(name, target) for name in model.model_fields}


# The DSL's `type`, the schema model that describes its params, and who owns each default.
_SETUPS: dict[str, tuple[type[Any], dict[str, Any]]] = {
    "mme9_breakout": (
        Mme9BreakoutParams,
        _owned_by(Mme9BreakoutParams, Mme9BreakoutStrategy),
    ),
    "ponto_continuo": (
        PontoContinuoParams,
        _owned_by(PontoContinuoParams, PontoContinuoStrategy),
    ),
    "structure_choch": (
        StructureParams,
        _owned_by(StructureParams, StructureStrategy),
    ),
    "structure_continuation": (
        ContinuationParams,
        _owned_by(ContinuationParams, StructureStrategy, max_bos=ContinuationQualifier),
    ),
}

# Per setup, per field: what the document carries, and what must reach the constructor. Every
# value here is deliberately *not* the field's default — `test_every_probe_is_a_non_default_value`
# holds that line, because a probe that drifted back onto its default would silently stop proving
# anything while still passing.
#
# The fractional ones are also deliberately not representable in binary. `Decimal("0.25") == 0.25`
# is `True`, so a probe of 0.25 would compare equal whether the factory built a `Decimal` or handed
# the raw float through — the sweep would look like it covered the `str` route and cover nothing.
# 0.3 is the dust-carrying kind, and it is what makes the comparison mean something.
_PROBES: dict[str, dict[str, tuple[Any, Any]]] = {
    "mme9_breakout": {
        "side": ("short", Side.SHORT),
        "period": (21, 21),
        "stop_buffer_ticks": (3, 3),
        "breakeven_at_r": (3.3, Decimal("3.3")),
    },
    "ponto_continuo": {
        "side": ("short", Side.SHORT),
        "period": (30, 30),
        "average": ("SMA", "SMA"),
        "stop_buffer_ticks": (4, 4),
        "breakeven_at_r": (1.1, Decimal("1.1")),
    },
    "structure_choch": {
        "allow_secondary": (True, True),
        "stop_buffer": (0.3, Decimal("0.3")),
        "breakeven_at_r": (4.7, Decimal("4.7")),
        # The document carries the wire string; the engine must receive the enum. `StrEnum` makes
        # the two compare equal, which is exactly why the probe is `midpoint` and not `edge`: on
        # the default the assertion would hold whether the factory routed the field or dropped it.
        "entry_point": ("midpoint", ZoneEntryPoint.MIDPOINT),
    },
    "structure_continuation": {
        "allow_secondary": (True, True),
        "stop_buffer": (0.3, Decimal("0.3")),
        "breakeven_at_r": (4.7, Decimal("4.7")),
        "max_bos": (7, 7),
        "entry_point": ("midpoint", ZoneEntryPoint.MIDPOINT),
    },
}

# The classes the factory reaches for. Swapping these for a recorder is what lets the routing tests
# read the arguments the factory *passed*, rather than infer them from private attributes of four
# different objects.
_FACTORY_CLASSES = (
    "Mme9BreakoutStrategy",
    "PontoContinuoStrategy",
    "StructureStrategy",
    "ChochQualifier",
    "ContinuationQualifier",
)

_IDS = list(_SETUPS)


def _schema_default(model: type[Any], field: str) -> Any:
    return model.model_fields[field].get_default(call_default_factory=True)


def _engine_default(target: Any, field: str) -> Any:
    parameters = inspect.signature(target).parameters
    assert field in parameters, (
        f"{target.__name__} has no {field!r} parameter — the DSL declares it, so the factory has "
        f"to route it to whichever constructor does"
    )
    return parameters[field].default


def _accepts_null(model: type[Any], field: str) -> bool:
    return type(None) in get_args(model.model_fields[field].annotation)


def _document(kind: str, **params: Any) -> dict[str, Any]:
    return {"type": kind, "params": params}


def _same(schema_value: Any, engine_value: Any) -> bool:
    """Are these the same default written in the two vocabularies?

    The DSL carries JSON numbers and the engine carries `Decimal`, so `2.0` and `Decimal("2")`
    are one default spelled twice and must compare equal. Everything else — `None`, `True`,
    `"EMA"` — compares as itself. `bool` is checked before the numeric branch because it is an
    `int` in Python, and `False == 0` would let a flag agree with a zero.
    """
    if isinstance(schema_value, bool) or isinstance(engine_value, bool):
        return schema_value is engine_value
    if isinstance(schema_value, int | float | Decimal) and isinstance(
        engine_value, int | float | Decimal
    ):
        return Decimal(str(schema_value)) == Decimal(str(engine_value))
    return bool(schema_value == engine_value)


def _record_construction(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Point every class the factory builds at a recorder, and return the arguments it receives.

    One dict across all of them on purpose: `max_bos` reaches `ContinuationQualifier` while the
    rest reach `StructureStrategy`, and what is under test is that the parameter arrived at all —
    routing it one level deeper is the factory's business, and `test_setup_factory.py` is where
    that is pinned against the real classes.
    """
    seen: dict[str, Any] = {}

    class _Recorder:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

    for name in _FACTORY_CLASSES:
        monkeypatch.setattr(setup_factory, name, _Recorder)
    return seen


# --------------------------------------------------------------------------- #
# The two copies of each default must be the same number                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kind", _IDS)
def test_the_schema_and_the_engine_agree_on_every_default(kind: str) -> None:
    model, owners = _SETUPS[kind]
    for field, target in owners.items():
        if model.model_fields[field].is_required():
            continue  # nothing to compare — see the test below
        schema_value = _schema_default(model, field)
        engine_value = _engine_default(target, field)
        assert _same(schema_value, engine_value), (
            f"{kind}.{field}: schema says {schema_value!r}, engine says {engine_value!r}"
        )


@pytest.mark.parametrize("model", [Mme9BreakoutParams, PontoContinuoParams])
def test_a_directional_setup_must_be_told_its_side(model: type[Any]) -> None:
    """`side` is the one parameter with no schema default, and that is deliberate rather than an
    omission — which is why the sweep above skips it instead of comparing.

    The engine classes *do* default it to `Side.LONG`, because a hand-built strategy in a test has
    to come from somewhere. If the schema ever gave `side` a default too, a document that forgot it
    would quietly become a long-only run — a whole backtest answering for one direction while the
    author reads it as the setup's result. The schema demanding it is what makes that unreachable.
    """
    assert model.model_fields["side"].is_required()


# --------------------------------------------------------------------------- #
# A parameter the factory never routes is a knob wired to nothing               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kind", _IDS)
def test_every_parameter_the_schema_declares_is_probed(kind: str) -> None:
    """The guard on the guard. The sweep below can only prove what it carries, so the set of
    probes is checked against the model itself: add a field to the DSL and this is what tells you
    the routing test has a hole, instead of the routing test quietly not covering it."""
    model, _ = _SETUPS[kind]
    assert set(_PROBES[kind]) == set(model.model_fields)


@pytest.mark.parametrize("kind", _IDS)
def test_every_probe_is_a_non_default_value(kind: str) -> None:
    """A probe equal to its default proves nothing: the assertion passes whether the factory routed
    the parameter or dropped it and let the class default answer. That is not hypothetical — it is
    how the fixtures came to cover `period` and `breakeven_at_r` without covering them."""
    model, _ = _SETUPS[kind]
    for field, (carried, _) in _PROBES[kind].items():
        if model.model_fields[field].is_required():
            continue  # no default to differ from
        default = _schema_default(model, field)
        assert not _same(default, carried), (
            f"{kind}.{field}: the probe {carried!r} is the default, so it cannot tell a routed "
            f"parameter from a dropped one"
        )


@pytest.mark.parametrize("kind", _IDS)
def test_every_parameter_reaches_the_constructor(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole document, every field carrying a non-default value, through the real factory.

    A dropped parameter is the failure with no symptom: `{"period": 21}` on the MME9 that never
    arrives runs the MME9 anyway, takes its entries on different bars, measures the stop from a
    different reference candle, and reports a result that is simply another strategy's.
    """
    model, _ = _SETUPS[kind]
    seen = _record_construction(monkeypatch)
    build_setup(_document(kind, **{field: doc for field, (doc, _) in _PROBES[kind].items()}))
    for field, (_, expected) in _PROBES[kind].items():
        assert field in seen, f"{kind}: the factory never passed {field!r} to any constructor"
        assert seen[field] == expected, (
            f"{kind}.{field}: the document said {expected!r}, the constructor got {seen[field]!r}"
        )
        # The vocabulary, not just the value: a JSON number that arrived as a `float` would go on
        # to build stop prices the engine cannot compare exactly, and `Decimal("2") == 2.0` would
        # never say so.
        assert type(seen[field]) is type(expected), (
            f"{kind}.{field}: expected a {type(expected).__name__}, the constructor got a "
            f"{type(seen[field]).__name__}"
        )
    assert set(model.model_fields) <= set(seen)


@pytest.mark.parametrize("kind", _IDS)
def test_an_explicit_null_reaches_the_constructor_as_none(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Present-and-null is a setting, and it is not the same as absent.

    `breakeven_at_r: null` asks "what would this setup earn *without* taking winners to breakeven",
    and `max_bos: null` asks for uncapped. Read as absent, both default back on: a 5R winner that
    pulled back through 1R is booked as a scratch, and the run answers the opposite question while
    looking entirely reasonable. Every nullable field is swept, so a helper that goes back to
    `_decimal` on any one of them fails here.
    """
    model, _ = _SETUPS[kind]
    nullable = [name for name in model.model_fields if _accepts_null(model, name)]
    assert nullable, f"{kind}: no nullable parameter — drop this case or the sweep is dead weight"
    for field in nullable:
        seen = _record_construction(monkeypatch)
        # `side` is required, so a directional setup still has to carry one.
        params: dict[str, Any] = {field: None}
        if model.model_fields.get("side") is not None:
            params["side"] = "long"
        build_setup(_document(kind, **params))
        assert seen.get(field, "absent") is None, (
            f"{kind}.{field}: null must switch the rule off, not be read as omitted"
        )


# --------------------------------------------------------------------------- #
# The two lists of setups                                                       #
# --------------------------------------------------------------------------- #


def test_every_setup_the_schema_names_can_be_built() -> None:
    """The two lists — the schema's discriminated union and the factory's table — must name the
    same setups. A type in the schema with no builder is a document the API accepts and the worker
    then dies on, forty minutes into a run."""
    schema_types = {
        member.model_fields["type"].annotation.__args__[0]
        for member in get_args(get_args(Setup.__value__)[0])
    }
    assert schema_types == set(_BUILDERS) == set(_SETUPS)


def test_the_choch_qualifier_takes_no_parameters() -> None:
    """`structure_choch` has no knobs of its own beyond the shared structure ones, so the schema
    reusing `StructureParams` unchanged is correct rather than an oversight. If the qualifier ever
    grows one, this fails and the schema has to grow a params model of its own."""
    assert list(inspect.signature(ChochQualifier).parameters) == []
