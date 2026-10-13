"""`tradeforge-signals`: keeps every followed signal session running (signals PR 5b, ADR-0033).

Every `every` seconds it compares what *should* run with what *does*, and closes the gap:

* the signal sessions of each market (instrument + timeframe) in one `live.market` process, capped
  at `max_sessions` sessions — which ones run where is the fleet's book (`live.fleet`);
* one `tradeforge-collector live --broker <slug>` per broker that has an active item, so the bars
  those sessions ask for (`live:wanted:<slug>`) are actually read from a terminal.

A session that fails is started again, after a pause that doubles up to five minutes, so one that
cannot start (no history, a broker offline) does not spin. An item turned off is **asked** to stop
— `live-session:<id>:stop`, the same request the screen sends — because a killed session leaves
its row `running` and is later reported as a crash; only a process that ignores the ask for
`grace` seconds is killed. Each round it also says what runs where (`SUPERVISOR_STATE_KEY`), for
the screen, and takes the screen's requests to restart a market.

Runs on the Windows host beside the collectors: the live loops need MetaTrader.
"""

import argparse
import datetime as dt
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

from redis import Redis
from redis.exceptions import ResponseError
from sqlalchemy.orm import Session, sessionmaker

from tradeforge_api.config import Settings
from tradeforge_api.live.fleet import (
    RESTART_PREFIX,
    SUPERVISOR_STATE_KEY,
    Fleet,
    Launch,
    MarketKey,
    MarketState,
    market_state_key,
    parse_market_state,
    parse_restart_key,
)
from tradeforge_api.live.orphans import OrphanWatch, annul, orphan_brokers, resolve_orphans
from tradeforge_api.live.outage import Outage
from tradeforge_api.live.signals import SIGNALS_STREAM
from tradeforge_api.live.stop import request_stop
from tradeforge_db.live_setups import FollowedPair, followed_pairs, record_event
from tradeforge_db.session import create_db_engine, create_session_factory, session_scope

__all__ = [
    "Child",
    "Supervisor",
    "Wanted",
    "as_wanted",
    "heard_from",
    "live_loops",
    "main",
    "supervise_round",
    "take_restarts",
    "within_cap",
]

logger = logging.getLogger(__name__)

RECORDER = "recorder"
"""The consumer group that writes the signals' history (09/10) — and its one consumer's name.

⚠️ **One name, not one per start (13/10).** A consumer's unacknowledged entries are offered only
to that consumer again; each supervisor used to read as `recorder-<random>`, so what a dead one
had taken and not written stayed pending under a name nobody would use again."""

CHILD_ENVIRONMENT = {"OPENBLAS_NUM_THREADS": "1"}
"""Set for every child unless the supervisor's own environment already names it (12/10).

numpy's OpenBLAS reserves its buffers for every core when it is imported — ~740 MB of committed
memory per process here, 82 MB with one thread — and a session never multiplies a matrix. With
sixteen sessions that was ~15 GB the machine could not lend to anything else."""


