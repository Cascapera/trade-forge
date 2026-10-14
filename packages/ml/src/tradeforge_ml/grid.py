"""The grid's report (ADR-0031, 14/10): each entry of a base under every stop and target, and the
question his idea asks of it — **is there a fixed stop and target, per setup and market, chosen on
the training years, that makes money on years it never saw?**

No model: this is the bar a model choosing a cell for each entry will have to clear. Four rules,
each chosen on the training years and judged on validation:

1. `every entry` — the setup's stop and no target: the base as the meta-label read it;
2. `markets that paid` — the same, only on the markets where it ended the training years positive;
3. `one cell` — the stop and target that made the most R over the whole base;
4. `a cell per market` — each market's own, among the cells with `min_trades` trades in its
   training years; a market none of whose cells ended positive is not traded.

A market is a setup on a symbol and a chart (`entry_id`, `symbol`, `timeframe`). Each rule is
summed as one account would book it: every trade at the same risk, in the order they closed — the
drawdown and the longest run of losses are that account's.

⚠️ **Every cell is judged on the same entries.** An entry one of whose stops was still open when
the bars ran out (`open`) is left out of every cell; else a wide stop would be the only one missing
its last trades. An entry a stop refused, or whose stop sits below the floor, is not taken under
it: no trade, which is what a trader with that stop would have had.

⚠️ **R is the same money under every stop.** With a fixed risk per trade, 1R is one risk whatever
the stop's distance; what a tighter stop changes is how much of it the spread takes — which the
floor bounds, cell by cell.

⚠️ **Validation is not a test** (`model`): seventy-two cells per market were compared on the
training years, and the choice flatters whatever it is judged on next. The last year stays unseen.

Pure: one Arrow table in, a report out. `cli` reads and writes the files.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.compute as pc

from tradeforge_engine.excursion import LADDER
from tradeforge_ml.feature_export import KEY
from tradeforge_ml.model import Floor, Market, Split, _us

TARGETS: tuple[float | None, ...] = (None, *(float(rung) for rung in LADDER))
"""Each stop's targets, in R of its own risk: none — the trade as it ran, out at its stop or at the
time barrier — then the engine's ladder (`excursion.LADDER`), read off each stop's excursion."""

MIN_TRADES = 30
"""Trades a cell needs in a market's training years to be chosen for it."""

REASONS = ("sl", "time", "refused", "open")
"""How a cell ended (`replay.Cell`), by code: its position here."""

Floats = npt.NDArray[np.float64]
Ints = npt.NDArray[np.int64]
Bools = npt.NDArray[np.bool_]


def cell_name(stop: float, target: float | None) -> str:
    """`2x 3R`, `1x as run`."""
    return f"{stop:g}x {'as run' if target is None else f'{target:g}R'}"


@dataclass
class GridRows:
    """A base's grid as arrays: one row per entry, one column per stop of `stops`."""

    stops: tuple[float, ...]
    markets: list[str]
    """`entry_id symbol timeframe` — a setup on a chart; `market` holds positions in it."""
    market: Ints
    entry_us: Ints
    exit_us: Ints
    """Per entry and stop; -1 without a trade."""
    reason: npt.NDArray[np.int8]
    """Per entry and stop, a position in `REASONS`."""
    r: Floats
    """Per entry and stop, net of costs and swap; NaN without a trade."""
    mfe_r: Floats
    cost_r: Floats
    """What each trade paid, in R: its result as a price ratio less its R in money."""
    risk_ticks: Floats | None = None
    """The stop's distance in ticks of the entry's day; known when the markets were given."""
    paid_r: Floats | None = None
    """The costs alone (spread and commission, no swap), in R; as `risk_ticks`."""

    def take(self, mask: Bools) -> GridRows:
        return GridRows(
            self.stops,
            self.markets,
            self.market[mask],
            self.entry_us[mask],
            self.exit_us[mask],
            self.reason[mask],
            self.r[mask],
            self.mfe_r[mask],
            self.cost_r[mask],
            None if self.risk_ticks is None else self.risk_ticks[mask],
            None if self.paid_r is None else self.paid_r[mask],
        )

    @property
    def year(self) -> Ints:
        return (
            self.entry_us.astype("datetime64[us]").astype("datetime64[Y]").astype(np.int64) + 1970
        )


