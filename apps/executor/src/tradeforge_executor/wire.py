"""The order as it crosses between processes, and back again.

The mirror of `tradeforge_collector.publisher.candle_fields`, and it lives here for the same
reason that one lives with the publisher: **one implementation of the encoding, imported by the
other side, never copied.** A second copy of "volume is decimal text and side is `long`/`short`"
would agree on the day it was written and disagree on the day one of them changed — and the
disagreement would arrive as an order sent at the wrong size, not as an error.

The direction is the opposite of the candle stream's, which is worth stating plainly: there the
collector produces and the API consumes; here the **API produces** (a session's `MT5Broker`) and
the executor consumes. So the format sits in the executor and the API imports it, which is safe
on Linux for the same reason depending on the collector is — nothing at this module's import
time touches MetaTrader 5.

⚠️ **Prices and volumes go out as decimal text, never as floats.** The engine prices in `Decimal`
precisely so that a tick is a tick, and a float round-trip at the edge would give that back for
nothing. The far side parses `Decimal(field)` and is exactly where it started.

⚠️ **`client_id` is the correlation key across three processes** (ADR-0014). It is the name the
strategy gave the order, it is what `order_audit` is indexed by, and it is what a fill carries
home. Nothing else spans the session, the executor and the venue.
"""

import datetime as dt
import json
from dataclasses import dataclass
from decimal import Decimal

from tradeforge_engine.domain import OrderRequest, Side, SignalKind

__all__ = [
    "FILLS_STREAM",
    "MAX_CLIENT_ID",
    "ORDERS_STREAM",
    "VENUE_STATE",
    "VENUE_STATE_FRESH_FOR",
    "HeldPosition",
    "Instruction",
    "VenueState",
    "WireCancel",
    "WireFill",
    "WireModifyStop",
    "WireOrder",
    "cancel_fields",
    "fill_fields",
    "fill_from_fields",
    "instruction_from_fields",
    "modify_stop_fields",
    "order_fields",
    "order_from_fields",
    "stream_for",
    "venue_state_from",
    "venue_state_text",
]

MAX_CLIENT_ID = 31
"""How much of a `client_id` the venue will keep, and therefore how long one may be.

MT5 stores the order comment in a fixed field and takes the first 31 characters. Everything past
that is dropped **without a word** — which is not a cosmetic loss: the comment is the only thing
that makes a position on somebody else's screen traceable back to the zone that armed it, and the
only part of this system that survives all three of its processes dying mid-flight.

⚠️ **The tail is where names differ.** Every name this system generates ends in the part that
distinguishes it — a counter, a bar's minute — so a truncation does not shorten a name, it
*merges* names. Two orders arriving at the venue with the same comment cannot be told apart by
anyone looking at the account, including the operator who is trying to work out which one to
close.

It lives here rather than in `gateway.py` because both ends need it: the venue's limit is what
makes a name valid, and the side that *chooses* names is the session, three processes away, which
must not learn how to talk to MetaTrader in order to find that out.
"""

ORDERS_STREAM = "orders.outbound"
"""One stream for every session, not one per session.

⚠️ Deliberate, and the opposite of `candles.{symbol}.{tf}`. A candle stream is *fan-out* — every
session must see every bar — while an order stream is a **work queue**: each order must be sent
exactly once, by exactly one executor. That is what a consumer group does naturally, and it is
the failure mode the candle stream had to be designed *away* from. Same primitive, opposite
requirement, and getting them backwards is either half a market each or an order sent twice.
"""

FILLS_STREAM = "fills.inbound"
"""What the venue did, on its way back to the session that asked.

⚠️ **Fan-out, not a work queue** — the opposite of `orders.outbound` one line above, and the two
sitting together is deliberate. An order must be handled by exactly one executor; a fill must be
seen by the session that placed it, and by anything else watching (a panel, later). So a session
reads this with **its own consumer group**, the way `CandleStream` does, and the executor writes
one entry per outcome.

Same file, same Redis, opposite requirements. Getting either backwards is silent: a shared group
here means a session never learns its order filled, and a per-session group over there means the
order goes out twice.
"""


def stream_for(_session_id: str) -> str:
    """The stream an order goes to. Constant, and the argument documents why it is not used.

    Kept as a function so the day a second executor is sharded by account, the shape of the
    change is visible here rather than in every call site.
    """
    return ORDERS_STREAM


VENUE_STATE = "venue.positions"
"""One Redis **key**, overwritten — not a stream, and the difference is the whole design.

A stream is a record of things that happened; this is an answer to "what is at the venue **right
now**". Nobody wants the history of that, and a consumer that fell behind on it would be reading
about a position that closed an hour ago and refusing to start over it.

⚠️ Written by the executor, which is the only process that may ask MetaTrader (AGENTS.md §5.4),
and read by a session's `MT5Broker`, which may not. That asymmetry is why this exists at all: the
broker keeps its own ledger, and a ledger has no way to notice a position it never opened.
"""

