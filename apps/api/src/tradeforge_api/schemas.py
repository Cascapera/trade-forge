"""Request and response bodies for the HTTP surface.

These are the API's own shapes — deliberately **not** the strategy DSL, which is owned by
`tradeforge_schema` and validated on its own terms (a strategy arrives as an opaque document
and is handed straight to that validator). A DTO here is a projection chosen for a client:
what a caller sends to start a backtest, and what a finished run looks like read back.

Money and every derived ratio cross the wire as a **string**, never a JSON number. A JSON
number is an IEEE double, and the exact-decimal discipline the engine and the database keep
would be lost the moment a price serialised as one — so the same `Decimal` that survived
Postgres survives the wire too, all the way to the client that renders it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
)

from tradeforge_api.walkforward import MAX_FOLDS, MIN_FOLDS
from tradeforge_db.models import SelectionMetric
from tradeforge_schema.models import TIMEFRAMES

# A Decimal that always serialises to a string. Applied to every monetary/ratio field below.
Money = Annotated[Decimal, PlainSerializer(str, return_type=str)]


def _storable(value: str) -> str:
    """Refuse a string Postgres cannot hold, which is exactly one character: NUL.

    ⚠️ Not a guess at what a symbol looks like. Broker symbols are genuinely strange —
    `EURUSD.raw`, `#AAPL`, `US500.cash` — so a pattern invented here would refuse real
    instruments to catch a fuzzer. The **one** constraint that is a fact rather than a taste is
    that a text column cannot store a NUL byte: Postgres raises `DataError` from inside the
    driver, and a value a client fully controls turns into a 500.

    Found by schemathesis drawing `?symbol=%00` for the first time, on a route that had been
    green for weeks — the same way the `offset` bigint overflow was found. Both are the same
    shape: a parameter whose whole range was never actually tried.
    """
    # `chr(0)` rather than the escape, so nothing that edits this file can turn the escape into
    # an actual NUL — which is precisely what happened while writing it, and Python then refuses
    # to parse its own source.
    if chr(0) in value:
        raise ValueError("must not contain a NUL byte")
    return value


def _known_timeframe(value: str) -> str:
    """Refuse a timeframe the DSL does not define.

    ⚠️ Checked against `tradeforge_schema.TIMEFRAMES` rather than a list retyped here — the DSL
    owns the set, and a second copy is a copy that disagrees the first time one is added.

    Worth validating at the edge rather than letting the lookup miss: an unknown timeframe would
    otherwise come back as a 404 saying the series has not been probed, which sends somebody to
    press a probe button for a timeframe that cannot exist. 422 says which of the two is wrong.
    """
    _storable(value)
    if value not in TIMEFRAMES:
        legal = ", ".join(TIMEFRAMES)
        raise ValueError(f"unknown timeframe {value!r}; expected one of {legal}")
    return value


Timeframe = Annotated[str, AfterValidator(_known_timeframe)]
"""A timeframe as it arrives from a client, refused unless the DSL defines it."""

Symbol = Annotated[str, AfterValidator(_storable)]
"""An instrument symbol as it arrives from a client, refused only where the database would."""

StorableText = Annotated[str, AfterValidator(_storable)]
"""Any free text from a client that reaches a query against a text column.

⚠️ **The same validator as `Symbol`, named for the reason rather than for the field.** When
`Symbol` was introduced the guard travelled with the one parameter the fuzzer had drawn a NUL
into, and its three siblings — `?q=`, `?name=`, `?timeframe=` — kept the plain `str` and kept
the 500. A rule attached to one field is a rule the next field does not inherit; naming it for
what it protects is what makes the next one obvious.
"""


class _Out(BaseModel):
    """Base for response bodies. `from_attributes` lets a handler return an ORM row directly and
    have FastAPI read the fields off it — no hand-written to-dict per endpoint."""

    model_config = ConfigDict(from_attributes=True)


class StrategyOut(_Out):
    """A stored strategy version. The DSL document is returned verbatim under `definition`."""

    id: uuid.UUID
    name: str
    version: int
    schema_version: str
    definition: dict[str, Any]
    created_at: dt.datetime


class StrategyListItem(_Out):
    """One row of the strategy picker: enough to choose between lineages without opening any.

    Deliberately **not** `StrategyOut`, which carries the whole DSL document. A picker renders
    forty-five of these and reads none of the documents; shipping them would send megabytes of
    JSONB to draw a list of names.

    `runs` is here because it is what tells a real strategy from an experiment somebody typed
    once and abandoned — the reader scanning this list is looking for the one they have been
    working on, and how often it has been executed says that better than a date does.
    """

    id: uuid.UUID
    name: str
    version: int
    """The **latest** version of this lineage; the id above is that version's row."""

    schema_version: str
    setup: str | None
    """The named setup this strategy runs (`mme9_breakout`, `structure_choch`), or null for a
    DSL document built from indicators and conditions. It is what a reader recognises a
    strategy by, and every strategy in this project's database has one."""

    runs: int
    created_at: dt.datetime


