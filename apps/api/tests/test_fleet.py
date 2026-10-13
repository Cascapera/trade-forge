"""The supervisor's book of market processes (ADR-0033): what to launch, keep, ask to stop."""

import datetime as dt
import json
import uuid
from decimal import Decimal

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from tradeforge_api.live.fleet import (
    Fleet,
    Launch,
    MarketKey,
    MarketState,
    parse_market_state,
    parse_restart_field,
    restart_field,
)
from tradeforge_db.live_setups import FollowedPair

WIN, WDO = uuid.uuid4(), uuid.uuid4()


def item(instrument: uuid.UUID = WIN, timeframe: str = "M5") -> FollowedPair:
    return FollowedPair(
        setup_id=uuid.uuid4(),
        strategy_id=uuid.uuid4(),
        instrument_id=instrument,
        timeframe=timeframe,
        cost_model={"type": "spread", "spread_points": "5"},
        no_target_r=Decimal(5),
        broker="xp",
    )


class Book:
    """A fleet on a clock the test moves, with every stop request it made."""

    def __init__(self) -> None:
        self.now = 0.0
        self.asked: list[uuid.UUID] = []
        self.fleet = Fleet(python="py", clock=lambda: self.now)

    def round(
        self,
        items: list[FollowedPair],
        heard: dict[str, MarketState | None] | None = None,
        restart: tuple[MarketKey, ...] = (),
    ) -> list[Launch]:
        return self.fleet.reconcile(
            items, heard=heard or {}, restart=restart, ask_to_stop=self.asked.append
        )


def carrying(launch: Launch, *sessions: uuid.UUID) -> dict[str, MarketState | None]:
    return {launch.key: MarketState(phase="live", sessions=frozenset(sessions), at=None)}


def members(launch: Launch) -> list[dict[str, str | None]]:
    argv = list(launch.argv)
    return [json.loads(argv[i + 1]) for i, word in enumerate(argv) if word == "--member"]


def test_at_start_the_sessions_of_each_market_launch_together() -> None:
    book = Book()
    win_a, win_b, wdo = item(WIN), item(WIN), item(WDO)

    launches = book.round([win_a, win_b, wdo])

    assert sorted(len(launch.sessions) for launch in launches) == [1, 2]
    together = next(launch for launch in launches if launch.market == MarketKey(WIN, "M5"))
    assert {m["setup"] for m in members(together)} == {str(win_a.setup_id), str(win_b.setup_id)}
    assert together.argv[:3] == ("py", "-m", "tradeforge_api.live.market")
    assert members(together)[0]["spread_points"] == "5"


def test_the_same_timeframe_is_one_market_and_another_timeframe_is_another() -> None:
    launches = Book().round([item(WIN, "M5"), item(WIN, "M15")])

    assert {launch.market for launch in launches} == {MarketKey(WIN, "M5"), MarketKey(WIN, "M15")}


def test_a_setup_added_to_a_running_market_starts_alone_and_the_others_are_not_touched() -> None:
    """Restarting the market for it would cancel the others' armed signals on the channel."""
    book = Book()
    first = [item(), item()]
    (running,) = book.round(first)

    added = item()
    (alone,) = book.round([*first, added])

    assert list(alone.sessions) == [(added.setup_id, added.instrument_id)]
    assert book.fleet.kept() == {running.key: running, alone.key: alone}
    assert book.asked == []


def test_a_setup_switched_off_is_asked_to_stop_inside_its_process_and_leaves_without_a_pause() -> (
    None
):
    book = Book()
    staying, leaving = item(), item()
    (launch,) = book.round([staying, leaving])
    leaving_id = launch.sessions[(leaving.setup_id, leaving.instrument_id)]
    staying_id = launch.sessions[(staying.setup_id, staying.instrument_id)]

    book.round([staying])
    assert book.asked == [leaving_id]
    assert launch.key in book.fleet.kept(), "the process goes on for the one still wanted"

    book.round([staying], heard=carrying(launch, staying_id))
    assert book.fleet.alive() == {staying_id}
    assert book.fleet.waiting() == {}


def test_a_session_that_left_unasked_failed_and_comes_back_alone_later_under_a_new_id() -> None:
    book = Book()
    healthy, broken = item(), item()
    (launch,) = book.round([healthy, broken])
    healthy_id = launch.sessions[(healthy.setup_id, healthy.instrument_id)]
    broken_id = launch.sessions[(broken.setup_id, broken.instrument_id)]

    assert book.round([healthy, broken], heard=carrying(launch, healthy_id)) == []
    assert book.fleet.waiting() == {(broken.setup_id, broken.instrument_id): 15.0}
    book.now = 15
    (again,) = book.round([healthy, broken], heard=carrying(launch, healthy_id))

    assert list(again.sessions) == [(broken.setup_id, broken.instrument_id)]
    assert again.sessions[(broken.setup_id, broken.instrument_id)] != broken_id, (
        "one id per launch: a relaunch must not claim what the dead one posted"
    )
    assert broken_id not in book.fleet.alive(), "its signals are orphans meanwhile"


