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
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator

# A Decimal that always serialises to a string. Applied to every monetary/ratio field below.
Money = Annotated[Decimal, PlainSerializer(str, return_type=str)]


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


class CreateBacktestRequest(BaseModel):
    """What a client sends to enqueue a backtest.

    `symbol` (not an instrument id) because that is what a human names; the API resolves it to
    the instrument row. `cost_model` is a document, not a column — the same plugged-in shape
    the engine consumes (`{"type": "spread", "spread_points": 10}`, `{"type": "none"}`), stored
    per run so "the same backtest with a wider spread" is a different, comparable row.
    """

    model_config = ConfigDict(extra="forbid")

    strategy_id: uuid.UUID
    symbol: str
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
    snapshot: SnapshotOut | None = None
    """The entry's picture, or `None` for a trade whose run recorded none.

    `{}` in the column means exactly that — a run from before this existed, or a strategy
    driven without a window — so it is normalised to `None` here rather than reaching a client
    as an object with no bars, which a chart would try to draw."""

    @field_validator("snapshot", mode="before")
    @classmethod
    def _empty_snapshot_is_none(cls, value: object) -> object:
        """`{}` is the column's NOT NULL default and means "no window was recorded".

        Validated as-is it would fail on every row written before `rev_0003` — the endpoint
        would 500 on historical backtests rather than serving them without a chart.
        """
        return None if value == {} else value


class TradesPage(BaseModel):
    """A page of trades. `total` lets a client size the pager without walking every page."""

    total: int
    limit: int
    offset: int
    items: list[TradeOut]


class EquityPointOut(_Out):
    time: dt.datetime
    equity: Money


class CreatedBacktest(_Out):
    """The 202 body: the run exists and is queued; poll `id` or subscribe to its WebSocket."""

    id: uuid.UUID
    status: str