class StrategiesPage(BaseModel):
    """A page of lineages. `total` lets a client size the pager without walking every page."""

    total: int
    limit: int
    offset: int
    items: list[StrategyListItem]


class InstrumentOut(_Out):
    """A tradable symbol and the numbers that price a position in it."""

    id: uuid.UUID
    symbol: str
    name: str
    asset_class: str
    currency_quote: str
    currency_base: str | None
    tick_size: Money
    tick_value: Money
    contract_size: Money
    digits: int
    # The broker's quoted spread in ticks, for a client to pre-fill a run's cost model with.
    # Null means nobody has measured this symbol — a seeded row, or one catalogued before the
    # collector recorded it — and a client must say so rather than charging zero, which is a
    # claim that trading it is free. The engine never reads this: costs reach a run as a
    # plugged-in `CostModel` (ADR-07), and this only decides what the screen offers first.
    default_spread_points: Money | None = None


class BrokerSymbolOut(_Out):
    """One line of the broker's own catalogue, as MetaTrader last described it.

    ⚠️ Not an `InstrumentOut`, and it deliberately carries none of the pricing numbers. This is
    a symbol the account *can see*; whether this system knows how to turn a tick of it into
    money is a separate question, answered by `instruments`, and a symbol can be in this list
    for a long time before anyone collects a candle of it.
    """

    symbol: str
    description: str | None = None
    path: str | None = None
    digits: int | None = None
    # Whether the symbol sits in Market Watch. For display only — measured on this project's
    # broker, 74 of 84 symbols are outside it and every one still answers a search and hands
    # over history. Only the live loop has to care.
    visible: bool = False

    # ⚠️ Whether this system has an `Instrument` row for the symbol, which is what a backtest
    # needs. Without it the screen would offer 84 symbols of which one can be run, and the user
    # would find out which by clicking. Not a claim that candles exist for any particular range
    # — only that the symbol has been catalogued at all.
    catalogued: bool = False


class SymbolSnapshotOut(_Out):
    """Where the symbol list came from, and when.

    ⚠️ Named for the symbols and not just "snapshot", because this project already has a
    `SnapshotOut`: the record of what a strategy was looking at when it entered a trade. Two
    unrelated meanings under one name in one module is how a reader ends up reading the wrong
    docstring and believing it.
    """

    server: str | None = None
    synced_at: dt.datetime


class SymbolSearchOut(BaseModel):
    """Search results, plus the provenance of the list they came from.

    ⚠️ **`snapshot` is nullable so that "no match" and "no catalogue" stay distinguishable.**
    Both produce an empty `symbols`, and they are opposite problems: one means type fewer
    letters, the other means nobody has ever synced this broker. A response that flattened them
    would have the screen tell a user with no catalogue that their search found nothing —
    technically true, and the least useful true sentence available.
    """

    symbols: list[BrokerSymbolOut]
    snapshot: SymbolSnapshotOut | None = None

    @classmethod
    def build(
        cls,
        *,
        symbols: Sequence[object],
        server: str | None,
        synced_at: dt.datetime | None,
    ) -> SymbolSearchOut:
        """Assemble from ORM rows and the snapshot's own two fields."""
        return cls(
            symbols=[BrokerSymbolOut.model_validate(row) for row in symbols],
            snapshot=(
                None if synced_at is None else SymbolSnapshotOut(server=server, synced_at=synced_at)
            ),
        )


