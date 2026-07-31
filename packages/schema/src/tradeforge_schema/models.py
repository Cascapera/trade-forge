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
    r"|price\.(?:open|high|low|close)"  # the candle being decided on
    r"|candle\[-[1-9][0-9]*\]\.(?:open|high|low|close)"  # a closed candle, N back
    r")$"
)

INDICATOR_ID_PATTERN = r"^[a-z_][a-z0-9_]*$"

type Timeframe = Literal["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"]
type PriceSource = Literal["open", "high", "low", "close"]

# A directional setup trades one side; the two-sided version is two of them. The structure
# family has no side at all — which way it trades follows the structure it reads.
type SetupSide = Literal["long", "short"]
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
type Condition = Comparison | AllOf | AnyOf | NotOf


# --------------------------------------------------------------------------- #
# Indicators                                                                    #
# --------------------------------------------------------------------------- #


class PeriodSourceParams(_Node):
    """A window length and which price it reads — shared by every single-period indicator
    (SMA, EMA, RSI, and ATR to come). Named for its shape, not for one indicator, because a
    period over a price source is exactly what they all take."""

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


# Discriminated on `type`: the generated JSON Schema gets a proper `oneOf` with a
# discriminator, and a new indicator (phase 2: ATR, ADX, MACD...) is a new member —
# an additive change that leaves every strategy already saved still valid. That is
# ADR-03 working as designed: new blocks without touching the core (and ADR-13:
# additive members keep the schema_version, they do not bump it).
type Indicator = Annotated[SMA | EMA | RSI, Field(discriminator="type")]


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
    """

    allow_secondary: bool = False
    stop_buffer: Annotated[float, Field(ge=0, le=10)] = 0.1
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
