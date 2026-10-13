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
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from tradeforge_api.live.heartbeat import session_heartbeat
from tradeforge_api.live.promotion import promotion_for
from tradeforge_api.live.recorder import LedgerView, LedgerWatch, TradeRecorder, record_bar
from tradeforge_api.live.signals import NumberedSink, SignalBroker
from tradeforge_api.live.splice import BarSource, SplicedCandles, splice
from tradeforge_api.runner import (
    ENGINE_VERSION,
    build_cost_model,
    instrument_spec,
    risk_percent,
    take_profit_rr,
    timeframe_refusal,
)
from tradeforge_collector import iter_candles, step
from tradeforge_db.live_sessions import finish_session, open_session, reconcile_stale
from tradeforge_db.models import Instrument, LiveSession, SessionMode, Strategy
from tradeforge_db.session import session_scope
from tradeforge_engine import BacktestBroker, PercentRiskManager, compile_strategy
from tradeforge_engine.domain import Candle, InstrumentSpec, Refusal
from tradeforge_engine.errors import EngineError
from tradeforge_engine.loop import BarOutcome, iter_run
from tradeforge_engine.protocols import Broker, CostModel
from tradeforge_engine.protocols import Strategy as CompiledStrategy
from tradeforge_engine.warmup import HandOver, hand_over

logger = logging.getLogger(__name__)

MAX_SEAM_GAP = dt.timedelta(days=4)
"""The longest silence between the disk and the stream a session accepts: a weekend with a holiday
on either side, and nothing a market that was open could explain."""

__all__ = [
    "InheritedTrade",
    "Prepared",
    "SessionLife",
    "SessionOutcome",
    "SessionPlan",
    "Venue",
    "prepare",
    "reconcile_on_start",
    "refuse_a_hole",
    "run_session",
]

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

    no_target_r: Decimal = Decimal(5)
    """SIGNAL only: a setup with no target closes its signal at this many R (his rule, 08/10)."""

    session_id: uuid.UUID | None = None
    """The id this session will have, when the caller needs to know it **before** it exists.

    ⚠️ **Not an output moved to the input for tidiness.** A process that can be asked to stop has
    to be able to recognise a request addressed to it, and a request is addressed by id — so the
    id has to be knowable by whoever builds the stopping predicate, which happens before
    `run_session` is even called. Minted below when nobody supplied one, exactly as before.

    It also fixes something that was merely annoying: the id used to appear in the log *after*
    the warm-up, which on the last real run was 39 363 bars. An operator watching a session come
    up had nothing to type into a query for minutes."""


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


@dataclass(frozen=True, slots=True)
class Prepared:
    """A plan checked and resolved against the database, before a single bar is read."""

    plan: SessionPlan
    spec: InstrumentSpec
    symbol: str
    definition: Mapping[str, Any]
    strategy: CompiledStrategy
    risk: PercentRiskManager
    timeframe: dt.timedelta


def prepare(
    factory: sessionmaker[Session],
    plan: SessionPlan,
    *,
    venue: Venue | None,
    signals: NumberedSink | None,
    promotion_days: int,
) -> Prepared:
    """Every refusal that is knowable before the warm-up, then the strategy compiled.

    Raises on the first one. Nothing has been read or written yet, so there is nothing to undo —
    which is the reason all of these come first: each of them would otherwise spend a warm-up of
    minutes to say something that was true at the first line.
    """
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
    if plan.mode is SessionMode.SIGNAL and signals is None:
        raise EngineError("refusing to start a signal session with nowhere to post its signals")
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

    # ⚠️ **Asked here for the same reason the promotion gate above is: it is knowable now.** A
    # document carries its own `timeframe` and this plan carries another, and a setup builds its
    # higher-timeframe bars out of the document's while the loop steps at the plan's. Under an
    # `htf` filter a disagreement can leave the filter silently copying the chart
    # (`runner.timeframe_refusal`) — and a live session is the one place where that is not a
    # report to throw away but orders on an account.
    refusal = timeframe_refusal(definition, plan.timeframe)
    if refusal is not None:
        raise EngineError(f"refusing to start a session at {plan.timeframe}: {refusal}")

    return Prepared(
        plan=plan,
        spec=spec,
        symbol=symbol,
        definition=definition,
        # The bars close on the instrument's clock (30/09), the one its candles are stored on.
        strategy=compile_strategy(definition, server_offset=spec.server_offset),
        risk=PercentRiskManager(percent=risk_percent(definition)),
        timeframe=step(plan.timeframe),
    )