class SymbolHistoryOut(_Out):
    """What a probe found about one series, and what bounded the answer.

    ⚠️ **Four fields where a screen wants one date, on purpose.** "Available from" would be four
    different claims wearing one hat, and only one of them is ever the binding one — measured on
    EURUSD D1, the terminal's ceiling, the filler and the typed costs give 1971, 1972 and 2009.
    A reader fixes the first in a settings dialog, the second by starting later and the third by
    not trusting old costs at all; a single date makes all three unavailable.

    `usable_from` is offered *as well*, because deriving it in three clients is three chances to
    derive it differently.
    """

    symbol: str
    timeframe: str
    oldest: dt.datetime | None = None
    bar_count: int
    terminal_maxbars: int
    bar_count_is_a_ceiling: bool
    last_fabricated: int | None = None
    first_measured_cost: int | None = None
    probed_at: dt.datetime

    capped_by_terminal: bool = False
    """The bar count is this machine's setting talking, not the broker's history."""

    usable_from: dt.datetime | None = None
    """The later of the two honest floors, never earlier than the data actually goes.

    ⚠️ A **lower** bound on trust and nothing more. It cannot see a reconstruction that carries
    plausible prices and volumes — EURUSD's own runs to 1999 and is invisible here — so a screen
    rendering this owes the reader that caveat.
    """

    @classmethod
    def build(cls, row: Any) -> SymbolHistoryOut:  # noqa: ANN401 — an ORM row, typed by the caller
        """Assemble from a stored probe, deriving the two answers a screen would otherwise
        derive for itself.

        ⚠️ Derived **here** rather than in each client. Three clients deriving "usable from"
        independently is three chances to disagree about which floor dominates, and the
        disagreement would show up as two screens recommending different windows for the same
        symbol.
        """
        after_filler = None if row.last_fabricated is None else row.last_fabricated + 1
        years = [year for year in (after_filler, row.first_measured_cost) if year is not None]
        if years:
            floor = dt.datetime(max(years), 1, 1, tzinfo=dt.UTC)
            usable = floor if row.oldest is None else max(floor, row.oldest)
        else:
            usable = row.oldest

        return cls(
            symbol=row.symbol,
            timeframe=row.timeframe,
            oldest=row.oldest,
            bar_count=row.bar_count,
            terminal_maxbars=row.terminal_maxbars,
            bar_count_is_a_ceiling=row.bar_count_is_a_ceiling,
            last_fabricated=row.last_fabricated,
            first_measured_cost=row.first_measured_cost,
            probed_at=row.probed_at,
            capped_by_terminal=row.bar_count >= row.terminal_maxbars > 0,
            usable_from=usable,
        )


class EnqueuedOut(BaseModel):
    """A job was accepted for a worker that this process cannot see.

    Carries the job's name and nothing else on purpose. There is no id worth returning: the
    enqueue is idempotent by construction, so "your request is pending" is the whole truth, and
    a handle would invite a caller to poll a job whose only observable result is the data it
    replaces.
    """

    job: str


class CreateBacktestRequest(BaseModel):
    """What a client sends to enqueue a backtest.

    `symbol` (not an instrument id) because that is what a human names; the API resolves it to
    the instrument row. `cost_model` is a document, not a column — the same plugged-in shape
    the engine consumes (`{"type": "spread", "spread_points": 10}`, `{"type": "none"}`), stored
    per run so "the same backtest with a wider spread" is a different, comparable row.
    """

    model_config = ConfigDict(extra="forbid")

    strategy_id: uuid.UUID
    symbol: Symbol
    timeframe: str
    date_from: dt.datetime
    date_to: dt.datetime
    initial_capital: Decimal = Field(gt=0)
    cost_model: dict[str, Any] = Field(default_factory=lambda: {"type": "none"})


class MetricsOut(_Out):
    """The §5 summary. Nullable fields are genuinely undefined, never a fabricated zero."""

    net_profit: Money
    gross_profit: Money
    gross_loss: Money
    total_trades: int
    long_trades: int
    short_trades: int
    win_rate: Money
    payoff: Money | None
    profit_factor: Money | None
    expectancy: Money | None
    max_drawdown_abs: Money
    max_drawdown_pct: Money
    max_dd_duration_days: int
    sharpe: Money | None
    sortino: Money | None
    cagr: Money | None
    avg_trade_duration: dt.timedelta | None


class BacktestOut(_Out):
    """A run: its request, what it actually read, its lifecycle status, and its metrics.

    `date_from`/`date_to` are the *request*; `first_candle`/`last_candle` are the answer.
    They differ whenever the dataset starts later or ends earlier than what was asked for,
    and a client that shows only the request is quietly reporting the wrong experiment.
    """

    id: uuid.UUID
    strategy_id: uuid.UUID
    instrument_id: uuid.UUID
    timeframe: str
    date_from: dt.datetime
    date_to: dt.datetime
    initial_capital: Money
    status: str
    error: str | None
    engine_version: str
    created_at: dt.datetime
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    candles_seen: int | None = None
    first_candle: dt.datetime | None = None
    last_candle: dt.datetime | None = None
    metrics: MetricsOut | None = None


class BacktestListItem(_Out):
    """One row of the run log: enough to compare runs without opening any of them.

    Deliberately *not* `BacktestOut`. Two differences, both of them the point of this schema.

    It resolves the foreign keys the detail view leaves as ids. A list of runs is read by a human
    deciding which experiment to look at, and `instrument_id` answers nothing — the symbol, the
    strategy's name and its version are what tell one row from another. Resolving them here costs
    two joins on the server and saves the client a lookup per row.

    It carries `cost_model` because a run's costs are part of what it *is*, not a detail of how it
    was launched. The same strategy over the same window with a wider spread is a different
    experiment (ADR-07), and a comparison table that hides that invites reading two incomparable
    rows as a like-for-like result.

    What it does not carry is the equity curve, which lives on `MetricsOut`'s sibling and is
    fetched per run from `/backtests/{id}/equity`. See `list_backtests` for what that costs.
    """

    id: uuid.UUID
    strategy_id: uuid.UUID
    strategy_name: str
    strategy_version: int
    symbol: str
    timeframe: str
    date_from: dt.datetime
    date_to: dt.datetime
    initial_capital: Money
    cost_model: dict[str, Any]
    status: str
    error: str | None
    created_at: dt.datetime
    finished_at: dt.datetime | None
    metrics: MetricsOut | None = None