def test_a_process_that_crashed_takes_its_sessions_with_it_and_they_come_back_together() -> None:
    book = Book()
    items = [item(), item()]
    (launch,) = book.round(items)

    book.fleet.exited({launch.key: 1})
    assert book.round(items) == [], "after a pause, not at once"
    book.now = 15
    (again,) = book.round(items)

    assert len(again.sessions) == 2
    assert book.fleet.kept() == {again.key: again}


def test_a_process_that_ended_cleanly_is_followed_by_no_pause() -> None:
    book = Book()
    items = [item()]
    (launch,) = book.round(items)

    book.fleet.exited({launch.key: 0})

    assert len(book.round(items)) == 1


def test_a_process_not_heard_from_is_taken_to_carry_everything() -> None:
    book = Book()
    items = [item(), item()]
    (launch,) = book.round(items)

    assert book.round(items, heard={launch.key: None}) == []
    assert book.fleet.alive() == set(launch.sessions.values())


def test_a_restart_asks_the_whole_market_and_waits_for_its_last_process_before_launching() -> None:
    book = Book()
    first, added, other = [item(WIN), item(WIN)], item(WIN), item(WDO)
    (main, elsewhere) = sorted(
        book.round([*first, other]), key=lambda one: one.market != MarketKey(WIN, "M5")
    )
    (satellite,) = book.round([*first, other, added])
    everything = [*first, other, added]

    assert book.round(everything, restart=(MarketKey(WIN, "M5"),)) == []
    win_sessions = {*main.sessions.values(), *satellite.sessions.values()}
    assert set(book.asked) == win_sessions
    assert book.fleet.kept() == {elsewhere.key: elsewhere}, "the other market is not touched"

    book.fleet.exited({main.key: 0})
    assert book.round(everything) == [], "the satellite is still running"
    book.fleet.exited({satellite.key: 0})
    (together,) = book.round(everything)

    assert len(together.sessions) == 3
    book.asked.clear()
    book.round(everything)
    assert book.asked == [], "the new process's sessions are not the old ones"


def test_the_sessions_alive_include_those_asked_to_stop_until_they_leave() -> None:
    """One being stopped watches its signals to its last bar; two voices would post twice."""
    book = Book()
    staying, leaving = item(), item()
    (launch,) = book.round([staying, leaving])

    book.round([staying])

    assert book.fleet.alive() == set(launch.sessions.values())


def test_a_stop_request_redis_did_not_take_is_asked_again_next_round() -> None:
    book = Book()
    staying, leaving = item(), item()
    (launch,) = book.round([staying, leaving])

    def down(_session_id: uuid.UUID) -> None:
        raise RedisConnectionError("Error 10061 connecting to localhost:6379")

    with pytest.raises(RedisConnectionError):
        book.fleet.reconcile([staying], heard={}, restart=(), ask_to_stop=down)
    book.round([staying])

    assert book.asked == [launch.sessions[(leaving.setup_id, leaving.instrument_id)]]


def test_what_it_tells_the_screen() -> None:
    book = Book()
    one = item()
    (launch,) = book.round([one])
    at = dt.datetime(2026, 10, 13, 12, tzinfo=dt.UTC)

    told = book.fleet.describe(carrying(launch, *launch.sessions.values()), at=at)

    assert told["at"] == "2026-10-13T12:00:00+00:00"
    processes = told["processes"]
    assert isinstance(processes, list)
    (process,) = processes
    assert process["instrument_id"] == str(WIN)
    assert process["phase"] == "live"
    assert process["sessions"] == [
        {
            "setup_id": str(one.setup_id),
            "session_id": str(launch.sessions[(one.setup_id, WIN)]),
            "asked": False,
        }
    ]


def test_a_market_process_word_and_a_restart_request_read_back() -> None:
    session_id = uuid.uuid4()
    raw = json.dumps(
        {"phase": "warming", "sessions": [str(session_id)], "at": "2026-10-13T12:00:00+00:00"}
    )

    state = parse_market_state(raw)

    assert state is not None
    assert state.phase == "warming"
    assert state.sessions == {session_id}
    assert parse_market_state(None) is None
    assert parse_market_state("{not json") is None
    assert parse_restart_field(restart_field(WIN, "M15")) == MarketKey(WIN, "M15")
    assert parse_restart_field("not-an-id:M5") is None
    assert parse_restart_field(str(WIN)) is None
