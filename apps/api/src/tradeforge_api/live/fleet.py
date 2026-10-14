"""Which market processes the signals supervisor keeps, and which sessions each carries (ADR-0033).

A *market* is an instrument on a timeframe — one bar stream — and every signal session followed on
it runs inside one `live.market` process. The fleet is the supervisor's book of those processes:
what each one carries, what should run, and what to launch, ask to stop or wait for. It does no
input or output of its own; the supervisor hands it what it read and acts on what it answers, so
every rule below is a function of its arguments.

**The one rule that decides launching: the sessions of a market that should run and do not are
launched together, in a new process.** At start that is one process per market. A setup added to
a market already running starts in a process of its own instead — restarting the market would
cancel every armed signal of the others on the channel (`orphans.py`) for a change that is not
theirs. They come together again at the next start, or when somebody asks for a restart of that
market (the screen's button): its processes are asked to stop, and nothing of it is launched until
the last of them is gone.

**An edited setup is a session launched with something that is no longer so** — another version
of its strategy, another no-target R — and a session cannot take that in flight: its strategy is
built when it starts. It is asked to stop inside its process, like a setup switched off, and once
it has left the edited one starts in a process of its own, like a setup added; the others of its
market are not touched. Not before it has left: one setup on one market would otherwise post its
signals twice, once per version. ⚠️ Until 13/10 nothing noticed an edit at all — a session was
known by its (setup, instrument) alone, and CONTINUAÇÃO, edited from a 2R target to 5R, went on at
2R on its three markets until somebody pressed restart.

**Who is alive is counted per session, not per process.** A market process publishes the
sessions it still carries (`market_state_key`); one that left without being asked has failed, and
is launched again on its own after a pause that doubles up to five minutes. Its signals meanwhile
belong to nobody, so the orphan watch sees them to their end.
"""

import datetime as dt
import json
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from tradeforge_db.live_setups import FollowedPair

__all__ = [
    "MARKET_STATE_TTL",
    "RESTARTS_KEY",
    "RESTART_TTL",
    "SUPERVISOR_STATE_KEY",
    "Fleet",
    "Launch",
    "MarketKey",
    "MarketState",
    # `Pair` is left out on purpose: a `type` alias (PEP 695), which CodeQL cannot see and reports
    # as an undefined export. Imported by name, it needs no listing.
    "market_state_key",
    "parse_market_state",
    "parse_restart_field",
    "restart_field",
]

type Pair = tuple[uuid.UUID, uuid.UUID]
"""A followed (setup, instrument): what the screen switches on and off."""

SESSION_CAPITAL = Decimal(10_000)
"""A signal session's ledger. Its size moves no signal: R is the unit every message speaks in."""

MARKET_STATE_TTL = 180
"""Seconds a market process's word lasts: three times the longest it goes between two stream reads
(`DEFAULT_BLOCK_MS`), so a healthy one never lapses and a wedged one is unheard within minutes."""

SUPERVISOR_STATE_KEY = "signals:supervisor"
"""The supervisor's own word, every round: what runs where. Lapsed means nobody supervises."""

RESTARTS_KEY = "signals:restarts"
"""The screen's restart requests: one hash, a field per market (`restart_field`) valued with when it
was asked.

⚠️ **One key, not one key per request (13/10).** Requests used to be keys of their own, found each
round with a SCAN of the whole keyspace — instant while Redis held a few thousand keys, 68 s once
a sweep's 2.3 million jobs were back in it: every round took over a minute, and the supervisor's
word lapsed in the middle of each, so the screen read it as stopped."""

RESTART_TTL = dt.timedelta(minutes=10)
"""How long a request is obeyed: a supervisor that was down longer does not act, when it comes
back, on a click somebody made ten minutes earlier."""


def market_state_key(process: str) -> str:
    """Where a market process says what it carries."""
    return f"signals:market:{process}"


def restart_field(instrument_id: uuid.UUID, timeframe: str) -> str:
    """A market's field in `RESTARTS_KEY`."""
    return f"{instrument_id}:{timeframe}"


@dataclass(frozen=True, slots=True, order=True)
class MarketKey:
    """One bar stream: an instrument on a timeframe."""

    instrument_id: uuid.UUID
    timeframe: str


def parse_restart_field(field: str) -> "MarketKey | None":
    """The market a restart request names; `None` for a field that is not one."""
    instrument, _, timeframe = field.partition(":")
    try:
        return MarketKey(uuid.UUID(instrument), timeframe) if timeframe else None
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class MarketState:
    """What a market process last said: its phase and the sessions it still carries."""

    phase: str
    sessions: frozenset[uuid.UUID]
    at: dt.datetime | None


