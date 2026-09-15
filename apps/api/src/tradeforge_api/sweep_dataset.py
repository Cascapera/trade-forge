"""A sweep as a dataset: one row per run, and a dictionary that says what every column is.

Two readers, and they fail in different ways. A model fed this file needs a table with nothing
ambiguous in it — one row per run, an empty cell never standing in for zero, numbers exactly as
stored. An AI reading it needs to know what each column *is*: which values were chosen, which
were measured, and which must never be treated as a knob. Without that it optimises a result as
if it were a parameter, and reads an in-sample number as a forecast.

⚠️ **The file and its dictionary come from one list, `columns_for`.** A dictionary written by hand
beside the file describes yesterday's file the first time a column is added. Generated from the
definitions that also write the header, the two cannot disagree — a column without a description
is a column that does not exist.

⚠️ **Cells are not escaped against spreadsheet formulas, on purpose.** A cell starting with `=`
runs as a formula when the file is opened in Excel, and entry names are free text. The usual
guard prefixes such cells with `'`, which corrupts the value for pandas and for an AI — the two
readers this file exists for — while the only author of those names is the person downloading.
"""

import csv
import datetime as dt
import io
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from tradeforge_db.models import Backtest

Role = Literal["identity", "choice", "condition", "outcome"]
"""What a column is *for*, which is what a reader most needs and cannot infer from a name.

* `identity` — which row this is. For grouping and reproducing, never a feature.
* `choice` — what was decided before the run: the market, the chart, the grid's values.
* `condition` — under what the run was measured: the window, the capital, the costs.
* `outcome` — what the run produced. The thing to explain, never an input to explain it with.
"""

PARAM_PREFIX = "param:"
"""Grid columns are named `param:<dotted path>`, so they cannot collide with a fixed column and a
reader can pick every chosen parameter out with one prefix."""

CHOSE_NULL = "null"
"""What a grid column holds when the entry swept the path and **chose** null — a setting switched
off, like `breakeven_at_r: null`.

⚠️ Not an empty cell, because an empty cell in a grid column already means "this entry does not
sweep this path, and its document's own value applied". Writing both as empty would tell a model
that "no break-even" and "break-even at 2R" are the same run."""

ROW = (
    "One backtest run of this sweep: one shelf entry, at one grid point, on one chart, "
    "over one market."
)

CAVEATS: tuple[str, ...] = (
    "Every outcome is in-sample: each run was scored on the same window the sweep searched. The "
    "best row is the best of this many draws, not a forecast; evidence needs a window that no row "
    "was chosen on.",
    "Entries are alternative methods. Do not pool their rows into one average: compare within an "
    "entry, or treat entry_id as a feature.",
    "Rows of one entry share everything but a parameter value, a chart or a market. Split training "
    "and test data by entry_id or by window, never by row, or the model is graded on near-copies "
    "of what it trained on.",
    "Read every other outcome through total_trades: over a handful of trades any ratio is a draw.",
    "An empty cell means not yet measured, undefined or not applicable — never zero. In a param: "
    "column, empty means the entry does not sweep that path, and `null` means it swept the path "
    "and chose null (the setting off).",
    "A run that failed keeps its row, with its status and error. Dropping it would describe a "
    "space that was never searched.",
)

OMITTED: tuple[tuple[str, str], ...] = (
    (
        "max_dd_duration_days",
        "Stored in whole days, so every intraday drawdown reads 0 — which would say the drawdown "
        "never lasted. Left out until it is stored at a finer grain.",
    ),
    (
        "cagr",
        "Annualises the window. Over a window of weeks that is an extrapolation of a few trades "
        "to a year, not a measurement.",
    ),
    (
        "net_profit, gross_profit, gross_loss, max_drawdown_abs, expectancy",
        "In account currency, so they scale with the capital. `return` and "
        "`expectancy_per_trade` carry the result as fractions of the starting capital, comparable "
        "across sweeps. `max_drawdown_pct` is the deepest fall relative to its own peak, which "
        "need not be the deepest fall in money.",
    ),
    (
        "equity_curve, trades",
        "Too large for a row. Read per run from /backtests/{run_id}/equity and "
        "/backtests/{run_id}/trades.",
    ),
)


@dataclass(frozen=True, slots=True)
class DatasetRun:
    """One run, joined to everything its row is read from."""

    sweep_id: str
    run: Backtest
    entry_id: str
    entry_name: str | None
    """Null once the entry has been removed from the shelf: the row survives, the label does
    not."""

    strategy_version: int
    symbol: str
    asset_class: str
    values: Mapping[str, Any]
    """The coordinates written at launch: the grid's dotted paths, plus `timeframe`."""


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    role: Role
    unit: str
    description: str
    read: Callable[[DatasetRun], object]