class BacktestsPage(BaseModel):
    """A page of runs. `total` lets a client size the pager without walking every page."""

    total: int
    limit: int
    offset: int
    items: list[BacktestListItem]


class SnapshotBarOut(BaseModel):
    """One bar of an entry's picture. Prices are strings for the same reason `Money` is."""

    time: dt.datetime
    open: Money
    high: Money
    low: Money
    close: Money


class SnapshotRegionOut(BaseModel):
    """A rectangle to draw over those bars: a price band with a left edge in time.

    `from_time` is the candle the zone was formed on, which is routinely older than the
    window's first bar — the client extends the rectangle rightward from wherever that falls,
    clipping at the chart's edge, and must not shift it to the first bar it can see.
    """

    label: str
    top: Money
    bottom: Money
    from_time: dt.datetime


class SnapshotSeriesOut(BaseModel):
    """A curve to draw across the bars — an indicator, as the strategy computed it.

    Each point is a `[time, value]` pair. The time is carried per point, not implied by
    position, so a client joins the curve to the bars on the timestamp: a series that ends
    before the last bar draws a curve that stops short, rather than one silently stretched.

    Expect exactly that. The curve ends on the decision bar while the bars can run further, to
    the fill — only the strategy knows the indicator, and it stopped contributing when it
    decided. A leading gap is the indicator warming up.
    """

    label: str
    points: list[tuple[dt.datetime, Money]]


class SnapshotLevelOut(BaseModel):
    """A horizontal segment: the structure a break of structure broke.

    Bounded at **both** ends, unlike a region. A zone is still in force after the entry and so
    extends rightward; a level is over the moment it is crossed, and `to_time` is that bar. The
    segment's length is how long the structure held, which is what says whether the break meant
    anything — so a client draws it between the two instants and does not extend it.

    `label` is `choch` or `bos`, and the difference matters: one turns the trend and the other
    continues it.
    """

    label: str
    price: Money
    from_time: dt.datetime
    to_time: dt.datetime


class SnapshotOut(BaseModel):
    """What the strategy was looking at when it entered, as recorded by the engine.

    Absent (`None`) on a trade whose run predates the snapshot, which is a different fact from
    an empty window and is why this is not simply an empty list of bars.
    """

    decided_at: dt.datetime
    filled_at: dt.datetime
    """The bar the order filled on — the window's last. Equal to `decided_at` when the order
    rested longer than the engine keeps bars for, in which case the window stops at the arming
    and the entry marker falls outside it. See `EntrySnapshot`."""

    bars: list[SnapshotBarOut]
    regions: list[SnapshotRegionOut] = []
    series: list[SnapshotSeriesOut] = []
    levels: list[SnapshotLevelOut] = []


class TradeOut(_Out):
    """One closed round trip, with the indicator snapshot that justified its entry."""

    id: int
    direction: str
    entry_time: dt.datetime
    entry_price: Money
    exit_time: dt.datetime | None
    exit_price: Money | None
    exit_reason: str | None
    volume: Money
    # The stop the trade was sized against, not the one it exited at — see the column comment
    # in `tradeforge_db.models`. A trailed trade reports `exit_reason='sl'` with an
    # `exit_price` some distance from this, and a table that renders them side by side without
    # saying so invites the reader to file a bug against the engine.
    stop_loss: Money | None
    take_profit: Money | None
    gross_pnl: Money | None
    costs: Money | None
    net_pnl: Money | None
    r_multiple: Money | None
    context: dict[str, str | None]
    has_snapshot: bool = Field(
        default=False,
        # Read off the row's `snapshot` column, which holds the window itself. The alias is what
        # makes this a *projection* of that column rather than a second column nobody writes:
        # without it Pydantic would look for `has_snapshot` on the row, not find it, and hand
        # back the default — every trade reporting no picture, quietly and always.
        validation_alias=AliasChoices("snapshot", "has_snapshot"),
    )
    """Whether this trade has an entry picture to fetch, **not** the picture itself.

    A snapshot is fifty-odd bars — around 7 kB of JSON per trade. Carrying them all in the list
    makes a five-hundred-trade run a multi-megabyte response assembled to be almost entirely
    thrown away: a reader opens the two or three entries that look wrong. So the list says only
    that one exists, and `GET /backtests/{id}/trades/{trade_id}/snapshot` serves it when asked.

    A flag rather than a nullable object because the client's question here is "is the button
    live?" — and answering it with an object would put the cost right back."""

    @field_validator("has_snapshot", mode="before")
    @classmethod
    def _snapshot_column_becomes_a_flag(cls, value: object) -> object:
        """Reads the `snapshot` JSONB column, which is `{}` when no window was recorded.

        `{}` is the column's NOT NULL default, so it is what every row written before
        `rev_0003` says — and it means "nothing recorded", which is a false flag, not a bug.
        """
        if isinstance(value, bool):
            return value
        return bool(value)


