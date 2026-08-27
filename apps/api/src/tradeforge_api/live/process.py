"""`tradeforge-session` — the process a paper session runs in.

Thin on purpose. Everything that decides anything is in `session.py`, which is drivable from a
test with no signals, no argv and no clock; this file owns only the three things a *process* has
that a function does not: the arguments it was started with, the signals it can be sent, and the
exit code it leaves behind.

⚠️ **A session is not an arq job**, and this file existing is that decision. `job_timeout` is
applied with `asyncio.wait_for`, a session runs for days, and a job that blocks the event loop
freezes the whole queue — which PR #133 documented after the collector agent did exactly that.
"""

import argparse
import logging
import signal
import sys
import threading
import types
import uuid
from decimal import Decimal

from redis import Redis

from tradeforge_api.config import Settings
from tradeforge_api.live.broker import MT5Broker
from tradeforge_api.live.candle_stream import CandleStream
from tradeforge_api.live.session import SessionPlan, Venue, reconcile_on_start, run_session
from tradeforge_collector.live import Subscription
from tradeforge_db.models import Instrument, SessionMode
from tradeforge_db.session import create_db_engine, create_session_factory, session_scope
from tradeforge_engine.domain import InstrumentSpec
from tradeforge_engine.protocols import Broker

logger = logging.getLogger(__name__)

__all__ = ["main", "stop_on_signals"]

# The signals a supervisor actually sends. `SIGTERM` is what `docker stop` and systemd use;
# `SIGINT` is Ctrl-C. Deliberately not `SIGKILL`, which cannot be caught — a session killed that
# way leaves a `running` row, and `reconcile_stale` is the answer to it, not a handler.
_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def stop_on_signals(stopping: threading.Event) -> None:
    """Ask the session to stop when the process is asked to stop.

    ⚠️ **The handler sets a flag and returns.** It does not finish the session, roll back a
    transaction or close a connection: a Python signal handler runs on the main thread between
    bytecodes, so doing real work there means doing it *in the middle of* whatever the session
    was doing — including halfway through a commit.

    The flag is read by `CandleStream` between reads, which is what bounds shutdown at
    `block_ms` rather than at the next bar. On H4 that is the difference between one minute and
    four hours.

    A **second** signal is left to the default handler on purpose. Somebody sending it twice is
    saying the orderly stop is taking too long, and the honest answer is to die — with a
    `running` row that `reconcile_stale` will settle, rather than a process that ignores its
    operator.
    """

    def handle(number: int, _frame: types.FrameType | None) -> None:
        logger.warning(
            "received %s; finishing the current bar and stopping", signal.Signals(number).name
        )
        stopping.set()
        signal.signal(number, signal.SIG_DFL)

    for number in _STOP_SIGNALS:
        signal.signal(number, handle)


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tradeforge-session",
        description="Run one session, paper or real, until it is stopped.",
    )
    parser.add_argument("--strategy", required=True, type=uuid.UUID, help="strategy id")
    parser.add_argument("--instrument", required=True, type=uuid.UUID, help="instrument id")
    parser.add_argument("--timeframe", required=True, help="e.g. H1")
    parser.add_argument("--capital", required=True, type=Decimal, help="initial capital")
    parser.add_argument(
        "--spread-points",
        type=Decimal,
        default=None,
        help="charge this fixed spread; omitted means the bar's own spread",
    )
    # ⚠️ **`--mode live` is spelled out or it does not happen.** The safe value is the default and
    # the dangerous one costs a flag — the same argument `SessionPlan.mode` and `open_session`
    # both make, and the third place it has to hold, because this is the one a human types at
    # three in the morning. `choices` rather than a `--live` switch so the paper case is
    # something an operator can also state, and so a typo is a parse error rather than a mode.
    parser.add_argument(
        "--mode",
        type=SessionMode,
        choices=list(SessionMode),
        default=SessionMode.PAPER,
        help="paper (default) trades a local ledger; live sends orders to the venue",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Start a session, run it until stopped, and report what it did."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _parse(argv)
    settings = Settings()

    engine = create_db_engine(settings.sqlalchemy_dsn)
    factory = create_session_factory(engine)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)

    # ⚠️ Before the new session, not after. The rows a dead session left behind say `running`,
    # and nothing but the next process to come up is ever going to correct them.
    reconcile_on_start(factory)

    stopping = threading.Event()
    stop_on_signals(stopping)

    # ⚠️ The stream is keyed on the *symbol*, and only the database knows which symbol an
    # instrument id is. Read here rather than inside `run_session` so that a wrong id fails
    # before a consumer group is created for a stream that will never carry anything.
    with session_scope(factory) as db:
        instrument = db.get(Instrument, args.instrument)
        if instrument is None:
            logger.error("no instrument with id %s", args.instrument)
            return 2
        subscription = Subscription(symbol=instrument.symbol, timeframe=args.timeframe)
    plan = SessionPlan(
        strategy_id=args.strategy,
        instrument_id=args.instrument,
        timeframe=args.timeframe,
        initial_capital=args.capital,
        cost_model=(
            {"type": "spread", "spread_points": str(args.spread_points)}
            if args.spread_points is not None
            else {"type": "none"}
        ),
        mode=args.mode,
    )

    try:
        outcome = run_session(
            factory=factory,
            source=_stream(redis, subscription, stopping),
            plan=plan,
            parquet_root=settings.parquet_root,
            stopping=stopping.is_set,
            venue=_venue(redis, plan.initial_capital),
            # ⚠️ Passed, where it used to fall through to `run_session`'s own default of 5. The
            # two agreed, so nothing was wrong — and nothing was configurable either: setting
            # `LIVE_PROMOTION_DAYS` moved a number no code read. A configuration option nobody
            # consults is worse than none, because it is a promise on a page.
            promotion_days=settings.live_promotion_days,
        )
    finally:
        redis.close()
        engine.dispose()

    logger.info(
        "session %s finished: %d bars, warmed over %d%s",
        outcome.session_id,
        outcome.bars,
        outcome.warmup_bars,
        f", error: {outcome.error}" if outcome.error else "",
    )
    return 1 if outcome.error else 0


