"""`MT5Broker`, against a double that models what the two streams promise.

⚠️ **Read this before trusting a green run here.** The double below is a description of Redis
written by the same person who wrote the code under test, so it agrees with this module by
construction — including where both are wrong. Everything here is a claim about *this broker's
logic*; the claim that Redis behaves this way lives in `test_broker_integration.py`.

What the double is uniquely good at is the arithmetic this broker exists to get right: turning
what a venue did into what the engine can account for, with a spread that has to leave the price
and land in the costs.
"""

import datetime as dt
from decimal import Decimal

import pytest
import redis
from redis.exceptions import ResponseError
from redis.typing import EncodableT, FieldT, GroupT, KeyT, StreamIdT

from tradeforge_api.live.broker import MT5Broker, OrderWire
from tradeforge_engine.domain import (
    AssetClass,
    Candle,
    InstrumentSpec,
    OrderRequest,
    Side,
    SignalKind,
)
from tradeforge_engine.errors import EngineError
from tradeforge_engine.protocols import Broker
from tradeforge_executor.wire import (
    FILLS_STREAM,
    MAX_CLIENT_ID,
    ORDERS_STREAM,
    HeldPosition,
    VenueState,
    WireFill,
    fill_fields,
    venue_state_text,
)

SESSION = "11111111-2222-3333-4444-555555555555"
NOON = dt.datetime(2026, 8, 26, 12, tzinfo=dt.UTC)
NOW = dt.datetime(2026, 8, 26, 20, tzinfo=dt.UTC)
"""When the broker thinks it is. Snapshot freshness is measured against this."""
HOUR = dt.timedelta(hours=1)

EURUSD = InstrumentSpec(
    symbol="EURUSD",
    name="Euro vs US Dollar",
    asset_class=AssetClass.FOREX,
    currency_quote="USD",
    tick_size=Decimal("0.00001"),
    tick_value=Decimal("1"),
    contract_size=Decimal("100000"),
    digits=5,
)
"""One tick is 0.00001 and one tick on a standard lot is worth $1 — so a spread of 0.00007
crossed on a 1.00 lot costs exactly $7, and the arithmetic below can be read by eye."""


class FakeStreams:
    """Two streams and their consumer groups, modelling only what `MT5Broker` leans on."""

    def __init__(self) -> None:
        self.entries: dict[str, list[tuple[str, dict[str, str]]]] = {
            ORDERS_STREAM: [],
            FILLS_STREAM: [],
        }
        self.groups: dict[str, int] = {}
        self.pending: dict[str, list[str]] = {}
        self.acked: list[str] = []
        self.refuse_xadd = False
        self._seq = 0
        self.state: str | None = venue_state_text(VenueState(at=NOW, positions=()))
        """A fresh, empty snapshot by default: the venue was asked and holds nothing.

        ⚠️ A *decision*, not a convenience. `None` means "no key at all", which the broker refuses
        to start on — so a double defaulting to `None` would make every test here fail for the
        same uninteresting reason, and one defaulting to a stale snapshot would hide the freshness
        rule behind a permanent refusal."""

    # --- what the broker writes ---------------------------------------------------
    def xadd(self, name: KeyT, fields: dict[FieldT, EncodableT]) -> object:
        if self.refuse_xadd:
            raise ResponseError("the stream is not accepting writes")
        self._seq += 1
        entry_id = f"{self._seq}-0"
        self.entries[str(name)].append((entry_id, {str(k): str(v) for k, v in fields.items()}))
        return entry_id

    # --- what the broker reads ----------------------------------------------------
    def xgroup_create(
        self,
        name: KeyT,
        groupname: GroupT,
        id: StreamIdT,  # noqa: A002 — redis-py's own parameter name
        mkstream: bool,
    ) -> object:
        group = str(groupname)
        if group in self.groups:
            raise ResponseError("BUSYGROUP Consumer Group name already exists")
        self.groups[group] = len(self.entries[str(name)]) if str(id) == "$" else 0
        return True

    def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[KeyT, StreamIdT],
        count: int | None = None,
        block: int | None = None,
    ) -> object:
        stream = next(str(name) for name in streams)
        cursor = str(streams[stream])
        if cursor == "0":
            waiting = self.pending.get(consumername, [])
            found = [entry for entry in self.entries[stream] if entry[0] in waiting]
            return [(stream, found)] if found else []

        position = self.groups[groupname]
        if position >= len(self.entries[stream]):
            return []
        fresh = self.entries[stream][position:]
        self.groups[groupname] = len(self.entries[stream])
        self.pending.setdefault(consumername, []).extend(entry[0] for entry in fresh)
        return [(stream, fresh)]

    def get(self, name: KeyT) -> object:
        """The venue snapshot, or `None` for "there is no key". Two answers, never one."""
        return self.state

    def xack(self, name: KeyT, groupname: GroupT, *ids: StreamIdT) -> object:
        for raw in ids:
            self.acked.append(str(raw))
            for waiting in self.pending.values():
                if str(raw) in waiting:
                    waiting.remove(str(raw))
        return len(ids)


