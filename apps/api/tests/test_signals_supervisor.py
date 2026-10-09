"""The signals supervisor: what should run, against what does (signals PR 5b)."""

import uuid
from decimal import Decimal

from tradeforge_api.live.supervisor import Supervisor, Wanted, wanted_children
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
