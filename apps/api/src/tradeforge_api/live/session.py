"""One paper session, start to finish.

Everything this needs already exists; what was missing is the order the pieces go in, and that
order is the content of this module. Read it top to bottom and it is the life of a session:

    warm up over history  ->  hand over to a fresh broker  ->  open the row  ->
    beat, and trade, and record, one bar at a time         ->  finish the row

⚠️ **A session is not a job.** `arq` applies `job_timeout` with `asyncio.wait_for`, and a
session runs for days; a job that blocks the event loop freezes the whole queue, which PR #133
documented after the collector agent did exactly that. So this is a plain function meant for a
process of its own. The row in `live_sessions` is the *record* that a session exists, never the
thing that schedules it.

**Nothing is written until the warm-up has finished**, which is why `open_session` is called
where it is. `warmup_bars` is a fact about what the seed actually used, and a row written first
would have to be updated with it — a second write that can fail on its own. A session whose
warm-up raised gets no row at all, which is the honest outcome: nothing ran.
"""

import datetime as dt
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from tradeforge_api.live.heartbeat import session_heartbeat
from tradeforge_api.live.promotion import promotion_for
from tradeforge_api.live.recorder import LedgerWatch, TradeRecorder, record_bar
from tradeforge_api.live.splice import BarSource, splice
from tradeforge_api.runner import (
    ENGINE_VERSION,
    build_cost_model,
    instrument_spec,
    risk_percent,
    take_profit_rr,
)
from tradeforge_collector import read_candles, step
from tradeforge_db.live_sessions import finish_session, open_session, reconcile_stale
from tradeforge_db.models import Instrument, LiveSession, SessionMode, Strategy
from tradeforge_db.session import session_scope
from tradeforge_engine import BacktestBroker, PercentRiskManager, compile_strategy
from tradeforge_engine.errors import EngineError
from tradeforge_engine.loop import iter_run
from tradeforge_engine.warmup import hand_over

logger = logging.getLogger(__name__)

__all__ = ["SessionOutcome", "SessionPlan", "reconcile_on_start", "run_session"]


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass(frozen=True, slots=True)
class SessionPlan:
    """What to run. Every field is settled before anything is written or read."""

    strategy_id: uuid.UUID
    instrument_id: uuid.UUID
    timeframe: str
    initial_capital: Decimal
    cost_model: Mapping[str, Any]
    mode: SessionMode = SessionMode.PAPER
    """Paper, unless somebody says otherwise in as many words.

    ⚠️ A default that fails **safe**. Every caller today means paper, and making it required
    would have each of them say so — which reads as tidier right up to the day a new caller is
    added and the value they have to remember is the dangerous one."""


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    """What a finished session did. Returned so a caller can report without re-reading the row."""

    session_id: uuid.UUID
    warmup_bars: int
    bars: int
    refused_orders: tuple[str, ...]
    error: str | None


def reconcile_on_start(factory: sessionmaker[Session]) -> list[uuid.UUID]:
    """Mark every session that stopped beating as `failed`, before starting a new one.

    ⚠️ Housekeeping about **other** sessions, which is why it is separate from `run_session`.
    The thing that would have marked those rows `stopped` is the thing that died, so nobody but
    the next process to come up is ever going to do it — and a panel is meanwhile reporting a
    session that has not existed since Tuesday.
    """
    with session_scope(factory) as db:
        marked = [row.id for row in reconcile_stale(db, now=_utcnow())]
    if marked:
        logger.warning("marked %d abandoned session(s) as failed: %s", len(marked), marked)
    return marked