def a_broker(client: FakeStreams, *, capital: str = "10000") -> MT5Broker:
    broker = MT5Broker(
        client,
        session_id=SESSION,
        instrument=EURUSD,
        initial_capital=Decimal(capital),
        now=lambda: NOW,
    )
    broker.start()
    return broker


def an_order(
    *,
    side: Side = Side.LONG,
    intent: SignalKind = SignalKind.ENTRY,
    volume: str = "1.00",
    client_id: str | None = "zone-42",
    decided_at: dt.datetime = NOON,
    **extra: object,
) -> OrderRequest:
    return OrderRequest(
        symbol="EURUSD",
        side=side,
        intent=intent,
        volume=Decimal(volume),
        decided_at=decided_at,
        client_id=client_id,
        **extra,  # type: ignore[arg-type]
    )


def a_candle(*, time: dt.datetime = NOON + HOUR, close: str = "1.16700") -> Candle:
    body = [Decimal("1.16650"), Decimal(close)]
    return Candle(
        time=time,
        open=Decimal("1.16650"),
        high=max(body) + Decimal("0.00050"),
        low=min(body) - Decimal("0.00050"),
        close=Decimal(close),
        tick_volume=500,
        spread=7,
        real_volume=0,
    )


def publish_fill(  # noqa: PLR0913 — keyword-only knobs on one fixture, not a real signature
    client: FakeStreams,
    *,
    client_id: str = "zone-42",
    price: str = "1.16667",
    volume: str = "1.00",
    spread: str = "0.00007",
    session_id: str = SESSION,
    at: dt.datetime = NOON + HOUR + dt.timedelta(seconds=18),
) -> None:
    """What the executor puts on `fills.inbound`, through the real encoder."""
    fields = fill_fields(
        WireFill(
            client_id=client_id,
            session_id=session_id,
            symbol="EURUSD",
            at=at,
            price=Decimal(price),
            volume=Decimal(volume),
            spread=Decimal(spread),
            ticket=99,
        )
    )
    client.xadd(FILLS_STREAM, dict(fields.items()))


# --------------------------------------------------------------------------- sending


def test_an_accepted_order_is_on_the_wire_and_nothing_waited_for_the_venue() -> None:
    """`submit` is synchronous and the venue is another process. What it promises — and all it
    promises — is that the order was *queued*."""
    client = FakeStreams()
    result = a_broker(client).submit(an_order())

    assert result.accepted
    (_entry_id, fields) = client.entries[ORDERS_STREAM][0]
    assert fields["client_id"] == "zone-42"
    assert fields["session_id"] == SESSION
    assert fields["volume"] == "1.00", "the size went through a float on the way out"
    assert client.entries[FILLS_STREAM] == [], "submitting invented a fill"


def test_an_order_the_strategy_did_not_name_still_gets_a_name() -> None:
    """The wire correlates by `client_id` and `order_audit` is indexed by it, so a market order
    that will never be cancelled still needs a handle."""
    client = FakeStreams()
    broker = a_broker(client)
    broker.submit(an_order(client_id=None))
    broker.submit(an_order(client_id=None))

    names = [fields["client_id"] for _id, fields in client.entries[ORDERS_STREAM]]
    assert names == ["s11111111-1", "s11111111-2"], "two orders shared one name"


