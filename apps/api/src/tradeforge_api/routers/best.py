"""`/best` — the best runs by market, setup and chart, across every sweep (02/10).

His ask: a page that shows, market by market, which setup did best on which chart, grouped so it
is not a wall of numbers, to choose ideas to validate later. `GET /best/map` is the overview, one
cell per (market, setup, chart); `GET /best/cell` is one cell's top runs.

⚠️ **What is ranked.** Finished runs of ordinary sweeps on the current engine, over their chart's
ranking floor (`RANK_MIN_TRADES`) unless every run is asked for. Left out:

* **reserved-window tests** (`Sweep.holdout_of`): they are the validation, and ranking them beside
  in-sample runs would mix "the best found" with "what held up";
* **a walk-forward's training sweeps** (`SweepWalkForwardFold.train_sweep_id`): the same points
  again over parts of a window, which would count one result several times;
* **runs of an older engine**: their numbers came from other rules (`ENGINE_VERSION`).

Nothing here validates anything: each point carries what its reserved-window tests found, or that
none was run.
"""

import datetime as dt
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import (
    ColumnElement,
    Float,
    and_,
    case,
    cast,
    extract,
    func,
    literal,
    select,
)
from sqlalchemy.orm import Session, defer, selectinload
from sqlalchemy.sql import Subquery
from sqlalchemy.types import Text

from tradeforge_api.best import BestMetric, market_of, score
from tradeforge_api.deps import SessionDep, SnapshotsDep
from tradeforge_api.holdout import Candidate, HoldoutRank, choose
from tradeforge_api.ranking_floor import RANK_MIN_TRADES
from tradeforge_api.routers.sweeps import entries_reader
from tradeforge_api.runner import ENGINE_VERSION
from tradeforge_api.schemas import (
    BestCellOut,
    BestMapCell,
    BestMapOut,
    BestPointOut,
    BestTestOut,
    Symbol,
)
from tradeforge_db.models import (
    Backtest,
    BacktestMetrics,
    BacktestStatus,
    BrokerSymbol,
    CatalogEntry,
    Instrument,
    Strategy,
    Sweep,
    SweepPoint,
    SweepWalkForwardFold,
)
from tradeforge_schema.models import Timeframe

router = APIRouter(tags=["best"])

_YEAR_SECONDS = 365.25 * 86_400


def _own_points() -> Subquery:
    """Each sweep strategy's entry: its own point (`same_as` null), one per (sweep, strategy).

    ⚠️ Grouped rather than joined plainly: nothing in the schema makes a strategy's own point
    unique within a sweep, and a plain join would rank its run once per such point (`#365`)."""
    return (
        select(
            SweepPoint.sweep_id,
            SweepPoint.strategy_id,
            func.min(SweepPoint.entry_id).label("entry_id"),
        )
        .where(SweepPoint.same_as.is_(None))
        .group_by(SweepPoint.sweep_id, SweepPoint.strategy_id)
        .subquery()
    )


def _ranked_runs(*, every_run: bool) -> list[ColumnElement[bool]]:
    """The conditions every ranked run meets — the module docstring's list, in SQL."""
    floor = case(
        {chart: max(trades, 1) for chart, trades in RANK_MIN_TRADES.items()},
        value=Backtest.timeframe,
        else_=1,
    )
    trained = select(SweepWalkForwardFold.train_sweep_id).where(
        SweepWalkForwardFold.train_sweep_id.is_not(None)
    )
    return [
        Backtest.status == BacktestStatus.DONE,
        Backtest.engine_version == ENGINE_VERSION,
        Sweep.holdout_of.is_(None),
        Sweep.holdout_rule.is_(None),
        Sweep.id.not_in(trained),
        BacktestMetrics.total_trades >= (literal(1) if every_run else floor),
    ]