def child_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment a session or a live loop starts with: this one, plus `CHILD_ENVIRONMENT`
    where it says nothing — an operator who sets the variable on purpose keeps their value."""
    return {**CHILD_ENVIRONMENT, **environ}


class Child(Protocol):
    """A running process, as far as the supervisor needs one."""

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Wanted:
    """One child that should be running: its command, and how to ask it to stop."""

    key: str
    """`market:<instrument>:<timeframe>:<launch>` or `live:<broker slug>`."""
    argv: tuple[str, ...]
    session_ids: tuple[uuid.UUID, ...] = ()
    """The sessions it carries, so each can be asked to stop; none for a live loop, killed."""
    log: str = ""
    """Its log file's name, without the extension; the key (`:` as `-`) when empty."""


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
    exited: dict[str, int] = field(default_factory=dict)
    """Children seen ending and not yet handed out by `poll`."""

    def poll(self) -> dict[str, int]:
        """The children that ended since the last `poll`, with their exit codes.

        ⚠️ **Including the ones `tick` saw end**, which is why they are kept rather than returned
        from wherever they were noticed. A process that ended between a round's `poll` and its
        `tick` was otherwise noticed by nobody who keeps the fleet's book: the book went on
        carrying it, and the next round asked for it again — the same command, the same session
        ids, which a relaunch must never reuse.
        """
        self._collect()
        exits, self.exited = self.exited, {}
        return exits

    def _collect(self) -> None:
        now = self.clock()
        for key, running in list(self.running.items()):
            code = running.child.poll()
            if code is None:
                continue
            del self.running[key]
            self.exited[key] = code
            if running.stopping_since is None:  # died on its own: try again, later each time
                failing = self.failing.setdefault(key, _Failing())
                failing.failures += 1
                pause = min(self.first_backoff * 2 ** (failing.failures - 1), self.max_backoff)
                failing.retry_at = now + pause
                logger.warning("%s exited with %s; retrying in %.0fs", key, code, pause)
            else:
                logger.info("%s stopped", key)

    def tick(self, wanted: Mapping[str, Wanted]) -> None:
        """Start what is missing, restart what died, stop what is no longer wanted."""
        self._collect()
        now = self.clock()
        for key, running in list(self.running.items()):
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
            if running.wanted.session_ids:
                # ⚠️ **Asked first, marked after (13/10).** The ask is a Redis write, and with
                # the mark first a Redis that was down at that instant left a session marked as
                # asked that never heard it — killed when the grace ran out, its row left
                # `running` and later reported as a crash. Marked after, a failed ask raises out
                # of the round and is simply asked again on the next one.
                for session_id in running.wanted.session_ids:
                    self.ask_to_stop(session_id)
                running.stopping_since = now
                logger.info("%s asked to stop", key)
                return
            running.stopping_since = now
        if not running.wanted.session_ids or now - running.stopping_since >= self.grace:
            running.child.terminate()
            logger.info("%s terminated", key)

    def stop_all(self) -> None:
        """On the supervisor's own exit: ask every session to stop, kill the live loops.

        One child that cannot be asked (Redis gone) does not keep the others from hearing it."""
        now = self.clock()
        for key, running in self.running.items():
            try:
                self._stop(key, running, now)
            except Exception:
                logger.exception("%s could not be asked to stop; left running", key)


def supervise_round(
    supervisor: Supervisor,
    *,
    wanted: Callable[[], Mapping[str, Wanted]],
    housekeeping: Callable[[], None],
    outage: Outage,
) -> bool:
    """One round: read what should run, reconcile the children, then record. `False` if lost.

    ⚠️ **What should run is read before anything is touched, and a failed read skips the round
    whole** (13/10). An unreachable database is not "nothing is followed": ticking with an empty
    set would ask every session to stop and kill every live loop, because the database blinked.
    The children are left exactly as they are until the read answers again.

    Every failure is caught, whatever its type. This process's one job is to outlive whatever
    the sessions depend on — a lost round costs fifteen seconds, a dead supervisor costs every
    signal until somebody notices (12/10: a `wsl --shutdown` took Postgres and Redis down for a
    few minutes and the signals never came back by themselves).
    """
    try:
        want = wanted()
    except Exception:  # noqa: BLE001 — see the docstring: the supervisor must outlive it
        outage.lost("reading what should run")
        return False
    try:
        supervisor.tick(want)
    except Exception:  # noqa: BLE001
        outage.lost("reconciling the children")
        return False
    try:
        housekeeping()
    except Exception:  # noqa: BLE001
        outage.lost("recording the signals")
        return False
    outage.back()
    return True