def test_a_minted_name_survives_the_venue_s_comment_field() -> None:
    """⚠️ The bug this test was written after, measured on names this code produced.

    A full uuid is 36 characters and the venue keeps 31, so `f"{session_id}:{n}"` reaches the
    account with the counter cut off — and *every* minted order of a session lands under one
    identical comment. Truncation does not shorten a name, it merges names, because every name
    here ends in the part that distinguishes it.
    """
    client = FakeStreams()
    broker = a_broker(client)
    for _ in range(200):
        broker.submit(an_order(client_id=None))

    names = [fields["client_id"] for _id, fields in client.entries[ORDERS_STREAM]]
    assert len(set(names)) == 200, "two minted names collided"
    assert all(len(name) <= MAX_CLIENT_ID for name in names), "a name the venue would truncate"
    assert len({name[:MAX_CLIENT_ID] for name in names}) == 200, "they merge at the account"


def test_a_name_the_venue_cannot_hold_is_refused_rather_than_truncated() -> None:
    """A strategy is free to name a zone anything; the venue is not free to keep it. A refusal is
    loud, recorded and fixable — a silent merge is none of the three."""
    client = FakeStreams()
    result = a_broker(client).submit(an_order(client_id="z" * (MAX_CLIENT_ID + 1)))

    assert not result.accepted
    assert "the venue keeps" in result.reason
    assert client.entries[ORDERS_STREAM] == [], "it went out to be merged anyway"


def test_a_name_exactly_at_the_limit_is_accepted() -> None:
    """The separating half: the guard must not be off by one against a name that fits."""
    client = FakeStreams()
    result = a_broker(client).submit(an_order(client_id="z" * MAX_CLIENT_ID))

    assert result.accepted, "a name that fits exactly was refused"


def test_a_stop_entry_is_refused_rather_than_sent_as_a_market_order() -> None:
    """⚠️ `order_fields` carries no `stop_price` and `MT5Gateway._order_type` only branches on
    `limit_price`, so a breakout entry would reach the venue as a **market order** — filling at
    once, at a price the strategy never authorised, instead of waiting for the level to break.
    """
    client = FakeStreams()
    result = a_broker(client).submit(
        an_order(stop_price=Decimal("1.17000"), limit_price=None),
    )

    assert not result.accepted
    assert "stop_price" in result.reason
    assert client.entries[ORDERS_STREAM] == [], "the order went out anyway"


def test_a_wire_that_refuses_the_write_is_a_refusal_not_a_crash() -> None:
    """And the order is **not** remembered: a broker waiting for a fill that cannot come would
    hold a phantom order for the life of the session."""
    client = FakeStreams()
    client.refuse_xadd = True
    broker = a_broker(client)

    result = broker.submit(an_order())

    assert not result.accepted
    client.refuse_xadd = False
    publish_fill(client)
    with pytest.raises(EngineError, match="never sent"):
        broker.on_bar(a_candle())


# --------------------------------------------------------------------------- receiving


def test_a_buy_fills_at_the_ask_and_the_engine_is_told_the_bid() -> None:
    """⚠️ The measurement this broker was shaped by.

    MT5's bars are bid-based and a buy fills at the ask, so the venue's own price sits above the
    bar it happened in — outside `candle.high` in 7.5% of EURUSD M15 bars, measured — and
    `loop._reject_lookahead` rightly refuses a price the bar never traded at. `SpreadCostModel`
    settled the doctrine in phase 1: crossing the spread is a **cost**, never a worse price.

    Ask 1.16667, spread 0.00007, so the bid was 1.16660 and the crossing cost $7 on a 1.00 lot.
    """
    client = FakeStreams()
    broker = a_broker(client)
    broker.submit(an_order())
    publish_fill(client, price="1.16667", spread="0.00007")

    (fill,) = broker.on_bar(a_candle())

    assert fill.price == Decimal("1.16660"), "the ask was handed to the engine as a traded price"
    assert fill.costs == Decimal("7"), "the crossing was not charged"
    candle = a_candle()
    assert candle.low <= fill.price <= candle.high, "the fill would not survive the loop's guard"


def test_a_sell_entry_fills_at_the_bid_and_crosses_nothing() -> None:
    """The other side of the same rule: a short sells at the bid, which *is* the bar's price
    series, so nothing is converted and nothing is charged. It pays on the way out."""
    client = FakeStreams()
    broker = a_broker(client)
    broker.submit(an_order(side=Side.SHORT))
    publish_fill(client, price="1.16660", spread="0.00007")

    (fill,) = broker.on_bar(a_candle())

    assert fill.price == Decimal("1.16660")
    assert fill.costs == Decimal("0"), "a sell was charged for crossing a spread it did not cross"


