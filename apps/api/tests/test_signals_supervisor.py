"""The signals supervisor: what should run, against what does (signals PR 5b)."""

import contextlib
import json
import logging
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from tradeforge_api.live.fleet import Fleet, MarketKey, market_state_key, restart_key
from tradeforge_api.live.outage import Outage
from tradeforge_api.live.supervisor import (
    Supervisor,
    Wanted,
    as_wanted,
    child_environment,
    heard_from,
    live_loops,
    record_signals,
    supervise_round,
    take_restarts,
    within_cap,
)
from tradeforge_db.live_setups import FollowedPair


class FakeChild:
    def __init__(self) -> None:
        self.code: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.terminated = True
        self.code = -1


class Harness:
    def __init__(self) -> None:
        self.now = 0.0
        self.children: dict[str, list[FakeChild]] = {}
        self.asked: list[uuid.UUID] = []
        self.supervisor = Supervisor(
            launch=self._launch, ask_to_stop=self.asked.append, clock=lambda: self.now, grace=60
        )

    def _launch(self, one: Wanted) -> FakeChild:
        child = FakeChild()
        self.children.setdefault(one.key, []).append(child)
        return child

    def last(self, key: str) -> FakeChild:
        return self.children[key][-1]


SESSION_ID = uuid.uuid4()
SESSION = Wanted("session:a", ("python", "session"), (SESSION_ID,))
LIVE = Wanted("live:xp", ("python", "live"))


def test_what_is_wanted_is_started_once() -> None:
    h = Harness()
    h.supervisor.tick({SESSION.key: SESSION, LIVE.key: LIVE})
    h.supervisor.tick({SESSION.key: SESSION, LIVE.key: LIVE})

    assert {key: len(started) for key, started in h.children.items()} == {
        "session:a": 1,
        "live:xp": 1,
    }


def test_a_child_that_dies_is_restarted_later_each_time() -> None:
    h = Harness()
    wanted = {SESSION.key: SESSION}
    h.supervisor.tick(wanted)
    h.last("session:a").code = 1  # crashed

    h.supervisor.tick(wanted)  # notices; waits 15 s before trying again
    assert len(h.children["session:a"]) == 1
    h.now = 15
    h.supervisor.tick(wanted)
    assert len(h.children["session:a"]) == 2

    h.last("session:a").code = 1  # again: now 30 s
    h.supervisor.tick(wanted)
    h.now = 30
    h.supervisor.tick(wanted)
    assert len(h.children["session:a"]) == 2
    h.now = 45
    h.supervisor.tick(wanted)
    assert len(h.children["session:a"]) == 3


def test_a_session_no_longer_wanted_is_asked_to_stop_then_killed_after_the_grace() -> None:
    h = Harness()
    h.supervisor.tick({SESSION.key: SESSION})

    h.supervisor.tick({})
    assert h.asked == [SESSION_ID]
    assert not h.last("session:a").terminated, "asked, not killed"

    h.now = 59
    h.supervisor.tick({})
    assert not h.last("session:a").terminated
    h.now = 60
    h.supervisor.tick({})
    assert h.last("session:a").terminated


def test_a_session_that_stops_when_asked_is_not_restarted() -> None:
    h = Harness()
    h.supervisor.tick({SESSION.key: SESSION})
    h.supervisor.tick({})
    h.last("session:a").code = 0  # finished its bar and stopped
    h.supervisor.tick({})

    assert len(h.children["session:a"]) == 1
    assert "session:a" not in h.supervisor.running


def test_a_live_loop_no_longer_needed_is_killed_at_once() -> None:
    h = Harness()
    h.supervisor.tick({LIVE.key: LIVE})
    h.supervisor.tick({})

    assert h.last("live:xp").terminated


def item(broker: str | None = "xp", **fields: object) -> FollowedPair:
    values: dict[str, object] = {
        "setup_id": uuid.uuid4(),
        "strategy_id": uuid.uuid4(),
        "instrument_id": uuid.uuid4(),
        "timeframe": "H1",
        "cost_model": {"type": "spread", "spread_points": "5"},
        "no_target_r": Decimal(5),
        "broker": broker,
    }
    values.update(fields)
    return FollowedPair(**values)  # type: ignore[arg-type]