VENUE_STATE_FRESH_FOR = dt.timedelta(seconds=45)
"""How old a snapshot may be before it stops counting as an answer.

⚠️ **Absent and stale both mean "I do not know", and "I do not know" is not "there is nothing
there".** A session that started on a missing key would be starting blind against an account that
may well be holding a trade — the exact failure this key exists to prevent, arriving through the
key itself. Same argument the kill switch makes about a layer that cannot be read.

Three times the executor's publishing interval, so one missed beat is not an outage.
"""


@dataclass(frozen=True, slots=True)
class HeldPosition:
    """A position the venue holds under this project's magic number, as it reports it now.

    ⚠️ **Its own type rather than the engine's `Position`**, and it lives here rather than in
    `gateway.py` because it crosses a process boundary. The engine's `Position` is what a ledger
    believes; this is what the venue says. Giving them one type would quietly assume the answer to
    the question they exist to disagree about.
    """

    ticket: int
    symbol: str
    side: Side
    volume: Decimal
    price_open: Decimal
    stop_loss: Decimal | None
    """`None` when the position carries no stop at all. Not zero — MT5 reports an absent stop as
    `0.0`, and a stop *at* zero is not a level anybody set."""


@dataclass(frozen=True, slots=True)
class VenueState:
    """What the executor last saw the venue holding, and when it looked."""

    at: dt.datetime
    positions: tuple[HeldPosition, ...]

    def is_stale(self, *, now: dt.datetime) -> bool:
        """⚠️ A snapshot from the *future* is stale too. Clock skew between the two processes is
        not something to average out — it is a reason to distrust the number entirely."""
        return abs(now - self.at) > VENUE_STATE_FRESH_FOR


def venue_state_text(state: VenueState) -> str:
    """The snapshot as one JSON document, which is the only shape a Redis string has.

    JSON rather than the flat field maps the streams use, because this is a **list** and a flat
    map has no shape for one. Prices as decimal text for the reason every other price here is.
    """
    return json.dumps(
        {
            "at": state.at.isoformat(),
            "positions": [
                {
                    "ticket": position.ticket,
                    "symbol": position.symbol,
                    "side": position.side.value,
                    "volume": str(position.volume),
                    "price_open": str(position.price_open),
                    **(
                        {} if position.stop_loss is None else {"stop_loss": str(position.stop_loss)}
                    ),
                }
                for position in state.positions
            ],
        },
        separators=(",", ":"),
    )


def venue_state_from(raw: str) -> VenueState:
    """The inverse. Raises on anything malformed, which the reader treats as "I do not know".

    ⚠️ **`stop_loss` absent means no stop**, and it is absent rather than `null` for the same
    reason an optional price is left out of an order's field map: a value that never existed and
    a value of nothing must not share a spelling.
    """
    document = json.loads(raw)
    return VenueState(
        at=dt.datetime.fromisoformat(document["at"]),
        positions=tuple(
            HeldPosition(
                ticket=int(position["ticket"]),
                symbol=position["symbol"],
                side=Side(position["side"]),
                volume=Decimal(position["volume"]),
                price_open=Decimal(position["price_open"]),
                stop_loss=(Decimal(position["stop_loss"]) if "stop_loss" in position else None),
            )
            for position in document["positions"]
        ),
    )


KIND = "kind"
"""The one field every entry on `orders.outbound` carries, and the first one read.

⚠️ **One stream for all three instructions, not one stream each.** A second stream for cancels
would be tidier to read and wrong for a reason that only shows up under load: a session arms a
limit order and cancels it two bars later, and across two streams the executor is free to process
the cancel *before* the placement. The cancel finds nothing, answers "nothing to withdraw", and
the order it was meant to withdraw is placed a moment later and lives at the venue for ever.

One stream is one order of arrival. That is the whole argument — not elegance, not fewer keys.

⚠️ **Required, never defaulted.** An entry with no `kind` is not "probably an order": it is an
entry this format cannot read, and it goes down the dead-letter path with the malformed ones.
Same doctrine as `order_from_fields` refusing to default a missing `volume` to zero — a guess
that usually works is the kind that fails silently the once it does not.
"""

KIND_ORDER = "order"
KIND_CANCEL = "cancel"
KIND_MODIFY_STOP = "modify_stop"


