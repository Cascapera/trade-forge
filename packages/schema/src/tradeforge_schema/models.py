"""The strategy DSL, v1 — the central contract of the system.

These models are the *source of truth*. The JSON Schema in `strategy.schema.json`
and the TypeScript types in `src/generated/` are generated from them, and CI fails
if either drifts. One definition, three consumers: the engine parses with it, the
frontend types against it, and (phase 3) an LLM generates against it.

Two things this file deliberately cannot do, both of which live in `semantic.py`:
tell whether a `{"ref": "sma_fast"}` points at an indicator that actually exists,
and tell whether a 2:1 take-profit means anything without a stop-loss to measure
risk against. JSON Schema validates *shape*, never *meaning*.
"""

from collections.abc import Mapping
from typing import Annotated, Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"

# An operand names an indicator by id, a field of the forming candle, or a field of
# a closed candle N bars back. The offset starts at 1: `candle[-0]` would be the
# current candle under another name, and allowing two spellings of one thing is how
# a DSL starts to rot.
REF_PATTERN = (
    r"^(?:"
    r"[a-z_][a-z0-9_]*"  # an indicator id
    # One component of a multi-output indicator: `bb.upper`. ⚠️ This alternative also matches
    # `price.clsoe` and `candle.high`, which the two dedicated forms below exist to refuse — and it
    # **cannot** be narrowed here. See the note under this pattern.
    r"|[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*"
    r"|price\.(?:open|high|low|close)"  # the candle being decided on
    r"|candle\[-[1-9][0-9]*\]\.(?:open|high|low|close)"  # a closed candle, N back
    r")$"
)
# ⚠️ **Widening this pattern is how a typo becomes a backtest of nothing, and it happened twice
# while this PR was being written.** The forms it accepts are the only thing standing between a
# misspelled ref and a condition that is false on every bar for ever: the engine resolves an
# unknown name through `indicator_at`, which answers `None` for anything it has no channel for,
# and a comparison against `None` is false. Nothing raises, nothing logs, and the run reports a
# rule that was never evaluated.
#
# The component alternative broke that twice over:
#   1. `bb.uppper` — well-formed, and `semantic.py` used to decide "is this an indicator ref?" by
#      asking whether the string contained a dot, so it skipped every component ref. Fixed by
#      deciding on the NAMESPACE (`price`, `candle`) and checking the component against the
#      indicator's declared type.
#   2. `price.clsoe` — well-formed *because of the same alternative*, since `price` is a perfectly
#      good identifier and `clsoe` a perfectly good component name. `semantic.py` then saw a
#      reserved head and returned "not an indicator ref" without ever looking at the tail.
#
# ⚠️ **The second one cannot be fixed here, and the reason is worth knowing: this pattern is
# compiled by Rust, not by Python.** Pydantic v2 validates `pattern=` with the `regex` crate, which
# has no look-around at all — `(?!(?:price|candle)\.)` fails to compile with "look-around,
# including look-ahead and look-behind, is not supported", and the failure is at import time, so
# the whole package stops loading. Excluding two literal names from an identifier without
# look-around means enumerating character prefixes, which is unreadable and goes stale.
#
# So the shape stays permissive here and `semantic.py` refuses the tail — which is the boundary
# this project already documents: shape is what a schema can say, meaning is not, and a document
# that passed in the browser is never assumed executable (`test_fixtures.py`). The consequence to
# know: `price.clsoe` reaches the API as a **422 from the semantic layer**, not as a schema error,
# and the frontend validator alone will accept it.
#
# The rule for anyone widening this again: any alternative that can reach a namespace with
# enumerated fields must be matched by a check in `semantic.py`, and `test_semantic.py` holds the
# cases that prove it. `expressions.py` refuses the same shape a third time, on the layer that
# executes.

INDICATOR_ID_PATTERN = r"^[a-z_][a-z0-9_]*$"

type Timeframe = Literal["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"]
type PriceSource = Literal["open", "high", "low", "close"]

# A directional setup trades one side; the two-sided version is two of them. The structure
# family has no side at all — which way it trades follows the structure it reads.
type SetupSide = Literal["long", "short"]
type ZoneEntryPoint = Literal["edge", "midpoint", "return_pass", "botinha"]
type AverageKind = Literal["EMA", "SMA"]

