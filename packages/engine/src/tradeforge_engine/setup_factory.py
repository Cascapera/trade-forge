"""Building the strategies the DSL *names* rather than describes.

`compile_strategy` turns a tree of conditions into a `CompiledStrategy`. It cannot turn a setup
into anything, because a setup is not a tree: it is a state machine that remembers which order is
resting, whether this turn already gave its trade, and how many breaks of structure the open
position has seen. A condition sees one closed candle and answers yes or no. No amount of
`all`/`any` nesting reaches the difference (ADR-0019).

So a setup document names one of these classes and hands it parameters, and this module is the
lookup that turns that name into the object. It is the same seam — and the same loud-failure
doctrine — as `build_indicator` and the cost-model builder: a name this engine does not know
raises, rather than quietly running something else.

**Nothing here has a default.** A parameter the document omits is simply not passed, so the engine
class's own default applies. That is what keeps the number in one place: the schema package
declares the same defaults so the builder and the generated TypeScript can show them, and a drift
test constructs each class to prove the two agree. A default restated here would be a third copy,
and the one that silently disagrees.
"""

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from tradeforge_engine.domain import Side
from tradeforge_engine.errors import EngineError
from tradeforge_engine.protocols import Strategy
from tradeforge_engine.setups import (
    ChochQualifier,
    ContinuationQualifier,
    StructureStrategy,
    ZoneEntryPoint,
)
from tradeforge_engine.swing import Mme9BreakoutStrategy, PontoContinuoStrategy

_SIDES: Mapping[str, Side] = {"long": Side.LONG, "short": Side.SHORT}


def _params(node: Mapping[str, object]) -> Mapping[str, object]:
    raw = node.get("params", {})
    if not isinstance(raw, Mapping):
        raise EngineError(f"setup params must be a mapping, got {raw!r}")
    return raw


def _side(params: Mapping[str, object]) -> Side:
    raw = params.get("side")
    side = _SIDES.get(raw) if isinstance(raw, str) else None
    if side is None:
        raise EngineError(f"setup side must be 'long' or 'short', got {raw!r}")
    return side


def _int(params: Mapping[str, object], key: str, into: dict[str, Any]) -> None:
    """Copy an integer parameter across, if the document carried one.

    `bool` is rejected explicitly: it is an `int` in Python, so `period: true` would otherwise
    become a period of 1 and run — a nonsense document producing a plausible backtest.
    """
    if key not in params:
        return
    value = params[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise EngineError(f"setup {key} must be an integer, got {value!r}")
    into[key] = value


def _flag(params: Mapping[str, object], key: str, into: dict[str, Any]) -> None:
    if key not in params:
        return
    value = params[key]
    if not isinstance(value, bool):
        raise EngineError(f"setup {key} must be true or false, got {value!r}")
    into[key] = value


def _decimal(params: Mapping[str, object], key: str, into: dict[str, Any]) -> None:
    """Copy a numeric parameter across as `Decimal`, going through `str`.

    DSL numbers arrive from JSONB as float, so `0.1` read directly would be the binary dust
    `0.1000000000000000055…` and every stop computed from it would sit a hair off the price the
    document asked for. The same `str` route the runner uses for the risk percent.
    """
    if key not in params:
        return
    value = params[key]
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise EngineError(f"setup {key} must be a number, got {value!r}")
    into[key] = Decimal(str(value))


def _optional_decimal(params: Mapping[str, object], key: str, into: dict[str, Any]) -> None:
    """Same, but an explicit `null` means *switch the rule off* and is not the same as omitting it.

    `breakeven_at_r: null` is a real setting — "what would this setup earn without taking winners
    to breakeven" has to be askable — and it is the one case where present-and-null must reach the
    class while absent must not.
    """
    if key in params and params[key] is None:
        into[key] = None
        return
    _decimal(params, key, into)


def _optional_int(params: Mapping[str, object], key: str, into: dict[str, Any]) -> None:
    """`max_bos: null` means uncapped, which is not the same as omitting the key."""
    if key in params and params[key] is None:
        into[key] = None
        return
    _int(params, key, into)


def _mme9(params: Mapping[str, object]) -> Strategy:
    kwargs: dict[str, Any] = {"side": _side(params)}
    _int(params, "period", kwargs)
    _int(params, "stop_buffer_ticks", kwargs)
    _optional_decimal(params, "breakeven_at_r", kwargs)
    return Mme9BreakoutStrategy(**kwargs)


def _ponto_continuo(params: Mapping[str, object]) -> Strategy:
    kwargs: dict[str, Any] = {"side": _side(params)}
    _int(params, "period", kwargs)
    _int(params, "stop_buffer_ticks", kwargs)
    _optional_decimal(params, "breakeven_at_r", kwargs)
    if "average" in params:
        average = params["average"]
        if average not in ("EMA", "SMA"):
            raise EngineError(f"setup average must be 'EMA' or 'SMA', got {average!r}")
        kwargs["average"] = average
    return PontoContinuoStrategy(**kwargs)


def _structure_kwargs(params: Mapping[str, object]) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    _flag(params, "allow_secondary", kwargs)
    _decimal(params, "stop_buffer", kwargs)
    _optional_decimal(params, "breakeven_at_r", kwargs)
    if "entry_point" in params:
        # Raised rather than defaulted, like the average above. A document naming an entry point
        # this engine does not have is asking for a method it will not get, and falling back to
        # the edge would run it silently at the widest stop of the two.
        raw = params["entry_point"]
        if not isinstance(raw, str) or raw not in {point.value for point in ZoneEntryPoint}:
            allowed = ", ".join(repr(point.value) for point in ZoneEntryPoint)
            raise EngineError(f"setup entry_point must be one of {allowed}, got {raw!r}")
        kwargs["entry_point"] = ZoneEntryPoint(raw)
    return kwargs


def _structure_choch(params: Mapping[str, object]) -> Strategy:
    return StructureStrategy(qualifier=ChochQualifier(), name="choch", **_structure_kwargs(params))


def _structure_continuation(params: Mapping[str, object]) -> Strategy:
    continuation: dict[str, Any] = {}
    _optional_int(params, "max_bos", continuation)
    return StructureStrategy(
        qualifier=ContinuationQualifier(**continuation),
        name="continuation",
        **_structure_kwargs(params),
    )


_BUILDERS = {
    "mme9_breakout": _mme9,
    "ponto_continuo": _ponto_continuo,
    "structure_choch": _structure_choch,
    "structure_continuation": _structure_continuation,
}


def build_setup(node: Mapping[str, object]) -> Strategy:
    """Build the named setup, or raise. `node` is the document's `setup` block.

    The name reaching here has already been through the schema's discriminated union, so an
    unknown one means the two lists have drifted — which is precisely why this raises with both
    the name and the alternatives rather than returning `None` and letting the run proceed
    strategy-less.
    """
    kind = node.get("type")
    build = _BUILDERS.get(kind) if isinstance(kind, str) else None
    if build is None:
        raise EngineError(f"unknown setup type {kind!r}; this engine builds {sorted(_BUILDERS)}")
    return build(_params(node))


__all__ = ["build_setup"]