class TradesPage(BaseModel):
    """A page of trades. `total` lets a client size the pager without walking every page."""

    total: int
    limit: int
    offset: int
    items: list[TradeOut]


class EquityPointOut(_Out):
    time: dt.datetime
    equity: Money


class CandleOut(_Out):
    """One bar of the price chart. Prices are strings for the same reason `Money` is.

    No volume. It is on the row in Parquet and deliberately left off the wire: a run of
    thirteen thousand bars pays for every field it carries, and nothing on this chart reads
    volume yet. Adding it later is additive; sending it now is bytes spent on nothing.
    """

    time: dt.datetime
    open: Money
    high: Money
    low: Money
    close: Money


class CandlesOut(BaseModel):
    """The bars a finished run read, with the provenance to check them against.

    The three provenance fields are not decoration. Parquet underneath a run can be
    re-collected or extended after the fact, so "what this run read" and "what the dataset
    holds today" are two different questions — and the second is the only one this endpoint
    can answer by reading. Carrying `candles_seen` alongside `count` lets a client see the
    two disagree instead of drawing a chart that quietly covers a different period than the
    trades on it.
    """

    timeframe: str
    symbol: str

    #: What the run recorded eating, copied from the `backtests` row.
    candles_seen: int
    first_candle: dt.datetime
    last_candle: dt.datetime

    #: What was actually found on disk just now, for that same window.
    count: int
    candles: list[CandleOut]


class OverlaySeriesOut(BaseModel):
    """A curve to draw across the run's bars — an indicator, as the strategy computed it.

    Each point is a `[time, value]` pair, and the time is carried per point rather than implied
    by position. A client joins the curve to the candles **on the timestamp**: the series is
    shorter than the bars whenever the indicator was still warming up, and a curve joined by
    index would then be drawn one period to the left of where it belongs — every point wrong,
    and the shape still perfectly plausible.

    Warm-up bars are absent rather than null. The curve begins where the indicator did.
    """

    label: str
    points: list[tuple[dt.datetime, Money]]


class ZoneOut(BaseModel):
    """One region over the whole run, with both ends of its life.

    Three instants, none interchangeable. `from_time` is where the rectangle begins — the candle
    before the gap, routinely far older than the break that revealed it. `confirmed_at` is when a
    strategy could first act on it; on this project's own data the gap between the two runs to a
    median of 8 bars, so collapsing them would redraw most regions as much younger than they
    are. `mitigated_at` is the bar whose wick took the region, and `null` means it was still
    standing when the run ended — a chart extends that one to its own right edge rather than
    closing it somewhere invented.
    """

    kind: str
    """`demand` or `supply`."""

    top: Money
    bottom: Money
    from_time: dt.datetime
    confirmed_at: dt.datetime
    mitigated_at: dt.datetime | None
    primary: bool
    """First gap event of the impulse; the rest are secondary and need `allow_secondary`."""


class OverlaysOut(BaseModel):
    """Every curve the run's strategy was reading, over the window the run read.

    `series` is empty for a strategy that declares none — the structure setups, whose overlay is
    a set of zones rather than a line. Empty is an answer, not a failure.
    """

    symbol: str
    timeframe: str

    #: The same provenance pair `/candles` carries, and the curve needs it *more* than the bars
    #: do. The bars are read back; the curve is **recomputed over them**, so one extra bar inside
    #: the window does not add a point at the end — it reseeds the average and moves the whole
    #: line. A client that could not see the two disagree would redraw a different curve under
    #: the same trades and have nothing to notice it by.
    candles_seen: int
    count: int

    series: list[OverlaySeriesOut]

    #: The regions the run's strategy marked. Empty for the swing setups, which read a curve and
    #: mark no zones — the two halves of a chart's overlay are independent, and a strategy having
    #: one says nothing about whether it has the other.
    zones: list[ZoneOut] = []


class CreatedBacktest(_Out):
    """The 202 body: the run exists and is queued; poll `id` or subscribe to its WebSocket."""

    id: uuid.UUID
    status: str


# --------------------------------------------------------------------------- #
# Baskets — one strategy across several markets                                 #
# --------------------------------------------------------------------------- #

# A basket of one is a backtest, and the endpoint that already does that is better at it.
_MIN_SYMBOLS = 2