class _OneBar:
    """The one bar a run pulls next, handed in from outside — `engine.batch`'s `_Feed`.

    `iter_run` pulls its candles; this lets a caller **push** them instead: set `bar`, then ask the
    run for its next outcome. That is what lets one reading of a market drive several sessions
    (ADR-0033), each through the very `iter_run` it would run alone. A bar nobody set ends the run,
    exactly as the end of a list ends a `for`.
    """

    __slots__ = ("bar",)

    def __init__(self) -> None:
        self.bar: Candle | None = None

    def __iter__(self) -> "_OneBar":
        return self

    def __next__(self) -> Candle:
        if self.bar is None:
            raise StopIteration
        bar, self.bar = self.bar, None
        return bar


type InheritedRun = Callable[[Iterator[Candle], Broker], Iterator[BarOutcome]]
"""How a session runs the bars of the trade its warm-up ended in: on the warm-up's broker."""


class InheritedTrade:
    """The trade a SIGNAL session's warm-up ended in, followed a bar at a time (09/10).

    ⚠️ **A signal session does not refuse to open mid-trade.** On M5 with a 5R target the setup is
    in a trade often, and refusing left a market with no signal at all for as long as the trade
    lasted, retried on a back-off: Usa500 and UsaTec on the first day. The trade is followed on the
    warm-up's own broker, **without a word to the channels** — nobody was shown its entry — until
    it ends at its stop, its target or the setup's exit; then the hand-over happens as it always
    did, onto a broker whose account the warm-up never touched.

    Bars are pushed, one per `follow`, so a market's process can follow one session's trade while
    the others beside it are already live (ADR-0033).
    """

    def __init__(self, run: InheritedRun, warm: Broker, symbol: str) -> None:
        self._bar = _OneBar()
        self._outcomes = run(self._bar, warm)
        self._warm = warm
        self._symbol = symbol

    def follow(self, candle: Candle) -> bool:
        """Take one bar of the trade. `True` while it is still open."""
        self._bar.bar = candle
        outcome = next(self._outcomes)
        if self._warm.positions(self._symbol):
            return True
        logger.info(
            "%s: the inherited trade ended on the %s bar", self._symbol, outcome.candle.time
        )
        return False


def _past_an_inherited_trade(  # noqa: PLR0913 — the cut, the mode, and what the trade runs on
    candles: SplicedCandles,
    *,
    mode: SessionMode,
    timeframe: str,
    warm: Broker,
    symbol: str,
    run: InheritedRun,
    stopping: Callable[[], bool],
) -> Iterator[Candle]:
    """The session's live bars — after, for a signal session, the trade the warm-up was in.

    Refuses first a hole between the disk and the live bars (`refuse_a_hole`). Other modes than
    signal are untouched: paper and live still refuse further on, because they would report a
    trade they never took. See `InheritedTrade` for why a signal session does not.
    """
    refuse_a_hole(candles, symbol, timeframe)
    stream = candles.live()
    if mode is not SessionMode.SIGNAL or not warm.positions(symbol):
        return stream
    logger.info("%s: the warm-up ended in a trade; following it quietly until it ends", symbol)
    trade = InheritedTrade(run, warm, symbol)
    for candle in stream:
        if not trade.follow(candle):
            break
        if stopping():
            raise EngineError(f"{symbol}: stopped while following the trade the warm-up was in")
    return stream


def refuse_a_hole(candles: SplicedCandles, symbol: str, timeframe: str) -> None:
    """Refuse to open over a hole between the candles on disk and the live stream.

    ⚠️ The live loop fills it from the terminal; when it could not, the strategy's averages run
    straight from the last bar on disk into today's, and the first "turn" it sees is the hole
    itself — the first live signal ever posted (Usa500, a week of M5 missing, 09/10) was exactly
    that.
    """
    if candles.seam_gap is not None and candles.seam_gap > MAX_SEAM_GAP:
        raise EngineError(
            f"refusing to open: {candles.seam_gap} of {symbol} {timeframe} missing between "
            "the candles on disk and the live stream; collect the series again, then restart"
        )


