"""`tradeforge-signals`: keeps one SIGNAL session per active watch item running (signals PR 5b).

Every `every` seconds it compares what *should* run with what *does*, and closes the gap:

* one `tradeforge-session --mode signal` per followed (setup, market) pair, capped at
  `max_sessions` — a structure setup's session holds its history in memory, 1-2 GB each;
* one `tradeforge-collector live --broker <slug>` per broker that has an active item, so the bars
  those sessions ask for (`live:wanted:<slug>`) are actually read from a terminal.

A child that exits is started again, after a pause that doubles up to five minutes, so a session
that cannot start (no history, a broker offline) does not spin. An item turned off is **asked** to
stop — `live-session:<id>:stop`, the same request the screen sends — because a killed session
leaves its row `running` and is later reported as a crash; only one that ignores the ask for
`grace` seconds is killed.

Runs on the Windows host beside the collectors: the live loops need MetaTrader.
"""

import argparse
import datetime as dt
import logging
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Protocol, cast

from redis import Redis
from redis.exceptions import ResponseError
from sqlalchemy.orm import Session, sessionmaker

from tradeforge_api.config import Settings
from tradeforge_api.live.signals import SIGNALS_STREAM
from tradeforge_api.live.stop import request_stop
from tradeforge_db.live_setups import FollowedPair, followed_pairs, record_event
from tradeforge_db.session import create_db_engine, create_session_factory, session_scope

__all__ = ["Child", "Supervisor", "Wanted", "main", "wanted_children"]

logger = logging.getLogger(__name__)

RECORDER = "recorder"
"""The consumer group that writes the signals' history (09/10)."""

SESSION_CAPITAL = Decimal(10_000)
"""A signal session's ledger. Its size moves no signal: R is the unit every message speaks in."""


class Child(Protocol):
    """A running process, as far as the supervisor needs one."""

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Wanted:
    """One child that should be running: its command, and how to ask it to stop."""

    key: str
    """`session:<watch item id>` or `live:<broker slug>`."""
    argv: tuple[str, ...]
    session_id: uuid.UUID | None = None
    """A session's id, so it can be asked to stop; `None` for a live loop, which is killed."""


@dataclass(slots=True)
class _Running:
    wanted: Wanted
    child: Child
    stopping_since: float | None = None


@dataclass(slots=True)
class _Failing:
    failures: int = 0
    retry_at: float = 0.0


@dataclass(slots=True)
class Supervisor:
    """Reconciles the children against what is wanted, one `tick` at a time."""

    launch: Callable[[Wanted], Child]
    ask_to_stop: Callable[[uuid.UUID], None]
    clock: Callable[[], float] = time.monotonic
    grace: float = 120.0
    first_backoff: float = 15.0
    max_backoff: float = 300.0
    running: dict[str, _Running] = field(default_factory=dict)
    failing: dict[str, _Failing] = field(default_factory=dict)

    def tick(self, wanted: Mapping[str, Wanted]) -> None:
        """Start what is missing, restart what died, stop what is no longer wanted."""
        now = self.clock()
        for key, running in list(self.running.items()):
            code = running.child.poll()
            if code is not None:
                del self.running[key]
                if running.stopping_since is None:  # died on its own: try again, later each time
                    failing = self.failing.setdefault(key, _Failing())
                    failing.failures += 1
                    pause = min(self.first_backoff * 2 ** (failing.failures - 1), self.max_backoff)
                    failing.retry_at = now + pause
                    logger.warning("%s exited with %s; retrying in %.0fs", key, code, pause)
                else:
                    logger.info("%s stopped", key)
                continue
            if key not in wanted or wanted[key] != running.wanted:
                self._stop(key, running, now)

        for key, one in wanted.items():
            if key in self.running:
                continue
            waiting = self.failing.get(key)
            if waiting is not None and now < waiting.retry_at:
                continue
            self.running[key] = _Running(one, self.launch(one))
            logger.info("%s started", key)

        for key in list(self.failing):
            if key not in wanted:
                del self.failing[key]

    def _stop(self, key: str, running: _Running, now: float) -> None:
        if running.stopping_since is None:
            running.stopping_since = now
            if running.wanted.session_id is not None:
                self.ask_to_stop(running.wanted.session_id)
                logger.info("%s asked to stop", key)
                return
        if running.wanted.session_id is None or now - running.stopping_since >= self.grace:
            running.child.terminate()
            logger.info("%s terminated", key)

    def stop_all(self) -> None:
        """On the supervisor's own exit: ask every session to stop, kill the live loops."""
        now = self.clock()
        for key, running in self.running.items():
            self._stop(key, running, now)