def test_each_broker_an_item_needs_gets_one_live_loop() -> None:
    items = [item(), item(), item(broker="tradeview"), item(broker=None)]

    wanted = live_loops(items, python="py")

    assert sorted(wanted) == ["live:tradeview", "live:xp"]
    assert wanted["live:xp"].session_ids == (), "a live loop is killed, not asked"


def test_an_orphans_broker_keeps_its_live_loop_with_no_setup_on_it() -> None:
    assert set(live_loops([item()], python="py", also_brokers={"activtrades"})) == {
        "live:xp",
        "live:activtrades",
    }


def test_a_market_process_is_asked_through_every_session_it_carries() -> None:
    fleet = Fleet(python="py", clock=lambda: 0.0)
    one = item(timeframe="M5")
    two = item(timeframe="M5", instrument_id=one.instrument_id)
    (launch,) = fleet.reconcile(
        [one, two], heard={}, restart=(), ask_to_stop=lambda _session_id: None
    )

    wanted = as_wanted(launch)

    assert set(wanted.session_ids) == set(launch.sessions.values())
    assert wanted.log == "", "its own log, named after its key: never shared with another"
    assert as_wanted(launch) == wanted, "the same process must look the same on every round"


def test_a_child_seen_ending_by_tick_is_still_handed_out_by_the_next_poll() -> None:
    """Otherwise a process ending between a round's poll and its tick was noticed by nobody
    keeping the fleet's book, and the next round asked for it again with the same session ids."""
    h = Harness()
    h.supervisor.tick({SESSION.key: SESSION})
    h.last("session:a").code = 1

    h.supervisor.tick({SESSION.key: SESSION})

    assert h.supervisor.poll() == {"session:a": 1}
    assert h.supervisor.poll() == {}


def test_children_start_with_numpy_held_to_one_thread() -> None:
    """~740 MB of committed memory per process for buffers no session uses (12/10): with
    sixteen sessions, the machine ran out of memory to lend while most of it sat reserved."""
    env = child_environment({"PATH": "C:/bin"})

    assert env["OPENBLAS_NUM_THREADS"] == "1"
    assert env["PATH"] == "C:/bin", "the child lost the supervisor's own environment"


def test_an_operator_setting_the_threads_on_purpose_keeps_it() -> None:
    assert child_environment({"OPENBLAS_NUM_THREADS": "4"})["OPENBLAS_NUM_THREADS"] == "4"


# --------------------------------------------------------------------------- outages

OUTAGE = logging.getLogger("tests.outage")


def unreachable() -> dict[str, Wanted]:
    raise OperationalError("SELECT 1", {}, Exception("connection refused"))


def nothing() -> None:
    pass


def test_an_unreachable_database_skips_the_round_and_touches_no_child() -> None:
    """12/10: a `wsl --shutdown` took Postgres away for minutes. A round that cannot read what
    should run must not read it as "nothing": that would ask every session to stop and kill
    every live loop because the database blinked."""
    h = Harness()
    h.supervisor.tick({SESSION.key: SESSION, LIVE.key: LIVE})

    ran = supervise_round(
        h.supervisor, wanted=unreachable, housekeeping=nothing, outage=Outage(OUTAGE)
    )

    assert ran is False
    assert h.asked == []
    assert not h.last("session:a").terminated
    assert not h.last("live:xp").terminated
    assert set(h.supervisor.running) == {"session:a", "live:xp"}


def test_the_round_after_an_outage_starts_what_is_wanted() -> None:
    h = Harness()
    outage = Outage(OUTAGE)
    supervise_round(h.supervisor, wanted=unreachable, housekeeping=nothing, outage=outage)

    ran = supervise_round(
        h.supervisor, wanted=lambda: {SESSION.key: SESSION}, housekeeping=nothing, outage=outage
    )

    assert ran is True
    assert len(h.children["session:a"]) == 1
    assert outage.rounds == 0