# A ceiling, because every list a client controls needs one for the same reason every numeric
# query parameter does: without it a single POST enqueues as many jobs as the caller likes.
# Twenty is well past what a human reads off one screen and far short of what hurts the queue.
_MAX_SYMBOLS = 20


class CreateBasketRequest(BaseModel):
    """Launch one strategy over several symbols, one run each.

    ⚠️ **No `cost_model`, and that is the design.** Each run is charged the spread measured
    for *its own* instrument (PR-226), resolved by the server at launch. A single figure
    across a basket would be meaningless: 8 ticks of EURUSD and 4 of AAPL are not only
    different numbers, they are counted in tick sizes that differ by a factor of a thousand.
    A symbol with no measured spread runs uncosted, and the response says which ones did.
    """

    model_config = ConfigDict(extra="forbid")

    strategy_id: uuid.UUID
    symbols: list[Symbol] = Field(min_length=_MIN_SYMBOLS, max_length=_MAX_SYMBOLS)
    timeframe: str
    date_from: dt.datetime
    date_to: dt.datetime
    initial_capital: Decimal = Field(gt=0)

    @field_validator("symbols")
    @classmethod
    def _distinct(cls, symbols: list[str]) -> list[str]:
        """The same symbol twice is a duplicated experiment wearing a comparison's clothes.

        Rejected rather than de-duplicated: silently returning three runs for four requested
        symbols would leave the caller's own list disagreeing with the basket, and a client
        that sent a duplicate by accident learns nothing from being quietly corrected.
        """
        duplicated = sorted({s for s in symbols if symbols.count(s) > 1})
        if duplicated:
            raise ValueError(f"symbols must be distinct; repeated: {', '.join(duplicated)}")
        return symbols


class BasketRunOut(_Out):
    """One symbol's place in the basket: which run it became, and what it is being charged."""

    backtest_id: uuid.UUID
    symbol: str
    status: str
    cost_model: dict[str, Any]
    # Null means the catalogue has no measured spread for this symbol, so the run is
    # uncosted — surfaced per row rather than as a basket-wide footnote, because it is
    # true of some symbols and not others and the reader has to know which.
    default_spread_points: Money | None


class CreatedBasket(_Out):
    """The 202 body: the basket exists and its runs are queued, one per symbol."""

    id: uuid.UUID
    runs: list[BasketRunOut]


class BasketAggregate(BaseModel):
    """What a basket says once its runs finish — **dispersion, never a combined account.**

    ⚠️ There is no summed equity curve here, and its absence is the most important decision
    in this schema. Every run in a basket starts with the whole `initial_capital`, so four
    runs of $10 000 are neither a $10 000 account nor a $40 000 one. Adding the curves would
    draw a line that looks like a portfolio and is not one — the same failure as the
    forward-fill rejected in the run comparator, where a fabricated shape is indistinguishable
    on screen from a measured one.

    What a basket is *for* is the spread of outcomes across markets. A strategy returning 30%
    on one symbol and -25% on another has a mean near zero and a story the mean destroys, so
    the extremes are reported by name and the median is reported instead of the average:
    one spectacular market cannot drag the middle.

    Returns are fractions of `initial_capital`, which the basket fixes for every run — so
    unlike the general run comparator, these percentages are comparable by construction.

    `null` for every statistic until at least one run finishes. Undefined, not zero: a basket
    whose runs are still queued has not returned 0%.
    """

    runs_total: int
    runs_finished: int
    runs_failed: int

    # Among the finished runs only. A queued run is not an unprofitable one.
    runs_profitable: int

    best_symbol: str | None
    best_return: Money | None
    worst_symbol: str | None
    worst_return: Money | None
    median_return: Money | None


class BasketOut(_Out):
    """A basket read back: how it was launched, every run in it, and the dispersion."""

    id: uuid.UUID
    strategy_id: uuid.UUID
    strategy_name: str
    strategy_version: int
    timeframe: str
    date_from: dt.datetime
    date_to: dt.datetime
    initial_capital: Money
    created_at: dt.datetime

    aggregate: BasketAggregate
    runs: list[BacktestListItem]


class CreateStudyRequest(BaseModel):
    """Launch one strategy over a grid of its own parameters, one run per combination.

    The mirror of `CreateBasketRequest`: that one lists **symbols** and holds the parameters
    still, this one lists **parameter values** and holds the symbol still. Holding the market
    still is what makes the points comparable to each other at all.

    `cost_model` is taken from the request, exactly as `POST /backtests` takes it, because a
    study is N of those over one instrument — one market, so one cost, and no reason for a
    second rule. (A basket resolves costs per instrument only because it spans several.)
    """

    model_config = ConfigDict(extra="forbid")

    strategy_id: uuid.UUID
    symbol: Symbol
    timeframe: str
    date_from: dt.datetime
    date_to: dt.datetime
    initial_capital: Decimal = Field(gt=0)
    cost_model: dict[str, Any] = Field(default_factory=lambda: {"type": "none"})

    grid: dict[str, list[Any]] = Field(min_length=1)
    """Paths into the strategy document, and the values to try at each.

    `{"setup.params.period": [5, 9, 20], "setup.params.take_profit_rr": [2, 3]}` expands to
    six runs. Dotted paths, with a numeric segment indexing a list, so a DSL strategy's first
    indicator is `indicators.0.params.period`.

    Only the emptiness is checked here; everything else is `grid.expand`'s, because the answers
    depend on the document being varied. ⚠️ In particular a path this strategy has *nothing* at
    is refused there rather than tolerated — substituting at one would add a parameter nothing
    reads, and the study would run N identical backtests and draw a flat heatmap that looks
    like a finding about the market.
    """


