"""`tradeforge-executor` — the process that sends orders, and the switches that stop it.

Thin, like `tradeforge-session`: everything that decides anything is in `safety`, `router`,
`ledger` and `service`, all runnable with no terminal. This file owns argv, signals, an exit
code, and the one thing only a process can own — **assembling the three kill-switch layers**.

⚠️ **`--dry-run` is the default.** Sending is opt-in, spelled `--arm`, and that asymmetry is the
point: the first `order_send` of this project's life should be a thing somebody typed, not a
thing that happened because a service came up with its usual flags.
"""

import argparse
import datetime as dt
import logging
import signal
import sys
import threading
import types
from decimal import Decimal

from redis import Redis
from sqlalchemy.orm import Session, sessionmaker

from tradeforge_db.session import create_db_engine, create_session_factory
from tradeforge_engine.domain import OrderRequest
from tradeforge_executor.config import ExecutorSettings
from tradeforge_executor.deals import DealWatch
from tradeforge_executor.gateway import MT5Gateway, OrderGateway, Placement
from tradeforge_executor.kill_switch import EndpointFlag, FileFlag, RedisFlag
from tradeforge_executor.ledger import deal_was_reported, session_for
from tradeforge_executor.router import Router
from tradeforge_executor.safety import KillSwitch
from tradeforge_executor.service import GROUP, OrderQueue, Service
from tradeforge_executor.snapshot import VenueSnapshot
from tradeforge_executor.wire import HeldPosition

logger = logging.getLogger(__name__)

__all__ = ["RefusingGateway", "main", "stop_on_signals"]

_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _session_of(factory: sessionmaker[Session], client_id: str) -> str | None:
    """Which session armed `client_id`, on a connection of this scan's own.

    ⚠️ **Its own session, opened and closed per question**, because the caller is another thread.
    A SQLAlchemy `Session` is not safe to share across threads, and `DealWatch` runs beside the
    order loop rather than inside it — handing it the loop's session would make two threads write
    through one connection, which fails intermittently and under load, which is to say when a
    session is busiest.

    ⚠️ **A database this cannot reach raises, and does not answer `None`.** The two are different
    facts and collapsing them loses a position for ever: `None` means "asked, and no session owns
    this name", which is final; a failed query means "could not ask", which is not. `DealWatch`
    reads the difference — a raise leaves the deal for the next scan, a `None` retires it — and an
    earlier draft returned `None` for both, so a `docker compose restart postgres` during a fill
    would have buried that fill behind the watermark permanently.
    """
    with factory() as db:
        return session_for(db, client_id)


def _already_reported(factory: sessionmaker[Session], ticket: int) -> bool:
    """Did the order loop already publish this deal? On its own connection, like `_session_of`.

    ⚠️ **Raises rather than guessing, like `_session_of`.** An earlier draft answered `True` on a
    failed query — "assume it was reported, do not publish" — which is safe for that one scan and
    permanent afterwards, because the watermark moved on regardless. A raise says "could not ask",
    and the scan leaves the deal where it is.
    """
    with factory() as db:
        return deal_was_reported(db, ticket)


class RefusingGateway:
    """An `OrderGateway` that reads the account and **never sends**. The default.

    ⚠️ Not a mock and not a test double — it is the production behaviour of an unarmed executor.
    Everything upstream runs for real: the queue is drained, the safeguards are asked, and the
    audit trail records what *would* have gone out and why it was allowed. The only thing that
    does not happen is the one thing that cannot be undone.

    A dry run that skipped the decisions would prove nothing about the day it is armed.
    """

    def __init__(self, inner: OrderGateway) -> None:
        self._inner = inner

    def send(self, order: OrderRequest, *, client_id: str) -> Placement:
        logger.warning(
            "DRY RUN: would send %s %s %s (%s) — pass --arm to send for real",
            order.side.value,
            order.volume,
            order.symbol,
            client_id,
        )
        return Placement(
            accepted=False,
            ticket=None,
            filled_volume=Decimal(0),
            price=None,
            retcode=0,
            comment="dry run: the executor is not armed",
            raw={"dry_run": True},
        )

    def withdraw(self, client_id: str) -> Placement:
        """⚠️ Refused, like `send`, and for the same reason — but the reason deserves a sentence,
        because a cancel *reduces* risk and the rest of this system waves those through.

        A dry run has placed nothing. There is therefore nothing of this executor's at the venue
        to withdraw, and a `TRADE_ACTION_REMOVE` from an unarmed process could only reach an
        order somebody else put there. "Touch nothing" is the whole promise of the flag.
        """
        logger.warning("DRY RUN: would withdraw %s — pass --arm to act for real", client_id)
        return Placement(
            accepted=False,
            ticket=None,
            filled_volume=Decimal(0),
            price=None,
            retcode=0,
            comment="dry run: the executor is not armed",
            raw={"dry_run": True, "withdraw": client_id},
        )

    def holdings(self) -> tuple[HeldPosition, ...]:
        """A **read**, so it passes through. The venue snapshot must be true even when this
        executor is unarmed — a session refusing to start over an orphaned position needs the
        real answer, and a dry run that reported an empty account would wave it through."""
        return self._inner.holdings()

    def held(self, symbol: str) -> HeldPosition | None:
        """⚠️ A **read**, so it passes through like the others. An unarmed executor must still be
        able to say what the account is holding — that is how an operator checks a dry run against
        reality, and inventing an empty answer would make the dry run agree with itself."""
        return self._inner.held(symbol)

    def tighten(self, ticket: int, stop_loss: Decimal) -> Placement:
        """Refused, like `send` and `withdraw`. A dry run has opened nothing, so any ticket it
        could act on belongs to somebody else — and `TRADE_ACTION_SLTP` carries no magic, so the
        instruction itself would not object."""
        logger.warning(
            "DRY RUN: would move the stop of position %s to %s — pass --arm to act for real",
            ticket,
            stop_loss,
        )
        return Placement(
            accepted=False,
            ticket=ticket,
            filled_volume=Decimal(0),
            price=None,
            retcode=0,
            comment="dry run: the executor is not armed",
            raw={"dry_run": True, "tighten": ticket},
        )

    # ⚠️ The reads pass straight through, and they must. The safeguards judge a real account —
    # a dry run against invented numbers would exercise the limits against a fiction and prove
    # nothing about the day this is armed.
    def balance(self) -> Decimal:
        return self._inner.balance()

    def open_positions(self) -> int:
        return self._inner.open_positions()

    def realised_since(self, moment: dt.datetime) -> Decimal:
        return self._inner.realised_since(moment)