class SessionLife:
    """One session, a bar at a time: warm up, open, trade, finish (ADR-0033).

    `run_session` drives one of these alone; `live.market` drives several side by side, one bar
    read once and handed to each. **The order of a session's steps, and every reason for it, is
    written here once** — whoever drives it only decides where the bars come from.

    Pushed rather than pulled (`_OneBar`): the caller hands each bar in. A session alone and a
    session among others of its market therefore run the same `iter_run`, over the same bars.
    """

    def __init__(
        self,
        prepared: Prepared,
        *,
        factory: sessionmaker[Session],
        venue: Venue | None = None,
        signals: NumberedSink | None = None,
        now: Callable[[], dt.datetime] = _utcnow,
    ) -> None:
        self.prepared = prepared
        self._factory = factory
        self._venue = venue
        self._signals = signals
        self._now = now
        plan = prepared.plan
        # ⚠️ **The id is minted before the row exists.** `MT5Broker` is built with it, and
        # `hand_over` submits — so an id that only appeared with the row would put this session's
        # first orders into `order_audit` under a session that was not yet on file. And earlier
        # still when the caller supplies one: a stop request is addressed by id, so whoever builds
        # the stopping predicate needs the name before the session starts. `is None` rather than
        # `or`, because the fallback must trigger on *absence* and nothing else.
        self.session_id = plan.session_id if plan.session_id is not None else uuid.uuid4()
        self.bars = 0
        # ⚠️ **History is always replayed against a `BacktestBroker`, in every mode.** Warm-up is a
        # backtest whose ledger is thrown away (ADR-0023), and replaying it against a venue would
        # put months of decided-in-hindsight orders on a real account in the first seconds.
        self._warm: BacktestBroker | None = BacktestBroker(
            instrument=prepared.spec,
            initial_capital=plan.initial_capital,
            cost_model=build_cost_model(plan.cost_model),
            take_profit_rr=take_profit_rr(prepared.definition),
        )
        self._bar = _OneBar()
        self._warming: Iterator[BarOutcome] | None = self.inherited_run(self._bar, self._warm)
        self._live: Iterator[BarOutcome] | None = None
        self._broker: Broker | None = None
        self._handover: HandOver | None = None
        self._beat = ExitStack()
        self._watch = LedgerWatch(prepared.symbol)
        self._recorder = TradeRecorder(self.session_id, plan.instrument_id)

    @property
    def warm(self) -> BacktestBroker:
        """The warm-up's broker, until the hand-over lets it go."""
        if self._warm is None:
            raise RuntimeError("the warm-up's broker was let go at the hand-over")
        return self._warm

    def inherited_run(self, bars: Iterator[Candle], broker: Broker) -> Iterator[BarOutcome]:
        """This session's strategy over `bars`, on `broker` — the warm-up's, both for the history
        and for the trade the history ended in."""
        prepared = self.prepared
        return iter_run(
            candles=bars,
            timeframe=prepared.timeframe,
            instrument=prepared.spec,
            strategy=prepared.strategy,
            broker=broker,
            risk=prepared.risk,
        )

    def warm_bar(self, candle: Candle) -> None:
        """One bar of history, through the strategy, on the warm-up's broker."""
        if self._warming is None:
            raise RuntimeError("the warm-up is over")
        self._bar.bar = candle
        next(self._warming)

    def end_warmup(self) -> None:
        """Take the warm-up past its last bar, as a `for` over the history would (`run_batch`)."""
        if self._warming is not None:
            with suppress(StopIteration):
                next(self._warming)
        self._warming = None

    def inherited_trade(self) -> InheritedTrade | None:
        """The trade a SIGNAL session's warm-up ended in, to follow before it opens; `None` when
        it ended flat, or when the session is not a signal one (those refuse in `open`)."""
        symbol = self.prepared.symbol
        if self.prepared.plan.mode is not SessionMode.SIGNAL or not self.warm.positions(symbol):
            return None
        logger.info("%s: the warm-up ended in a trade; following it quietly until it ends", symbol)
        return InheritedTrade(self.inherited_run, self.warm, symbol)

    def open(self, *, warmed: int, recent: Sequence[Candle], opened_at: dt.datetime) -> None:
        """Build the session's broker, write its row, start its heartbeat, hand over.

        Raises on a refusal; once the row is written, a refusal leaves it `running` for
        `reconcile_stale` to settle — the same as a process that died there.
        """
        plan, spec, symbol = self.prepared.plan, self.prepared.spec, self.prepared.symbol
        warm = self.warm
        # ⚠️ **`hand_over` asks this too, and this one decides whether a row is written.** The
        # order below is `open_session` *then* `hand_over`, so leaving the refusal to `hand_over`
        # alone would leave a `running` row behind for a session that never opened. `hand_over`
        # keeps its own guard because it is that function's invariant and it has other callers;
        # delete this one and the session opens a row before finding out — which a test pins.
        if warm.positions(symbol):
            raise EngineError(
                f"warm-up ended with an open position in {symbol}; a session cannot open "
                "mid-trade. Seed a window that ends flat, or start once the strategy is out."
            )
        live = _broker_for(
            plan,
            self._venue,
            session_id=self.session_id,
            spec=spec,
            cost_model=build_cost_model(plan.cost_model),
            take_profit_rr=take_profit_rr(self.prepared.definition),
            signals=self._signals,
            recent=tuple(recent),
        )

        # ⚠️ **Written before the hand-over, not after**, and in live that is the whole point of
        # the ordering. `hand_over` re-submits what the warm-up left resting — measured at 35% to
        # 73% of bars — and in live those go to the venue. A row opened afterwards would mean the
        # riskiest orders a session ever places are the ones whose audit rows have no session.
        with session_scope(self._factory) as db:
            open_session(
                db,
                strategy_id=plan.strategy_id,
                instrument_id=plan.instrument_id,
                timeframe=plan.timeframe,
                initial_capital=plan.initial_capital,
                cost_model=dict(plan.cost_model),
                engine_version=ENGINE_VERSION,
                mode=plan.mode,
                warmup_bars=warmed,
                at=opened_at,
                session_id=self.session_id,
            )

        # ⚠️ **The beat opens before the hand-over, not after it, and that ordering is a live bug
        # this cost an account to find.** Measured on the demo on 2026-08-28: a session warmed
        # over 39 204 bars, opened its row, and `hand_over` put its limit on the wire — and the
        # executor refused it, because `session_is_alive` saw `heartbeat_at` NULL and fell back to
        # `started_at`, which is stamped *before* the warm-up. 700 s of "silence" against a 60 s
        # limit. So the heartbeat covers every moment the session acts, not just the moments it
        # trades — and it stops before the row is finished (`finish`), because a beat landing
        # after `stopped_at` would leave a stopped row claiming it was heard from afterwards.
        #
        # ⚠️ The session hands over its own clock. `stopped_at` and `heartbeat_at` are compared by
        # anyone reading the row, and a beat stamped from another clock can land after the stop.
        beating = self._beat.enter_context(
            session_heartbeat(self._factory, self.session_id, now=self._now)
        )
        try:
            # ⚠️ **How the ordering above fails if it fails.** `Heartbeat.start()` beats on this
            # thread and swallows what that beat raises, so "the heartbeat is open" is not by
            # itself "the row says so" — a database that refused this one write would put us
            # straight back in the failure above, silently.
            if not beating.beats:
                raise EngineError(
                    "refusing to hand over: the session could not write its first heartbeat, so "
                    "the executor would refuse every order it places as coming from a silent "
                    "session"
                )
            handover = hand_over(
                warm, live, symbol=symbol, bars=warmed, risk=self.prepared.risk, instrument=spec
            )
        except BaseException:
            self._beat.close()
            raise
        logger.info(
            "%s %s: warmed over %d bars, carried %d resting order(s), refused %d",
            symbol,
            plan.timeframe,
            handover.bars,
            len(handover.carried),
            len(handover.refused),
        )
        # ⚠️ **Let the warm-up's broker go (12/10).** Its job ended with the hand-over, and this
        # session lives for days, so keeping it kept its whole book of warm-up trades. Measured:
        # ~20 MB on USDJPY M5 (2 179 warm-up trades), ~140 MB on Usa500 M5 while the history was
        # still read as one list. Nothing below may reach it: the session trades through `live`.
        self._warm = None
        self._handover = handover
        self._broker = live
        # ⚠️ **`refusals=` is the whole of PR-304-B-D-2b, and dropping it is silent.** The warm-up
        # and the live bars are two loops around one strategy object, and the gap between them is
        # the only place an order can be refused with no bar running to report it. The strategy
        # marked that order placed when it emitted the signal; without this argument it holds the
        # name for the rest of the session, never offers the zone again, and eventually sends a
        # cancel for a name the venue never heard of.
        self._live = iter_run(
            candles=self._bar,
            timeframe=self.prepared.timeframe,
            instrument=spec,
            strategy=self.prepared.strategy,
            broker=live,
            risk=self.prepared.risk,
            refusals=handover.refused,
        )

    def live_bar(self, candle: Candle) -> None:
        """One live bar: through the strategy on the session's own broker, then recorded."""
        if self._live is None or self._broker is None:
            raise RuntimeError("the session has not opened")
        self._bar.bar = candle
        outcome = next(self._live)
        _persist(
            self._factory,
            watch=self._watch,
            recorder=self._recorder,
            broker=self._broker,
            at=outcome.candle.time,
        )
        self.bars += 1

    def end_live(self) -> None:
        """Take the session past its last bar, for a stream that ended rather than was stopped."""
        if self._live is not None:
            with suppress(StopIteration):
                next(self._live)

    def close(self) -> None:
        """Stop the heartbeat. Safe to call more than once; `finish` calls it first."""
        self._beat.close()

    def finish(self, error: str | None = None) -> SessionOutcome:
        """Stop the heartbeat, then mark the row: `stopped`, or `failed` with `error`."""
        if self._handover is None:
            raise RuntimeError("the session has not opened")
        self.close()
        with session_scope(self._factory) as db:
            finish_session(db, self.session_id, at=self._now(), error=error)
        return SessionOutcome(
            session_id=self.session_id,
            warmup_bars=self._handover.bars,
            bars=self.bars,
            refused_orders=self._handover.refused,
            error=error,
        )


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
    signals: NumberedSink | None = None,
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
    The steps themselves are `SessionLife`'s; this drives one session alone, pulling its bars
    from `source`.
    """
    opened_at = now()
    prepared = prepare(factory, plan, venue=venue, signals=signals, promotion_days=promotion_days)
    candles = splice(
        source,
        history=lambda: iter_candles(parquet_root, prepared.symbol, plan.timeframe),
        timeframe=prepared.timeframe,
        opened_at=opened_at,
    )
    life = SessionLife(prepared, factory=factory, venue=venue, signals=signals, now=now)
    try:
        for candle in candles.warmup():
            life.warm_bar(candle)
        life.end_warmup()
        stream = _past_an_inherited_trade(
            candles,
            mode=plan.mode,
            timeframe=plan.timeframe,
            warm=life.warm,
            symbol=prepared.symbol,
            run=life.inherited_run,
            stopping=stopping,
        )
        # `candles.warmed` rather than the hand-over's own count: they are the same number —
        # `hand_over` is handed the one and returns it as the other — and only one exists yet.
        life.open(warmed=candles.warmed, recent=candles.recent, opened_at=opened_at)
        failure: str | None = None
        try:
            for candle in stream:
                life.live_bar(candle)
                if stopping():
                    break
            else:
                life.end_live()
        except Exception as exc:  # a failed session is a recorded result, not a crash
            failure = f"{type(exc).__name__}: {exc}"
            logger.exception("session %s failed after %d bars", life.session_id, life.bars)
        return life.finish(failure)
    finally:
        life.close()


def _broker_for(  # noqa: PLR0913 — the plan, the seam, and the four facts a paper broker needs
    plan: SessionPlan,
    venue: Venue | None,
    *,
    session_id: uuid.UUID,
    spec: InstrumentSpec,
    cost_model: CostModel,
    take_profit_rr: Decimal | None,
    signals: NumberedSink | None = None,
    recent: Sequence[Candle] = (),
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
        simulated = BacktestBroker(
            instrument=spec,
            initial_capital=plan.initial_capital,
            cost_model=cost_model,
            take_profit_rr=take_profit_rr,
        )
        if plan.mode is not SessionMode.SIGNAL:
            return simulated
        if signals is None:  # refused by `run_session` before the warm-up; kept for the same
            raise EngineError("a signal session needs somewhere to post")  # reason as below
        # Imported here: only a signal session draws, and matplotlib is heavy to load.
        from tradeforge_api.live.signal_image import render_signal_png  # noqa: PLC0415

        # Paper, exactly — the wrapper only watches (signals PR 5), and draws (PR 7).
        return SignalBroker(
            simulated,
            signals,
            number=signals.number,
            take_profit_rr=take_profit_rr,
            no_target_r=plan.no_target_r,
            picture=render_signal_png,
            recent=recent,
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