class StudyPointOut(_Out):
    """One combination: the run it became, and the strategy that was stored for it."""

    backtest_id: uuid.UUID
    strategy_id: uuid.UUID
    label: str
    """`period=9, take_profit_rr=3` — what makes this point different from the others."""

    values: dict[str, Any]
    """The same thing keyed by full path, for a client that lays out an axis rather than
    printing a caption. The label is for people; this is for the heatmap."""

    status: str


class CreatedStudy(_Out):
    """The 202 body: the study exists, and every point of the grid is queued."""

    id: uuid.UUID
    points: list[StudyPointOut]


class StudyAggregate(BaseModel):
    """What a grid says once its runs finish — **dispersion, and never the maximum alone.**

    ⚠️ **The most important thing in this schema is what the headline is not.** A grid always
    has a best point; a grid of pure noise has a best point. Reporting that number on its own
    is how an optimiser becomes a machine for producing convincing false results, and the wider
    the grid the more convincing they get — searching a hundred parameter sets and keeping the
    winner is, statistically, mostly a search for the luckiest arrangement of the same noise.

    So the honest summary is here in three parts, and they answer different questions:

    * `median_return` — what a parameter set picked *without* hindsight would have returned.
      This is the closest thing to "does the method work", and it is the number to read first.
    * `points_profitable` out of `points_finished` — how much of the searched space works at
      all. Ninety out of a hundred is a property of the method; three out of a hundred is a
      corner, whatever the best of those three returned.
    * `best_return` beside `worst_return` — the range, so the best is read as one end of a
      spread rather than as a result.

    **Every figure here is in-sample**, including the best one: these runs were scored on the
    same data the grid was searched over, so nothing in this response is evidence that the
    winning parameters will work next month. Walk-forward (PR-204) is the experiment that can
    say that — choose on one window, measure on the *next*, and see whether the choice survives
    being made blind. Until then, this is a description of a past, not a prediction.

    No summed curve, for the same reason a basket has none: every point starts with the whole
    `initial_capital`, so the runs cannot be added up.
    """

    points_total: int
    points_finished: int
    points_failed: int
    points_profitable: int

    best_label: str | None
    best_return: Money | None
    worst_label: str | None
    worst_return: Money | None
    median_return: Money | None
    """All four are null until at least one run finishes. Null is "nothing has landed yet",
    never zero — zero would be a measured result of no profit."""


class StudyOut(_Out):
    """A study read back: how it was launched, the grid it searched, and where it landed."""

    id: uuid.UUID
    strategy_id: uuid.UUID
    strategy_name: str
    """The **base** strategy's name — the lineage the grid was built from. Each point has its
    own stored strategy, named for its own values; those names are on the runs."""

    symbol: str
    timeframe: str
    date_from: dt.datetime
    date_to: dt.datetime
    initial_capital: Money
    created_at: dt.datetime

    grid: dict[str, list[Any]]
    """As declared, in order. Served because it is **not recoverable from the runs**: each
    point's values survive only as text inside its strategy's name, and a client laying out a
    heatmap's axes needs the axes, not a parse of captions."""

    points: list[StudyPointOut]
    """Each run's coordinates on the grid, read back out of the document that ran.

    Parallel to `runs` and in the same order, but it is `points` a heatmap should place cells
    from — a client that derived coordinates by parsing `strategy_name` would be splitting a
    caption on commas and equals signs, and that works until a value contains one.
    """

    aggregate: StudyAggregate
    runs: list[BacktestListItem]


