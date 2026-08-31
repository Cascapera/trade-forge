"""One session, paper or real, start to finish.

Everything this needs already exists; what was missing is the order the pieces go in, and that
order is the content of this module. Read it top to bottom and it is the life of a session:

    warm up over history  ->  build the broker it will trade through  ->  open the row  ->
    say it is alive  ->  hand over what is still resting  ->  trade and record, one bar at a
    time  ->  finish the row

⚠️ **"Say it is alive" comes before the hand-over, and it did not always.** The hand-over
submits, the executor refuses orders from a session it has not heard from, and `started_at` is
stamped before a warm-up that takes minutes — so a session that beat only once it started
trading had every one of its first orders refused. Measured against the demo account; the
comment at the `session_heartbeat` call carries the numbers.

⚠️ **A session is not a job.** `arq` applies `job_timeout` with `asyncio.wait_for`, and a
session runs for days; a job that blocks the event loop freezes the whole queue, which PR #133
documented after the collector agent did exactly that. So this is a plain function meant for a
process of its own. The row in `live_sessions` is the *record* that a session exists, never the
thing that schedules it.

**Nothing is written until the warm-up has finished**, which is why `open_session` is called
where it is. `warmup_bars` is a fact about what the seed actually used, and a row written first
would have to be updated with it — a second write that can fail on its own. A session whose
warm-up raised gets no row at all, which is the honest outcome: nothing ran.

**Nothing is written *after* an order has gone out either**, and that is the newer half of the
same sentence. A paper session's broker has no identity, so the row could be opened whenever;
a real one's broker *is* its identity — `MT5Broker` is built with the session id, which names
its consumer group, tags every envelope on `orders.outbound`, prefixes every `client_id` the
account displays, and is what `order_audit.live_session_id` links a row by. `hand_over` submits.
So a row opened after the hand-over would leave the first orders of a session — placed in its
riskiest instant, on levels decided over history — sitting in `order_audit` with a NULL session,
because the executor's `_known_session` degrades to NULL rather than losing the audit row.

The id is therefore minted before the row exists, and `open_session` takes it. The row is still
the record; it is just no longer the thing that names what it records.

⚠️ **The venue is a seam, not an import.** This module never mentions `MT5Broker` and cannot:
`AGENTS.md` §5.4 keeps everything outside `apps/collector` and `apps/executor` away from the
broker's world, and a module that could reach a venue is a module a paper session could reach
one through. What it takes is a `Venue` — a callable handed the session id — and it consults it
**only** in live mode. There is no code path on which a session marked paper touches an account.
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
from tradeforge_api.live.recorder import LedgerView, LedgerWatch, TradeRecorder, record_bar
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
from tradeforge_engine.domain import InstrumentSpec, Refusal
from tradeforge_engine.errors import EngineError
from tradeforge_engine.loop import iter_run
from tradeforge_engine.protocols import Broker, CostModel
from tradeforge_engine.warmup import hand_over

logger = logging.getLogger(__name__)

__all__ = ["SessionOutcome", "SessionPlan", "Venue", "reconcile_on_start", "run_session"]

type Venue = Callable[[uuid.UUID, InstrumentSpec], Broker]
"""Builds the broker a **live** session trades through, ready to use.

Handed the session's id and the instrument it will trade, and expected to return a broker that
has already done whatever starting it needs — `MT5Broker.start()` reads the venue's snapshot and
refuses over an orphaned position, or an absent, stale or unreadable one. Refusing *inside* the
callable is deliberate: this module calls it at the one instant where a refusal still means no
row was written, and a two-step `build then start` would let a caller get that order wrong.

