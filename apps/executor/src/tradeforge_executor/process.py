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

from tradeforge_db.session import create_db_engine, create_session_factory
from tradeforge_engine.domain import OrderRequest
from tradeforge_executor.config import ExecutorSettings
from tradeforge_executor.gateway import MT5Gateway, OrderGateway, Placement
from tradeforge_executor.kill_switch import EndpointFlag, FileFlag, RedisFlag
from tradeforge_executor.router import Router
from tradeforge_executor.safety import KillSwitch
from tradeforge_executor.service import GROUP, OrderQueue, Service

logger = logging.getLogger(__name__)

__all__ = ["RefusingGateway", "main", "stop_on_signals"]

_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


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

    terminal = MT5Gateway().connect()
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

    try:
        handled = service.run()
    finally:
        terminal.close()
        redis.close()
        engine.dispose()

    logger.info("stopped after %d order(s)", handled)
    return 0


if __name__ == "__main__":  # pragma: no cover — the process entry point
    sys.exit(main())