def parse_market_state(raw: str | None) -> MarketState | None:
    """A market process's word, or `None` when it has said nothing readable (not yet, or lapsed)."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return MarketState(
            phase=str(value["phase"]),
            sessions=frozenset(uuid.UUID(one) for one in value["sessions"]),
            at=dt.datetime.fromisoformat(value["at"]) if value.get("at") else None,
        )
    except (ValueError, KeyError, TypeError):
        return None


def _new_key(market: MarketKey) -> str:
    """A process's own name: its market, and a launch of its own beside any other of it."""
    return f"market:{market.instrument_id}:{market.timeframe}:{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True, slots=True)
class Launch:
    """A market process to start: its key, the sessions it carries, and its command line."""

    key: str
    market: MarketKey
    sessions: Mapping[Pair, uuid.UUID]
    argv: tuple[str, ...]


@dataclass(slots=True)
class _Process:
    key: str
    market: MarketKey
    sessions: dict[Pair, uuid.UUID]
    """The sessions it still carries, by pair: those that left are taken out as they leave."""
    launch: Launch
    launched: Mapping[Pair, FollowedPair]
    """What each session was launched with — its strategy version, its no-target R, its costs."""
    asked: set[Pair] = field(default_factory=set)
    """Sessions asked to stop: no longer wanted here, still running until they leave."""


@dataclass(slots=True)
class _Backoff:
    failures: int = 0
    retry_at: float = 0.0