def test_a_recording_that_fails_loses_the_round_but_not_the_reconciliation() -> None:
    h = Harness()

    def redis_down() -> None:
        raise RedisConnectionError("Error 10061 connecting to localhost:6379")

    ran = supervise_round(
        h.supervisor,
        wanted=lambda: {SESSION.key: SESSION},
        housekeeping=redis_down,
        outage=Outage(OUTAGE),
    )

    assert ran is False
    assert len(h.children["session:a"]) == 1, "the children were reconciled before it failed"


def test_a_stop_request_redis_did_not_take_is_asked_again_and_the_grace_starts_then() -> None:
    """With the mark set before the ask, the ask lost to a Redis outage was never repeated, and
    the session was killed when the grace ran out — its row left `running`, reported as a crash."""
    h = Harness()
    h.supervisor.tick({SESSION.key: SESSION})
    down = True

    def ask(session_id: uuid.UUID) -> None:
        if down:
            raise RedisConnectionError("Error 10061 connecting to localhost:6379")
        h.asked.append(session_id)

    h.supervisor.ask_to_stop = ask
    with pytest.raises(RedisConnectionError):
        h.supervisor.tick({})
    down = False
    h.now = 61  # past the 60 s grace, counted from a mark the failed ask must not have set
    h.supervisor.tick({})

    assert h.asked == [SESSION_ID]
    assert not h.last("session:a").terminated


def test_on_exit_one_session_that_cannot_be_asked_does_not_keep_the_others_from_it() -> None:
    h = Harness()
    other_id = uuid.uuid4()
    other = Wanted("session:b", ("python", "session"), (other_id,))
    h.supervisor.tick({SESSION.key: SESSION, other.key: other})

    def ask(session_id: uuid.UUID) -> None:
        if session_id == SESSION_ID:
            raise RedisConnectionError("Error 10061 connecting to localhost:6379")
        h.asked.append(session_id)

    h.supervisor.ask_to_stop = ask
    h.supervisor.stop_all()

    assert h.asked == [other_id]


