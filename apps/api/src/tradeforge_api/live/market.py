"""Every signal session of one market, in one process (ADR-0033).

A market here is an instrument on a timeframe — one bar stream. Each setup followed on it is still
a whole session: its own row in `live_sessions`, its own id, strategy, broker and signals, the same
`SessionLife` a session alone runs. What changes is only that the market's history is read once
and each bar is handed to every session in turn, instead of every session reading the same history
and the same stream in a process of its own. Measured on the live setups (13/10): WIN M5, four
setups, 386 MB committed against 1 420 MB apart; trades and balances identical, setup by setup.

**It tells the supervisor what it carries** (`live.fleet.market_state_key`): its phase and the
sessions still in it, on every stream read and every bar, so a session that left without being
asked is seen as failed and launched again on its own.

**One session's failure is that session's alone**, as in `engine.batch`: a setup that raises is
recorded `failed` with its reason and leaves the market, and the others go on. A failure of what
they all share — the stream, the disk, the hole between them — ends every one of them, because
every one of them reads it.

Signal sessions only. Paper and live keep their process each (`live.process`): the path that can
reach an account gains nothing from sharing one, and would only gain risk.
"""

import argparse
import datetime as dt
import json
import logging
import sys
import threading
import uuid
from collections.abc import Callable, Collection, Iterable, Sequence
from decimal import Decimal
from pathlib import Path

from redis import Redis
from sqlalchemy.orm import Session, sessionmaker

from tradeforge_api.config import Settings
from tradeforge_api.live.candle_stream import CandleStream
from tradeforge_api.live.fleet import MARKET_STATE_TTL, market_state_key
from tradeforge_api.live.outage import Outage
from tradeforge_api.live.process import stop_on_signals
from tradeforge_api.live.session import (
    InheritedTrade,
    SessionLife,
    SessionOutcome,
    SessionPlan,
    prepare,
    reconcile_on_start,
    refuse_a_hole,
)
from tradeforge_api.live.signals import NumberedSink, RedisSignalSink
from tradeforge_api.live.splice import BarSource, SplicedCandles, splice
from tradeforge_api.live.stop import stop_requested
from tradeforge_collector import iter_candles
from tradeforge_collector.demand import DemandLease
from tradeforge_collector.live import Subscription
from tradeforge_db.brokers import broker_slug_for_symbol
from tradeforge_db.models import Instrument, SessionMode, Strategy
from tradeforge_db.session import create_db_engine, create_session_factory, session_scope
from tradeforge_engine.domain import Candle
from tradeforge_engine.errors import EngineError

logger = logging.getLogger(__name__)

__all__ = ["Market", "main"]


REPORT_EVERY_WARM_BARS = 20_000
"""How often a warm-up that takes minutes says it is still alive and what it is warming."""


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _say_nothing(_phase: str, _sessions: Collection[uuid.UUID]) -> None:
    """For a market nobody supervises (a test, a process started by hand without `--key`)."""