def _value(metric: BestMetric) -> tuple[ColumnElement[Any], ColumnElement[bool]]:
    """`metric` in SQL, and whether it is unbounded — a drawdown ratio with no drawdown and a
    gain, which ranks above every finite one (`holdout.recovery_r`) and has no number to show."""
    net = BacktestMetrics.net_r
    drawdown = BacktestMetrics.max_drawdown_r
    no_bound: ColumnElement[bool] = literal(value=False)
    value: Any
    if metric is BestMetric.RECOVERY_R:
        value = case((drawdown > 0, net / drawdown), else_=None)
        no_bound = and_(drawdown <= 0, net > 0)
    elif metric is BestMetric.NET_R:
        value = net
    elif metric is BestMetric.NET_R_PER_YEAR:
        seconds = extract("epoch", Backtest.date_to - Backtest.date_from)
        value = case((seconds > 0, cast(net, Float) / (seconds / _YEAR_SECONDS)), else_=None)
    else:
        value = BacktestMetrics.positive_year_share
    return value, no_bound


@router.get("/best/map", response_model=BestMapOut)
def best_map(
    session: SessionDep,
    snapshots: SnapshotsDep,
    metric: BestMetric = BestMetric.RECOVERY_R,
    every_run: bool = False,
) -> BestMapOut:
    """The best run of every (market, setup, chart), by `metric` — the last map kept (05/10).

    ⚠️ **Served from a snapshot, computed off the request.** Ranking every finished run of every
    sweep took 30 s an opening on 05/10, and grows with every sweep. `snapshots` computes the map
    again every few minutes when runs have finished; this computes it only when none is kept."""
    kept = snapshots.read_best(metric, every_run=every_run)
    if kept is not None:
        return kept
    made = compute_best_map(session, metric, every_run=every_run)
    snapshots.keep_best(made)
    return made


def compute_best_map(session: Session, metric: BestMetric, *, every_run: bool) -> BestMapOut:
    """The best run of every (market, setup, chart), by `metric` — one query, no trades read.

    Ties go to launch order (`created_at`, the strategy's name, the id), as a sweep's page."""
    own = _own_points()
    value, unbounded = _value(metric)
    rank = (
        func.row_number()
        .over(
            partition_by=(Backtest.instrument_id, own.c.entry_id, Backtest.timeframe),
            order_by=(
                unbounded.desc(),
                value.desc().nulls_last(),
                Backtest.created_at,
                Strategy.name,
                Backtest.id,
            ),
        )
        .label("nth")
    )
    ranked = (
        select(
            Backtest.id.label("run_id"),
            Backtest.sweep_id,
            Backtest.instrument_id,
            Backtest.timeframe,
            own.c.entry_id,
            value.label("value"),
            unbounded.label("unbounded"),
            rank,
            func.count()
            .over(partition_by=(Backtest.instrument_id, own.c.entry_id, Backtest.timeframe))
            .label("ranked"),
        )
        .join(BacktestMetrics, BacktestMetrics.backtest_id == Backtest.id)
        .join(Sweep, Sweep.id == Backtest.sweep_id)
        .join(Strategy, Strategy.id == Backtest.strategy_id)
        .join(
            own,
            and_(own.c.sweep_id == Backtest.sweep_id, own.c.strategy_id == Backtest.strategy_id),
        )
        .where(*_ranked_runs(every_run=every_run), (value.is_not(None)) | unbounded)
        .subquery()
    )
    rows = session.execute(
        select(
            ranked,
            Instrument.symbol,
            Instrument.asset_class,
            BrokerSymbol.path,
            CatalogEntry.name.label("entry_name"),
        )
        .join(Instrument, Instrument.id == ranked.c.instrument_id)
        .outerjoin(BrokerSymbol, BrokerSymbol.symbol == Instrument.symbol)
        # The entry is kept as text on the point; a removed entry still ranks, without a name.
        .outerjoin(CatalogEntry, cast(CatalogEntry.id, Text) == ranked.c.entry_id)
        .where(ranked.c.nth == 1)
        .order_by(Instrument.symbol, ranked.c.entry_id, ranked.c.timeframe)
    ).all()
    return BestMapOut(
        metric=metric,
        every_run=every_run,
        engine_version=ENGINE_VERSION,
        as_of=dt.datetime.now(tz=dt.UTC),
        cells=[
            BestMapCell(
                market=market_of(row.path, row.asset_class),
                symbol=row.symbol,
                entry_id=row.entry_id,
                entry_name=row.entry_name,
                timeframe=row.timeframe,
                value=None if row.unbounded else _decimal(row.value),
                unbounded=bool(row.unbounded),
                run_id=row.run_id,
                sweep_id=row.sweep_id,
                ranked=row.ranked,
            )
            for row in rows
        ],
    )