def test_an_outage_is_said_once_with_its_traceback_then_counted_then_ended(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = Harness()
    outage = Outage(OUTAGE)
    with caplog.at_level(logging.INFO, logger=OUTAGE.name):
        for _ in range(3):
            supervise_round(h.supervisor, wanted=unreachable, housekeeping=nothing, outage=outage)
        supervise_round(h.supervisor, wanted=dict, housekeeping=nothing, outage=outage)

    said = [(r.levelname, r.exc_info is not None) for r in caplog.records if r.name == OUTAGE.name]
    assert said == [("ERROR", True), ("WARNING", False), ("WARNING", False), ("INFO", False)]
    assert caplog.records[-1].getMessage() == "back after 3 lost round(s)"


# --------------------------------------------------------------------------- what Redis says


class Said:
    """The few Redis calls the supervisor reads the fleet's news with."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = dict(values)

    def mget(self, keys: list[str]) -> list[str | None]:
        return [self.values.get(key) for key in keys]

    def scan_iter(self, match: str) -> Iterator[str]:
        prefix = match.removesuffix("*")
        return iter([key for key in list(self.values) if key.startswith(prefix)])

    def delete(self, key: str) -> int:
        return 1 if self.values.pop(key, None) is not None else 0


def test_what_each_market_process_said_is_read_and_silence_is_none() -> None:
    session_id = uuid.uuid4()
    redis = Said(
        {market_state_key("market:a"): json.dumps({"phase": "live", "sessions": [str(session_id)]})}
    )

    heard = heard_from(redis, ["market:a", "market:b"])  # type: ignore[arg-type]

    state = heard["market:a"]
    assert state is not None
    assert state.sessions == {session_id}
    assert heard["market:b"] is None


def test_a_restart_request_is_taken_once() -> None:
    instrument = uuid.uuid4()
    redis = Said({restart_key(instrument, "M5"): "1", "signals:restart:garbage": "1"})

    first = take_restarts(redis)  # type: ignore[arg-type]
    second = take_restarts(redis)  # type: ignore[arg-type]

    assert first == [MarketKey(instrument, "M5")]
    assert second == []


# --------------------------------------------------------------------------- the signals' history


class Events:
    """`signals.events` with one consumer group: `>` hands out what was never delivered, `0` what
    this consumer was handed and never acknowledged."""

    def __init__(self, entries: list[tuple[str, dict[str, str]]]) -> None:
        self.entries = entries
        self.delivered: dict[str, dict[str, str]] = {}
        self.acked: list[str] = []
        self.cursors: list[str] = []

    def xgroup_create(
        self,
        name: str,
        groupname: str,
        id: str = "$",  # noqa: A002 — redis-py's own name for it
        mkstream: bool = False,
    ) -> bool:
        return True

    def xreadgroup(
        self, groupname: str, consumername: str, streams: dict[str, str], count: int = 10
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        cursor = next(iter(streams.values()))
        self.cursors.append(cursor)
        if cursor == "0":
            found = [(i, f) for i, f in self.delivered.items() if i not in self.acked]
        else:
            found = [(i, f) for i, f in self.entries if i not in self.delivered][:count]
            self.delivered.update(found)
        return [("signals.events", found)] if found else []

    def xack(self, name: str, groupname: str, *ids: str) -> int:
        self.acked.extend(ids)
        return len(ids)


def recording(monkeypatch: pytest.MonkeyPatch, *, down_after: int | None = None) -> list[str]:
    """What reaches the history; the database goes away after `down_after` rows."""
    written: list[str] = []

    def record(_session: object, fields: dict[str, str]) -> None:
        if down_after is not None and len(written) >= down_after:
            raise OperationalError("INSERT", {}, Exception("connection refused"))
        written.append(fields["number"])

    monkeypatch.setattr("tradeforge_api.live.supervisor.record_event", record)
    monkeypatch.setattr(
        "tradeforge_api.live.supervisor.session_scope", lambda _factory: contextlib.nullcontext()
    )
    return written


def entry(number: int) -> tuple[str, dict[str, str]]:
    return (f"{number}-0", {"number": str(number), "kind": "armed"})


def test_what_a_round_took_and_did_not_write_is_written_by_the_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database gone half way used to leave the rest delivered, unacknowledged and never read
    again — a read of `>` offers only what was never delivered."""
    events = Events([entry(1), entry(2), entry(3)])
    written = recording(monkeypatch, down_after=1)

    with pytest.raises(OperationalError):
        record_signals(events, factory=None)  # type: ignore[arg-type]
    monkeypatch.setattr(
        "tradeforge_api.live.supervisor.record_event", lambda _s, f: written.append(f["number"])
    )
    record_signals(events, factory=None)  # type: ignore[arg-type]

    assert written == ["1", "2", "3"], "in order, the pending ones first"
    assert events.acked == ["1-0", "2-0", "3-0"]


def test_an_entry_that_can_never_be_written_is_skipped_and_holds_nothing_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = ("2-0", {"kind": "armed"})  # no number
    events = Events([entry(1), broken, entry(3)])
    written = recording(monkeypatch)

    record_signals(events, factory=None)  # type: ignore[arg-type]

    assert written == ["1", "3"]
    assert events.acked == ["1-0", "2-0", "3-0"]


def test_under_the_cap_a_session_running_keeps_its_place_and_a_newcomer_waits() -> None:
    """A market added to an old setup sorts first; cut at the cap, it used to push out a session
    already running — its armed signals cancelled for a pair that only had to wait."""
    old_setup_new_market, running_a, running_b = item(), item(), item()
    followed = [old_setup_new_market, running_a, running_b]
    carried = {(one.setup_id, one.instrument_id) for one in (running_a, running_b)}

    may_run, waiting = within_cap(followed, carried=carried, cap=2)

    assert may_run == [running_a, running_b]
    assert waiting == [old_setup_new_market]