def record_signals(redis: Redis, factory: sessionmaker[Session], consumer: str = RECORDER) -> int:
    """Fold `signals.events` entries into the history (09/10); how many it read.

    Its own consumer group, `recorder`, beside the notifier's: each reads every entry, and an
    entry is acknowledged only once its row is written, so a crash re-reads rather than loses.

    ⚠️ **What it took and did not write comes first (13/10).** A round that failed half way —
    the database gone after three rows of twenty — left the other seventeen delivered and not
    acknowledged, and a read of `>` (never delivered) offers them to nobody: they stayed out of the
    history for good. Its own pending entries (`0`) are read before the new ones, as the
    notifier's are. Folding one twice is harmless: `record_event` folds an event already seen
    into the same row.
    """
    try:
        redis.xgroup_create(SIGNALS_STREAM, RECORDER, id="0", mkstream=True)
    except ResponseError as exists:
        if "BUSYGROUP" not in str(exists):
            raise
    entries = _entries(redis.xreadgroup(RECORDER, consumer, {SIGNALS_STREAM: "0"}, count=200))
    if not entries:
        entries = _entries(redis.xreadgroup(RECORDER, consumer, {SIGNALS_STREAM: ">"}, count=200))
    for entry_id, fields in entries:
        try:
            if fields:  # an entry trimmed off the stream while pending comes back empty
                with session_scope(factory) as session:
                    record_event(session, fields)
        except (KeyError, ValueError, ArithmeticError) as malformed:
            # ⚠️ Skipped, not left pending: read first on every round, an entry that can never
            # be written would keep every later one out of the history. A database that is down
            # raises something else, and that one is left to lose the round and come back.
            logger.warning(
                "signals.events %s cannot be recorded (%r); skipped", entry_id, malformed
            )
        redis.xack(SIGNALS_STREAM, RECORDER, entry_id)
    return len(entries)


def _entries(read: object) -> list[tuple[str, dict[str, str]]]:
    found = cast("list[tuple[str, list[tuple[str, dict[str, str]]]]]", read)
    return found[0][1] if found else []


def within_cap(
    items: Sequence[FollowedPair], *, carried: Collection[tuple[uuid.UUID, uuid.UUID]], cap: int
) -> tuple[list[FollowedPair], list[FollowedPair]]:
    """The pairs that may run under `--max-sessions`, and those left waiting.

    ⚠️ **Those already running first (13/10).** The pairs come in the order they were followed, so
    a market added to an old setup sorts before a newer setup's — and cut at the cap, the newcomer
    used to push out a session already running: asked to stop, its armed signals cancelled on the
    channel, for a pair that only had to wait. Running ones keep their place; the rest queue.
    """
    running = [item for item in items if (item.setup_id, item.instrument_id) in carried]
    others = [item for item in items if (item.setup_id, item.instrument_id) not in carried]
    ordered = running + others
    return ordered[:cap], ordered[cap:]


def _note_the_cap(
    said: list[tuple[uuid.UUID, uuid.UUID]],
    waiting: Sequence[FollowedPair],
    *,
    followed: int,
    cap: int,
) -> None:
    """Warn when who waits for room under `--max-sessions` changes — not every round."""
    cut = [(item.setup_id, item.instrument_id) for item in waiting]
    if cut == said:
        return
    if cut:
        logger.warning(
            "%d sessions followed, %d may run (--max-sessions): %d wait for room",
            followed,
            cap,
            len(cut),
        )
    said[:] = cut


def as_wanted(launch: Launch) -> Wanted:
    """A market process as the supervisor runs it, logging to a file of its own.

    ⚠️ **Its own file, not its market's** (13/10). Two processes of one market run side by side
    while a setup added since the last restart runs apart, and on Windows two processes appending
    to one file write over each other — measured: 200 of 400 lines left.
    """
    return Wanted(launch.key, launch.argv, tuple(launch.sessions.values()))


def live_loops(
    items: Sequence[FollowedPair], *, python: str, also_brokers: Collection[str] = ()
) -> dict[str, Wanted]:
    """A live loop for each broker these items need — and for `also_brokers`, whose orphans
    still need bars."""
    brokers = set(also_brokers) | {item.broker for item in items if item.broker is not None}
    wanted: dict[str, Wanted] = {}
    for slug in sorted(brokers):
        argv = [
            python,
            "-c",
            "import sys; from tradeforge_collector.cli import main; "
            f"sys.exit(main(['live', '--broker', '{slug}']))",
        ]
        wanted[f"live:{slug}"] = Wanted(f"live:{slug}", tuple(argv))
    return wanted