def _floats(column: pa.ChunkedArray | pa.Array) -> Floats:
    cast = pc.cast(column, pa.float64())
    return np.asarray(pc.fill_null(cast, math.nan).to_numpy(), dtype=np.float64)


def _instants(column: pa.ChunkedArray | pa.Array) -> Ints:
    cast = pc.cast(column, pa.timestamp("us", tz="UTC")).cast(pa.int64())
    return np.asarray(pc.fill_null(cast, -1).to_numpy(), dtype=np.int64)


def _encoded(column: pa.ChunkedArray) -> tuple[Ints, list[str]]:
    """A string column as positions in its distinct values, in the order first seen."""
    encoded = column.combine_chunks().dictionary_encode()
    codes = np.asarray(encoded.indices.to_numpy(zero_copy_only=False), dtype=np.int64)
    return codes, [str(value) for value in encoded.dictionary.to_pylist()]


def grid_rows(grid: pa.Table, markets: Mapping[str, Market] | None = None) -> GridRows:
    """`grid.parquet` as arrays, refusing a file that is not one row per entry and stop in order;
    with the `markets`, each stop's size in ticks and spreads too (`taken`).

    In Arrow throughout: a base of five million entries is thirty million rows, too many to turn
    into Python strings."""
    multiples = _floats(grid.column("stop_multiple"))
    stops = tuple(dict.fromkeys(multiples.tolist()))
    width = len(stops)
    if width == 0 or grid.num_rows % width:
        raise ValueError("grid.parquet is not one row per entry and stop")
    count = grid.num_rows // width
    if not np.array_equal(multiples, np.tile(np.asarray(stops), count)):
        raise ValueError("grid.parquet is not one row per entry and stop, in the grid's order")
    firsts = np.arange(count, dtype=np.int64) * width
    spread = pa.array(np.repeat(firsts, width))
    for name in KEY:
        column = grid.column(name)
        if not pc.all(pc.equal(column, pc.take(column, spread))).as_py():
            raise ValueError(f"the rows of an entry disagree on {name}: not one entry per block")
    heads = grid.take(pa.array(firsts))
    market, labels = _encoded(
        pc.binary_join_element_wise(
            *(heads.column(name) for name in ("entry_id", "symbol", "timeframe")), " "
        )
    )
    reason = pc.index_in(grid.column("exit_reason"), value_set=pa.array(REASONS))
    if reason.null_count:
        raise ValueError(f"a cell's outcome is not one of {REASONS}")
    entry_us = _instants(heads.column("entry_time"))

    def matrix(name: str) -> Floats:
        return _floats(grid.column(name)).reshape(count, width)

    entry, exit_, stop, r = (
        matrix(name) for name in ("entry_price", "exit_price", "stop_loss", "r_multiple")
    )
    sign = np.where(pc.equal(heads.column("side"), "long").to_numpy(zero_copy_only=False), 1, -1)
    distance = np.abs(entry - stop)
    with np.errstate(divide="ignore", invalid="ignore"):
        gross_r = sign[:, None] * (exit_ - entry) / distance
    risk_ticks = paid_r = None
    if markets is not None:
        symbol, symbols = _encoded(heads.column("symbol"))
        unknown = sorted(set(symbols) - set(markets))
        if unknown:
            raise ValueError(f"no tick for {', '.join(unknown)}: no floor to read")
        tick_size, tick_value, tick_of_the_day = (np.empty(count) for _ in range(3))
        for code, name in enumerate(symbols):
            mask, chosen = symbol == code, markets[name]
            tick_size[mask], tick_value[mask] = chosen.tick_size, chosen.tick_value
            tick_of_the_day[mask] = chosen.ticks_of_the_day(entry_us[mask])
        risk_ticks = distance / tick_of_the_day[:, None]
        # The engine's own money: a price move over the tick, times the tick's value and the lots.
        at_risk = distance / tick_size[:, None] * tick_value[:, None] * matrix("volume")
        with np.errstate(divide="ignore", invalid="ignore"):
            paid_r = matrix("costs") / at_risk
    return GridRows(
        stops=stops,
        markets=labels,
        market=market,
        entry_us=entry_us,
        exit_us=_instants(grid.column("exit_time")).reshape(count, width),
        reason=np.asarray(reason.to_numpy(zero_copy_only=False), dtype=np.int8).reshape(
            count, width
        ),
        r=r,
        mfe_r=matrix("mfe_r"),
        cost_r=gross_r - r,
        risk_ticks=risk_ticks,
        paid_r=paid_r,
    )