A callable rather than a broker, because the id does not exist until the warm-up is over, and a
broker built before then would be built around an id nothing will ever be recorded under.
"""


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

    refused_orders: tuple[Refusal, ...]
    """What the hand-over could not carry onto the session's broker, and which gate said so.

    ⚠️ **The same objects the strategy was shown**, not a report built beside them. These are
    handed to `iter_run` as the first live bar's `Context.refusals`, so an operator reading this
    field and the strategy reading its context are reading one fact — and a future caller that
    reports these without passing them on will be reporting a ghost it helped create.
    """

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
    venue: Venue | None = None,
    promotion_days: int = 5,
    now: Callable[[], dt.datetime] = _utcnow,
) -> SessionOutcome:
    """Run one session until `stopping` says otherwise, and record everything it did.

    Returns rather than raises on a strategy that blows up mid-session: the row is marked
    `failed` with the reason, because a live session ending badly is a *result* to record, not a
    crash to propagate — the same doctrine the backtest worker follows.

    ⚠️ **A refusal to start does raise.** A warm-up that cannot run, a hand-over that lands
    mid-position, a missing instrument, a venue that will not have this session: none of those
    produce a session, so none of them produce a row to mark. `open_session`'s docstring says the
    same thing from the other side.

    `venue` is consulted only when `plan.mode` is live, and is then required — see `_broker_for`.
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

    # ⚠️ Asked here too, and for the same reason the promotion gate is: a live session with no
    # venue to trade through is knowable now, and discovering it after the warm-up would spend
    # minutes to say so. The refusal itself is not a convenience — see `_broker_for`.
    if plan.mode is SessionMode.LIVE and venue is None:
        raise EngineError(
            "refusing to start a live session with no venue: without one this would run the "
            "market through a BacktestBroker and report imaginary fills as real ones"
        )

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

    # ⚠️ **History is always replayed against a `BacktestBroker`, in both modes.** Warm-up is a
    # backtest whose ledger is thrown away (ADR-0023), and replaying it against a venue would put
    # months of decided-in-hindsight orders on a real account in the first seconds of a session.
    warm = BacktestBroker(
        instrument=spec,
        initial_capital=plan.initial_capital,
        cost_model=build_cost_model(plan.cost_model),
        take_profit_rr=take_profit_rr(definition),
    )
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

    # ⚠️ **`hand_over` asks this too, and this one decides whether a row is written.** The order
    # below is `open_session` *then* `hand_over`, so leaving the refusal to `hand_over` alone
    # would leave a `running` row behind for a session that never opened. `hand_over` keeps its
    # own guard because it is that function's invariant and it has other callers; delete this one
    # and the session opens a row before finding out — which is the failure a test pins.
    if warm.positions(symbol):
        raise EngineError(
            f"warm-up ended with an open position in {symbol}; a session cannot open mid-trade. "
            "Seed a window that ends flat, or start once the strategy is out."
        )

    # ⚠️ **The id is minted before the row exists.** See the module docstring: `MT5Broker` is
    # built with it, and `hand_over` submits — so an id that only appeared with the row would put
    # this session's first orders into `order_audit` under a session that was not yet on file.
    session_id = uuid.uuid4()
    live = _broker_for(
        plan,
        venue,
        session_id=session_id,
        spec=spec,
        cost_model=build_cost_model(plan.cost_model),
        take_profit_rr=take_profit_rr(definition),
    )

    # ⚠️ **Written before the hand-over, not after**, and in live that is the whole point of the
    # ordering. `hand_over` re-submits what the warm-up left resting — measured at 35% to 73% of
    # bars — and in live those go to the venue. A row opened afterwards would mean the riskiest
    # orders a session ever places are the ones whose audit rows have no session to point at.
    #
    # `candles.warmed` rather than `handover.bars`: they are the same number — `hand_over` is
    # handed the one and returns it as the other — and only one of them exists yet.
    with session_scope(factory) as db:
        open_session(
            db,
            strategy_id=plan.strategy_id,
            instrument_id=plan.instrument_id,
            timeframe=plan.timeframe,
            initial_capital=plan.initial_capital,
            cost_model=dict(plan.cost_model),
            engine_version=ENGINE_VERSION,
            mode=plan.mode,
            warmup_bars=candles.warmed,
            at=opened_at,
            session_id=session_id,
        )

    bars = 0
    failure: str | None = None
    watch = LedgerWatch(symbol)
    recorder = TradeRecorder(session_id, plan.instrument_id)

    # ⚠️ **The beat opens before the hand-over, not after it, and that ordering is a live bug
    # this cost an account to find.** Measured on the demo on 2026-08-28: a session warmed over
    # 39 204 bars, opened its row, and `hand_over` put its limit on the wire — and the executor
    # refused it, because `session_is_alive` saw `heartbeat_at` NULL and fell back to
    # `started_at`, which is stamped *before* the warm-up. 700 s of "silence" against a 60 s
    # limit. Three correct decisions summing to a session declared dead in the instant it first
    # acts, and it gets **worse the more history a session warms over**.
    #
    # So the heartbeat now covers every moment the session acts, not just the moments it trades.
    # The other end is unchanged and still matters: it stops before the row is finished, because
    # a beat landing after `stopped_at` would leave a stopped row claiming it was heard from
    # afterwards.
    #
    # ⚠️ The session hands over its own clock. Two clocks in one session is not a test artefact:
    # `stopped_at` and `heartbeat_at` are compared by anyone reading the row, and a beat stamped
    # from a different source than the stop can land after it.
    with session_heartbeat(factory, session_id, now=now) as beating:
        # ⚠️ **How the ordering above fails if it fails.** `Heartbeat.start()` beats on this
        # thread and swallows what that beat raises, so "the heartbeat is open" is not by itself
        # "the row says so" — a database that refused this one write would put us straight back
        # in the failure above, silently, with the strategy believing it armed an order the
        # venue was never allowed to hear about. A refusal to start, like every other one here.
        if not beating.beats:
            raise EngineError(
                "refusing to hand over: the session could not write its first heartbeat, so the "
                "executor would refuse every order it places as coming from a silent session"
            )

        handover = hand_over(
            warm, live, symbol=symbol, bars=candles.warmed, risk=risk, instrument=spec
        )
        logger.info(
            "warmed over %d bars, carried %d resting order(s), refused %d",
            handover.bars,
            len(handover.carried),
            len(handover.refused),
        )

        try:
            # ⚠️ **`refusals=` is the whole of PR-304-B-D-2b, and dropping it is silent.** The
            # warm-up and the live bars are two loops around one strategy object, and the gap
            # between them is the only place an order can be refused with no bar running to
            # report it. `compiled` marked that order placed when it emitted the signal; without
            # this argument it holds the name for the rest of the session, never offers the zone
            # again, and eventually sends a cancel for a name the venue never heard of. The
            # session logs `refused 2` while the strategy believes both are resting.
            for outcome in iter_run(
                candles=candles.live(),
                timeframe=timeframe,
                instrument=spec,
                strategy=compiled,
                broker=live,
                risk=risk,
                refusals=handover.refused,
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


def _broker_for(  # noqa: PLR0913 — the plan, the seam, and the four facts a paper broker needs
    plan: SessionPlan,
    venue: Venue | None,
    *,
    session_id: uuid.UUID,
    spec: InstrumentSpec,
    cost_model: CostModel,
    take_profit_rr: Decimal | None,
) -> Broker:
    """The broker the session itself will trade through. Paper is local; live is the venue.

    ⚠️ **The branch is on `plan.mode`, not on whether a venue was handed in**, and the difference
    is the whole safety property. `venue if venue is not None else a BacktestBroker` reads as the
    same function and is not: it makes a paper session trade a real account the moment somebody
    passes a venue by mistake, and — worse, because it is silent — it makes a *live* session run
    on imaginary fills the moment somebody forgets to. One of those loses money and the other
    reports money it never had. Modes are stated, never inferred.

    ⚠️ **Built empty rather than reset.** A `reset_ledger()` on the warm-up's broker would have to
    decide field by field what survives, and every field somebody forgets is a warm-up trade
    leaking into a live equity curve (ADR-0023). Building this one fresh makes the money untouched
    by construction: there is no path a warm-up fill could cross on, because there is no crossing.

    ⚠️ **`initial_capital` is the plan's number even in live**, and it is worth saying why rather
    than reaching for the account balance. `MT5Broker` keeps its own ledger (see its module
    docstring), and what a session risks 1% of is the capital it was *allotted*, not everything
    the account happens to hold — an account funding three sessions would otherwise have each of
    them sizing against all of it. Reconciling that ledger against the venue's own is PR-304-A5's,
    and the drift it finds is a signal precisely because the two are computed independently.
    """
    if plan.mode is not SessionMode.LIVE:
        return BacktestBroker(
            instrument=spec,
            initial_capital=plan.initial_capital,
            cost_model=cost_model,
            take_profit_rr=take_profit_rr,
        )
    if venue is None:
        # ⚠️ Unreachable through `run_session`, which refuses before the warm-up — and kept, on
        # the criterion that matters: what does removing it do? `venue` is `Venue | None`, so
        # `mypy --strict` catches the call today; the day this function grows a second caller,
        # or the type widens, an absent guard means a live session silently falling through to
        # `None()` at the one instant it is about to place real orders. A local invariant that
        # fails loudly costs one branch.
        raise EngineError("a live session needs a venue")
    return venue(session_id, spec)


def _persist(
    factory: sessionmaker[Session],
    *,
    watch: LedgerWatch,
    recorder: TradeRecorder,
    broker: LedgerView,
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