def heard_from(redis: Redis, keys: Sequence[str]) -> dict[str, MarketState | None]:
    """What each market process last said (`live.market`); `None` where it said nothing readable."""
    if not keys:
        return {}
    raw = cast("list[str | None]", redis.mget([market_state_key(key) for key in keys]))
    return {key: parse_market_state(value) for key, value in zip(keys, raw, strict=True)}


def take_restarts(redis: Redis) -> list[MarketKey]:
    """The markets the screen asked to restart, each request consumed as it is read."""
    markets = []
    for key in cast("list[str]", list(redis.scan_iter(match=f"{RESTART_PREFIX}*"))):
        if redis.delete(key) and (market := parse_restart_key(key)) is not None:
            markets.append(market)
    return markets


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tradeforge-signals",
        description="Keep every followed signal session running, one process per market.",
    )
    parser.add_argument("--every", type=float, default=15.0, help="seconds between checks")
    parser.add_argument(
        "--max-sessions",
        type=int,
        default=10,
        help="signal sessions at once, setup x market (a market's process ~350-650 MB, each "
        "session beside others of its market ~10-30 MB more)",
    )
    parser.add_argument(
        "--logs", type=Path, default=Path("data/logs/signals"), help="one log file per process"
    )
    parser.add_argument(
        "--annul",
        type=int,
        metavar="NUMBER",
        help="withdraw signal NUMBER from the channels and the metrics, then exit",
    )
    parser.add_argument("--reason", default="", help="why, posted with --annul")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run until interrupted."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = _parse(argv)
    settings = Settings()
    engine = create_db_engine(settings.sqlalchemy_dsn)
    factory = create_session_factory(engine)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    if args.annul is not None:
        done = annul(redis, factory, args.annul, args.reason or "sinal anulado")
        logger.info("signal #%d %s", args.annul, "annulled" if done else "is not open: unchanged")
        return 0 if done else 1
    args.logs.mkdir(parents=True, exist_ok=True)

    def launch(one: Wanted) -> Child:
        name = one.log or one.key.replace(":", "-")
        log = (args.logs / f"{name}.log").open("a", encoding="utf-8")
        return subprocess.Popen(  # noqa: S603
            one.argv, stdout=log, stderr=subprocess.STDOUT, env=child_environment(os.environ)
        )

    def ask(session_id: uuid.UUID) -> None:
        request_stop(redis, session_id, now=dt.datetime.now(dt.UTC))

    supervisor = Supervisor(launch=launch, ask_to_stop=ask)
    fleet = Fleet(python=sys.executable, clock=time.monotonic)
    orphans = OrphanWatch()
    heard: dict[str, MarketState | None] = {}

    left_out: list[tuple[uuid.UUID, uuid.UUID]] = []

    def wanted_now() -> dict[str, Wanted]:
        with session_scope(factory) as session:
            followed = followed_pairs(session)
        items, waiting = within_cap(followed, carried=fleet.carried(), cap=args.max_sessions)
        _note_the_cap(left_out, waiting, followed=len(followed), cap=args.max_sessions)
        fleet.exited(supervisor.poll())
        heard.clear()
        heard.update(heard_from(redis, list(fleet.processes)))
        for launched in fleet.reconcile(
            items, heard=heard, restart=take_restarts(redis), ask_to_stop=ask
        ):
            logger.info("%s: launching %d session(s)", launched.key, len(launched.sessions))
        wanted = {key: as_wanted(kept) for key, kept in fleet.kept().items()}
        wanted.update(
            live_loops(
                items,
                python=sys.executable,
                also_brokers=orphan_brokers(factory, fleet.alive()),
            )
        )
        return wanted

    def housekeeping() -> None:
        state = fleet.describe(heard, at=dt.datetime.now(dt.UTC))
        redis.set(SUPERVISOR_STATE_KEY, json.dumps(state), ex=max(60, round(3 * args.every)))
        record_signals(redis, factory)
        resolve_orphans(redis, factory, fleet.alive(), orphans)

    outage = Outage(logger)
    logger.info(
        "supervising signals every %.0fs, at most %d sessions", args.every, args.max_sessions
    )
    try:
        while True:
            supervise_round(supervisor, wanted=wanted_now, housekeeping=housekeeping, outage=outage)
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
