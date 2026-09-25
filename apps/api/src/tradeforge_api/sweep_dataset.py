"""A sweep as a dataset: one row per run, and a dictionary that says what every column is.

Two readers, and they fail in different ways. A model fed this file needs a table with nothing
ambiguous in it — one row per run, an empty cell never standing in for zero, stored numbers with
the digits they were stored with and derived ratios computed in `Decimal`, never in float. An AI
reading it needs to know what each column *is*: which values were chosen, which were measured,
and which must never be treated as a knob. Without that it optimises a result as if it were a
parameter, and reads an in-sample number as a forecast.

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
from tradeforge_engine.excursion import LADDER

Role = Literal["identity", "choice", "condition", "outcome"]
"""What a column is *for*, which is what a reader most needs and cannot infer from a name.

* `identity` — which row this is: for grouping, splitting and reproducing. Not a feature, except
  `entry_id`, which may stand in for "which method" as a categorical one.
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

_BEFORE_R = "Empty for a run recorded before 25/09/2026, when these were first computed."

ROW = (
    "One grid point of this sweep, over one market: one shelf entry, at one grid point, on one "
    "chart. Each row is its own backtest run unless same_as names the point whose run answers it."
)

CAVEATS: tuple[str, ...] = (
    "Every outcome is in-sample: each run was scored on the same window the sweep searched. The "
    "best row is the best of this many draws, not a forecast; evidence needs a window that no row "
    "was chosen on.",
    "Entries are alternative methods. Do not pool their rows into one average: compare within an "
    "entry, or treat entry_id as a categorical feature.",
    "Rows of one entry share everything but a parameter value, a chart or a market. Split training "
    "and test data by entry_id or by window, never by row, or the model is graded on near-copies "
    "of what it trained on.",
    "Read every other outcome through total_trades: over a handful of trades any ratio is a draw.",
    "A row with same_as is not a measurement of its own: its point differs from the named one only "
    "in a parameter its entry point never reads, so both are one run and every outcome is that "
    "run's. Keep one row per run_id when counting evidence, or one run is counted several times.",
    "An empty cell means not measured, undefined or not applicable — never zero. In a param: "
    "column, empty means the entry does not sweep that path, and `null` means it swept the path "
    "and chose null (the setting off).",
    "A run that failed keeps its row, with its status and error. Dropping it would describe a "
    "space that was never searched.",
)