def _metric(name: str) -> Callable[[DatasetRun], object]:
    def read(row: DatasetRun) -> object:
        metrics = row.run.metrics
        return None if metrics is None else getattr(metrics, name)

    return read


def _return(row: DatasetRun) -> object:
    metrics = row.run.metrics
    return None if metrics is None else metrics.net_profit / row.run.initial_capital


def _win_rate(row: DatasetRun) -> object:
    """Empty for a run that closed no trade.

    ⚠️ The engine stores `0` there — the column is NOT NULL — and a 0 in this file reads as a
    measured 0% win rate. With no trade there is no rate to measure.
    """
    metrics = row.run.metrics
    if metrics is None or metrics.total_trades == 0:
        return None
    return metrics.win_rate


def _expectancy_per_trade(row: DatasetRun) -> object:
    metrics = row.run.metrics
    if metrics is None or metrics.expectancy is None:
        return None
    return metrics.expectancy / row.run.initial_capital


def _average_duration(row: DatasetRun) -> object:
    metrics = row.run.metrics
    if metrics is None or metrics.avg_trade_duration is None:
        return None
    return metrics.avg_trade_duration // dt.timedelta(seconds=1)


_UNFINISHED = "Empty until the run finishes."

_BEFORE_PARAMS: tuple[Column, ...] = (
    Column(
        "sweep_id",
        "identity",
        "id",
        "The sweep this row belongs to.",
        lambda r: r.sweep_id,
    ),
    Column(
        "run_id",
        "identity",
        "id",
        "The backtest run; its page is /results/{run_id}.",
        lambda r: r.run.id,
    ),
    Column(
        "entry_id",
        "identity",
        "id",
        "The shelf entry the run came from. Runs of one entry are near-duplicates of each other.",
        lambda r: r.entry_id,
    ),
    Column(
        "entry_name",
        "identity",
        "text",
        "The label on the shelf. Empty once the entry has been removed from it.",
        lambda r: r.entry_name,
    ),
    Column(
        "strategy_id",
        "identity",
        "id",
        "The exact strategy document that ran — one per grid point and chart.",
        lambda r: r.run.strategy_id,
    ),
    Column(
        "strategy_version",
        "identity",
        "integer",
        "That document's version.",
        lambda r: r.strategy_version,
    ),
    Column(
        "engine_version",
        "identity",
        "text",
        "The engine that produced the outcomes. Rows from different engines are not like for like.",
        lambda r: r.run.engine_version,
    ),
    Column(
        "symbol",
        "choice",
        "text",
        "The market the run traded.",
        lambda r: r.symbol,
    ),
    Column(
        "asset_class",
        "choice",
        "asset class code, e.g. forex",
        "What kind of market that is.",
        lambda r: r.asset_class,
    ),
    Column(
        "timeframe",
        "choice",
        "timeframe code, e.g. M15 or H1",
        "The chart the run stepped on, which is also the one written into its document.",
        lambda r: r.run.timeframe,
    ),
)

