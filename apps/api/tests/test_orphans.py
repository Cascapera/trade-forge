"""Signals whose session is gone, ended by price (09/10): #1 and #2 stayed open for good."""

import contextlib
import datetime as dt
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest

from tradeforge_api.live import orphans
from tradeforge_api.live.notifier import format_message
from tradeforge_api.live.orphans import ORPHAN_NOTE, OrphanWatch, annul, orphan_brokers
from tradeforge_api.live.signals import SIGNALS_STREAM
from tradeforge_api.live.supervisor import (
    Supervisor,
    Wanted,
    alive_sessions,
    forget_the_stopped,
    wanted_children,
)
from tradeforge_db.live_setups import FollowedPair, OpenSignal

T0 = dt.datetime(2026, 10, 9, 18, 45, tzinfo=dt.UTC)
M5 = dt.timedelta(minutes=5)


class Streams:
    """The Redis calls the resolver makes: streams by id, and the demand's sorted sets."""

    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.sets: dict[str, dict[str, float]] = {}

    def xadd(self, name: str, fields: dict[str, str], **_: object) -> str:
        entries = self.streams.setdefault(name, [])
        entry_id = f"{len(entries) + 1}-0"
        entries.append((entry_id, fields))
        return entry_id

    def bar(self, symbol: str, at: dt.datetime, low: str, high: str) -> None:
        ms = int(at.timestamp() * 1000)
        fields = {"time": at.isoformat(), "low": low, "high": high, "open": low, "close": high}
        self.streams.setdefault(f"candles.{symbol}.M5", []).append((f"{ms}-0", fields))

    def xrange(self, name: str, min: str = "-", **_: object) -> list[tuple[str, dict[str, str]]]:  # noqa: A002
        floor = 0 if min == "-" else int(min)
        return [
            (entry_id, fields)
            for entry_id, fields in self.streams.get(name, [])
            if name == SIGNALS_STREAM or int(entry_id.split("-")[0]) >= floor
        ]

    def zadd(self, name: str, mapping: dict[str, float]) -> int:
        self.sets.setdefault(name, {}).update(mapping)
        return len(mapping)

    def posted(self) -> list[dict[str, str]]:
        return [fields for _, fields in self.streams.get(SIGNALS_STREAM, [])]


def signal(**fields: Any) -> OpenSignal:
    """#2 as it was: USDJPY sold at 158.237, stop 158.267, target 158.087, filled at T0."""
    base: dict[str, Any] = {
        "number": 2,
        "status": "triggered",
        "symbol": "USDJPY",
        "timeframe": "M5",
        "side": "short",
        "entry": Decimal("158.237"),
        "stop": Decimal("158.267"),
        "target": Decimal("158.087"),
        "armed_at": T0 - M5,
        "triggered_at": T0,
        "session_id": uuid.uuid4(),
        "setup_id": uuid.uuid4(),
        "strategy_id": uuid.uuid4(),
        "instrument_id": uuid.uuid4(),
        "strategy": "9.1 ORIGINAL",
        "broker": "activtrades",
        "no_target_r": Decimal(5),
    }
    base.update(fields)
    return OpenSignal(**base)


@pytest.fixture
def found(monkeypatch: pytest.MonkeyPatch) -> list[OpenSignal]:
    """What the database answers: the orphans a test puts here."""
    rows: list[OpenSignal] = []

    @contextlib.contextmanager
    def scope(_factory: object) -> Iterator[None]:
        yield None

    monkeypatch.setattr(orphans, "session_scope", scope)
    monkeypatch.setattr(orphans, "orphan_signals", lambda _session, _alive: list(rows))
    monkeypatch.setattr(
        orphans,
        "open_signal",
        lambda _session, number: next((one for one in rows if one.number == number), None),
    )
    return rows


def resolve(redis: Streams, watch: OrphanWatch | None = None) -> int:
    return orphans.resolve_orphans(redis, None, set(), watch or OrphanWatch())  # type: ignore[arg-type]


def test_an_orphan_in_a_trade_is_closed_at_its_stop_by_price(found: list[OpenSignal]) -> None:
    """#2: the session restarted at 15:54 and the stop was hit at 16:20 with nobody to say so."""
    found.append(signal())
    redis = Streams()
    redis.bar("USDJPY", T0, "158.200", "158.250")  # the fill bar: its range proves nothing
    redis.bar("USDJPY", T0 + M5, "158.210", "158.260")
    redis.bar("USDJPY", T0 + 2 * M5, "158.220", "158.280")  # reaches 158.267

    assert resolve(redis) == 1

    [closed] = redis.posted()
    assert (closed["kind"], closed["number"], closed["exit_price"], closed["result_r"]) == (
        "closed",
        "2",
        "158.267",
        "-1.00",
    )
    assert closed["time"] == (T0 + 2 * M5).isoformat()
    assert closed["note"] == ORPHAN_NOTE
    assert redis.sets["live:wanted:activtrades"], "its market is still asked of the live loop"


def test_an_orphan_waits_for_the_bar_that_ends_it_and_reads_each_bar_once(
    found: list[OpenSignal],
) -> None:
    found.append(signal())
    redis, watch = Streams(), OrphanWatch()
    redis.bar("USDJPY", T0 + M5, "158.210", "158.260")

    assert resolve(redis, watch) == 0
    assert watch.checked[2] == T0 + M5

    redis.bar("USDJPY", T0 + 2 * M5, "158.080", "158.200")  # the target
    assert resolve(redis, watch) == 1
    assert redis.posted()[0]["result_r"] == "5.00"
    assert resolve(redis, watch) == 0, "posted once, while the history catches up"