# The same list, as a runtime value. The database needs it for a CHECK constraint
# and the engine will need it to parse a bar interval; deriving both from the type
# alias means a new timeframe is one edit above, never three edits in three places
# that drift apart the day someone forgets the third.
TIMEFRAMES: Final[tuple[Timeframe, ...]] = get_args(Timeframe.__value__)


class _Node(BaseModel):
    """Every node forbids unknown keys.

    A typo like `"perod": 9` must be an error, not a silently ignored field that
    leaves the indicator running on its default and the backtest quietly wrong.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------- #
# Operands and conditions — an expression tree, evaluated once per candle       #
# --------------------------------------------------------------------------- #


class Ref(_Node):
    """A reference to a value the engine can resolve at evaluation time."""

    ref: Annotated[
        str,
        Field(
            pattern=REF_PATTERN,
            examples=["sma_fast", "price.close", "candle[-1].high"],
        ),
    ]


class Constant(_Node):
    """A literal number — the fixed side of a threshold like `RSI < 30`. It references nothing,
    so the semantic layer skips it; `inf`/`nan` are rejected because no comparison means them."""

    value: Annotated[float, Field(allow_inf_nan=False, examples=[30, 70, 0.0])]


# An operand is a value the engine reads each bar, or a constant it does not. Untagged: a `ref`
# key selects the first, a `value` key the second, and `_Node`'s `extra="forbid"` is what keeps
# any one object from matching both.
type Operand = Ref | Constant


type ComparisonOp = Literal[
    "gt",
    "lt",
    "gte",
    "lte",
    "crosses_above",
    "crosses_below",
    "breaks_above",
    "breaks_below",
]


class Comparison(_Node):
    """A leaf: two operands and an operator.

    `gt` asks about this candle. `crosses_above` asks about this candle *and the
    previous one* — it is true only on the bar where the relation flips, which is
    what stops a strategy from re-entering on every bar of a trend it already owns.
    """

    op: ComparisonOp
    left: Operand
    right: Operand


class Between(_Node):
    """Three operands: is `value` inside the band `[low, high]`?

    ⚠️ **Both bounds are inclusive**, and it is written down here because "between" is the kind
    of word every reader is sure they already know. Exclusive would make `between 0 and 100`
    reject the two values a bounded oscillator can actually reach.

    A node of its own rather than an `op` on `Comparison`, because it takes three operands and
    `Comparison` takes two. Folding it in would make `right` mean one thing for seven operators
    and another for this one — and the untagged union below discriminates on *shape*, so the
    distinct shape is what keeps a document unambiguous.

    The bounds are operands, not numbers, so a band can be drawn between two indicators — a
    price inside its own channel is `between(price.close, lower, upper)`.
    """

    op: Literal["between"]
    value: Operand
    low: Operand
    high: Operand


class Trend(_Node):
    """Has `of` moved in one direction on each of the last `bars` bars?

    ⚠️ **Monotonic over the window, not "higher than it was N bars ago".** The looser reading
    calls a series that fell for four bars and jumped on the fifth "rising", which is the
    opposite of what anybody writing the rule meant. Matching the reading used by the charting
    platforms this project's indicators were transcribed from is the point: a rule that means
    something different here than on his chart is a rule he cannot check.

    `bars: 1` is the base case and the default — "higher than the previous bar".
    """

    op: Literal["rising", "falling"]
    of: Operand
    bars: Annotated[int, Field(ge=1, le=100)] = 1


class AllOf(_Node):
    """Logical AND."""

    all: Annotated[list["Condition"], Field(min_length=1)]


class AnyOf(_Node):
    """Logical OR."""

    any: Annotated[list["Condition"], Field(min_length=1)]


class NotOf(_Node):
    """Logical NOT. Serialised as `not`, which is a Python keyword — hence the alias."""

    not_: Annotated["Condition", Field(alias="not")]


# Untagged union: the shape of the object decides which node it is. `extra="forbid"`
# is what makes that unambiguous — exactly one member can accept any given object.
#
# ⚠️ The three leaf shapes are distinguished by their *field names*, not by `op` alone:
# `left`/`right`, `value`/`low`/`high`, `of`. `extra="forbid"` is what makes that a proof
# rather than a hope — a `between` carrying a `left` matches nothing and is refused, instead
# of being read as a comparison with a stray key.
type Condition = Comparison | Between | Trend | AllOf | AnyOf | NotOf


# --------------------------------------------------------------------------- #
# Indicators                                                                    #
# --------------------------------------------------------------------------- #


class PeriodSourceParams(_Node):
    """A window length and which price it reads — shared by every single-period indicator
    (SMA, EMA, RSI, and ATR to come). Named for its shape, not for one indicator, because a
    period over a price source is exactly what they all take. ATR is *not* one of them — see
    `PeriodParams`."""

    period: Annotated[int, Field(ge=1, le=1000)]
    source: PriceSource = "close"


class SMA(_Node):
    id: Annotated[str, Field(pattern=INDICATOR_ID_PATTERN, max_length=40)]
    type: Literal["SMA"]
    params: PeriodSourceParams


class EMA(_Node):
    id: Annotated[str, Field(pattern=INDICATOR_ID_PATTERN, max_length=40)]
    type: Literal["EMA"]
    params: PeriodSourceParams


class RSI(_Node):
    """Relative Strength Index — a bounded 0-100 momentum oscillator (Wilder). Same params as a
    moving average; the engine smooths gains and losses with Wilder's method (ADR-13: adding it
    is additive, so `schema_version` stays 1.0)."""

    id: Annotated[str, Field(pattern=INDICATOR_ID_PATTERN, max_length=40)]
    type: Literal["RSI"]
    params: PeriodSourceParams


class PeriodParams(_Node):
    """A window length and nothing else — for the indicators that read the whole candle.

    ⚠️ Deliberately **not** `PeriodSourceParams` with the source defaulted. ATR is defined over
    high, low and the previous close together; a channel is defined over highs and lows. An
    "ATR of the close" is not a variant of ATR, it is a different measurement — and a parameter
    the engine would have to ignore is a request the document believes was honoured.
    """

    period: Annotated[int, Field(ge=1, le=1000)]


class ATR(_Node):
    """Average True Range (Wilder) — the size of a typical bar, in price.

    True Range counts the gap: a bar that opens away from the previous close travelled further
    than its own high minus its low. Used to size stops and to ask whether a market is moving
    at all.
    """

    id: Annotated[str, Field(pattern=INDICATOR_ID_PATTERN, max_length=40)]
    type: Literal["ATR"]
    params: PeriodParams


class Highest(_Node):
    """The highest **high** of the N bars that closed *before* this one — a breakout's rail.

    ⚠️ **The current bar is not in the window**, and that is what makes the level breakable:
    include it and `HIGHEST(20) >= high` holds on every bar of every market, so a comparison
    against it is constantly false and a band drawn with it is constantly true. A charting
    platform's `ta.highest` does include the current bar and is read shifted (`[1]`); the DSL
    has no shift for an indicator ref, so the level is defined where it can be used.

    Highs, not closes, so the level is where price actually traded. A breakout of it therefore
    fires on the wick, which is the classical reading and the more sensitive one; comparing
    against `price.close` instead is how the same channel is made to require a close beyond it.
    """

    id: Annotated[str, Field(pattern=INDICATOR_ID_PATTERN, max_length=40)]
    type: Literal["HIGHEST"]
    params: PeriodParams


class Lowest(_Node):
    """The lowest **low** of the N bars that closed before this one — the lower rail."""

    id: Annotated[str, Field(pattern=INDICATOR_ID_PATTERN, max_length=40)]
    type: Literal["LOWEST"]
    params: PeriodParams


class BollingerParams(_Node):
    """A window, the price it reads, and how many deviations wide the envelope is.

    `deviations` is a float rather than an int because 1.5 and 2.5 are ordinary settings, and it
    is bounded above 0 exclusively: a multiplier of zero collapses the three bands onto the
    average, which is an SMA spelled in a way that hides that it is one. The upper bound is
    generous — a band ten deviations out is useless, not malformed.
    """

    period: Annotated[int, Field(ge=1, le=1000)]
    source: PriceSource = "close"
    deviations: Annotated[float, Field(gt=0, le=10, allow_inf_nan=False)] = 2.0


class Bollinger(_Node):
    """Bollinger Bands — an average with a volatility envelope, read by component.

    ⚠️ **One indicator with three readings, not three indicators.** Declared once as `bb`, its
    bands are referenced as `bb.upper`, `bb.middle` and `bb.lower`. Three separate declarations
    would let a document give the upper band a period of 20 and the lower one 50, and nothing
    could object: for this schema they would be two well-formed indicators. What came out would
    be a band whose rails describe different markets, and it would look exactly like a band.
    """

    # ⚠️ **The component names belong in the schema, as data.** They used to live only in the
    # `COMPOSITE_COMPONENTS` map below and in the prose above, which meant a consumer without a
    # Python runtime — the web builder — could not offer `bb.upper` without writing a third copy
    # of the list by hand. `json_schema_extra` puts them in this node's schema, and the map below
    # is now *read back* from here, so this class is the one place they are written.
    model_config = ConfigDict(json_schema_extra={"components": ["middle", "upper", "lower"]})

    id: Annotated[str, Field(pattern=INDICATOR_ID_PATTERN, max_length=40)]
    type: Literal["BOLLINGER"]
    params: BollingerParams


class ADX(_Node):
    """Average Directional Index (Wilder) — how strongly a market trends, read by component.

    Referenced as `adx.adx` for the strength line and `adx.plus_di` / `adx.minus_di` for the two
    direction lines. ⚠️ **The `adx` component has no direction**: it rises in a hard sell-off
    exactly as it does in a rally, so a rule meaning "trending up" needs the `DI` pair as well.
    Reads the whole candle, so there is no price source to name — same argument as `PeriodParams`.
    """

    model_config = ConfigDict(json_schema_extra={"components": ["adx", "plus_di", "minus_di"]})

    id: Annotated[str, Field(pattern=INDICATOR_ID_PATTERN, max_length=40)]
    type: Literal["ADX"]
    params: PeriodParams


# Discriminated on `type`: the generated JSON Schema gets a proper `oneOf` with a
# discriminator, and a new indicator is a new member — an additive change that leaves
# every strategy already saved still valid. That is ADR-03 working as designed: new
# blocks without touching the core (and ADR-13: additive members keep the
# schema_version, they do not bump it).
type Indicator = Annotated[
    SMA | EMA | RSI | ATR | Highest | Lowest | Bollinger | ADX,
    Field(discriminator="type"),
]


def _declared_components(model: type[BaseModel]) -> tuple[str, ...]:
    """The component names a model publishes in its own schema, or `()` for a single-valued one.

    Read off `json_schema_extra` rather than a list kept beside it, so the names reach three
    consumers from one place: this map, the generated JSON Schema, and — through the schema — a
    frontend with no Python runtime. The type check is not ceremony: a `json_schema_extra` given
    as a *callable* is legal Pydantic and would silently answer "no components", which reads as
    "this indicator is single-valued" and quietly makes every `bb.upper` a reference error.
    """
    extra = model.model_config.get("json_schema_extra")
    if extra is None:
        return ()
    if not isinstance(extra, dict):
        raise TypeError(f"{model.__name__} publishes a json_schema_extra no reader can inspect")
    components = extra.get("components", ())
    if not isinstance(components, list | tuple):
        raise TypeError(f"{model.__name__} declares components that are not a list")
    # Built by comprehension rather than `tuple(components)` behind an `all(isinstance(...))`
    # guard: the guard convinces a reader and not the type checker, and `json_schema_extra` is
    # typed loosely enough that anything at all can be in there.
    names = tuple(name for name in components if isinstance(name, str))
    if len(names) != len(components):
        raise TypeError(f"{model.__name__} declares a component that is not a name")
    return names


def _composite_components() -> Mapping[str, tuple[str, ...]]:
    """Walk the indicator union and collect what each member answers to, keyed by its DSL type."""
    # `Indicator` is a PEP-695 alias, so the annotation is behind `__value__`; `get_args` on the
    # alias itself answers `()`, which would make this map silently empty — every composite
    # indicator reclassified as single-valued, and every `bb.upper` a reference error.
    union, *_ = get_args(Indicator.__value__)
    found: dict[str, tuple[str, ...]] = {}
    for member in get_args(union):
        declared = _declared_components(member)
        if declared:
            (dsl_type,) = get_args(member.model_fields["type"].annotation)
            found[dsl_type] = declared
    return found


# Which component names each multi-output indicator answers to. A reference to one of these
# indicators must name a component, and a reference to any other indicator must not.
#
# ⚠️ **Derived from the classes, not written here.** It used to be a literal map, which made the
# names exist twice inside this file alone — and the copy the *schema* carried was prose, so the
# web builder could not read it at all. A new composite indicator now appears here by declaring
# its components on itself, and nothing in this file needs editing.
#
# ⚠️ Still duplicated in `tradeforge_engine.indicators.COMPOSITE_COMPONENTS` — the two packages do
# not import each other — and pinned equal by a test in `apps/api`, which depends on both. Drift is
# the silent kind of failure: this layer would accept `bb.uppper`, the engine would resolve it to
# `None`, and a comparison against `None` is simply false on every bar for ever.
# ⚠️ Ordered **primary first**: a consumer with no idea what a band is uses this order to tell
# the subject from its envelope, and the chart draws the first solid and the rest dashed.
COMPOSITE_COMPONENTS: Final[Mapping[str, tuple[str, ...]]] = _composite_components()


# --------------------------------------------------------------------------- #
# Exits                                                                         #
# --------------------------------------------------------------------------- #


class CandleExtremeParams(_Node):
    lookback: Annotated[int, Field(ge=1, le=100)]
    side: Literal["low", "high"]


class CandleExtremeStop(_Node):
    """Stop at the low (or high) of the last N closed candles."""

    type: Literal["candle_extreme"]
    params: CandleExtremeParams


class RiskMultipleParams(_Node):
    rr: Annotated[float, Field(gt=0, le=100)]


class RiskMultipleTakeProfit(_Node):
    """Target at N times the distance to the stop.

    Meaningless without a stop — there is no risk to take a multiple of. The schema
    cannot say that; `semantic.py` does.
    """

    type: Literal["risk_multiple"]
    params: RiskMultipleParams


class Exit(_Node):
    stop_loss: CandleExtremeStop | None = None
    take_profit: RiskMultipleTakeProfit | None = None
    conditions: list[Condition] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Setups — the strategies the DSL *names* instead of describing                  #
# --------------------------------------------------------------------------- #
#
# Everything above this line describes a strategy as a tree of conditions: this
# indicator crossed that price, so buy. A setup cannot be written that way, and the
# reason is not expressive power — it is memory. A condition sees one closed candle
# and answers yes or no. A setup remembers: which order is resting and on which bar's
# high, whether this turn already gave its trade, how many breaks of structure this
# position has seen. That is a state machine, and no amount of `all`/`any` nesting
# reaches it. See ADR-0019.
#
# So the DSL names the setup and hands it parameters. The defaults below are the
# author's own rules, and they are duplicated from the engine classes deliberately:
# a default that lives only in Python is invisible to the builder, the generated
# TypeScript, and the phase-3 LLM. The drift is caught by a test that constructs each
# engine class and compares.


class Mme9BreakoutParams(_Node):
    """The break of the candle that closed across the MME9 (ADR-0016)."""

    side: SetupSide
    period: Annotated[int, Field(ge=1, le=1000)] = 9
    stop_buffer_ticks: Annotated[int, Field(ge=0, le=10_000)] = 0
    breakeven_at_r: Annotated[float | None, Field(gt=0, le=100)] = 2.0


class Mme9BreakoutSetup(_Node):
    type: Literal["mme9_breakout"]
    params: Mme9BreakoutParams


class PontoContinuoParams(_Node):
    """Two corrections back to the average, then the bar that touches it and closes back."""

    side: SetupSide
    period: Annotated[int, Field(ge=1, le=1000)] = 20
    average: AverageKind = "EMA"
    stop_buffer_ticks: Annotated[int, Field(ge=0, le=10_000)] = 0
    breakeven_at_r: Annotated[float | None, Field(gt=0, le=100)] = 2.0


class PontoContinuoSetup(_Node):
    type: Literal["ponto_continuo"]
    params: PontoContinuoParams


class StructureParams(_Node):
    """Shared by every setup that arms a limit order on a zone market structure left behind.

    Not directional: which side is traded follows the structure, so there is no `side` here.
    `stop_buffer` is a *fraction of the zone's width*, not ticks — the zone is the unit.

    `entry_point` chooses which of the author's activations the setup uses. `edge` and `midpoint`
    are his chapter 11.4 and both rest a *limit* inside the region, waiting for price to come
    back: `edge` on the near edge, costing a stop the full width of the region, and `midpoint` at
    50%, roughly halving the stop so the same risk buys more size. The trade-off between those two
    is his — "muitas vezes o preço não chega aos 50% e acaba indo em direção ao nosso alvo sem nos
    ativar". `edge` is the default because changing it would move every result already recorded.

    `return_pass` is his chapter 11.5 and a different shape of trade. Nothing is placed when the
    zone is marked; price coming back to the 50% is what *places* the order, as a stop on the far
    side of the near edge, and it fills only if price then resumes its move and passes back out
    through the region. It carries the widest stop of the three, and its order is cancelled — not
    filled — if price instead reaches the level that stop would occupy.

    `botinha` is his chapter 11.2, and the only one whose order is not arithmetic on the
    region at all. The zone marks the territory; the entry comes from a *formation* inside it —
    the reaction's extreme anchors two volume-weighted averages, one on the typical price and
    one on the lows, and a bar closing in the trade's direction confirms them. The order then
    rests a tenth of the way up the band from the lower line, with its stop a whole band below,
    and it has seven bars to fill. Two things follow that no other value does: the level is
    **re-priced** on every close, because both averages move, and the region is **not** retired
    by price touching it — entering the region is where the setup begins, so a zone spent by its
    own first touch could never form one. What retires it instead is the window running out.

    ⚠️ The stop buffer says nothing about `botinha`: its stop comes from the band, not from the
    region. Its own numbers — the window, the tenth, and which volume the averages use — are not
    on this document yet and run on the engine's defaults.

    ⚠️ **Named values, not a fraction.** A free number would let an entry approach the far edge,
    where risk collapses to the stop buffer alone and position sizing divides by nearly nothing.
    His model 2 — the marking candle's body limit — is absent for a different reason: the region
    records only that candle's high and low, so the body is not there to read.
    """

    allow_secondary: bool = False
    stop_buffer: Annotated[float, Field(ge=0, le=10)] = 0.1
    entry_point: ZoneEntryPoint = "edge"
    breakeven_at_r: Annotated[float | None, Field(gt=0, le=100)] = 2.0


class StructureChochSetup(_Node):
    """Trade the zone the change of character left behind."""

    type: Literal["structure_choch"]
    params: StructureParams = Field(default_factory=StructureParams)


class ContinuationParams(StructureParams):
    """`max_bos` caps how many breaks after a change of character may still be traded.

    `null` is uncapped, which is the default and not a degenerate setting: "every favourable
    break re-arms" is a real reading of the method, and capping it is the experiment.
    """

    max_bos: Annotated[int | None, Field(ge=1, le=100)] = None


class StructureContinuationSetup(_Node):
    """Trade the zone a break in the trend's favour leaves behind, after a change of character."""

    type: Literal["structure_continuation"]
    params: ContinuationParams = Field(default_factory=ContinuationParams)