@router.get("/best/cell", response_model=BestCellOut)
def best_cell(  # noqa: PLR0913 — query parameters; each is one part of the cell asked
    session: SessionDep,
    *,
    # ⚠️ Text the database can store, refused at the edge (schemathesis, 02/10): a NUL in either
    # reached Postgres as a 500 rather than a 422.
    symbol: Symbol,
    entry_id: Symbol,
    timeframe: Timeframe,
    metric: BestMetric = BestMetric.RECOVERY_R,
    every_run: bool = False,
    top_n: Annotated[int, Query(ge=1, le=50)] = 10,
) -> BestCellOut:
    """One cell's top `top_n` runs by `metric`, as a reserved-window test would choose them:
    no clone, no point whose entries nest in a better one's (`holdout.choose`, #370/#371)."""
    own = _own_points()
    query = (
        select(Backtest, own.c.entry_id)
        .join(BacktestMetrics, BacktestMetrics.backtest_id == Backtest.id)
        .join(Sweep, Sweep.id == Backtest.sweep_id)
        .join(Strategy, Strategy.id == Backtest.strategy_id)
        .join(Instrument, Instrument.id == Backtest.instrument_id)
        .join(
            own,
            and_(own.c.sweep_id == Backtest.sweep_id, own.c.strategy_id == Backtest.strategy_id),
        )
        .where(
            *_ranked_runs(every_run=every_run),
            Instrument.symbol == symbol,
            own.c.entry_id == entry_id,
            Backtest.timeframe == timeframe,
        )
        .order_by(Backtest.created_at, Strategy.name, Backtest.id)
        .options(
            selectinload(Backtest.metrics).options(
                defer(BacktestMetrics.equity_curve), defer(BacktestMetrics.targets)
            )
        )
    )
    runs = [run for run, _entry in session.execute(query).all()]
    runs_of = {order: (run, symbol) for order, run in enumerate(runs)}
    candidates = [
        Candidate(group=(entry_id, timeframe, symbol), order=order, metrics=run.metrics)
        for order, run in enumerate(runs)
        if run.metrics is not None
    ]

    def valued(one: Candidate) -> Decimal | None:
        run = runs[one.order]
        return score(metric, one.metrics, run.date_from, run.date_to)

    chosen = choose(
        candidates,
        # Ignored: `score` ranks. Named because the rule needs one.
        metric=HoldoutRank.NET_R,
        top_n=top_n,
        floors={} if every_run else RANK_MIN_TRADES,
        entries_of=entries_reader(session, runs_of),
        score=valued,
    )
    picked = sorted(chosen, key=lambda one: (_order_key(valued(one)), one.order))
    picked_runs = [runs[one.order] for one in picked]
    labels = _labels(session, picked_runs)
    tests = _tests_of(session, picked_runs)
    entry = session.get(CatalogEntry, _uuid_or_none(entry_id)) if _uuid_or_none(entry_id) else None
    return BestCellOut(
        symbol=symbol,
        entry_id=entry_id,
        entry_name=None if entry is None else entry.name,
        timeframe=timeframe,
        metric=metric,
        every_run=every_run,
        ranked=len(candidates),
        points=[_point(run, metric, labels, tests) for run in picked_runs],
    )


def _order_key(value: Decimal | None) -> Decimal:
    """Best first: the value's negative, an unbounded one before every finite one."""
    return -(value if value is not None else Decimal("-Infinity"))