def taken(rows: GridRows, floor: Floor | None) -> Bools:
    """Per entry and stop: a trade, and — with a `floor` — a stop at least `min_ticks` ticks of
    its day and `min_spreads` spreads away (`model.executable`, cell by cell)."""
    traded = ~np.isnan(rows.r)
    if floor is None:
        return traded
    if rows.risk_ticks is None or rows.paid_r is None:
        raise ValueError("the stop floor needs each market's tick (grid_rows with markets)")
    with np.errstate(invalid="ignore"):
        able = (rows.risk_ticks >= floor.min_ticks * (1 - 1e-9)) & (
            rows.paid_r * floor.min_spreads <= 1 + 1e-9
        )
    return traded & able


def outcome(rows: GridRows, target: float | None, kept: Bools) -> Floats:
    """Per entry and stop, its R under `target` (`model.with_target`, stop by stop): a trade whose
    excursion reached it closes there, less what it paid; NaN where none was `kept`."""
    r = rows.r if target is None else np.where(rows.mfe_r >= target, target - rows.cost_r, rows.r)
    return np.where(kept, r, np.nan)


@dataclass
class Summary:
    """What a set of trades made, booked at one risk each in the order they closed."""

    trades: int
    win_rate: float
    mean_r: float
    total_r: float
    max_drawdown_r: float
    """The deepest fall of the running sum from its peak (starting at 0), in R."""
    longest_losing_streak: int


EMPTY = Summary(0, 0.0, 0.0, 0.0, 0.0, 0)


def summarise(r: Floats, exit_us: Ints) -> Summary:
    """`r` booked in the order of `exit_us`; a NaN — no trade — is left out."""
    kept = ~np.isnan(r)
    if not kept.any():
        return EMPTY
    realised = r[kept][np.argsort(exit_us[kept], kind="stable")]
    equity = np.cumsum(realised)
    peak = np.maximum.accumulate(np.concatenate(([0.0], equity)))[1:]
    losing = np.concatenate(([0], (realised < 0).astype(np.int8), [0]))
    edges = np.flatnonzero(np.diff(losing))
    return Summary(
        trades=len(realised),
        win_rate=float((realised > 0).mean()),
        mean_r=float(realised.mean()),
        total_r=float(realised.sum()),
        max_drawdown_r=float(np.max(peak - equity)),
        longest_losing_streak=int(np.max(edges[1::2] - edges[::2], initial=0)),
    )


@dataclass
class Rule:
    """One way of trading the base, chosen on training and judged on validation."""

    name: str
    training: Summary
    """⚠️ Where the choice was made: flattering by construction."""
    validation: Summary
    by_year: dict[int, Summary] = field(default_factory=dict)
    """Validation, year by year."""


