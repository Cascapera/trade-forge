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

import datetime as dt
import inspect
from collections.abc import Callable, Mapping
from decimal import Decimal
from enum import StrEnum
from typing import Any

from tradeforge_engine.average_setups import AVERAGE_DIALS, AVERAGE_DIALS_READ, AverageEntryPoint
from tradeforge_engine.bar_setups import GiftStop
from tradeforge_engine.domain import TIMEFRAME_DELTAS, Side
from tradeforge_engine.errors import EngineError
from tradeforge_engine.higher_timeframe import RegionChoice
from tradeforge_engine.protocols import Strategy
from tradeforge_engine.reading import MarketReading
from tradeforge_engine.setups import (
    DIALS_READ,
    ENTRY_DIALS,
    ChochQualifier,
    ContinuationQualifier,
    StructureStrategy,
    ZoneEntryPoint,
)
from tradeforge_engine.swing import (
    BothSides,
    Mme9BreakoutStrategy,
    Mme9FailedTurnStrategy,
    Mme9PullbackStrategy,
    Mme9TurnStrategy,
    PontoContinuoStrategy,
)

_SIDES: Mapping[str, Side] = {"long": Side.LONG, "short": Side.SHORT}

# The setups written for one side, which `both` composes out of two instances (`_one_or_both`).
type _Swing = (
    Mme9BreakoutStrategy
    | Mme9FailedTurnStrategy
    | Mme9PullbackStrategy
    | Mme9TurnStrategy
    | PontoContinuoStrategy
)


def _params(node: Mapping[str, object]) -> Mapping[str, object]:
    raw = node.get("params", {})
    if not isinstance(raw, Mapping):
        raise EngineError(f"setup params must be a mapping, got {raw!r}")
    return raw


def _side(params: Mapping[str, object]) -> Side:
    raw = params.get("side")
    side = _SIDES.get(raw) if isinstance(raw, str) else None
    if side is None:
        raise EngineError(f"setup side must be 'long', 'short' or 'both', got {raw!r}")
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
    """Present-and-null is a setting; absent is not, and the two must not be confused.

    What the `null` *means* is each parameter's own business: `max_bos: null` is uncapped, and
    `long_average_period: null` is his direction filter switched off. What they share is only
    that reading either as absent would put the class default back — and the day one of those
    defaults stops being the same thing as "off", that would be a silent change of question.
    """
    if key in params and params[key] is None:
        into[key] = None
        return
    _int(params, key, into)


def _choice(
    params: Mapping[str, object], key: str, choices: type[StrEnum], into: dict[str, Any]
) -> None:
    """Copy an enumerated parameter across as its enum, or refuse it with the alternatives.

    Raised rather than defaulted, the same doctrine as the average kind: a document naming a value
    this engine does not have is asking for a method it will not get, and falling back to the
    default would run it silently as something else.
    """
    if key not in params:
        return
    raw = params[key]
    if not isinstance(raw, str) or raw not in {choice.value for choice in choices}:
        allowed = ", ".join(repr(choice.value) for choice in choices)
        raise EngineError(f"setup {key} must be one of {allowed}, got {raw!r}")
    into[key] = choices(raw)


def _optional_timeframe(params: Mapping[str, object], key: str, into: dict[str, Any]) -> None:
    """Copy a timeframe name across as its duration; an explicit `null` switches the rule off.

    The document names a bar (`"H4"`), the engine reasons in durations, and `TIMEFRAME_DELTAS`
    is the one table both read — so a name this engine does not know is refused with the
    alternatives, like an unknown entry point, rather than run as no filter at all.
    """
    if key in params and params[key] is None:
        into[key] = None
        return
    if key not in params:
        return
    raw = params[key]
    delta = TIMEFRAME_DELTAS.get(raw) if isinstance(raw, str) else None
    if delta is None:
        raise EngineError(f"setup {key} must be one of {sorted(TIMEFRAME_DELTAS)}, got {raw!r}")
    into[key] = delta


def _no_stated_clock(params: Mapping[str, object]) -> None:
    """Refuse a document that states the broker's clock itself.

    ⚠️ **The clock is the instrument's since 30/09** (his rule: *"htf offset não vamos usar em
    nenhum time frame, vamos adotar o horário do servidor mt5 como real"*). It reaches the setup
    from the run (`InstrumentSpec.server_offset`), the same number the collector stored the
    candles under. Every saved document carries `htf_offset: null`, which is read as the silence
    it is; a number is refused rather than obeyed or ignored — obeyed, two runs of one market
    would cut its higher bars in two places again; ignored, the document would claim a cut it
    did not get.
    """
    if params.get("htf_offset") is not None:
        raise EngineError(
            f"setup htf_offset is no longer read, got {params['htf_offset']!r}; the higher bars "
            f"are cut on the broker's clock, which is the instrument's (server_offset)"
        )


