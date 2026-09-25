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
from tradeforge_api.queue import RUN_CLUSTER
from tradeforge_api.runner import risk_percent
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

    ⚠️ **Refused, with the members named**, for a run that does not exist, has not finished, or
    kept no trades — never replayed with those left out, which would present a smaller portfolio
    as the one asked for.
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
    for member in request.members:
        pair = found.get(member.backtest_id)
        if pair is None:
            problems.append(f"{member.backtest_id}: no such run")
            continue
        run, strategy = pair
        if run.status is not BacktestStatus.DONE:
            problems.append(f"{member.backtest_id}: not finished ({run.status.value})")
            continue
        if run.recorded is Recorded.METRICS:
            problems.append(
                f"{member.backtest_id}: kept no trades (a sweep's run below its bar) — run it "
                "again as a single backtest to keep them"
            )
            continue
        if member.risk_percent is not None:
            percent = member.risk_percent
        else:
            try:
                percent = risk_percent(strategy.definition)
            except ValueError:
                problems.append(f"{member.backtest_id}: its strategy sizes by no percent; give one")
                continue
        members.append({"backtest_id": str(run.id), "risk_percent": str(percent)})
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
    await queue.enqueue_job(RUN_CLUSTER, str(cluster.id), _job_id=f"cluster-{cluster.id}")
    return cluster_out(session, cluster)


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