OMITTED: tuple[tuple[str, str], ...] = (
    (
        "max_dd_duration_days",
        "Truncated to whole days: a drawdown shorter than 24 hours reads 0, and one of 3 days and "
        "23 hours reads 3. On intraday charts most drawdowns read 0, which would say they never "
        "lasted. Left out until it is stored at a finer grain.",
    ),
    (
        "cagr",
        "The engine computes it only over an equity curve spanning at least a year and leaves it "
        "empty otherwise, so on most sweeps it would be an empty column. `return` over the "
        "stated window is the measurement.",
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
        "Too large for a row. Read per run from the API: GET /backtests/{run_id}/equity (once the "
        "run is done) and GET /backtests/{run_id}/trades (paginated).",
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

    same_as: str | None = None
    """The label of the point whose run answers this one, or `None` for a point that ran itself
    (24/09): a row for every point, and this says which rows share a run."""


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


_DONE_ONLY = "Empty unless the run is done."

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
        "The backtest run; its page in the web app is /results/{run_id}.",
        lambda r: r.run.id,
    ),
    Column(
        "same_as",
        "identity",
        "text",
        "Empty when this row's point ran itself. Otherwise the label of the point whose run "
        "answers it: the two differ only in a parameter this entry point never reads, so they are "
        "one run, and run_id and every outcome are that run's.",
        lambda r: r.same_as,
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
        "The engine version stamped on the run when it was launched. Rows stamped with different "
        "versions are not like for like.",
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
        "Candles the run actually read. Weekends and holidays keep it below the calendar; below "
        "another run on the same market and chart means missing data. "
        f"{_DONE_ONLY}",
        lambda r: r.run.candles_seen,
    ),
    Column(
        "first_candle",
        "condition",
        "ISO 8601, UTC",
        f"The first candle read. {_DONE_ONLY}",
        lambda r: r.run.first_candle,
    ),
    Column(
        "last_candle",
        "condition",
        "ISO 8601, UTC",
        f"The last candle read. {_DONE_ONLY}",
        lambda r: r.run.last_candle,
    ),
    Column(
        "status",
        "outcome",
        "queued | running | done | failed",
        "Where the run is. Only `done` rows carry the measured outcomes; `failed` rows carry "
        "`error` instead.",
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
        f"Net profit over the starting capital; 0.025 is 2.5%. The comparable result. {_DONE_ONLY}",
        _return,
    ),
    Column(
        "total_trades",
        "outcome",
        "count",
        f"Closed trades. Read every other outcome through this one. {_DONE_ONLY}",
        _metric("total_trades"),
    ),
    Column(
        "long_trades",
        "outcome",
        "count",
        f"Closed long trades. {_DONE_ONLY}",
        _metric("long_trades"),
    ),
    Column(
        "short_trades",
        "outcome",
        "count",
        f"Closed short trades. {_DONE_ONLY}",
        _metric("short_trades"),
    ),
    Column(
        "win_rate",
        "outcome",
        "fraction of total_trades",
        f"Trades that closed in profit. {_DONE_ONLY} Empty too when the run closed no trade.",
        _win_rate,
    ),
    Column(
        "payoff",
        "outcome",
        "ratio",
        f"Average winning trade over average losing trade. {_DONE_ONLY} Empty too unless the "
        "run has at least one winning and one losing trade.",
        _metric("payoff"),
    ),
    Column(
        "profit_factor",
        "outcome",
        "ratio",
        f"Gross profit over gross loss. {_DONE_ONLY} Empty too when no trade lost; 0 when trades "
        "lost and none won.",
        _metric("profit_factor"),
    ),
    Column(
        "expectancy_per_trade",
        "outcome",
        "fraction of initial_capital per trade",
        f"Average result of one trade over the starting capital. {_DONE_ONLY} Empty too when "
        "the run closed no trade.",
        _expectancy_per_trade,
    ),
    Column(
        "max_drawdown_pct",
        "outcome",
        "fraction of peak equity",
        f"The deepest fall from a running peak, relative to that peak. {_DONE_ONLY}",
        _metric("max_drawdown_pct"),
    ),
    Column(
        "net_r",
        "outcome",
        "R",
        "Sum of every trade's result in R: net profit over the risk its stop defined, after costs "
        f"and swap. Trades with no stop are left out. {_DONE_ONLY} {_BEFORE_R}",
        _metric("net_r"),
    ),
    Column(
        "max_drawdown_r",
        "outcome",
        "R",
        "The deepest fall of the cumulative R from its running peak (the peak starts at 0). "
        f"Comparable across runs and windows, unlike max_drawdown_pct. {_DONE_ONLY} {_BEFORE_R}",
        _metric("max_drawdown_r"),
    ),
    Column(
        "losing_streak",
        "outcome",
        "count",
        f"The most consecutive losing trades; a break-even trade ends a streak. {_DONE_ONLY} "
        f"{_BEFORE_R}",
        _metric("losing_streak"),
    ),
    Column(
        "losing_streak_r",
        "outcome",
        "R",
        "The deepest run of consecutive losing trades, summed in R (zero or negative). May be a "
        f"different streak from the longest. {_DONE_ONLY} {_BEFORE_R}",
        _metric("losing_streak_r"),
    ),
    Column(
        "positive_year_share",
        "outcome",
        "fraction of years with a trade",
        "Calendar years (by entry) that ended above zero R, over the years that had a trade. "
        f"{_DONE_ONLY} Empty too with fewer than two such years. {_BEFORE_R}",
        _metric("positive_year_share"),
    ),
    Column(
        "sharpe",
        "outcome",
        "ratio of per-trade returns, not annualised",
        "Mean per-trade return (net P&L over initial_capital) over its sample standard deviation. "
        "Not annualised: do not read it against annual benchmarks. "
        f"{_DONE_ONLY} Empty too with fewer than two trades, or when every trade returned the "
        "same.",
        _metric("sharpe"),
    ),
    Column(
        "sortino",
        "outcome",
        "ratio of per-trade returns, not annualised",
        "Mean per-trade return over the downside deviation (squared losing returns, summed and "
        "divided by n-1 over all trades). Not annualised. "
        f"{_DONE_ONLY} Empty too with fewer than two trades, or when no trade lost.",
        _metric("sortino"),
    ),
    Column(
        "avg_trade_duration_seconds",
        "outcome",
        "seconds",
        f"How long a trade stayed open, on average. {_DONE_ONLY} Empty too when the run closed "
        "no trade.",
        _average_duration,
    ),
)


