"""`CandleStream`, against a double that models what Redis promises.

⚠️ **Read this before trusting a green run here.** The double below is a description of Redis
consumer groups written by the same person who wrote the code under test, so it agrees with
this module by construction — including where both are wrong. Everything in this file is
therefore a claim about *this module's logic*, and the claim that Redis actually behaves this
way lives in `test_candle_stream_integration.py`, which talks to a real server.

The one thing a double is uniquely good at is the failure this module exists to survive: a
session dying between two bars. Killing a real process at a chosen instant is awkward; simply
not asking the generator for the next bar is exact.
"""

import datetime as dt
from decimal import Decimal

import pytest
import redis
from redis.exceptions import ResponseError
from redis.typing import GroupT, KeyT, StreamIdT

from tradeforge_api.live import CandleStream, StreamReader
from tradeforge_api.live.testing import FakeRedisStreams, published
from tradeforge_collector.live import Subscription
from tradeforge_collector.publisher import entry_id
from tradeforge_engine.domain import Candle

EURUSD_H1 = Subscription(symbol="EURUSD", timeframe="H1")
STREAM = "candles.EURUSD.H1"

# How many empty blocking reads the double tolerates before calling the scenario broken.


def a_candle(index: int, *, close: str = "1.10000", spread: int = 12) -> Candle:
    """The high and low are derived from the body rather than fixed, because `Candle` refuses a
    bar that does not contain its own body — a hand-picked high is a fixture that breaks the
    moment a scenario wants a different close."""
    open_ = Decimal("1.10000")
    body = [open_, Decimal(close)]
    return Candle(
        time=dt.datetime(2026, 8, 24, 10, tzinfo=dt.UTC) + index * dt.timedelta(hours=1),
        open=open_,
        high=max(body) + Decimal("0.00050"),
        low=min(body) - Decimal("0.00050"),
        close=Decimal(close),
        tick_volume=500,
        spread=spread,
        real_volume=0,
    )


def a_stream(client: StreamReader, *, start_id: str = "0") -> CandleStream:
    """⚠️ `start_id="0"`, not the production default of `"$"`.

    `$` means "only bars published after this group was created", so a scenario that seeds the
    stream first and *then* starts the session sees nothing at all — the generator blocks for
    ever, correctly. That is the warm-up problem `CandleStream.__init__` documents, and it is
    also the reason a test double earns its keep: the first version of this file hung.
    """
    return CandleStream(client, EURUSD_H1, group="session-1", block_ms=10, start_id=start_id)


def test_the_real_redis_client_satisfies_the_protocol() -> None:
    """Proved by assignment, so mypy checks it — a `Protocol` nothing is ever assigned to is a
    description of an imaginary client. A runtime `isinstance` could not do this: protocol
    checks at runtime compare method *names*, not signatures, so a client whose `xack` took
    different arguments would pass one and fail in production."""
    client: StreamReader = redis.Redis()
    assert client is not None


def test_candles_come_back_in_order_and_survive_the_round_trip() -> None:
    """The bars go in through the publisher's encoder and come out as `Candle`s. Every field,
    including the spread — which is the one `BarSpreadCostModel` will charge from."""
    sent = [a_candle(0, close="1.10100", spread=12), a_candle(1, close="1.10250", spread=31)]
    stream = a_stream(FakeRedisStreams(EURUSD_H1, published(EURUSD_H1, *sent)))

    got = [next(stream.candles()) for _ in range(1)]
    assert got[0] == sent[0]

    everything = []
    candles = a_stream(FakeRedisStreams(EURUSD_H1, published(EURUSD_H1, *sent))).candles()
    for _ in range(2):
        everything.append(next(candles))
    assert everything == sent
    assert [c.spread for c in everything] == [12, 31]


def test_a_bar_is_not_acknowledged_until_the_next_one_is_asked_for() -> None:
    """The crash-safety claim, stated as the only thing that distinguishes it.

    A session that has *received* bar 0 has not necessarily *survived* bar 0. If the ack went
    out on delivery, a crash right here would have told Redis the bar was handled and the bar
    would never be offered again — a hole in the equity curve that nothing anywhere reports.

    ⚠️ The assertion has to be made in the middle. Drained to the end, an eager ack and a lazy
    one both finish with everything acked; the scenario that separates them stops after the
    first bar and looks.
    """
    client = FakeRedisStreams(EURUSD_H1, published(EURUSD_H1, a_candle(0), a_candle(1)))
    candles = a_stream(client).candles()

    first = next(candles)
    assert first == a_candle(0)
    assert client.acked == [], "the bar was acknowledged before the consumer had finished with it"
    assert client.pending["session"] == [entry_id(a_candle(0)), entry_id(a_candle(1))]

    next(candles)
    assert client.acked == [entry_id(a_candle(0))], (
        "asking for the next bar did not confirm this one"
    )