def _venue(redis: Redis, initial_capital: Decimal) -> Venue:
    """How this process builds the broker a live session trades through.

    Here rather than in `session.py`, and that placement is the point of the seam existing. This
    file is where a *process* keeps the things a function does not have — its arguments, its
    signals, its clients — and the Redis connection is one of them. `session.py` stays drivable
    from a test with no server, and a session marked paper has no object to reach a venue with.

    ⚠️ **`start()` is called here, inside the callable**, so that "built" and "usable" are one
    step for whoever holds it. It reads the executor's snapshot of the account and refuses over an
    orphaned position — or over one that is absent, stale or unreadable, because "I cannot tell
    what the venue holds" is not "the venue holds nothing" (PR-304-A4). `session.py` invokes this
    at the one instant where that refusal still means no row was written and no order was sent.
    """

    def build(session_id: uuid.UUID, spec: InstrumentSpec) -> Broker:
        broker = MT5Broker(
            redis,
            session_id=str(session_id),
            instrument=spec,
            initial_capital=initial_capital,
        )
        broker.start()
        return broker

    return build


def _stream(redis: Redis, subscription: Subscription, stopping: threading.Event) -> CandleStream:
    """The session's own consumer group, reading from the beginning of the stream.

    ⚠️ `start_id="0"`, not the class default of `"$"`. The splice needs to *see* the bars that
    are already there in order to sort them into history and live; `$` would hide exactly the
    ones that fell into the gap between the last Parquet write and now.

    The group name carries a uuid because a group is a session's, not a symbol's: two sessions
    sharing one would each be handed half the bars, and each would trade half a market.
    """
    return CandleStream(
        redis,
        subscription,
        group=f"session-{uuid.uuid4().hex}",
        start_id="0",
        stopping=stopping.is_set,
    )


if __name__ == "__main__":  # pragma: no cover — the process entry point
    sys.exit(main())