@dataclass
class MarketChoice:
    """What `a cell per market` chose for one market, and what it then made."""

    market: str
    cell: str | None
    """The chosen stop and target (`cell_name`); `None` — no cell ended its training positive."""
    training: Summary
    validation: Summary
    as_run_training: Summary
    as_run_validation: Summary
    """The setup's own stop and no target, on the same entries: what the choice is against."""


@dataclass
class GridReport:
    """What the grid made, in the terms his idea asks: total R, the fall, the run of losses."""

    stops: list[float]
    targets: list[float | None]
    min_trades: int
    entries: int
    left_open: int
    """Entries left out of every cell: one of their stops was still open when the bars ran out."""
    training_entries: int
    embargoed: int
    validation_entries: int
    floor: Floor | None = None
    below_floor: dict[str, int] = field(default_factory=dict)
    """Per stop, the trades the floor left out, training and validation together."""
    refused: dict[str, int] = field(default_factory=dict)
    """Per stop, the entries the broker refused with it, training and validation together."""
    training_cells: dict[str, Summary] = field(default_factory=dict)
    validation_cells: dict[str, Summary] = field(default_factory=dict)
    best_cell: str = ""
    rules: list[Rule] = field(default_factory=list)
    markets: list[MarketChoice] = field(default_factory=list)
    """Every market, the best of its training years first."""


def _split(rows: GridRows, plan: Split) -> tuple[Bools, Bools, int]:
    """Training and validation entries, and how many training ones the embargo dropped: an entry
    whose longest-lived stop closed after validation began."""
    start, end = _us(plan.validation_from), _us(plan.validation_to)
    before = rows.entry_us < start
    embargoed = before & (rows.exit_us.max(axis=1) >= start)
    training = before & ~embargoed
    for left, right in plan.excluded:
        training &= ~((rows.entry_us >= _us(left)) & (rows.entry_us < _us(right)))
    validation = (rows.entry_us >= start) & (rows.entry_us < end)
    return training, validation, int(embargoed.sum())