def test_a_restarted_session_is_offered_what_it_never_confirmed() -> None:
    """The other half of the same decision: at least once, never at most once.

    The first session takes bar 0 and dies — modelled by simply abandoning the generator. The
    second session, same group and same consumer name, must be handed bar 0 again before it
    sees bar 1, because as far as Redis knows nobody ever finished with it.
    """
    client = FakeRedisStreams(EURUSD_H1, published(EURUSD_H1, a_candle(0), a_candle(1)))

    first_life = a_stream(client).candles()
    assert next(first_life) == a_candle(0)
    del first_life  # the process died here; nothing was acked

    second_life = a_stream(client).candles()
    assert next(second_life) == a_candle(0), "the unconfirmed bar was lost on restart"
    assert next(second_life) == a_candle(1)


def test_the_pending_backlog_is_drained_before_new_bars() -> None:
    """Order matters more than it looks: the engine's `_reject_out_of_order` refuses a bar
    older than the last one it saw, so a session handed its backlog *after* the new bars would
    not merely be confused — it would raise and stop."""
    client = FakeRedisStreams(EURUSD_H1, published(EURUSD_H1, a_candle(0), a_candle(1)))
    abandoned = a_stream(client).candles()
    next(abandoned)  # bar 0 is now pending and unacked
    del abandoned

    client.entries.extend(published(EURUSD_H1, a_candle(2)))

    resumed = a_stream(client).candles()
    assert [next(resumed) for _ in range(3)] == [a_candle(0), a_candle(1), a_candle(2)]


def test_draining_an_empty_backlog_does_not_wait() -> None:
    """A clean start-up has nothing pending, and blocking on that would stall every session
    start for the whole timeout — a minute of nothing, on every restart.

    The count is per cursor: reading `>` for new bars blocks, and must. What may not block is
    the `0` that asks what this consumer left unconfirmed, because on a clean start the honest
    answer is "nothing" and it is available immediately.
    """
    client = FakeRedisStreams(EURUSD_H1, published(EURUSD_H1, a_candle(0)))
    candles = a_stream(client).candles()

    next(candles)
    assert client.blocked_on == {">"}, (
        f"the pending list was read with a block: {client.blocked_on}"
    )


def test_an_existing_group_is_not_an_error() -> None:
    """A restart must not fail because the group it created last time is still there."""
    client = FakeRedisStreams(EURUSD_H1, published(EURUSD_H1, a_candle(0)))
    stream = a_stream(client)

    assert stream.ensure_group() is True
    assert stream.ensure_group() is False


def test_any_other_server_error_propagates() -> None:
    """redis-py raises one exception type for every server-side error, so "already exists" is
    matched on the message. Everything else — a key holding another type, a server out of
    memory — must not be swallowed as a benign restart."""

    class Broken(FakeRedisStreams):
        def xgroup_create(
            self,
            name: KeyT,
            groupname: GroupT,
            id: StreamIdT,  # noqa: A002
            mkstream: bool,
        ) -> object:
            raise ResponseError("WRONGTYPE Operation against a key holding the wrong kind of value")

    with pytest.raises(ResponseError, match="WRONGTYPE"):
        a_stream(Broken(EURUSD_H1)).ensure_group()


def test_a_session_started_now_does_not_replay_what_is_already_on_the_stream() -> None:
    """`start_id="$"` is the production default, and this is what it means.

    The bars already sitting in the stream are history — the collector wrote them before this
    session existed. Handing them over would make a session "trade" a week of the past at
    prices long gone and report the result as live. The cost is the warm-up hole this default
    leaves behind, which `CandleStream.__init__` names and PR-302-B has to close.
    """
    client = FakeRedisStreams(EURUSD_H1, published(EURUSD_H1, a_candle(0), a_candle(1)))
    stream = a_stream(client, start_id="$")

    # ⚠️ Explicitly, before publishing. `candles()` is a generator, so the group it creates does
    # not exist until the first `next()` — and `$` is resolved at creation. Seeding the stream
    # first would put bar 2 in the backlog too, and the test would prove nothing.
    stream.ensure_group()
    client.entries.extend(published(EURUSD_H1, a_candle(2)))
    candles = stream.candles()

    assert next(candles) == a_candle(2), "a session started at $ was handed the backlog"
