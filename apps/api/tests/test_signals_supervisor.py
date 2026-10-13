"""The signals supervisor: what should run, against what does (signals PR 5b)."""

import logging
import uuid
from decimal import Decimal

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from tradeforge_api.live.outage import Outage
from tradeforge_api.live.supervisor import (
    Supervisor,
    Wanted,
    child_environment,
    supervise_round,
    wanted_children,
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
SESSION = Wanted("session:a", ("python", "session"), SESSION_ID)
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


def test_each_item_is_a_signal_session_and_each_broker_one_live_loop() -> None:
    items = [item(), item(), item(broker="tradeview"), item(broker=None)]
    ids: dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID] = {}

    wanted = wanted_children(items, python="py", max_sessions=10, session_ids=ids)

    assert sorted(key for key in wanted if key.startswith("live:")) == ["live:tradeview", "live:xp"]
    session = wanted[f"session:{items[0].setup_id}:{items[0].instrument_id}"]
    mode = session.argv.index("--mode")
    assert session.argv[mode + 1] == "signal"
    assert list(session.argv[-2:]) == ["--spread-points", "5"]
    assert session.session_id == ids[items[0].setup_id, items[0].instrument_id]


def test_the_same_items_want_the_same_children_on_every_tick() -> None:
    """An unchanged item must look unchanged, or it would be stopped and restarted each tick."""
    items = [item()]
    ids: dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID] = {}

    first = wanted_children(items, python="py", max_sessions=10, session_ids=ids)
    second = wanted_children(items, python="py", max_sessions=10, session_ids=ids)

    assert first == second


def test_sessions_beyond_the_cap_wait() -> None:
    items = [item() for _ in range(5)]

    wanted = wanted_children(items, python="py", max_sessions=3, session_ids={})

    assert sum(key.startswith("session:") for key in wanted) == 3


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
    other = Wanted("session:b", ("python", "session"), uuid.uuid4())
    h.supervisor.tick({SESSION.key: SESSION, other.key: other})

    def ask(session_id: uuid.UUID) -> None:
        if session_id == SESSION_ID:
            raise RedisConnectionError("Error 10061 connecting to localhost:6379")
        h.asked.append(session_id)

    h.supervisor.ask_to_stop = ask
    h.supervisor.stop_all()

    assert h.asked == [other.session_id]


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