def evaluate(  # noqa: PLR0915 — one pass over the rules the module docstring lists
    rows: GridRows,
    plan: Split,
    floor: Floor | None = None,
    min_trades: int = MIN_TRADES,
    targets: Sequence[float | None] = TARGETS,
) -> GridReport:
    """Every cell on the training and validation entries, and the four rules.

    Two passes over the targets, one stop-by-entry matrix at a time — a base of five million
    entries would not hold all seventy-two cells at once: the first scores every cell and every
    market, the second keeps what the chosen cells made."""
    if 1.0 not in rows.stops or None not in targets:
        raise ValueError("the grid must hold the setup's own stop (1) and no target (None)")
    left_open = (rows.reason == REASONS.index("open")).any(axis=1)
    kept = rows.take(~left_open)
    training, validation, embargoed = _split(kept, plan)
    if not training.any() or not validation.any():
        raise ValueError("no training or no validation entries for these dates")
    able = taken(kept, floor)
    seen = training | validation
    traded = ~np.isnan(kept.r)
    width, groups = len(kept.stops), len(kept.markets)
    report = GridReport(
        stops=list(kept.stops),
        targets=list(targets),
        min_trades=min_trades,
        entries=len(rows.entry_us),
        left_open=int(left_open.sum()),
        training_entries=int(training.sum()),
        embargoed=embargoed,
        validation_entries=int(validation.sum()),
        floor=floor,
        below_floor={
            f"{stop:g}": int((traded[seen, n] & ~able[seen, n]).sum())
            for n, stop in enumerate(kept.stops)
        },
        refused={
            f"{stop:g}": int((kept.reason[seen, n] == REASONS.index("refused")).sum())
            for n, stop in enumerate(kept.stops)
        },
    )
    cells = [(target, column) for target in targets for column in range(width)]
    own = cells.index((None, kept.stops.index(1.0)))

    # First pass: every cell over the base, and per market its training trades and total.
    trades = np.zeros((len(cells), groups), dtype=np.int64)
    totals = np.zeros((len(cells), groups))
    for target in targets:
        values = outcome(kept, target, able)
        for column, stop in enumerate(kept.stops):
            n = cells.index((target, column))
            name = cell_name(stop, target)
            exits = kept.exit_us[:, column]
            report.training_cells[name] = summarise(values[training, column], exits[training])
            report.validation_cells[name] = summarise(values[validation, column], exits[validation])
            mask = training & ~np.isnan(values[:, column])
            trades[n] = np.bincount(kept.market[mask], minlength=groups)
            totals[n] = np.bincount(
                kept.market[mask], weights=values[mask, column], minlength=groups
            )

    def named(n: int) -> str:
        target, column = cells[n]
        return cell_name(kept.stops[column], target)

    best = int(np.argmax([report.training_cells[named(n)].total_r for n in range(len(cells))]))
    report.best_cell = named(best)
    eligible = np.where(trades >= min_trades, totals, -np.inf)
    chosen = np.argmax(eligible, axis=0)
    paid = eligible[chosen, np.arange(groups)] > 0
    as_run_paid = eligible[own] > 0

    # Second pass: per entry, what the chosen cells made.
    picks = {"as run": own, "best": best}
    picked = {key: np.full(len(kept.entry_us), np.nan) for key in (*picks, "per market")}
    exited = {key: np.full(len(kept.entry_us), -1, dtype=np.int64) for key in picked}
    for target in targets:
        values = outcome(kept, target, able)
        for key, n in picks.items():
            if cells[n][0] == target:
                column = cells[n][1]
                picked[key], exited[key] = values[:, column].copy(), kept.exit_us[:, column]
        of_this_target = [cells.index((target, column)) for column in range(width)]
        for market in np.flatnonzero(paid & np.isin(chosen, of_this_target)):
            column = cells[chosen[market]][1]
            mask = kept.market == market
            picked["per market"][mask] = values[mask, column]
            exited["per market"][mask] = kept.exit_us[mask, column]

    def booked(key: str, where: Ints) -> Summary:
        return summarise(picked[key][where], exited[key][where])

    order = np.argsort(kept.market, kind="stable")
    bounds = np.searchsorted(kept.market[order], np.arange(groups + 1))
    for group in range(groups):
        rows_of = order[bounds[group] : bounds[group + 1]]
        train_of, valid_of = rows_of[training[rows_of]], rows_of[validation[rows_of]]
        report.markets.append(
            MarketChoice(
                market=kept.markets[group],
                cell=named(int(chosen[group])) if paid[group] else None,
                training=booked("per market", train_of),
                validation=booked("per market", valid_of),
                as_run_training=booked("as run", train_of),
                as_run_validation=booked("as run", valid_of),
            )
        )
    report.markets.sort(key=lambda choice: -choice.training.total_r)

    years = kept.year
    everywhere = np.ones(len(kept.entry_us), dtype=np.bool_)
    rules = (
        ("every entry", "as run", everywhere),
        ("markets that paid", "as run", np.isin(kept.market, np.flatnonzero(as_run_paid))),
        (f"one cell ({report.best_cell})", "best", everywhere),
        ("a cell per market", "per market", everywhere),
    )
    for name, key, where in rules:

        def on(mask: Bools, key: str = key) -> Summary:
            return summarise(picked[key][mask], exited[key][mask])

        report.rules.append(
            Rule(
                name=name,
                training=on(training & where),
                validation=on(validation & where),
                by_year={
                    int(year): on(validation & where & (years == year))
                    for year in sorted(set(years[validation].tolist()))
                },
            )
        )
    return report


__all__ = [
    "EMPTY",
    "MIN_TRADES",
    "REASONS",
    "TARGETS",
    "GridReport",
    "GridRows",
    "MarketChoice",
    "Rule",
    "Summary",
    "cell_name",
    "evaluate",
    "grid_rows",
    "outcome",
    "summarise",
    "taken",
]