def test_the_short_pays_its_crossing_on_the_exit() -> None:
    """Closing a short *buys*, at the ask. That is the leg that crosses, and charging it on the
    entry instead would put the cost on the wrong leg of the round trip.

    Sold at the bid 1.16860 for nothing, bought back at the ask 1.16667 for $7.
    """
    client = FakeStreams()
    broker = a_broker(client)
    broker.submit(an_order(side=Side.SHORT))
    publish_fill(client, price="1.16860", spread="0.00007")
    (entry,) = broker.on_bar(a_candle(close="1.16860"))
    assert entry.costs == Decimal("0"), "the short paid on the way in"

    broker.submit(
        an_order(
            side=Side.SHORT, intent=SignalKind.EXIT, client_id="exit-1", decided_at=NOON + HOUR
        )
    )
    publish_fill(client, client_id="exit-1", price="1.16667", spread="0.00007")

    (fill,) = broker.on_bar(a_candle(time=NOON + 2 * HOUR, close="1.16660"))

    assert fill.price == Decimal("1.16660")
    assert fill.costs == Decimal("7")


def test_the_ledger_reproduces_the_account_after_a_round_trip() -> None:
    """⚠️ The property the whole cost decision was made for.

    Bought at the ask 1.16667, sold at the bid 1.16860. The account made
    (1.16860 - 1.16667) = 0.00193 on a 1.00 lot, which is $193 — and the engine, pricing both
    legs at the bid and charging the crossing once, must reach the same number.
    """
    client = FakeStreams()
    broker = a_broker(client, capital="10000")
    broker.submit(an_order())
    publish_fill(client, price="1.16667", spread="0.00007")
    broker.on_bar(a_candle())

    broker.submit(an_order(intent=SignalKind.EXIT, client_id="exit-1", decided_at=NOON + HOUR))
    publish_fill(client, client_id="exit-1", price="1.16860", spread="0.00007")
    broker.on_bar(a_candle(time=NOON + 2 * HOUR, close="1.16860"))

    (trade,) = broker.trades()
    assert trade.net_pnl == Decimal("193"), "the ledger and the account statement disagree"
    assert broker.account().balance == Decimal("10193")


def test_the_account_is_marked_to_market_on_a_bar_with_no_fill() -> None:
    """⚠️ The protocol's first obligation, and the one a broker forgets silently: the loop reads
    equity straight after every `on_bar`. A broker that only marked on bars where something
    happened draws a flat line through every drawdown it is not trading in.
    """
    client = FakeStreams()
    broker = a_broker(client)
    broker.submit(an_order())
    publish_fill(client, price="1.16667", spread="0.00007")
    broker.on_bar(a_candle())

    assert broker.on_bar(a_candle(time=NOON + 2 * HOUR, close="1.16460")) == ()
    # Bought the bid at 1.16660, the bar closed at 1.16460: 200 ticks against, $200 on a 1.00
    # lot. The $7 crossing was real money and left the balance at the entry; the $200 has not.
    assert broker.account().balance == Decimal("9993"), "an open loss was taken as realised"
    assert broker.account().equity == Decimal("9793"), "equity did not follow the price"


def test_exits_are_folded_in_before_entries_that_arrived_in_the_same_bar() -> None:
    """A reversal inside one bar arrives as two fills whose order is arrival, not causality.
    Applied the wrong way round the ledger refuses the second position — correctly, on a
    sequence that was merely mis-sorted."""
    client = FakeStreams()
    broker = a_broker(client)
    broker.submit(an_order())
    publish_fill(client, price="1.16667", spread="0.00007")
    broker.on_bar(a_candle())

    broker.submit(an_order(side=Side.SHORT, client_id="zone-43", decided_at=NOON + HOUR))
    broker.submit(an_order(intent=SignalKind.EXIT, client_id="exit-1", decided_at=NOON + HOUR))
    publish_fill(client, client_id="zone-43", price="1.16860", spread="0.00007")
    publish_fill(client, client_id="exit-1", price="1.16860", spread="0.00007")

    born = broker.on_bar(a_candle(time=NOON + 2 * HOUR, close="1.16860"))

    assert [fill.order.intent for fill in born] == [SignalKind.EXIT, SignalKind.ENTRY]
    (position,) = broker.positions("EURUSD")
    assert position.side is Side.SHORT, "the reversal did not end short"