class CreateWalkForwardRequest(BaseModel):
    """Re-run a study honestly: choose the parameters on one window, score them on the next.

    The only thing named here is the study, and that is the design. This experiment exists to
    support one comparison — *"the heatmap said this, a blind choice got that"* — and the
    comparison is only sound if both halves searched the same grid over the same market. Taking
    a grid again would let the two drift apart with nothing able to notice: a retyped grid that
    differs by one value still looks like the same experiment.

    Everything else on the wire is about how the period is cut and how a winner is picked.
    """

    model_config = ConfigDict(extra="forbid")

    study_id: uuid.UUID

    folds: int = Field(default=6, ge=MIN_FOLDS, le=MAX_FOLDS)
    """How many train→test pairs. Six over a couple of years of H1 leaves each test window a
    few hundred bars — enough for a result to mean something, and enough folds to see whether
    the winning parameters stay put."""

    train_multiple: int = Field(default=3, ge=1, le=MAX_FOLDS)
    """How many times longer the training window is than the test window that follows.

    Three is the usual starting point: the choice is made on three times the evidence it is
    then scored on. Raising it buys steadier choices and costs test windows, since the period
    is divided into `train_multiple + folds` parts and the folds get `folds` of them.
    """

    anchored: bool = False
    """`False` (rolling, the default) slides a fixed-length training window forward, so every
    fold chooses from a sample of the same size and the folds are comparable to each other.
    `True` trains on all history before each test window instead. See `walkforward.split`."""

    metric: SelectionMetric = SelectionMetric.NET_PROFIT
    """What each fold maximises when it picks its winner. The result is always reported as a
    *return*, whatever this is set to — selecting by Sharpe and reporting Sharpe would leave a
    reader unable to answer "so what would this have made?"."""


class WalkForwardFoldOut(_Out):
    """One fold: the windows, the choice, and what the choice was worth on each side of it."""

    index: int
    study_id: uuid.UUID
    """The training grid, readable through `GET /studies/{id}` — heatmap included. Fold 1's
    heatmap beside fold 5's is what over-fitting looks like, and no number states it as
    plainly."""

    train_from: dt.datetime
    train_to: dt.datetime
    test_from: dt.datetime
    test_to: dt.datetime
    train_bars: int
    test_bars: int
    """The counts the split was cut from. Served because the boundaries were decided by
    counting candles and expressed as dates: a reader who only saw the dates could not tell an
    even split from one where a holiday week halved a fold's evidence."""

    chosen_label: str | None
    """`period=9, take_profit_rr=3` — the point this fold selected, or null if it could not
    select one. Null is a **result**, not a pending state: it says nothing in the grid traded
    that window, or nothing that traded had a defined score."""

    chosen_strategy_id: uuid.UUID | None
    test_backtest_id: uuid.UUID | None

    in_sample_return: Money | None
    """What the chosen point returned over the window it was chosen on. The promise."""

    out_of_sample_return: Money | None
    """What it returned over the window that followed — which no part of the choice had seen.
    The delivery, and the only figure here that is evidence about a future."""

    test_status: str | None
    test_trades: int | None
    """How many trades the out-of-sample run actually took. Read before believing its return:
    a fold that traded twice has a return, not a finding."""


class WalkForwardVerdict(BaseModel):
    """What the folds add up to. See `walkforward.Verdict` for why each figure is this one."""

    folds_total: int
    folds_decided: int
    folds_scored: int
    folds_profitable: int

    in_sample_median: Money | None
    out_of_sample_median: Money | None
    degradation: Money | None
    """`out_of_sample_median - in_sample_median` — **the headline**. Normally negative; a small
    gap is a method that generalises, a gap that swallows the whole in-sample result is a grid
    that was fitting noise."""

    compounded: Money | None
    """The folds multiplied together, `Π(1 + r) - 1`: what an account would have done trading
    this and re-choosing its parameters every window. An approximation — each run starts from
    the same capital, so this models an account that scales position size with equity."""

    distinct_choices: int
    """How many *different* points the folds chose. 1 is the strongest evidence a grid can
    give; a number near `folds_decided` means there is no "the parameters" to go and trade."""


class CreatedWalkForward(_Out):
    """The 202 body: the walk-forward exists, its folds are cut, and its training runs queued."""

    id: uuid.UUID
    folds: list[WalkForwardFoldOut]
    runs_queued: int
    """`folds x grid points`. Stated back because it is the cost of the request and it grows
    multiplicatively — a fourth axis on the grid multiplies this, and so does a seventh fold."""


class WalkForwardOut(_Out):
    """A walk-forward read back: how it was cut, what each fold decided, and what it adds up to."""

    id: uuid.UUID
    study_id: uuid.UUID
    strategy_id: uuid.UUID
    strategy_name: str
    symbol: str
    timeframe: str
    initial_capital: Money
    grid: dict[str, list[Any]]

    folds_requested: int = Field(validation_alias=AliasChoices("folds_requested", "folds"))
    """Spelled apart from the `folds` list below, which is the same word for a different thing:
    the number asked for, against the rows that were cut. They agree, and a client showing
    "3 of 6 decided" needs both to say so."""

    train_multiple: int
    anchored: bool
    metric: str
    status: str
    error: str | None
    created_at: dt.datetime
    started_at: dt.datetime | None
    finished_at: dt.datetime | None

    folds: list[WalkForwardFoldOut]
    verdict: WalkForwardVerdict