def record_signals(redis: Redis, factory: sessionmaker[Session], consumer: str) -> int:
    """Fold the new `signals.events` entries into the history (09/10); how many it read.

    Its own consumer group, `recorder`, beside the notifier's: each reads every entry, and an
    entry is acknowledged only once its row is written, so a crash re-reads rather than loses."""
    try:
        redis.xgroup_create(SIGNALS_STREAM, RECORDER, id="0", mkstream=True)
    except ResponseError as exists:
        if "BUSYGROUP" not in str(exists):
            raise
    read = cast(
        "list[tuple[str, list[tuple[str, dict[str, str]]]]]",
        redis.xreadgroup(RECORDER, consumer, {SIGNALS_STREAM: ">"}, count=200),
    )
    entries = read[0][1] if read else []
    for entry_id, fields in entries:
        with session_scope(factory) as session:
            record_event(session, fields)
        redis.xack(SIGNALS_STREAM, RECORDER, entry_id)
    return len(entries)


def wanted_children(
    items: Sequence[FollowedPair],
    *,
    python: str,
    max_sessions: int,
    session_ids: dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID],
) -> dict[str, Wanted]:
    """What should run for these (setup, market) pairs: the first `max_sessions` sessions, and a
    live loop for each broker they need. `session_ids` keeps one id per pair across ticks, so an
    unchanged pair is recognised as the session already running rather than as a new one."""
    wanted: dict[str, Wanted] = {}
    brokers: set[str] = set()
    for item in items[:max_sessions]:
        pair = (item.setup_id, item.instrument_id)
        session_id = session_ids.setdefault(pair, uuid.uuid4())
        argv = [
            python,
            "-m",
            "tradeforge_api.live.process",
            "--strategy",
            str(item.strategy_id),
            "--instrument",
            str(item.instrument_id),
            "--timeframe",
            item.timeframe,
            "--capital",
            str(SESSION_CAPITAL),
            "--mode",
            "signal",
            "--no-target-r",
            str(item.no_target_r),
            "--live-setup",
            str(item.setup_id),
            "--session-id",
            str(session_id),
        ]
        if item.cost_model.get("type") == "spread" and "spread_points" in item.cost_model:
            argv += ["--spread-points", str(item.cost_model["spread_points"])]
        key = f"session:{item.setup_id}:{item.instrument_id}"
        wanted[key] = Wanted(key, tuple(argv), session_id)
        if item.broker is not None:
            brokers.add(item.broker)
    for slug in sorted(brokers):
        argv = [
            python,
            "-c",
            "import sys; from tradeforge_collector.cli import main; "
            f"sys.exit(main(['live', '--broker', '{slug}']))",
        ]
        wanted[f"live:{slug}"] = Wanted(f"live:{slug}", tuple(argv))
    return wanted


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tradeforge-signals",
        description="Keep one SIGNAL session per followed setup and market, and its live loops.",
    )
    parser.add_argument("--every", type=float, default=15.0, help="seconds between checks")
    parser.add_argument(
        "--max-sessions",
        type=int,
        default=10,
        help="sessions at once (each holds its history in memory: 1-2 GB on structure setups)",
    )
    parser.add_argument(
        "--logs", type=Path, default=Path("data/logs/signals"), help="one log file per child"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run until interrupted."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _parse(argv)
    settings = Settings()
    engine = create_db_engine(settings.sqlalchemy_dsn)
    factory = create_session_factory(engine)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    args.logs.mkdir(parents=True, exist_ok=True)

    def launch(one: Wanted) -> Child:
        log = (args.logs / f"{one.key.replace(':', '-')}.log").open("a", encoding="utf-8")
        return subprocess.Popen(one.argv, stdout=log, stderr=subprocess.STDOUT)  # noqa: S603

    supervisor = Supervisor(
        launch=launch,
        ask_to_stop=lambda session_id: request_stop(redis, session_id, now=dt.datetime.now(dt.UTC)),
    )
    session_ids: dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID] = {}
    consumer = f"recorder-{uuid.uuid4().hex[:8]}"
    logger.info(
        "supervising signals every %.0fs, at most %d sessions", args.every, args.max_sessions
    )
    try:
        while True:
            with session_scope(factory) as session:
                items = followed_pairs(session)
            supervisor.tick(
                wanted_children(
                    items,
                    python=sys.executable,
                    max_sessions=args.max_sessions,
                    session_ids=session_ids,
                )
            )
            try:
                record_signals(redis, factory, consumer)
            except Exception:
                logger.exception("recording the signals failed; retrying next tick")
            time.sleep(args.every)
    except KeyboardInterrupt:
        logger.info("stopping every child")
        supervisor.stop_all()
    finally:
        redis.close()
        engine.dispose()
    return 0


if __name__ == "__main__":  # pragma: no cover — the process entry point
    sys.exit(main())