def _one_or_both(
    build: Callable[..., _Swing], params: Mapping[str, object], kwargs: dict[str, Any]
) -> Strategy:
    """One side of a swing setup, or both of them behind `BothSides` (his request of 18/09).

    ⚠️ **Each half gets its own name**, the class's own default with the side after it
    (`mme9-long`, `mme9-short`). Order names are `name-time-count`, and two halves sharing a name
    could mint the same one on the same bar — the broker would answer the second with silence
    (`BothSides`, point 3). The base is read off the class's signature rather than written here,
    for the rule in this module's docstring: a copy of a default kept beside the class is the one
    that silently disagrees. A document naming one side keeps the class's name untouched, so every
    run saved before 18/09 still names its orders exactly as it did.
    """
    if params.get("side") != "both":
        return build(side=_side(params), **kwargs)
    base = inspect.signature(build).parameters["name"].default
    return BothSides(
        long=build(side=Side.LONG, name=f"{base}-long", **kwargs),
        short=build(side=Side.SHORT, name=f"{base}-short", **kwargs),
    )


def _mme9(params: Mapping[str, object], _timeframe: dt.timedelta | None) -> Strategy:
    kwargs: dict[str, Any] = {}
    _int(params, "period", kwargs)
    _int(params, "stop_buffer_ticks", kwargs)
    _optional_decimal(params, "breakeven_at_r", kwargs)
    _choice(params, "entry_point", AverageEntryPoint, kwargs)
    _choice(params, "gift_stop", GiftStop, kwargs)
    _flag(params, "volume_filter", kwargs)
    _optional_int(params, "long_average_period", kwargs)
    return _one_or_both(Mme9BreakoutStrategy, params, kwargs)


def _mme9_turn(params: Mapping[str, object], _timeframe: dt.timedelta | None) -> Strategy:
    kwargs: dict[str, Any] = {}
    _int(params, "period", kwargs)
    _int(params, "stop_buffer_ticks", kwargs)
    _optional_decimal(params, "breakeven_at_r", kwargs)
    _optional_int(params, "long_average_period", kwargs)
    return _one_or_both(Mme9TurnStrategy, params, kwargs)


def _mme9_failed_turn(params: Mapping[str, object], _timeframe: dt.timedelta | None) -> Strategy:
    kwargs: dict[str, Any] = {}
    _int(params, "period", kwargs)
    _int(params, "stop_buffer_ticks", kwargs)
    _optional_decimal(params, "breakeven_at_r", kwargs)
    _optional_int(params, "long_average_period", kwargs)
    return _one_or_both(Mme9FailedTurnStrategy, params, kwargs)


def _mme9_pullback(params: Mapping[str, object], _timeframe: dt.timedelta | None) -> Strategy:
    kwargs: dict[str, Any] = {}
    _int(params, "corrections", kwargs)
    _int(params, "period", kwargs)
    _int(params, "stop_buffer_ticks", kwargs)
    _optional_decimal(params, "breakeven_at_r", kwargs)
    _optional_int(params, "long_average_period", kwargs)
    return _one_or_both(Mme9PullbackStrategy, params, kwargs)


def _ponto_continuo(params: Mapping[str, object], _timeframe: dt.timedelta | None) -> Strategy:
    kwargs: dict[str, Any] = {}
    _int(params, "period", kwargs)
    _int(params, "stop_buffer_ticks", kwargs)
    _optional_decimal(params, "breakeven_at_r", kwargs)
    _choice(params, "entry_point", AverageEntryPoint, kwargs)
    _choice(params, "gift_stop", GiftStop, kwargs)
    _flag(params, "volume_filter", kwargs)
    _optional_int(params, "long_average_period", kwargs)
    if "average" in params:
        average = params["average"]
        if average not in ("EMA", "SMA"):
            raise EngineError(f"setup average must be 'EMA' or 'SMA', got {average!r}")
        kwargs["average"] = average
    return _one_or_both(PontoContinuoStrategy, params, kwargs)


