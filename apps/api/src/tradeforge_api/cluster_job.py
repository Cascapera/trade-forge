"""The worker's side of a cluster: read the members' trades and bars, replay, keep the answer.

Kept apart from `worker.py` because nothing in it runs the engine: it reads what the members
already kept (`trades`) and the bars their positions were open across (to mark them), and hands
both to `cluster.replay`.
"""

import datetime as dt
import uuid
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_api.candle_cache import CandleReader
from tradeforge_api.cluster import (
    Limits,
    Mark,
    MemberTrade,
    daily,
    replay,
    yearly_returns,
)
from tradeforge_collector import step
from tradeforge_db.models import Backtest, BacktestStatus, Cluster, Instrument, Trade
from tradeforge_engine.domain import Candle, Side


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


def _marks(
    member: int,
    candles: Sequence[Candle],
    timeframe: str,
    spans: Sequence[tuple[dt.datetime, dt.datetime]],
) -> list[Mark]:
    """The closes of this member's bars that fall strictly inside one of its trades — only those
    move an open position's value, and a series of years holds millions of the others.

    A member alone never holds two positions (`loop._open_position`), so its trades' spans do not
    overlap, and one pointer walking both lists in time order is enough.
    """
    width = step(timeframe)
    ordered = sorted(spans)
    out: list[Mark] = []
    index = 0
    for candle in candles:
        closed = candle.time + width
        while index < len(ordered) and ordered[index][1] <= closed:
            index += 1
        if index == len(ordered):
            break
        if ordered[index][0] < closed:
            out.append(Mark(member=member, time=closed, close=candle.close))
    return out


def process_cluster(
    *, session: Session, parquet_root: Path, cluster_id: uuid.UUID, read: CandleReader
) -> None:
    """Replay one cluster and record the answer, or the reason there is none."""
    cluster = session.get(Cluster, cluster_id)
    if cluster is None or cluster.status is not BacktestStatus.QUEUED:
        return
    cluster.status = BacktestStatus.RUNNING
    session.commit()
    try:
        _replay_into(session, parquet_root, cluster, read)
        cluster.status = BacktestStatus.DONE
    except Exception as exc:  # noqa: BLE001 — recorded on the row, the only place anyone looks
        session.rollback()
        cluster = session.get(Cluster, cluster_id)
        assert cluster is not None  # noqa: S101 — it was read above, and nothing deletes it here
        cluster.status = BacktestStatus.FAILED
        cluster.error = f"{type(exc).__name__}: {exc}"
    cluster.finished_at = _now()
    session.commit()


def _replay_into(
    session: Session, parquet_root: Path, cluster: Cluster, read: CandleReader
) -> None:
    ids = [uuid.UUID(one["backtest_id"]) for one in cluster.members]
    runs = {
        run.id: (run, symbol)
        for run, symbol in session.execute(
            select(Backtest, Instrument.symbol)
            .join(Instrument, Instrument.id == Backtest.instrument_id)
            .where(Backtest.id.in_(ids))
        )
    }
    trades: list[MemberTrade] = []
    marks: list[Mark] = []
    for member, run_id in enumerate(ids):
        run, symbol = runs[run_id]
        rows = session.execute(
            select(
                Trade.entry_time,
                Trade.exit_time,
                Trade.direction,
                Trade.entry_price,
                Trade.stop_loss,
                Trade.r_multiple,
            )
            .where(Trade.backtest_id == run_id, Trade.exit_time.is_not(None))
            .order_by(Trade.entry_time, Trade.id)
        ).all()
        mine = [
            MemberTrade(
                member=member,
                entry_time=entered,
                exit_time=left,
                long=direction == Side.LONG,
                entry_price=price,
                stop=stop,
                r=r,
            )
            for entered, left, direction, price, stop, r in rows
        ]
        trades += mine
        spans = [(one.entry_time, one.exit_time) for one in mine]
        marks += _marks(member, read(parquet_root, symbol, run.timeframe), run.timeframe, spans)

    done = replay(
        initial_capital=cluster.initial_capital,
        risk_pcts=[Decimal(one["risk_percent"]) / 100 for one in cluster.members],
        trades=trades,
        marks=marks,
        limits=Limits(
            max_open_positions=cluster.max_open_positions,
            max_open_risk_pct=cluster.max_open_risk_percent / 100,
        ),
    )
    cluster.result = {
        "final_balance": str(done.final_balance),
        "net_return": str((done.final_balance - done.initial_capital) / done.initial_capital),
        "max_drawdown_pct": str(done.max_drawdown_pct),
        "max_drawdown_abs": str(done.max_drawdown_abs),
        "most_open": done.most_open,
        "yearly_return": {
            str(year): str(value)
            for year, value in yearly_returns(done.curve, done.initial_capital).items()
        },
        "members": [
            {
                "offered": one.offered,
                "taken": one.taken,
                "skipped": one.skipped,
                "net_pnl": str(one.net_pnl),
            }
            for one in done.members
        ],
        "curve": [
            {
                "time": point.time.isoformat(),
                "balance": str(point.balance),
                "equity": str(point.equity),
            }
            for point in daily(done.curve)
        ],
    }


__all__ = ["process_cluster"]