@dataclass(frozen=True, slots=True)
class WireCancel:
    """Withdraw the order named `client_id`, if it is still waiting.

    Not an `OrderRequest` with a different intent, and `OrderRequest.__post_init__` refuses to be
    one — rightly: a cancel put in the queue the broker fills is in the queue it exists to empty.
    A separate type is what keeps that impossible rather than merely discouraged.
    """

    client_id: str
    session_id: str


@dataclass(frozen=True, slots=True)
class WireModifyStop:
    """Move the protective stop of the open position in `symbol` (ADR-0018).

    ⚠️ **Its own `client_id`, minted for the instruction** rather than borrowed from the entry
    that opened the position. `order_audit.client_id` is not nullable and the trail is indexed by
    it, and this *is* a separate instruction with its own outcome: it can be refused while the
    entry that opened the position succeeded, and reusing that entry's name would file two
    unrelated verdicts under one heading.

    `decided_at` travels because it is the anti-lookahead stamp — the opening instant of the bar
    whose close decided the new level. It is not the executor's to use, and it is not dropped
    either: `order_audit.request` is evidence, and the instant a level was decided is exactly
    what an incident asks about.
    """

    client_id: str
    session_id: str
    symbol: str
    stop_loss: Decimal
    decided_at: dt.datetime


type Instruction = WireOrder | WireCancel | WireModifyStop
"""What comes off `orders.outbound`. Three shapes, one stream, one order of arrival."""


@dataclass(frozen=True, slots=True)
class WireOrder:
    """An order as it arrived off the queue: the request, and who it belongs to.

    Not an `OrderRequest`. The engine's object has no idea which session produced it — it does
    not need one, and giving it one would put a database concept inside the core (AGENTS.md
    §5.4). The pairing happens here, at the transport, which is where both are known.
    """

    client_id: str
    session_id: str
    request: OrderRequest


def order_fields(order: OrderRequest, *, session_id: str, client_id: str) -> dict[str, str]:
    """The order as a flat map of strings, which is the only shape a stream entry has.

    ⚠️ **Absent, not empty.** An optional price that never existed is left out of the map
    entirely rather than written as `""`. Redis has no NULL, so the empty string would be the
    only thing distinguishing "no take profit" from "a take profit of nothing" — and `Decimal("")`
    raises where `None` reads.
    """
    fields = {
        KIND: KIND_ORDER,
        "client_id": client_id,
        "session_id": session_id,
        "symbol": order.symbol,
        "side": order.side.value,
        "intent": order.intent.value,
        "volume": str(order.volume),
        "decided_at": order.decided_at.isoformat(),
    }
    for name, value in (
        ("stop_loss", order.stop_loss),
        ("take_profit", order.take_profit),
        ("limit_price", order.limit_price),
    ):
        if value is not None:
            fields[name] = str(value)
    if order.reason:
        fields["reason"] = order.reason
    return fields


def cancel_fields(*, session_id: str, client_id: str) -> dict[str, str]:
    """Withdraw one order, by the name its strategy gave it (ADR-0014).

    Three fields, and that is the whole instruction. The *reason* an order should stop existing
    is never the executor's to know — a limit order here is alive because a zone is alive, and
    zones are the strategy's vocabulary. `Broker.cancel`'s docstring says the same from the
    other side.
    """
    return {KIND: KIND_CANCEL, "client_id": client_id, "session_id": session_id}


def modify_stop_fields(
    *, session_id: str, client_id: str, symbol: str, stop_loss: Decimal, decided_at: dt.datetime
) -> dict[str, str]:
    """Move the stop of the open position in `symbol`. See `WireModifyStop`."""
    return {
        KIND: KIND_MODIFY_STOP,
        "client_id": client_id,
        "session_id": session_id,
        "symbol": symbol,
        "stop_loss": str(stop_loss),
        "decided_at": decided_at.isoformat(),
    }


def instruction_from_fields(fields: dict[str, str]) -> Instruction:
    """One entry off `orders.outbound`, whichever of the three it is.

    ⚠️ **The `kind` is read first and never guessed.** A `fields.get(KIND, KIND_ORDER)` would turn
    an entry this format cannot read into an order — sent, filled, and recorded as fine. The
    `KeyError` and the `ValueError` here are both messages on a dead-letter path, which the
    service already knows how to walk; a guess is a position nobody asked for.
    """
    kind = fields[KIND]
    if kind == KIND_ORDER:
        return order_from_fields(fields)
    if kind == KIND_CANCEL:
        return WireCancel(client_id=fields["client_id"], session_id=fields["session_id"])
    if kind == KIND_MODIFY_STOP:
        return WireModifyStop(
            client_id=fields["client_id"],
            session_id=fields["session_id"],
            symbol=fields["symbol"],
            stop_loss=Decimal(fields["stop_loss"]),
            decided_at=dt.datetime.fromisoformat(fields["decided_at"]),
        )
    raise ValueError(f"unknown instruction kind {kind!r} on {ORDERS_STREAM}")