def test_another_session_s_fill_is_acknowledged_and_ignored() -> None:
    """⚠️ `fills.inbound` is fan-out, so this group is offered every fill any executor
    publishes. Acknowledged as well as ignored: an entry left pending is redelivered on every
    bar for the life of the session, and the pending list grows without bound."""
    client = FakeStreams()
    broker = a_broker(client)
    publish_fill(client, session_id="somebody-else", client_id="their-zone")

    assert broker.on_bar(a_candle()) == ()
    assert client.acked == ["1-0"], "another session's entry was left on the pending list"


def test_a_fill_this_session_never_sent_stops_it() -> None:
    """A fill that cannot be attributed is a real position at a real venue that this ledger is
    about to be wrong about — and every number computed from here on, including the equity the
    risk manager sizes against, comes from a ledger that has lost a trade."""
    client = FakeStreams()
    broker = a_broker(client)
    publish_fill(client, client_id="a-zone-from-a-previous-life")

    with pytest.raises(EngineError, match="never sent"):
        broker.on_bar(a_candle())


def test_an_unreadable_entry_is_skipped_rather_than_wedging_every_bar() -> None:
    """It is acknowledged either way: an entry that is never acked comes back on the next bar,
    and the next bar fails exactly like this one, for ever."""
    client = FakeStreams()
    broker = a_broker(client)
    client.xadd(FILLS_STREAM, {"client_id": "zone-42", "session_id": SESSION})

    assert broker.on_bar(a_candle()) == ()
    assert client.acked == ["1-0"]


def test_a_fill_read_but_not_folded_in_comes_back_on_the_next_bar() -> None:
    """The pending list is read before the new entries, so a session that died between reading
    a fill and recording it does not resume holding a position it has never heard of."""
    client = FakeStreams()
    broker = a_broker(client)
    broker.submit(an_order())
    publish_fill(client)
    # The first broker reads the entry and dies before acking: model that by reading through the
    # double directly, which is what leaves it on the pending list.
    client.xreadgroup(
        groupname=f"session-{SESSION}",
        consumername="session",
        streams={FILLS_STREAM: ">"},
    )

    (fill,) = broker.on_bar(a_candle())

    assert fill.volume == Decimal("1.00"), "the unacknowledged fill was lost"


def test_the_group_already_existing_is_not_an_error() -> None:
    client = FakeStreams()
    a_broker(client)
    assert (
        MT5Broker(
            client, session_id=SESSION, instrument=EURUSD, initial_capital=Decimal("10000")
        ).ensure_group()
        is False
    )


# --------------------------------------------------------------------------- refusing


def test_a_cancel_reaches_the_wire_under_the_name_the_strategy_gave_it() -> None:
    """⚠️ `True` means the instruction is on its way, not that anything was withdrawn.

    Same asymmetry `submit` has, for the same reason: the venue is another process, and what it
    finds when it looks is not knowable here.
    """
    client = FakeStreams()
    broker = a_broker(client)
    broker.submit(an_order(client_id="zone-42"))

    assert broker.cancel("zone-42") is True

    (_id, fields) = client.entries[ORDERS_STREAM][-1]
    assert fields["kind"] == "cancel"
    assert fields["client_id"] == "zone-42"
    assert fields["session_id"] == SESSION


def test_cancelling_an_order_this_session_never_sent_is_false() -> None:
    """The one thing this end *can* answer. Narrower than `BacktestBroker`'s `False`, which
    really does know whether it held the order — and honest about the difference: in live, "the
    venue had nothing under that name" is a race with a fill, and reporting that as a failed
    cancel would have a strategy re-arming a zone that had just been entered."""
    client = FakeStreams()

    assert a_broker(client).cancel("a-zone-from-a-previous-life") is False
    assert client.entries[ORDERS_STREAM] == [], "an instruction went out for an unknown order"


def test_a_cancel_and_the_order_it_withdraws_share_one_stream() -> None:
    """⚠️ **The reason the envelope is tagged rather than the streams split.**

    Order of arrival is the whole argument. A limit armed and cancelled two bars later must be
    placed before it is withdrawn; across two streams the executor is free to do it the other way
    round, find nothing to cancel, and leave the order alive at the venue for ever.
    """
    client = FakeStreams()
    broker = a_broker(client)
    broker.submit(an_order(client_id="zone-42"))
    broker.cancel("zone-42")

    kinds = [fields["kind"] for _id, fields in client.entries[ORDERS_STREAM]]
    assert kinds == ["order", "cancel"], "the two instructions can be reordered"