def test_one_bar_through_both_levels_is_read_as_the_stop() -> None:
    one = signal(side="long", entry=Decimal(100), stop=Decimal(90), target=Decimal(120))

    ended = orphans._exit(
        one,
        entry=Decimal(100),
        first_stop=Decimal(90),
        stop=Decimal(90),
        bar={"low": "85", "high": "125"},
    )

    assert ended == (Decimal(90), Decimal(-1))


def test_a_breakeven_moves_the_stop_it_is_followed_to(found: list[OpenSignal]) -> None:
    found.append(signal())
    redis = Streams()
    redis.xadd(SIGNALS_STREAM, {"kind": "breakeven", "number": "2", "moved_stop": "158.237"})
    redis.bar("USDJPY", T0 + M5, "158.200", "158.240")  # back to the entry: out at 0R

    resolve(redis)

    closed = redis.posted()[-1]
    assert (closed["exit_price"], closed["result_r"]) == ("158.237", "0.00")


def test_with_no_target_it_ends_at_no_target_r(found: list[OpenSignal]) -> None:
    found.append(signal(side="long", entry=Decimal(100), stop=Decimal(98), target=None))
    redis = Streams()
    redis.bar("USDJPY", T0 + M5, "99", "110")  # 5R is 110

    resolve(redis)

    assert (redis.posted()[0]["exit_price"], redis.posted()[0]["result_r"]) == ("110", "5.00")


def test_an_armed_orphan_is_cancelled_with_the_clocks_time(found: list[OpenSignal]) -> None:
    found.append(signal(status="armed", triggered_at=None, number=5))
    redis = Streams()

    resolve(redis)

    [cancelled] = redis.posted()
    assert (cancelled["kind"], cancelled["number"], cancelled["clock"]) == (
        "cancelled",
        "5",
        "wall",
    )
    assert "sessão reiniciada" in cancelled["reason"]
    assert "live:wanted:activtrades" not in redis.sets, "an order nobody holds needs no bars"


def test_a_trade_without_its_levels_cannot_be_followed_and_is_cancelled(
    found: list[OpenSignal],
) -> None:
    found.append(signal(stop=None))
    redis = Streams()

    resolve(redis)

    assert redis.posted()[0]["kind"] == "cancelled"


def test_annulling_withdraws_an_open_signal_and_refuses_a_closed_one(
    found: list[OpenSignal],
) -> None:
    found.append(signal(number=1, symbol="Usa500"))
    redis = Streams()

    assert annul(redis, None, 1, "gerado com histórico incompleto")  # type: ignore[arg-type]
    assert not annul(redis, None, 9, "x")  # type: ignore[arg-type]

    [annulled] = redis.posted()
    assert (annulled["kind"], annulled["annulled"], annulled["reason"]) == (
        "cancelled",
        "1",
        "gerado com histórico incompleto",
    )
    text = format_message(annulled)
    assert text.splitlines()[0] == "⚪ ANULADO #1 — Usa500 M5"
    assert "Sinal anulado: gerado com histórico incompleto" in text
    assert "candle" not in text, "the clock's time, not a bar's"


def test_the_brokers_an_orphan_in_a_trade_needs_keep_their_live_loop(
    found: list[OpenSignal],
) -> None:
    found.extend([signal(), signal(number=7, status="armed", broker="xp")])

    assert orphan_brokers(None, set()) == {"activtrades"}  # type: ignore[arg-type]


def test_an_orphans_close_says_how_it_was_followed() -> None:
    text = format_message(
        {
            "kind": "closed",
            "number": "2",
            "symbol": "USDJPY",
            "timeframe": "M5",
            "entry": "158.237",
            "exit_price": "158.267",
            "result_r": "-1.00",
            "note": ORPHAN_NOTE,
            "time": "2026-10-09T19:20:00+00:00",
        }
    )

    assert f"_{ORPHAN_NOTE}_" in text
    assert "16:25 (Brasília) · candle M5 das 16:20" in text


# --- the supervisor ----------------------------------------------------------------------------


class Child:
    def poll(self) -> int | None:
        return None

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


def test_a_pair_whose_session_stopped_gets_a_new_id_on_its_next_launch() -> None:
    """One id per launch: a relaunched session must not claim what the dead one posted."""
    running_pair, stopped_pair = (uuid.uuid4(), uuid.uuid4()), (uuid.uuid4(), uuid.uuid4())
    ids = {running_pair: uuid.uuid4(), stopped_pair: uuid.uuid4()}

    forget_the_stopped(ids, {f"session:{running_pair[0]}:{running_pair[1]}"})

    assert list(ids) == [running_pair]


def test_the_sessions_alive_are_those_running_even_while_stopping() -> None:
    one, two = uuid.uuid4(), uuid.uuid4()
    supervisor = Supervisor(launch=lambda _wanted: Child(), ask_to_stop=lambda _id: None)
    supervisor.tick(
        {
            "session:a": Wanted("session:a", ("x",), one),
            "session:b": Wanted("session:b", ("y",), two),
            "live:xp": Wanted("live:xp", ("z",)),
        }
    )
    supervisor.tick({"session:a": Wanted("session:a", ("x",), one)})  # b asked to stop

    assert alive_sessions(supervisor) == {one, two}


def test_an_orphans_broker_keeps_its_live_loop_with_no_setup_on_it() -> None:
    pair = FollowedPair(
        setup_id=uuid.uuid4(),
        strategy_id=uuid.uuid4(),
        instrument_id=uuid.uuid4(),
        timeframe="M5",
        cost_model={},
        no_target_r=Decimal(5),
        broker="xp",
    )

    wanted = wanted_children(
        [pair], python="py", max_sessions=10, session_ids={}, also_brokers={"activtrades"}
    )

    assert {"live:xp", "live:activtrades"} <= set(wanted)
