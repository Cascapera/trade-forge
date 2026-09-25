"""Clusters: several finished runs replayed on one shared account (25/09, `cluster`).

Created here, replayed by the worker (`RUN_CLUSTER`), read back whole. The members must be
finished runs that kept their trades: a sweep's losing run keeps only its metrics, and a replay
of trades nobody kept would be a replay of nothing that says it is something.
"""

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from tradeforge_api.deps import QueueDep, SessionDep
from tradeforge_api.queue import RUN_BACKTEST, RUN_CLUSTER
from tradeforge_api.runner import ENGINE_VERSION, risk_percent
from tradeforge_api.schemas import (
    ClusterListItem,
    ClusterMemberOut,
    ClusterOut,
    ClusterPointOut,
    CreateCluster,
)
from tradeforge_db.models import (
    Backtest,
    BacktestStatus,
    Cluster,
    Instrument,
    Recorded,
    Strategy,
)

router = APIRouter(tags=["clusters"])

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"description": "not found"}
}


@router.post("/clusters", response_model=ClusterOut, status_code=status.HTTP_202_ACCEPTED)
async def create_cluster(
    request: CreateCluster, session: SessionDep, queue: QueueDep
) -> ClusterOut:
    """Check every member, fix each one's risk %, keep the cluster and queue its replay.

    ⚠️ **A member that kept no trades is run again, not refused** (25/09). A sweep's losing run
    keeps only its metrics, and those are exactly the runs a portfolio may want. Its twin — the
    same strategy, market, chart, window, capital and costs, outside any sweep, so it keeps its
    trades — is queued, or reused if one already finished under this engine; the engine is
    deterministic, so the twin's trades are the ones the original made. The cluster waits for it.

    ⚠️ **Refused, with the members named**, for a run that does not exist or has not finished —
    never replayed with those left out, which would present a smaller portfolio as the one asked
    for.
    """
    ids = [one.backtest_id for one in request.members]
    found = {
        run.id: (run, strategy)
        for run, strategy in session.execute(
            select(Backtest, Strategy)
            .join(Strategy, Strategy.id == Backtest.strategy_id)
            .where(Backtest.id.in_(ids))
        )
    }
    problems: list[str] = []
    members: list[dict[str, str]] = []
    twins: list[Backtest] = []
    for member in request.members:
        pair = found.get(member.backtest_id)
        if pair is None:
            problems.append(f"{member.backtest_id}: no such run")
            continue
        run, strategy = pair
        if run.status is not BacktestStatus.DONE:
            problems.append(f"{member.backtest_id}: not finished ({run.status.value})")
            continue
        if member.risk_percent is not None:
            percent = member.risk_percent
        else:
            try:
                percent = risk_percent(strategy.definition)
            except ValueError:
                problems.append(f"{member.backtest_id}: its strategy sizes by no percent; give one")
                continue
        if run.recorded is not Recorded.METRICS:
            members.append({"backtest_id": str(run.id), "risk_percent": str(percent)})
            continue
        twin, new = _twin_of(session, run)
        if new:
            twins.append(twin)
            session.add(twin)
            session.flush()
        members.append(
            {"backtest_id": str(twin.id), "rerun_of": str(run.id), "risk_percent": str(percent)}
        )
    if problems:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="these members cannot be replayed: " + "; ".join(problems),
        )

    cluster = Cluster(
        name=request.name,
        initial_capital=request.initial_capital,
        max_open_positions=request.max_open_positions,
        max_open_risk_percent=request.max_open_risk_percent,
        members=members,
        status=BacktestStatus.QUEUED,
    )
    session.add(cluster)
    session.commit()
    session.refresh(cluster)
    # The twins first: the cluster's job finds them queued and waits for them (`cluster_job`).
    for twin in twins:
        await queue.enqueue_job(RUN_BACKTEST, str(twin.id), _job_id=str(twin.id))
    await queue.enqueue_job(RUN_CLUSTER, str(cluster.id), _job_id=f"cluster-{cluster.id}")
    return cluster_out(session, cluster)