class Market:
    """The sessions of one market, driven a bar at a time from one reading of it."""

    def __init__(  # noqa: PLR0913 — keyword-only; each one names a seam of the market's process
        self,
        *,
        factory: sessionmaker[Session],
        plans: Sequence[SessionPlan],
        parquet_root: Path,
        signals_for: Callable[[SessionPlan], NumberedSink],
        signalled: Callable[[], bool],
        asked_to_stop: Callable[[uuid.UUID], bool],
        now: Callable[[], dt.datetime] = _utcnow,
        promotion_days: int = 5,
        publish: Callable[[str, Collection[uuid.UUID]], None] = _say_nothing,
    ) -> None:
        if not plans:
            raise ValueError("a market needs at least one session")
        markets = {(plan.instrument_id, plan.timeframe) for plan in plans}
        if len(markets) > 1:
            raise ValueError(f"one market per process, not {len(markets)}: {sorted(markets)}")
        if any(plan.mode is not SessionMode.SIGNAL for plan in plans):
            raise ValueError("a market's process runs signal sessions only (ADR-0033)")
        if any(plan.session_id is None for plan in plans):
            raise ValueError("every session needs its id before it starts, to be asked to stop")
        self._factory = factory
        self._plans = tuple(plans)
        self._parquet_root = parquet_root
        self._signals_for = signals_for
        self._signalled = signalled
        self._asked_to_stop = asked_to_stop
        self._now = now
        self._promotion_days = promotion_days
        self._publish = publish
        self._telling = Outage(logger)
        self.outcomes: list[SessionOutcome] = []
        self.failures = 0
        """Sessions that ended on an error — refused, failed in the warm-up, or failed live."""
        self._live: list[SessionLife] = []
        self._following: dict[uuid.UUID, tuple[SessionLife, InheritedTrade]] = {}
        self._candles: SplicedCandles | None = None
        self._opened_at: dt.datetime | None = None

    @property
    def sessions(self) -> int:
        """Sessions still running: live, or following the trade their warm-up ended in."""
        return len(self._live) + len(self._following)

    def should_stop(self) -> bool:
        """Handed to the stream, which asks between reads — every minute at most, bar or not.

        Finishes every session asked to stop, then says whether the market is done: signalled, or
        with nobody left. ⚠️ **A session asked to stop is finished here, not at the next bar**: on
        H4 that bar is four hours away, and the supervisor would have killed the whole market for
        one setup turned off long before.
        """
        self._settle_stops()
        self._report()
        return self._signalled() or (self._candles is not None and self.sessions == 0)

    def run(self, source: BarSource) -> list[SessionOutcome]:
        """Warm every session over the market's history, then hand each live bar to each."""
        opened_at = self._now()
        lives = [life for plan in self._plans if (life := self._start(plan)) is not None]
        if not lives:
            raise EngineError("no session of this market could start")
        first = lives[0].prepared
        candles = splice(
            source,
            history=lambda: iter_candles(self._parquet_root, first.symbol, first.plan.timeframe),
            timeframe=first.timeframe,
            opened_at=opened_at,
        )
        self._report("warming", lives)
        warming = self._warm(lives, candles)
        # The hole is the market's, not a session's: every one of them would read it.
        refuse_a_hole(candles, first.symbol, first.plan.timeframe)
        stream = candles.live()
        self._candles, self._opened_at = candles, opened_at
        for life in warming:
            trade = life.inherited_trade()
            if trade is None:
                self._open(life)
            else:
                self._following[life.session_id] = (life, trade)
        self._report()

        failure: str | None = None
        try:
            for candle in stream:
                self._bar(candle)
                self._settle_stops()
                self._report()
                if self._signalled() or self.sessions == 0:
                    break
            else:
                for life in list(self._live):
                    self._guarded(life, life.end_live)
        except Exception as exc:  # the stream every session reads: it ends every one of them
            failure = f"{type(exc).__name__}: {exc}"
            logger.exception("%s %s: the market failed", first.symbol, first.plan.timeframe)
        finally:
            for life, _trade in self._following.values():
                life.close()
            self._following.clear()
        for life in self._live:
            self._finish(life, failure)
        self._live.clear()
        return self.outcomes

    def _start(self, plan: SessionPlan) -> SessionLife | None:
        """A session's refusals before the warm-up (`prepare`); refused alone, the others go on."""
        sink = self._signals_for(plan)
        try:
            prepared = prepare(
                self._factory,
                plan,
                venue=None,
                signals=sink,
                promotion_days=self._promotion_days,
            )
        except Exception:
            self.failures += 1
            logger.exception("session %s refused to start; the others go on", plan.session_id)
            return None
        return SessionLife(prepared, factory=self._factory, signals=sink, now=self._now)

    def _warm(self, lives: list[SessionLife], candles: SplicedCandles) -> list[SessionLife]:
        """The history read once, each bar handed to every session still warming."""
        warming = list(lives)
        for index, candle in enumerate(candles.warmup(), start=1):
            for life in list(warming):
                if not self._guarded(life, life.warm_bar, candle):
                    warming.remove(life)
            if index % REPORT_EVERY_WARM_BARS == 0:
                self._report("warming", warming)
        return [life for life in warming if self._guarded(life, life.end_warmup)]

    def _bar(self, candle: Candle) -> None:
        """One live bar for everybody: the open sessions first, then those following a trade.

        ⚠️ **In that order, and it is load-bearing.** A session whose inherited trade ends on this
        bar opens *after* it, so its first bar of its own is the next one — exactly as alone,
        where `_past_an_inherited_trade` hands the stream over after the bar the trade ended on.
        Handing it this bar too would run one bar twice, once on each broker.
        """
        for life in list(self._live):
            try:
                life.live_bar(candle)
            except Exception as exc:  # this session's failure, not the market's
                logger.exception("session %s failed after %d bars", life.session_id, life.bars)
                self._live.remove(life)
                self._finish(life, f"{type(exc).__name__}: {exc}")
        for session_id, (life, trade) in list(self._following.items()):
            try:
                still = trade.follow(candle)
            except Exception:
                logger.exception("session %s failed following its inherited trade", session_id)
                del self._following[session_id]
                life.close()
                self.failures += 1
                continue
            if not still:
                del self._following[session_id]
                self._open(life)

    def _open(self, life: SessionLife) -> None:
        if self._candles is None or self._opened_at is None:
            raise RuntimeError("the market has not finished its warm-up")
        try:
            life.open(
                warmed=self._candles.warmed, recent=self._candles.recent, opened_at=self._opened_at
            )
        except Exception:
            logger.exception("session %s could not open; the others go on", life.session_id)
            life.close()
            self.failures += 1
            return
        self._live.append(life)

    def _report(self, phase: str | None = None, sessions: Iterable[SessionLife] = ()) -> None:
        """Tell the supervisor what this process carries: `sessions` while warming, every one
        live or following its inherited trade after. Never fatal — a supervisor that cannot be
        told goes on believing the last thing it heard until that lapses, and the sessions trade."""
        carried = list(sessions) or [
            *self._live,
            *(life for life, _trade in self._following.values()),
        ]
        try:
            self._publish(
                phase or ("live" if self._candles is not None else "warming"),
                [life.session_id for life in carried],
            )
        except Exception:  # noqa: BLE001 — see the docstring
            self._telling.lost("telling the supervisor what this market carries")
        else:
            self._telling.back()

    def _settle_stops(self) -> None:
        """Finish every session somebody asked to stop (`live-session:<id>:stop`)."""
        for life in list(self._live):
            if self._asked_to_stop(life.session_id):
                self._live.remove(life)
                self._finish(life, None)
        for session_id, (life, _trade) in list(self._following.items()):
            if self._asked_to_stop(session_id):
                # Nothing was opened yet, so there is no row to finish: alone it would have
                # refused to start ("stopped while following"), and that is what this is.
                logger.info("session %s stopped while following its inherited trade", session_id)
                del self._following[session_id]
                life.close()

    def _finish(self, life: SessionLife, error: str | None) -> None:
        try:
            self.outcomes.append(life.finish(error))
        finally:
            life.close()
        if error is not None:
            self.failures += 1

    def _guarded(self, life: SessionLife, step: Callable[..., None], *args: Candle) -> bool:
        """Run one step of one session; `False` when it raised and the session is out."""
        try:
            step(*args)
        except Exception:
            logger.exception("session %s failed in its warm-up; the others go on", life.session_id)
            life.close()
            self.failures += 1
            return False
        return True