def _structure_kwargs(
    params: Mapping[str, object], timeframe: dt.timedelta | None, server_offset: dt.timedelta
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    _flag(params, "allow_secondary", kwargs)
    _decimal(params, "stop_buffer", kwargs)
    _optional_decimal(params, "breakeven_at_r", kwargs)
    # Raised rather than defaulted, like the average above. A document naming an entry point this
    # engine does not have is asking for a method it will not get, and falling back to the edge
    # would run it silently at the widest stop of the two.
    _choice(params, "entry_point", ZoneEntryPoint, kwargs)
    _choice(params, "gift_stop", GiftStop, kwargs)
    _flag(params, "volume_filter", kwargs)
    # His minimum age of a region before it may be traded (29/09), counted in bars.
    _int(params, "min_bars_to_touch", kwargs)
    # The timeframe above, and this setup's own for it to build on. The base is passed only
    # when the document set a filter: the class accepts it unused, but a keyword the document
    # never asked for is a third place a default could hide (see the module docstring).
    _optional_timeframe(params, "htf", kwargs)
    _no_stated_clock(params)
    if kwargs.get("htf") is not None:
        kwargs["timeframe"] = timeframe
        # The bars above close on the broker's clock, which is the instrument's (30/09) — passed
        # only with a filter, the rule the base timeframe above follows.
        kwargs["htf_offset"] = server_offset
        # Which regions above may release (29/09) — read only with a filter, and passed only when
        # they narrow the class's defaults (any region, secondaries included), the rule `side`
        # follows below: a keyword the document never asked for is a place a default could hide.
        _choice(params, "htf_regions", RegionChoice, kwargs)
        if kwargs.get("htf_regions") is RegionChoice.ANY:
            del kwargs["htf_regions"]
        if params.get("htf_allow_secondary", True) is not True:
            _flag(params, "htf_allow_secondary", kwargs)
    # ⚠️ `both` is the class's own default (`None`), so it is passed only when narrowed — the same
    # rule as the timeframe above: a keyword the document never asked for is a place a default
    # could hide. Anything but the three names is refused, never read as both.
    traded = params.get("side", "both")
    if traded != "both":
        kwargs["side"] = _side(params)
    return kwargs


def _structure_choch(
    params: Mapping[str, object],
    timeframe: dt.timedelta | None,
    server_offset: dt.timedelta,
    reading: MarketReading | None = None,
) -> Strategy:
    return StructureStrategy(
        qualifier=ChochQualifier(),
        name="choch",
        reading=reading,
        **_structure_kwargs(params, timeframe, server_offset),
    )


def _structure_continuation(
    params: Mapping[str, object],
    timeframe: dt.timedelta | None,
    server_offset: dt.timedelta,
    reading: MarketReading | None = None,
) -> Strategy:
    continuation: dict[str, Any] = {}
    _optional_int(params, "max_bos", continuation)
    return StructureStrategy(
        qualifier=ContinuationQualifier(**continuation),
        name="continuation",
        reading=reading,
        **_structure_kwargs(params, timeframe, server_offset),
    )


_ReadingBuilder = Callable[
    [Mapping[str, object], dt.timedelta | None, dt.timedelta, MarketReading | None], Strategy
]

_READING_BUILDERS: dict[str, _ReadingBuilder] = {
    "structure_choch": _structure_choch,
    "structure_continuation": _structure_continuation,
}
"""The setups that read a `MarketReading` and so can share one (ADR-0029)."""


_BUILDERS: dict[str, Callable[[Mapping[str, object], dt.timedelta | None], Strategy]] = {
    "mme9_breakout": _mme9,
    "mme9_failed_turn": _mme9_failed_turn,
    "mme9_pullback": _mme9_pullback,
    "mme9_turn": _mme9_turn,
    "ponto_continuo": _ponto_continuo,
}
"""The setups that read no market of their own, and so neither a higher timeframe nor a clock."""


def build_setup(
    node: Mapping[str, object],
    *,
    timeframe: dt.timedelta | None = None,
    server_offset: dt.timedelta = dt.timedelta(0),
    reading: MarketReading | None = None,
) -> Strategy:
    """Build the named setup, or raise. `node` is the document's `setup` block.

    The name reaching here has already been through the schema's discriminated union, so an
    unknown one means the two lists have drifted — which is precisely why this raises with both
    the name and the alternatives rather than returning `None` and letting the run proceed
    strategy-less.

    `timeframe` is the document's own bar, which `compile_strategy` always has and hands over.
    Only a setup reading a *higher* timeframe needs it — its bars are assembled from this one —
    and the class refuses such a filter without it, so a caller that builds a filtered setup by
    hand cannot get one that silently reads nothing.

    `server_offset` is the broker's clock, the instrument's (`InstrumentSpec.server_offset`, 30/09):
    where a setup reading a higher timeframe closes those bars. Zero is a broker on UTC.

    `reading` is a batch's shared market (ADR-0029), `shared_reading`'s answer for this block —
    refused for any setup that does not read one, rather than built without it and left to read
    a market of its own that the batch cannot see.
    """
    kind = node.get("type")
    reads = _READING_BUILDERS.get(kind) if isinstance(kind, str) else None
    if reads is not None:
        return reads(_params(node), timeframe, server_offset, reading)
    if reading is not None:
        raise EngineError(f"a {kind!r} setup does not read a shared market")
    build = _BUILDERS.get(kind) if isinstance(kind, str) else None
    if build is None:
        known = sorted({*_BUILDERS, *_READING_BUILDERS})
        raise EngineError(f"unknown setup type {kind!r}; this engine builds {known}")
    return build(_params(node), timeframe)


def shared_reading(
    node: Mapping[str, object],
    *,
    timeframe: dt.timedelta,
    server_offset: dt.timedelta = dt.timedelta(0),
) -> tuple[dt.timedelta | None, dt.timedelta] | None:
    """What of the market this setup block reads, as a key — or `None` if it cannot share one.

    Two blocks with the same key, over the same bars, read the same market: the same structure and
    regions on the chart, and the same regions above (the higher timeframe and the broker's clock
    it is cut on, the instrument's since 30/09). That is what a batch groups runs by (ADR-0029),
    and `MarketReading` is built from it. `None` for a setup that builds no reading, and for a
    block too malformed to parse — running it on its own is never wrong, only slower.
    """
    kind = node.get("type")
    if not isinstance(kind, str) or kind not in _READING_BUILDERS:
        return None
    try:
        kwargs = _structure_kwargs(_params(node), timeframe, server_offset)
    except (EngineError, ValueError):
        return None
    return (kwargs.get("htf"), server_offset)


def reading_for(
    key: tuple[dt.timedelta | None, dt.timedelta], *, timeframe: dt.timedelta
) -> MarketReading:
    """The reading a batch whose members share `key` (`shared_reading`) advances once per bar."""
    htf, offset = key
    return MarketReading(timeframe=timeframe, htf=htf, htf_offset=offset)


HTF_CHOICES = frozenset({"htf_regions", "htf_allow_secondary"})
"""The structure setups' choices of which regions above may release (29/09) — read only when the
document names an `htf`, so unread without one (`unread_params`)."""


_ZONE_SETUPS = frozenset({"structure_choch", "structure_continuation"})
"""The setups whose entry point is a `ZoneEntryPoint`, and so whose dials `DIALS_READ` knows."""


_AVERAGE_SETUPS = frozenset({"mme9_breakout", "ponto_continuo"})
"""The setups hosted by an average whose entry point is an `AverageEntryPoint`, and so whose dials
`AVERAGE_DIALS_READ` knows."""


def unread_params(node: Mapping[str, object]) -> frozenset[str]:
    """The parameters of this `setup` block that the setup it builds never reads.

    Two documents that differ only in these run the same, trade for trade — which is what lets a
    sweep run the pair once (`tradeforge_api.sweep`). Known for the structure setups
    (`DIALS_READ`) and the two average setups with entry points (`AVERAGE_DIALS_READ`). Empty for
    every other setup, and for a block too malformed to say: an answer of "nothing is unread" is
    never wrong, only slower.
    """
    kind = node.get("type")
    raw = node.get("params", {})
    if not isinstance(raw, Mapping):
        return frozenset()
    if kind in _ZONE_SETUPS:
        zone = raw.get("entry_point", ZoneEntryPoint.EDGE.value)
        if not isinstance(zone, str) or zone not in {one.value for one in ZoneEntryPoint}:
            return frozenset()
        # Which regions above may release is a question only a filter asks (29/09).
        unfiltered = HTF_CHOICES if raw.get("htf") is None else frozenset()
        return (ENTRY_DIALS - DIALS_READ[ZoneEntryPoint(zone)]) | unfiltered
    if kind in _AVERAGE_SETUPS:
        average = raw.get("entry_point", AverageEntryPoint.CLASSIC.value)
        if not isinstance(average, str) or average not in {one.value for one in AverageEntryPoint}:
            return frozenset()
        return AVERAGE_DIALS - AVERAGE_DIALS_READ[AverageEntryPoint(average)]
    return frozenset()


__all__ = ["build_setup", "reading_for", "shared_reading", "unread_params"]
