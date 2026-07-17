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
    entry: Entry
    exit: Exit
    risk: Risk


AllOf.model_rebuild()
AnyOf.model_rebuild()
NotOf.model_rebuild()