# --------------------------------------------------------------------------- the process


def _id_of(plan: SessionPlan) -> uuid.UUID:
    if plan.session_id is None:  # refused by `Market` before anything runs
        raise ValueError("a market's session needs its id")
    return plan.session_id


def _member(text: str) -> dict[str, str | None]:
    """One `--member`: a JSON object with `strategy`, `setup`, `session`, `no_target_r` and
    `spread_points` (or null for the bar's own spread)."""
    value = json.loads(text)
    if not isinstance(value, dict) or not {"strategy", "setup", "session"} <= value.keys():
        raise argparse.ArgumentTypeError("a member is {strategy, setup, session, ...} as JSON")
    return value


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tradeforge-market",
        description="Run every signal session of one market (instrument + timeframe) together.",
    )
    parser.add_argument(
        "--key",
        default=None,
        help="the supervisor's name for this process; tells it nothing without",
    )
    parser.add_argument("--instrument", required=True, type=uuid.UUID, help="instrument id")
    parser.add_argument("--timeframe", required=True, help="e.g. M5")
    parser.add_argument("--capital", required=True, type=Decimal, help="each session's capital")
    parser.add_argument(
        "--member", required=True, action="append", type=_member, help="one session, as JSON"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the market's sessions until stopped; `1` when any of them ended on an error."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _parse(argv)
    settings = Settings()
    engine = create_db_engine(settings.sqlalchemy_dsn)
    factory = create_session_factory(engine)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    reconcile_on_start(factory)
    stopping = threading.Event()
    stop_on_signals(stopping)

    with session_scope(factory) as db:
        instrument = db.get(Instrument, args.instrument)
        if instrument is None:
            logger.error("no instrument with id %s", args.instrument)
            return 2
        subscription = Subscription(symbol=instrument.symbol, timeframe=args.timeframe)
        broker = broker_slug_for_symbol(db, instrument.symbol)
        names: dict[str, str] = {}
        for member in args.member:
            strategy_id = str(member["strategy"])
            found = db.get(Strategy, uuid.UUID(strategy_id))
            names[strategy_id] = found.name if found is not None else strategy_id

    plans: list[SessionPlan] = []
    sinks: dict[uuid.UUID, NumberedSink] = {}
    for member in args.member:
        session_id = uuid.UUID(str(member["session"]))
        no_target_r = Decimal(str(member.get("no_target_r") or 5))
        spread = member.get("spread_points")
        plans.append(
            SessionPlan(
                strategy_id=uuid.UUID(str(member["strategy"])),
                instrument_id=args.instrument,
                timeframe=args.timeframe,
                initial_capital=args.capital,
                cost_model=(
                    {"type": "spread", "spread_points": str(spread)}
                    if spread is not None
                    else {"type": "none"}
                ),
                mode=SessionMode.SIGNAL,
                session_id=session_id,
                no_target_r=no_target_r,
            )
        )
        # The signals' context, per session: what a message needs beside the event itself.
        sinks[session_id] = RedisSignalSink(
            redis,
            {
                "session_id": str(session_id),
                "setup_id": str(member["setup"]),
                "strategy_id": str(member["strategy"]),
                "instrument_id": str(args.instrument),
                "strategy": names[str(member["strategy"])],
                "timeframe": args.timeframe,
                "broker": broker or "",
                "no_target_r": str(no_target_r),
            },
        )

    def publish(phase: str, sessions: Collection[uuid.UUID]) -> None:
        if args.key is None:
            return
        word = {
            "phase": phase,
            "sessions": [str(one) for one in sessions],
            "at": dt.datetime.now(dt.UTC).isoformat(),
        }
        redis.set(market_state_key(args.key), json.dumps(word), ex=MARKET_STATE_TTL)

    market = Market(
        factory=factory,
        plans=plans,
        parquet_root=settings.parquet_root,
        signals_for=lambda plan: sinks[_id_of(plan)],
        signalled=stopping.is_set,
        asked_to_stop=lambda session_id: stop_requested(redis, session_id),
        promotion_days=settings.live_promotion_days,
        publish=publish,
    )
    # One consumer group for the market: every session reads the bars this one group is handed.
    source = CandleStream(
        redis,
        subscription,
        group=f"market-{uuid.uuid4().hex}",
        start_id="0",
        stopping=market.should_stop,
    )
    lease = None if broker is None else DemandLease(redis, broker, subscription).start()
    logger.info("%s: %d session(s) starting; warming up before they open", subscription, len(plans))
    try:
        outcomes = market.run(source)
    finally:
        if lease is not None:
            lease.stop()
        redis.close()
        engine.dispose()
    for outcome in outcomes:
        logger.info(
            "session %s finished: %d bars, warmed over %d%s",
            outcome.session_id,
            outcome.bars,
            outcome.warmup_bars,
            f", error: {outcome.error}" if outcome.error else "",
        )
    return 1 if market.failures else 0


if __name__ == "__main__":  # pragma: no cover — the process entry point
    sys.exit(main())