def test_a_stop_move_reaches_the_wire_with_the_instant_it_was_decided() -> None:
    """`decided_at` is the anti-lookahead stamp. It is not the executor's to use, and it is not
    the executor's to lose either — `order_audit.request` is evidence."""
    client = FakeStreams()
    broker = a_broker(client)
    broker.submit(an_order())
    publish_fill(client)
    broker.on_bar(a_candle())

    assert broker.modify_stop("EURUSD", Decimal("1.16500"), NOON + HOUR) is True

    (_id, fields) = client.entries[ORDERS_STREAM][-1]
    assert fields["kind"] == "modify_stop"
    assert fields["symbol"] == "EURUSD"
    assert fields["stop_loss"] == "1.16500", "the level went through a float"
    assert fields["decided_at"] == (NOON + HOUR).isoformat()


def test_a_stop_move_carries_its_own_name_not_the_entry_s() -> None:
    """`order_audit.client_id` is not nullable and the trail is indexed by it. A stop move is a
    separate instruction with its own outcome — it can be refused while the entry that opened
    the position succeeded — so filing both under one heading would file two unrelated verdicts
    in one place."""
    client = FakeStreams()
    broker = a_broker(client)
    broker.submit(an_order(client_id="zone-42"))
    publish_fill(client)
    broker.on_bar(a_candle())
    broker.modify_stop("EURUSD", Decimal("1.16500"), NOON + HOUR)

    (_id, fields) = client.entries[ORDERS_STREAM][-1]
    assert fields["client_id"] != "zone-42"
    assert len(fields["client_id"]) <= MAX_CLIENT_ID


def test_moving_a_stop_with_no_position_is_false_and_reaches_nothing() -> None:
    """The protocol's own `False`: nothing to protect. Answered here rather than three processes
    away, because this ledger is the thing that knows."""
    client = FakeStreams()

    assert a_broker(client).modify_stop("EURUSD", Decimal("1.16500"), NOON) is False
    assert client.entries[ORDERS_STREAM] == []


def test_an_instruction_the_wire_refuses_is_false_not_a_crash() -> None:
    client = FakeStreams()
    broker = a_broker(client)
    broker.submit(an_order(client_id="zone-42"))
    client.refuse_xadd = True

    assert broker.cancel("zone-42") is False


def test_positions_answers_for_the_symbol_it_was_asked_about() -> None:
    client = FakeStreams()
    broker = a_broker(client)
    broker.submit(an_order())
    publish_fill(client)
    broker.on_bar(a_candle())

    assert len(broker.positions("EURUSD")) == 1
    assert broker.positions("GBPUSD") == (), "a EURUSD position answered for GBPUSD"


def test_a_spread_wider_than_the_price_is_refused() -> None:
    """Nonsense in, a stop rather than a `Fill` the ledger would price at nothing."""
    client = FakeStreams()
    broker = a_broker(client)
    broker.submit(an_order())
    publish_fill(client, price="0.00005", spread="0.00007")

    with pytest.raises(EngineError, match="not a quote"):
        broker.on_bar(a_candle())


# --------------------------------------------------------------------------- conformance


def test_the_broker_satisfies_the_engine_s_protocol() -> None:
    """⚠️ **Proved by assignment, because that is the only thing mypy checks.**

    `isinstance(broker, Broker)` on a `runtime_checkable` protocol looks like a proof and is
    not: it compares *names*, never signatures, so a `submit` taking the wrong argument or
    returning the wrong type passes it. This project has been bitten three ways by that — a
    narrow parameter, a narrow return, and positional order — and each time the assignment is
    what said so.
    """
    broker: Broker = MT5Broker(
        FakeStreams(),
        session_id=SESSION,
        instrument=EURUSD,
        initial_capital=Decimal("10000"),
    )
    assert broker.account().balance == Decimal("10000")


def test_the_real_redis_client_satisfies_the_wire_protocol() -> None:
    """No server needed: the question is whether the *signatures* match, and that is answered
    at type-check time. `dict` is invariant, so a protocol spelled in `str` where redis-py uses
    `KeyT` describes a client that does not exist — and every double written against it would
    type-check while the real thing did not."""
    wire: OrderWire = redis.Redis()
    assert wire is not None


# --------------------------------------------------------------------------- starting


def a_snapshot(*positions: HeldPosition, at: dt.datetime = NOW) -> str:
    return venue_state_text(VenueState(at=at, positions=positions))