def _point(
    run: Backtest,
    metric: BestMetric,
    labels: Mapping[tuple[uuid.UUID, uuid.UUID], tuple[str, dict[str, Any]]],
    tests: Mapping[uuid.UUID, list[BestTestOut]],
) -> BestPointOut:
    metrics = run.metrics
    assert metrics is not None, "a ranked run has metrics"  # noqa: S101 — filtered above
    value = score(metric, metrics, run.date_from, run.date_to)
    label, values = labels.get((run.sweep_id, run.strategy_id), ("", {}))  # type: ignore[arg-type]
    return BestPointOut(
        run_id=run.id,
        sweep_id=run.sweep_id,
        strategy_id=run.strategy_id,
        label=label,
        values=values,
        date_from=run.date_from,
        date_to=run.date_to,
        value=None if value is not None and value.is_infinite() else value,
        unbounded=value is not None and value.is_infinite(),
        net_r=metrics.net_r,
        net_r_per_year=score(BestMetric.NET_R_PER_YEAR, metrics, run.date_from, run.date_to),
        recovery_r=_finite(score(BestMetric.RECOVERY_R, metrics, run.date_from, run.date_to)),
        positive_year_share=metrics.positive_year_share,
        max_drawdown_r=metrics.max_drawdown_r,
        total_trades=metrics.total_trades,
        profit_factor=metrics.profit_factor,
        yearly_r=dict(metrics.yearly_r or {}),
        tests=tests.get(run.strategy_id, []),
    )


def _labels(
    session: Session, runs: Sequence[Backtest]
) -> dict[tuple[uuid.UUID, uuid.UUID], tuple[str, dict[str, Any]]]:
    """Each run's own point — its caption and its coordinates — by (sweep, strategy)."""
    if not runs:
        return {}
    found: dict[tuple[uuid.UUID, uuid.UUID], tuple[str, dict[str, Any]]] = {}
    for sweep_id, strategy_id, label, coordinates in session.execute(
        select(
            SweepPoint.sweep_id, SweepPoint.strategy_id, SweepPoint.label, SweepPoint.coordinates
        )
        .where(
            SweepPoint.same_as.is_(None),
            SweepPoint.sweep_id.in_({run.sweep_id for run in runs}),
            SweepPoint.strategy_id.in_({run.strategy_id for run in runs}),
        )
        .order_by(SweepPoint.position)
    ):
        found.setdefault((sweep_id, strategy_id), (label, dict(coordinates)))
    return found


def _tests_of(session: Session, runs: Sequence[Backtest]) -> dict[uuid.UUID, list[BestTestOut]]:
    """What the reserved-window tests found for each run's point — the same strategy on the same
    market and chart, in a test sweep (`holdout_of`). Empty for a point never tested."""
    if not runs:
        return {}
    wanted = {(run.strategy_id, run.instrument_id, run.timeframe) for run in runs}
    found: dict[uuid.UUID, list[BestTestOut]] = {}
    rows = session.execute(
        select(Backtest, BacktestMetrics.net_r, BacktestMetrics.total_trades)
        .join(Sweep, Sweep.id == Backtest.sweep_id)
        .outerjoin(BacktestMetrics, BacktestMetrics.backtest_id == Backtest.id)
        .where(
            Sweep.holdout_of.is_not(None),
            Backtest.strategy_id.in_({run.strategy_id for run in runs}),
        )
        .order_by(Backtest.date_from, Backtest.created_at)
    ).all()
    for test, net_r, trades in rows:
        if (test.strategy_id, test.instrument_id, test.timeframe) not in wanted:
            continue
        found.setdefault(test.strategy_id, []).append(
            BestTestOut(
                sweep_id=test.sweep_id,
                status=test.status,
                date_from=test.date_from,
                date_to=test.date_to,
                net_r=net_r,
                total_trades=trades,
            )
        )
    return found


def _finite(value: Decimal | None) -> Decimal | None:
    return None if value is None or value.is_infinite() else value


def _decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _uuid_or_none(text: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(text)
    except ValueError:
        return None