def stop_on_signals(stopping: threading.Event) -> None:
    """Ask the loop to stop when the process is asked to stop.

    The handler sets a flag and returns — a Python handler runs on the main thread between
    bytecodes, so real work there happens in the middle of whatever was in flight, including
    halfway through an `order_send`. A second signal is left to the default handler.
    """

    def handle(number: int, _frame: types.FrameType | None) -> None:
        logger.warning("received %s; finishing the order in hand", signal.Signals(number).name)
        stopping.set()
        signal.signal(number, signal.SIG_DFL)

    for number in _STOP_SIGNALS:
        signal.signal(number, handle)


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tradeforge-executor",
        description="Consume orders and, if armed, send them to MetaTrader 5.",
    )
    parser.add_argument(
        "--arm",
        action="store_true",
        help="actually send orders. Without it the service runs everything except the send.",
    )
    parser.add_argument("--consumer", default="executor-1", help="this instance's name")
    return parser.parse_args(argv)


def _switches(settings: ExecutorSettings, redis: Redis, endpoint: EndpointFlag) -> list[KillSwitch]:
    """The three layers, in the order an operator is most likely to reach them.

    ⚠️ Order is presentation, not logic: `admits` stops at the first engaged one and any of them
    stops everything. What the order decides is which name an operator sees first in the audit
    trail when more than one is engaged.
    """
    return [RedisFlag(redis), FileFlag(settings.kill_switch_file), endpoint]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _parse(argv)
    settings = ExecutorSettings()

    engine = create_db_engine(settings.sqlalchemy_dsn)
    factory = create_session_factory(engine)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    stopping = threading.Event()
    stop_on_signals(stopping)

    terminal = MT5Gateway(server_offset=settings.server_offset).connect()
    gateway: OrderGateway = terminal if args.arm else RefusingGateway(terminal)
    if args.arm:
        logger.critical("ARMED: orders from this queue will be sent to MetaTrader 5")
    else:
        logger.info("dry run: everything but the send. Pass --arm to send for real.")

    service = Service(
        queue=OrderQueue(
            client=redis,
            consumer=args.consumer,
            block_ms=settings.block_ms,
            stopping=stopping.is_set,
        ),
        router=Router(
            gateway=gateway,
            limits=settings.limits,
            switches=_switches(settings, redis, EndpointFlag()),
        ),
        factory=factory,
        now=_utcnow,
    )
    logger.info("consuming as %s in group %s", args.consumer, GROUP)

    # ⚠️ **The snapshot runs whether or not this executor is armed**, because it publishes a fact
    # about the *venue*, not about this process's willingness to act on it. A dry run that stopped
    # publishing would leave every session refusing to start — correctly, since absent means "I do
    # not know" — for a reason that has nothing to do with the account.
    #
    # ⚠️ And it is `gateway`, the outer one: `RefusingGateway` passes reads straight through, so a
    # dry run still reports what is really out there rather than an empty account it invented.
    snapshot = VenueSnapshot(redis, gateway)

    # ⚠️ **Armed or not, for the same reason the snapshot is**, and here the argument is sharper.
    # A dry run still means a *previous* armed run may have left a limit resting at the venue, and
    # that limit can fill while this process watches. Not scanning would be this executor choosing
    # not to look at a position that exists.
    #
    # ⚠️ It reads through `terminal`, not `gateway`: `RefusingGateway` refuses the *send*, and
    # `deals_since`/`spread_at` are reads. Passing the refusing wrapper would make a dry run report
    # an account with no history, which is the empty-answer failure `VenueSnapshot` documents.
    deals = DealWatch(
        redis,
        terminal,
        # ⚠️ `service.queue`, not `service`: `publish_outcome` belongs to `OrderQueue`, which is
        # the object that owns the Redis client. mypy refused the wrong one, which is the whole
        # reason `OutcomePublisher` is a protocol and not a `Callable` somebody remembered to pass.
        service.queue,
        session_of=lambda client_id: _session_of(factory, client_id),
        already_reported=lambda ticket: _already_reported(factory, ticket),
        every=settings.deal_scan_every,
        quote_window=settings.deal_quote_window,
    )

    try:
        with snapshot, deals:
            handled = service.run()
    finally:
        terminal.close()
        redis.close()
        engine.dispose()

    logger.info("stopped after %d order(s)", handled)
    return 0


if __name__ == "__main__":  # pragma: no cover — the process entry point
    sys.exit(main())