def an_orphan(*, symbol: str = "EURUSD") -> HeldPosition:
    """A position left at the venue by a session that died. Fields as the terminal reports them,
    recorded 26/08."""
    return HeldPosition(
        ticket=47_096_513,
        symbol=symbol,
        side=Side.LONG,
        volume=Decimal("0.01"),
        price_open=Decimal("1.16524"),
        stop_loss=Decimal("1.16014"),
    )


def starting(client: FakeStreams) -> MT5Broker:
    return MT5Broker(
        client,
        session_id=SESSION,
        instrument=EURUSD,
        initial_capital=Decimal("10000"),
        now=lambda: NOW,
    )


def test_a_session_will_not_start_over_a_position_it_did_not_open() -> None:
    """⚠️ **The failure this whole path exists for, and it is silent without it.**

    Every session process writes a *new* `live_sessions` row, so a restarted session has a new
    id — and `_read` filters `fills.inbound` by that id. The dead session's fills are therefore
    discarded without a word, its position stays open at the venue, and this ledger starts empty
    and never learns. The next trade is then sized against an account that is already committed.
    """
    client = FakeStreams()
    client.state = a_snapshot(an_orphan())

    with pytest.raises(EngineError, match="already holding") as refusal:
        starting(client).start()

    message = str(refusal.value)
    assert "EURUSD long 0.01" in message, "the refusal does not say what is out there"
    assert "47096513" in message, "the refusal does not say which ticket to close"
    assert client.groups == {}, "a group was created for a session that never ran"


def test_a_flat_venue_starts_normally() -> None:
    """The separating half. Without it, "refuse when a position exists" and "refuse always" are
    the same test."""
    client = FakeStreams()
    client.state = a_snapshot()

    starting(client).start()

    assert client.groups, "the consumer group was never created"


def test_no_snapshot_at_all_is_a_refusal_not_an_empty_account() -> None:
    """⚠️ "I cannot tell what the venue holds" is not "the venue holds nothing", and only one of
    them authorises trading. The same argument the kill switch makes about a layer that cannot be
    read: refusing wrongly costs a session that does not start, allowing wrongly costs money."""
    client = FakeStreams()
    client.state = None

    with pytest.raises(EngineError, match="no venue snapshot"):
        starting(client).start()


def test_a_stale_snapshot_is_a_refusal_and_says_how_stale() -> None:
    """A snapshot nobody has refreshed says what the account looked like, not what it looks like.
    The executor publishes every 15s; a minute of silence means it is not running."""
    client = FakeStreams()
    client.state = a_snapshot(at=NOW - dt.timedelta(minutes=5))

    with pytest.raises(EngineError, match="nothing has looked at the account") as refusal:
        starting(client).start()

    assert (NOW - dt.timedelta(minutes=5)).isoformat() in str(refusal.value)


def test_a_snapshot_from_the_future_is_stale_too() -> None:
    """⚠️ Clock skew between the two processes is not something to average out — it is a reason
    to distrust the number entirely. A snapshot stamped ahead of now is not fresher than fresh."""
    client = FakeStreams()
    client.state = a_snapshot(at=NOW + dt.timedelta(minutes=5))

    with pytest.raises(EngineError, match="nothing has looked at the account"):
        starting(client).start()


def test_an_unreadable_snapshot_is_a_refusal() -> None:
    """Malformed is one more way of not knowing, and it lands in the same place as the others."""
    client = FakeStreams()
    client.state = "{not json at all"

    with pytest.raises(EngineError, match="unreadable"):
        starting(client).start()


def test_the_venue_is_looked_at_before_the_group_is_created() -> None:
    """Order matters: a broker that subscribed first and then refused would leave a consumer
    group behind for a session that never ran, and that group would collect every fill on the
    stream for ever with nobody acknowledging any of it."""
    client = FakeStreams()
    client.state = a_snapshot(an_orphan())

    with pytest.raises(EngineError):
        starting(client).start()

    assert client.groups == {}


def test_a_position_in_another_symbol_still_stops_the_session() -> None:
    """⚠️ The snapshot is everything under this project's magic number, not everything in the
    instrument this session trades. A position in GBPUSD is still this project's risk, still
    unaccounted for by this ledger, and still sized against the same account."""
    client = FakeStreams()
    client.state = a_snapshot(an_orphan(symbol="GBPUSD"))

    with pytest.raises(EngineError, match="already holding"):
        starting(client).start()