def order_from_fields(fields: dict[str, str]) -> WireOrder:
    """The inverse of `order_fields`. Raises rather than guessing at anything missing.

    ⚠️ **No defaults for the required fields.** A `fields.get("volume", "0")` would turn a
    malformed entry into an order for nothing, sent successfully, recorded as fine — and the
    session would spend the day believing it had a position. A `KeyError` here is a message on
    a dead-letter path; a zero-volume order is a silent lie.
    """
    return WireOrder(
        client_id=fields["client_id"],
        session_id=fields["session_id"],
        request=OrderRequest(
            symbol=fields["symbol"],
            side=Side(fields["side"]),
            intent=SignalKind(fields["intent"]),
            volume=Decimal(fields["volume"]),
            decided_at=dt.datetime.fromisoformat(fields["decided_at"]),
            stop_loss=_price(fields, "stop_loss"),
            take_profit=_price(fields, "take_profit"),
            limit_price=_price(fields, "limit_price"),
            reason=fields.get("reason", ""),
        ),
    )


def _price(fields: dict[str, str], name: str) -> Decimal | None:
    raw = fields.get(name)
    return Decimal(raw) if raw is not None else None


@dataclass(frozen=True, slots=True)
class WireFill:
    """What came back: which order, and what the venue actually did with it.

    ⚠️ **`client_id` is the whole correlation.** The session holds the `OrderRequest` it
    submitted, keyed by that id, and rebuilds the engine's `Fill` from the two halves — because
    a `Fill` needs the request, and putting the request back on the wire would be sending the
    same document twice and inviting the two copies to disagree.
    """

    client_id: str
    session_id: str
    symbol: str
    at: dt.datetime
    price: Decimal
    """**The venue's own price, untouched.** A buy fills at the ask, a sell at the bid, and this
    is whichever one happened. Converting it here would put a piece of engine accounting in the
    transport; the broker does that on arrival, with the instrument in hand."""

    volume: Decimal
    spread: Decimal
    """Ask minus bid at the instant of the deal, **in price units**, as the venue quoted it.

    ⚠️ **Measured, not configured, and it has to cross the wire because it cannot be recovered
    later.** `order_send` answers with the price that traded and nothing else; the quote either
    side of it is gone a second afterwards. Without this the session cannot tell an ask from a
    bid, and MT5's own bars are bid-based — measured on EURUSD M15, an ask lands outside the
    bar's high in 7.5% of bars, which the engine's lookahead guard correctly refuses. See
    `SpreadCostModel`: crossing the spread is a **cost**, never a worse price.

    In price units rather than points on purpose: points need the venue's `point`, which is not
    always the instrument's `tick_size`, and a conversion that is usually right is the worst
    kind. A price delta needs nothing to be understood.

    ⚠️ Zero is a real answer here, not a missing one — this venue quotes bid == ask at quiet
    hours, measured. It is charged as free execution because that is what it was.
    """

    ticket: int | None


def fill_fields(fill: WireFill) -> dict[str, str]:
    """The fill as a flat map of strings. Prices as decimal text, for the reason above.

    ⚠️ `volume` is what was **actually** filled, which is not always what was asked for. A
    partial fill that travelled home as the requested size would have the session believing it
    holds a position twice the one it has.
    """
    fields = {
        "client_id": fill.client_id,
        "session_id": fill.session_id,
        "symbol": fill.symbol,
        "at": fill.at.isoformat(),
        "price": str(fill.price),
        "volume": str(fill.volume),
        "spread": str(fill.spread),
    }
    if fill.ticket is not None:
        fields["ticket"] = str(fill.ticket)
    return fields


def fill_from_fields(fields: dict[str, str]) -> WireFill:
    """The inverse. Raises on anything missing, for the same reason `order_from_fields` does.

    ⚠️ **`spread` is required, and a missing one raises rather than defaulting to zero.** Zero
    is a legitimate quote at a quiet hour, so it cannot double as "the field went missing" — the
    two would then be the same word for "charge nothing", and only one of them means it. The
    same argument `BarSpreadCostModel` makes about a bar that cannot say what it cost.
    """
    ticket = fields.get("ticket")
    return WireFill(
        client_id=fields["client_id"],
        session_id=fields["session_id"],
        symbol=fields["symbol"],
        at=dt.datetime.fromisoformat(fields["at"]),
        price=Decimal(fields["price"]),
        volume=Decimal(fields["volume"]),
        spread=Decimal(fields["spread"]),
        ticket=int(ticket) if ticket is not None else None,
    )