def _twin_of(session: SessionDep, run: Backtest) -> tuple[Backtest, bool]:
    """A run of the same measurement that keeps its trades: one already finished under this
    engine if there is one (`False`), or a new one to queue (`True`)."""
    same = (
        Backtest.strategy_id == run.strategy_id,
        Backtest.instrument_id == run.instrument_id,
        Backtest.timeframe == run.timeframe,
        Backtest.date_from == run.date_from,
        Backtest.date_to == run.date_to,
        Backtest.initial_capital == run.initial_capital,
        Backtest.cost_model == run.cost_model,
        Backtest.engine_version == ENGINE_VERSION,
        Backtest.recorded != Recorded.METRICS,
        Backtest.status.in_([BacktestStatus.DONE, BacktestStatus.QUEUED, BacktestStatus.RUNNING]),
        # The same instrument as the original ran with, when it kept one (25/09): a twin run on
        # a rewritten tick value would make the same trades for different money.
        *([] if run.instrument_spec is None else [Backtest.instrument_spec == run.instrument_spec]),
    )
    found = session.scalars(
        select(Backtest).where(*same).order_by(Backtest.created_at.desc()).limit(1)
    ).first()
    if found is not None:
        return found, False
    twin = Backtest(
        strategy_id=run.strategy_id,
        instrument_id=run.instrument_id,
        timeframe=run.timeframe,
        date_from=run.date_from,
        date_to=run.date_to,
        initial_capital=run.initial_capital,
        cost_model=dict(run.cost_model),
        # Inherited, so the twin executes on the instrument the original did (`runner.spec_for`).
        # ⚠️ Left out, not passed as `None`, when the original kept none: `None` on a JSONB column
        # is written as JSON `null`, not SQL NULL, and the CHECK refuses it.
        **({} if run.instrument_spec is None else {"instrument_spec": dict(run.instrument_spec)}),
        status=BacktestStatus.QUEUED,
        engine_version=ENGINE_VERSION,
    )
    return twin, True


@router.get("/clusters", response_model=list[ClusterListItem])
def list_clusters(
    session: SessionDep, limit: int = Query(default=50, ge=1, le=200)
) -> list[ClusterListItem]:
    """The latest clusters first."""
    rows = session.scalars(select(Cluster).order_by(Cluster.created_at.desc()).limit(limit))
    return [
        ClusterListItem(
            id=row.id,
            name=row.name,
            status=row.status.value,
            members=len(row.members),
            created_at=row.created_at,
            net_return=None if row.result is None else row.result.get("net_return"),
            max_drawdown_pct=None if row.result is None else row.result.get("max_drawdown_pct"),
        )
        for row in rows
    ]


@router.get("/clusters/{cluster_id}", response_model=ClusterOut, responses=_NOT_FOUND)
def get_cluster(cluster_id: uuid.UUID, session: SessionDep) -> ClusterOut:
    cluster = session.get(Cluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="cluster not found")
    return cluster_out(session, cluster)


def cluster_out(session: SessionDep, cluster: Cluster) -> ClusterOut:
    """The cluster with each member named — market, chart, strategy — and its replay, if any."""
    ids = [uuid.UUID(one["backtest_id"]) for one in cluster.members]
    described = {
        run_id: (name, symbol, timeframe)
        for run_id, name, symbol, timeframe in session.execute(
            select(Backtest.id, Strategy.name, Instrument.symbol, Backtest.timeframe)
            .join(Strategy, Strategy.id == Backtest.strategy_id)
            .join(Instrument, Instrument.id == Backtest.instrument_id)
            .where(Backtest.id.in_(ids))
        )
    }
    result = cluster.result or {}
    outcomes = result.get("members", [])
    members: list[ClusterMemberOut] = []
    for index, member in enumerate(cluster.members):
        run_id = uuid.UUID(member["backtest_id"])
        name, symbol, timeframe = described.get(run_id, ("(removed run)", "", ""))
        outcome = outcomes[index] if index < len(outcomes) else {}
        members.append(
            ClusterMemberOut(
                backtest_id=run_id,
                rerun_of=member.get("rerun_of"),
                risk_percent=member["risk_percent"],
                label=name,
                symbol=symbol,
                timeframe=timeframe,
                offered=outcome.get("offered"),
                taken=outcome.get("taken"),
                skipped=outcome.get("skipped"),
                net_pnl=outcome.get("net_pnl"),
            )
        )
    return ClusterOut(
        id=cluster.id,
        name=cluster.name,
        status=cluster.status.value,
        error=cluster.error,
        initial_capital=cluster.initial_capital,
        max_open_positions=cluster.max_open_positions,
        max_open_risk_percent=cluster.max_open_risk_percent,
        created_at=cluster.created_at,
        finished_at=cluster.finished_at,
        members=members,
        final_balance=result.get("final_balance"),
        net_return=result.get("net_return"),
        max_drawdown_pct=result.get("max_drawdown_pct"),
        max_drawdown_abs=result.get("max_drawdown_abs"),
        most_open=result.get("most_open"),
        yearly_return=result.get("yearly_return"),
        curve=None
        if "curve" not in result
        else [ClusterPointOut.model_validate(one) for one in result["curve"]],
    )


__all__ = ["cluster_out", "router"]