_AFTER_PARAMS: tuple[Column, ...] = (
    Column(
        "date_from",
        "condition",
        "ISO 8601, UTC",
        "Start of the window the run read.",
        lambda r: r.run.date_from,
    ),
    Column(
        "date_to",
        "condition",
        "ISO 8601, UTC",
        "End of the window the run read.",
        lambda r: r.run.date_to,
    ),
    Column(
        "initial_capital",
        "condition",
        "account currency",
        "The capital every run of the sweep starts with.",
        lambda r: r.run.initial_capital,
    ),
    Column(
        "cost_model",
        "condition",
        "JSON",
        'What the run was charged to trade, as launched. {"type":"none"} means costless, which '
        "flatters every outcome.",
        lambda r: r.run.cost_model,
    ),
    Column(
        "candles_seen",
        "condition",
        "count",
        "Candles the run actually read — fewer than the window holds when data is missing. "
        f"{_UNFINISHED}",
        lambda r: r.run.candles_seen,
    ),
    Column(
        "first_candle",
        "condition",
        "ISO 8601, UTC",
        f"The first candle read. {_UNFINISHED}",
        lambda r: r.run.first_candle,
    ),
    Column(
        "last_candle",
        "condition",
        "ISO 8601, UTC",
        f"The last candle read. {_UNFINISHED}",
        lambda r: r.run.last_candle,
    ),
    Column(
        "status",
        "outcome",
        "queued | running | done | failed",
        "Where the run is. Only `done` rows carry the outcomes below.",
        lambda r: r.run.status,
    ),
    Column(
        "error",
        "outcome",
        "text",
        "Why a failed run failed. Empty otherwise.",
        lambda r: r.run.error,
    ),
    Column(
        "return",
        "outcome",
        "fraction of initial_capital",
        "Net profit over the starting capital; 0.025 is 2.5%. The comparable result. "
        f"{_UNFINISHED}",
        _return,
    ),
    Column(
        "total_trades",
        "outcome",
        "count",
        f"Closed trades. Read every other outcome through this one. {_UNFINISHED}",
        _metric("total_trades"),
    ),
    Column(
        "long_trades",
        "outcome",
        "count",
        f"Closed long trades. {_UNFINISHED}",
        _metric("long_trades"),
    ),
    Column(
        "short_trades",
        "outcome",
        "count",
        f"Closed short trades. {_UNFINISHED}",
        _metric("short_trades"),
    ),
    Column(
        "win_rate",
        "outcome",
        "fraction of total_trades",
        f"Trades that closed in profit. {_UNFINISHED} Empty too when the run closed no trade.",
        _win_rate,
    ),
    Column(
        "payoff",
        "outcome",
        "ratio",
        f"Average winning trade over average losing trade. {_UNFINISHED} Empty too unless the "
        "run has at least one winning and one losing trade.",
        _metric("payoff"),
    ),
    Column(
        "profit_factor",
        "outcome",
        "ratio",
        f"Gross profit over gross loss. {_UNFINISHED} Empty too when no trade lost; 0 when trades "
        "lost and none won.",
        _metric("profit_factor"),
    ),
    Column(
        "expectancy_per_trade",
        "outcome",
        "fraction of initial_capital per trade",
        f"Average result of one trade over the starting capital. {_UNFINISHED} Empty too when "
        "the run closed no trade.",
        _expectancy_per_trade,
    ),
    Column(
        "max_drawdown_pct",
        "outcome",
        "fraction of peak equity",
        f"The deepest fall from a running peak, relative to that peak. {_UNFINISHED}",
        _metric("max_drawdown_pct"),
    ),
    Column(
        "sharpe",
        "outcome",
        "ratio of per-trade returns, not annualised",
        "Mean per-trade return (net P&L over initial_capital) over its sample standard deviation. "
        "Not annualised: do not read it against annual benchmarks. "
        f"{_UNFINISHED} Empty too with fewer than two trades, or when every trade returned the "
        "same.",
        _metric("sharpe"),
    ),
    Column(
        "sortino",
        "outcome",
        "ratio of per-trade returns, not annualised",
        "Mean per-trade return over the downside deviation (squared losing returns, summed and "
        "divided by n-1 over all trades). Not annualised. "
        f"{_UNFINISHED} Empty too with fewer than two trades, or when no trade lost.",
        _metric("sortino"),
    ),
    Column(
        "avg_trade_duration_seconds",
        "outcome",
        "seconds",
        f"How long a trade stayed open, on average. {_UNFINISHED} Empty too when the run closed "
        "no trade.",
        _average_duration,
    ),
)


def _value_at(path: str) -> Callable[[DatasetRun], object]:
    def read(row: DatasetRun) -> object:
        if path not in row.values:
            return None
        value = row.values[path]
        return CHOSE_NULL if value is None else value

    return read


def columns_for(runs: Sequence[DatasetRun]) -> list[Column]:
    """Every column of this sweep's dataset, in file order.

    The grid columns are the union of every entry's axes, sorted, so the same sweep always yields
    the same header. `timeframe` is a coordinate too but has its own fixed column, and a second
    copy under `param:` would be two names for one fact.
    """
    paths = sorted({path for row in runs for path in row.values if path != "timeframe"})
    grid = [
        Column(
            f"{PARAM_PREFIX}{path}",
            "choice",
            "as in the strategy document",
            f"The value this run's entry swept at `{path}`. `{CHOSE_NULL}` when it swept the path "
            "and chose null (the setting off). Empty when the entry does not sweep this path: its "
            "document's own value applied, or the path does not exist for its setup.",
            _value_at(path),
        )
        for path in paths
    ]
    return [*_BEFORE_PARAMS, *grid, *_AFTER_PARAMS]


def cell(value: object) -> str:
    """One value as the exact text a reader should parse.

    ⚠️ `None` is the only thing that becomes an empty cell. `bool` is checked before anything
    numeric because it *is* an `int` in Python, and `True` would otherwise print as `True`. A
    `Decimal` is written in fixed notation: `str` would turn a stored zero into `0E-8`, which
    parses but puts two spellings of one kind of number in one column.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return format(value, "f") if isinstance(value, Decimal) else str(value)


def to_csv(runs: Sequence[DatasetRun]) -> str:
    """The dataset as CSV: a header from `columns_for`, then one line per run, in given order."""
    columns = columns_for(runs)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([column.name for column in columns])
    for row in runs:
        writer.writerow([cell(column.read(row)) for column in columns])
    return buffer.getvalue()
