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
from dataclasses import dataclass
from decimal import Decimal

from tradeforge_engine.domain import OrderRequest, Side, SignalKind

__all__ = [
    "FILLS_STREAM",
    "MAX_CLIENT_ID",
    "ORDERS_STREAM",
    "WireFill",
    "WireOrder",
    "fill_fields",
    "fill_from_fields",
    "order_fields",
    "order_from_fields",
    "stream_for",
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