type Setup = Annotated[
    Mme9BreakoutSetup | PontoContinuoSetup | StructureChochSetup | StructureContinuationSetup,
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------- #
# Risk                                                                          #
# --------------------------------------------------------------------------- #


class PercentRiskParams(_Node):
    percent: Annotated[float, Field(gt=0, le=100)]


class PercentRiskSizing(_Node):
    """Size the position so that hitting the stop costs `percent` of the account."""

    type: Literal["percent_risk"]
    params: PercentRiskParams


class Risk(_Node):
    sizing: PercentRiskSizing
    max_open_positions: Annotated[int, Field(ge=1, le=100)] = 1
    max_daily_loss_percent: Annotated[float, Field(gt=0, le=100)] = 3.0


# --------------------------------------------------------------------------- #
# The strategy                                                                  #
# --------------------------------------------------------------------------- #


class Entry(_Node):
    """Entry conditions per side. A strategy must trade at least one of them."""

    long: Condition | None = None
    short: Condition | None = None


class Strategy(_Node):
    """A complete, self-contained strategy definition."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        title="Strategy",
        json_schema_extra={"$id": "https://tradeforge.dev/schema/strategy/v1.json"},
    )

    # Pinned, not free-form: a saved strategy is immutable for its version, and the
    # engine must be able to refuse a document it was not built to interpret.
    schema_version: Literal["1.0"]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    description: str = ""
    timeframe: Timeframe
    indicators: Annotated[list[Indicator], Field(max_length=20)] = Field(default_factory=list)
    # A document describes its strategy *either* as conditions (`indicators` + `entry`) *or* by
    # naming a `setup`, never both and never neither. The schema cannot say that — it is a rule
    # about which fields may coexist, not about any one field's shape — so `semantic.py` says it,
    # which is the same boundary that already owns "no entry side" and "target without a stop".
    #
    # `entry` and `exit` became optional to make this possible, and that is additive: every saved
    # document supplied them, and one that does not is a setup document. `schema_version` stays at
    # "1.0" (ADR-0013 — additive changes do not bump it).
    setup: Setup | None = None
    entry: Entry = Field(default_factory=Entry)
    exit: Exit = Field(default_factory=Exit)
    risk: Risk


AllOf.model_rebuild()
AnyOf.model_rebuild()
NotOf.model_rebuild()