@dataclass(slots=True)
class Fleet:
    """The supervisor's book of market processes. See the module docstring for the rules."""

    python: str
    clock: Callable[[], float]
    first_backoff: float = 15.0
    max_backoff: float = 300.0
    new_key: Callable[[MarketKey], str] = _new_key
    processes: dict[str, _Process] = field(default_factory=dict)
    backoff: dict[Pair, _Backoff] = field(default_factory=dict)
    restarting: set[MarketKey] = field(default_factory=set)

    def exited(self, exits: Mapping[str, int]) -> None:
        """Processes that ended: what they still carried and was not asked to stop failed with
        them, unless they ended cleanly (`0`: every session they carried had left)."""
        now = self.clock()
        for key, code in exits.items():
            process = self.processes.pop(key, None)
            if process is None or code == 0:
                continue
            for pair in process.sessions:
                if pair not in process.asked:
                    self._failed(pair, now)

    def reconcile(
        self,
        items: Sequence[FollowedPair],
        *,
        heard: Mapping[str, MarketState | None],
        restart: Collection[MarketKey],
        ask_to_stop: Callable[[uuid.UUID], None],
    ) -> list[Launch]:
        """One round: settle what the processes said, ask to stop what is no longer wanted, and
        answer what to launch. `heard` is each process's last word (`None`: nothing readable)."""
        wanted = {(item.setup_id, item.instrument_id): item for item in items}
        self._settle(heard, wanted)
        self.restarting.update(restart)
        self._ask_the_gone(wanted, ask_to_stop)
        for pair in list(self.backoff):
            if pair not in wanted:
                del self.backoff[pair]
        launches = [
            self._launch(market, members)
            for market, members in sorted(self._missing(wanted).items())
            if self._may_launch(market)
        ]
        # A restart of a market with nothing left to launch is over once its processes are gone.
        self.restarting = {market for market in self.restarting if self._running(market)}
        return launches

    def _settle(
        self, heard: Mapping[str, MarketState | None], wanted: Mapping[Pair, object]
    ) -> None:
        """Take out of each process the sessions it no longer carries; one that left without
        being asked, while still wanted, failed."""
        now = self.clock()
        for process in self.processes.values():
            state = heard.get(process.key)
            if state is None:  # not heard from yet, or lapsed: nothing to conclude
                continue
            for pair, session_id in list(process.sessions.items()):
                if session_id not in state.sessions:
                    del process.sessions[pair]
                    if pair in wanted and pair not in process.asked:
                        self._failed(pair, now)

    def _ask_the_gone(
        self, wanted: Mapping[Pair, FollowedPair], ask_to_stop: Callable[[uuid.UUID], None]
    ) -> None:
        """Ask to stop every session no longer wanted, every one launched with what has since
        been edited, and every one of a market restarting."""
        for process in self.processes.values():
            for pair, session_id in process.sessions.items():
                gone = (
                    wanted.get(pair) != process.launched[pair] or process.market in self.restarting
                )
                if gone and pair not in process.asked:
                    ask_to_stop(session_id)  # raises when Redis is gone: asked again next round
                    process.asked.add(pair)

    def _missing(self, wanted: Mapping[Pair, FollowedPair]) -> dict[MarketKey, list[FollowedPair]]:
        """What should run and does not, by market — past any pause after a failure."""
        now = self.clock()
        carried = {pair for process in self.processes.values() for pair in process.sessions}
        missing: dict[MarketKey, list[FollowedPair]] = {}
        for pair, item in wanted.items():
            if pair not in carried and now >= self._retry_at(pair):
                missing.setdefault(MarketKey(item.instrument_id, item.timeframe), []).append(item)
        return missing

    def _may_launch(self, market: MarketKey) -> bool:
        """`False` while a restart of `market` waits for the last of its old processes."""
        if market not in self.restarting:
            return True
        if self._running(market):
            return False
        # ⚠️ Done with before the launch, not after: still marked, the new process's sessions
        # would be asked to stop on the next round as if they were the old ones.
        self.restarting.discard(market)
        return True

    def kept(self) -> dict[str, Launch]:
        """The processes to keep running: those carrying a session nobody asked to stop."""
        return {
            key: process.launch
            for key, process in self.processes.items()
            if any(pair not in process.asked for pair in process.sessions)
        }

    def carried(self) -> set[Pair]:
        """The (setup, instrument) pairs some process carries now."""
        return {pair for process in self.processes.values() for pair in process.sessions}

    def alive(self) -> set[uuid.UUID]:
        """The sessions running now. One asked to stop counts until it leaves: it watches its
        signals to its last bar, and two voices on one signal would post its end twice."""
        return {
            session_id
            for process in self.processes.values()
            for session_id in process.sessions.values()
        }

    def session_ids(self, key: str) -> tuple[uuid.UUID, ...]:
        """The sessions a process carries, to ask each to stop when the process is not kept."""
        process = self.processes.get(key)
        return () if process is None else tuple(process.sessions.values())

    def waiting(self) -> dict[Pair, float]:
        """Sessions waiting out a pause after a failure: seconds left, by pair."""
        now = self.clock()
        return {
            pair: max(0.0, b.retry_at - now) for pair, b in self.backoff.items() if b.retry_at > now
        }

    def describe(
        self, heard: Mapping[str, MarketState | None], *, at: dt.datetime
    ) -> dict[str, object]:
        """What runs where, as the supervisor tells the screen (`SUPERVISOR_STATE_KEY`)."""
        processes = []
        for process in self.processes.values():
            state = heard.get(process.key)
            processes.append(
                {
                    "key": process.key,
                    "instrument_id": str(process.market.instrument_id),
                    "timeframe": process.market.timeframe,
                    "phase": None if state is None else state.phase,
                    "heard_at": None if state is None or state.at is None else state.at.isoformat(),
                    "sessions": [
                        {
                            "setup_id": str(pair[0]),
                            "session_id": str(session_id),
                            "asked": pair in process.asked,
                        }
                        for pair, session_id in process.sessions.items()
                    ],
                }
            )
        return {
            "at": at.isoformat(),
            "processes": processes,
            "waiting": [
                {"setup_id": str(pair[0]), "instrument_id": str(pair[1]), "retry_in": round(left)}
                for pair, left in self.waiting().items()
            ],
            "restarting": [
                {"instrument_id": str(market.instrument_id), "timeframe": market.timeframe}
                for market in sorted(self.restarting)
            ],
        }

    def _launch(self, market: MarketKey, members: Iterable[FollowedPair]) -> Launch:
        key = self.new_key(market)
        sessions: dict[Pair, uuid.UUID] = {}
        launched: dict[Pair, FollowedPair] = {}
        argv = [
            self.python,
            "-m",
            "tradeforge_api.live.market",
            "--key",
            key,
            "--instrument",
            str(market.instrument_id),
            "--timeframe",
            market.timeframe,
            "--capital",
            str(SESSION_CAPITAL),
        ]
        for item in members:
            # ⚠️ **One id per launch, not per pair (09/10).** A session relaunched after a crash
            # under the same id would claim what the dead one posted, and its armed and open
            # signals would never be seen as orphans — nobody watching them.
            session_id = uuid.uuid4()
            sessions[(item.setup_id, item.instrument_id)] = session_id
            launched[(item.setup_id, item.instrument_id)] = item
            spread = item.cost_model.get("spread_points")
            argv += [
                "--member",
                json.dumps(
                    {
                        "strategy": str(item.strategy_id),
                        "setup": str(item.setup_id),
                        "session": str(session_id),
                        "no_target_r": str(item.no_target_r),
                        "spread_points": (
                            str(spread)
                            if item.cost_model.get("type") == "spread" and spread is not None
                            else None
                        ),
                    }
                ),
            ]
        launch = Launch(key=key, market=market, sessions=sessions, argv=tuple(argv))
        self.processes[key] = _Process(
            key=key, market=market, sessions=dict(sessions), launch=launch, launched=launched
        )
        return launch

    def _running(self, market: MarketKey) -> bool:
        return any(process.market == market for process in self.processes.values())

    def _retry_at(self, pair: Pair) -> float:
        backoff = self.backoff.get(pair)
        return 0.0 if backoff is None else backoff.retry_at

    def _failed(self, pair: Pair, now: float) -> None:
        backoff = self.backoff.setdefault(pair, _Backoff())
        backoff.failures += 1
        backoff.retry_at = now + min(
            self.first_backoff * 2 ** (backoff.failures - 1), self.max_backoff
        )