def _rung_key(rung: Decimal) -> str:
    """The rung as `backtest_metrics.targets` keys it: `2`, not `2.0`; `0.5` as it is."""
    return format(rung.normalize(), "f")


def _rung_field(key: str, field: str) -> Callable[[DatasetRun], object]:
    def read(row: DatasetRun) -> object:
        metrics = row.run.metrics
        ladder = None if metrics is None else metrics.targets
        outcome = None if ladder is None else ladder.get(key)
        if outcome is None:
            return None
        value = outcome[field]
        # The document keeps the R figures as text, for their precision; the file writes numbers.
        return Decimal(value) if isinstance(value, str) else value

    return read


_RUNG_FIELDS: tuple[tuple[str, str, str], ...] = (
    (
        "net_r",
        "R, net of costs and swap",
        "What the run's trades would have made with this target instead of their own exit, "
        "summed: each trade scored alone, in R of its own risk, less its own costs and swap in R.",
    ),
    (
        "expectancy_r",
        "R, net of costs and swap",
        "net_r over trades: what one trade was worth on average.",
    ),
    ("hits", "count", "How many of the trades this target would have closed."),
    (
        "trades",
        "count",
        "The trades the rung was scored over — every trade of the run, or the rung is empty.",
    ),
    (
        "max_drawdown_r",
        "R",
        "The deepest fall of the running sum of net R from its peak. 0 for one that never fell.",
    ),
)

_LADDER_NOTE = (
    "Scored per trade while the run's trades were in memory (2026-09-23), so it exists for every "
    "run, the ones that kept no trades included. ⚠️ Per trade, not per run: a real run with this "
    "target frees its position earlier and can take trades this one never saw (measured on "
    "24/09: the median net R did not move, the sign changed on 0-4% of runs). Empty when the run "
    "is not done, or when some trade cannot answer the rung: no stop, not measured, or a target of "
    "its own closer than this one."
)

_LADDER: tuple[Column, ...] = tuple(
    Column(
        f"target_{_rung_key(rung)}r_{field}",
        "outcome",
        unit,
        f"At a {_rung_key(rung)} R target. {description} {_LADDER_NOTE}",
        _rung_field(_rung_key(rung), field),
    )
    for rung in LADDER
    for field, unit, description in _RUNG_FIELDS
)
"""The target ladder, one column per rung and figure — what the ML plan reads to choose a target
without a run per target (his plan, 23/09). Built from the engine's `LADDER`, so a rung added there
is a column here."""


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
    return [*_BEFORE_PARAMS, *grid, *_AFTER_PARAMS, *_LADDER]


def cell(value: object) -> str:
    """One value as the exact text a reader should parse.

    ⚠️ `None` is the only thing that becomes an empty cell. `bool` is checked first because
    `str(True)` is `True` and this file writes `true`. A timestamp is converted to UTC, so the
    dictionary's unit holds whatever timezone the database session happened to use. A `Decimal` is
    written in fixed notation: `str` would turn a stored zero into `0E-8`, which parses but puts
    two spellings of one kind of number in one column.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, dt.datetime):
        return (value.astimezone(dt.UTC) if value.tzinfo is not None else value).isoformat()
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