def run_session(  # noqa: PLR0913 — keyword-only; each names one seam of a session
    *,
    factory: sessionmaker[Session],
    source: BarSource,
    plan: SessionPlan,
    parquet_root: Path,
    stopping: Callable[[], bool],
    promotion_days: int = 5,
    now: Callable[[], dt.datetime] = _utcnow,
) -> SessionOutcome:
    """Run one session until `stopping` says otherwise, and record everything it did.

    Returns rather than raises on a strategy that blows up mid-session: the row is marked
    `failed` with the reason, because a live session ending badly is a *result* to record, not a
    crash to propagate — the same doctrine the backtest worker follows.

    ⚠️ **A refusal to start does raise.** A warm-up that cannot run, a hand-over that lands
    mid-position, a missing instrument: none of those produce a session, so none of them produce
    a row to mark. `open_session`'s docstring says the same thing from the other side.
    """
    opened_at = now()

    with session_scope(factory) as db:
        # ⚠️ **Before the warm-up, not after.** Warming a live session takes minutes — 38 987 bars
        # on the last real run — and refusing it at the end would spend all of that to say a thing
        # that was knowable at the start. It is also the honest place: nothing has happened yet,
        # so there is no session to mark and nothing to unwind.
        verdict = promotion_for(db, plan.strategy_id, mode=plan.mode, required_days=promotion_days)
        if not verdict:
            raise EngineError(f"refusing to start a {plan.mode.value} session: {verdict.reason}")

    with session_scope(factory) as db:
        strategy, instrument = _load(db, plan)
        spec = instrument_spec(instrument)
        symbol = instrument.symbol
        definition = dict(strategy.definition)

    timeframe = step(plan.timeframe)
    candles = splice(
        source,
        history=lambda: read_candles(parquet_root, symbol, plan.timeframe),
        timeframe=timeframe,
        opened_at=opened_at,
    )

    # ⚠️ Two brokers, and the second is built empty rather than reset. A `reset_ledger()` would
    # have to decide field by field what survives, and every field somebody forgets is a warm-up
    # trade leaking into a live equity curve (ADR-0023).
    def a_broker() -> BacktestBroker:
        return BacktestBroker(
            instrument=spec,
            initial_capital=plan.initial_capital,
            cost_model=build_cost_model(plan.cost_model),
            take_profit_rr=take_profit_rr(definition),
        )

    warm = a_broker()
    live = a_broker()
    risk = PercentRiskManager(percent=risk_percent(definition))
    compiled = compile_strategy(definition)

    for _outcome in iter_run(
        candles=candles.warmup(),
        timeframe=timeframe,
        instrument=spec,
        strategy=compiled,
        broker=warm,
        risk=risk,
    ):
        pass

    handover = hand_over(warm, live, symbol=symbol, bars=candles.warmed, risk=risk, instrument=spec)
    logger.info(
        "warmed over %d bars, carried %d resting order(s), refused %d",
        handover.bars,
        len(handover.carried),
        len(handover.refused),
    )

    with session_scope(factory) as db:
        row = open_session(
            db,
            strategy_id=plan.strategy_id,
            instrument_id=plan.instrument_id,
            timeframe=plan.timeframe,
            initial_capital=plan.initial_capital,
            cost_model=dict(plan.cost_model),
            engine_version=ENGINE_VERSION,
            mode=plan.mode,
            warmup_bars=handover.bars,
            at=opened_at,
        )
        session_id = row.id

    bars = 0
    failure: str | None = None
    watch = LedgerWatch(symbol)
    recorder = TradeRecorder(session_id, plan.instrument_id)

    # The heartbeat starts before the first bar and stops before the row is finished. Both ends
    # matter: a session is alive from the moment it opens, and a beat landing after `stopped_at`
    # would leave a stopped row claiming it was heard from afterwards.
    try:
        # ⚠️ The session hands over its own clock. Two clocks in one session is not a test
        # artefact: `stopped_at` and `heartbeat_at` are compared by anyone reading the row, and a
        # beat stamped from a different source than the stop can land after it.
        with session_heartbeat(factory, session_id, now=now):
            for outcome in iter_run(
                candles=candles.live(),
                timeframe=timeframe,
                instrument=spec,
                strategy=compiled,
                broker=live,
                risk=risk,
            ):
                _persist(
                    factory,
                    watch=watch,
                    recorder=recorder,
                    broker=live,
                    at=outcome.candle.time,
                )
                bars += 1
                if stopping():
                    break
    except Exception as exc:  # a failed session is a recorded result, not a crash
        failure = f"{type(exc).__name__}: {exc}"
        logger.exception("session %s failed after %d bars", session_id, bars)

    with session_scope(factory) as db:
        finish_session(db, session_id, at=now(), error=failure)

    return SessionOutcome(
        session_id=session_id,
        warmup_bars=handover.bars,
        bars=bars,
        refused_orders=handover.refused,
        error=failure,
    )


def _persist(
    factory: sessionmaker[Session],
    *,
    watch: LedgerWatch,
    recorder: TradeRecorder,
    broker: BacktestBroker,
    at: dt.datetime,
) -> None:
    """One bar's writes, in one transaction.

    ⚠️ **Its own short transaction, per bar.** The trades and `last_bar_time` have to land
    together — a window where a trade exists and the bar that produced it does not is a window
    where the record contradicts itself. And it has to be *short*: the heartbeat thread writes
    to this same row, so a transaction held open across bars would make it wait, and a beat that
    waits is a healthy session reconciled to `failed` (see `heartbeat.BEAT_LOCK_TIMEOUT`).
    """
    with session_scope(factory) as db:
        record_bar(db, watch=watch, recorder=recorder, broker=broker)
        session = db.get(LiveSession, recorder.session_id)
        if session is not None:
            session.last_bar_time = at


def _load(db: Session, plan: SessionPlan) -> tuple[Strategy, Instrument]:
    strategy = db.get(Strategy, plan.strategy_id)
    instrument = db.get(Instrument, plan.instrument_id)
    if strategy is None or instrument is None:
        # A refusal to start, not a failed session: there is nothing to write a row about.
        raise LookupError("the session references a missing strategy or instrument")
    return strategy, instrument
