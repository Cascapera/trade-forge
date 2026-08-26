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

__all__ = ["ORDERS_STREAM", "WireOrder", "order_fields", "order_from_fields", "stream_for"]

ORDERS_STREAM = "orders.outbound"
"""One stream for every session, not one per session.

⚠️ Deliberate, and the opposite of `candles.{symbol}.{tf}`. A candle stream is *fan-out* — every
session must see every bar — while an order stream is a **work queue**: each order must be sent
exactly once, by exactly one executor. That is what a consumer group does naturally, and it is
the failure mode the candle stream had to be designed *away* from. Same primitive, opposite
requirement, and getting them backwards is either half a market each or an order sent twice.
"""

FILLS_STREAM = "fills.inbound"


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
