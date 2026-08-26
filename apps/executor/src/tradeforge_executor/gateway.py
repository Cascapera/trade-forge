"""The one place in this project that sends an order to a venue.

Everything else decides; this does. That asymmetry is why it is a `Protocol` with a small
implementation behind it rather than calls scattered through a loop: **the code that can lose
money should be small enough to read in one sitting**, and the code that decides whether to call
it should be runnable on Linux, in CI, with no terminal in sight.

⚠️ `MetaTrader5` is imported inside `connect()`, never at module scope. Same discipline as
`tradeforge_collector.mt5_source`, and the reason is the same: `apps/api` depends on this package
for the wire format, and importing it must not drag a Windows-only wheel onto a Linux box.
"""

import datetime as dt
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, Self

from tradeforge_engine.domain import OrderRequest, Side, SignalKind

logger = logging.getLogger(__name__)

__all__ = ["MAGIC", "MT5Gateway", "OrderGateway", "Placement"]

# Stamped on every order this project sends, and the only thing that makes an account's positions
# answerable.
#
# ⚠️ **Without it, `positions()` is a lie.** A live MT5 account reports everything it holds: a
# manual trade, another expert advisor's, a different instrument entirely. The `Broker` protocol
# already says so — "an interface that returns the positions and lets the caller take the first
# is an interface that will one day close five lots of gold because a strategy trading EURUSD
# asked to exit". The magic number is how this executor recognises its own work.
MAGIC = 770_302


@dataclass(frozen=True, slots=True)
class Placement:
    """What the venue said. Not what happened next — a fill arrives on its own.

    `raw` is the terminal's answer verbatim, and it goes into `order_audit.response` unchanged.
    A projection records the fields somebody thought of; an incident asks about a field nobody
    thought of.

    ⚠️ **Accepted is not filled, and the terminal says so in the same breath as both.** Measured
    against a live terminal: a buy limit resting 200 points away comes back `retcode=10009`
    (*DONE*, not even *PLACED*), `volume='0.01'` — the volume that was *asked for*, echoed — and
    `deal=0`. The account held one pending order, zero positions and **zero deals**. Reading
    `volume` as "what filled" therefore reports a fill for an order still sitting in the book:
    the strategy's own "decide on the breakout, fill on the breakout" fantasy, arriving through
    the venue instead of through the broker, where no engine guard is watching for it.

    **`deal` is the only field that distinguishes the two**, so it is the field the others are
    derived from. See `__post_init__`.
    """

    accepted: bool
    ticket: int | None
    filled_volume: Decimal
    price: Decimal | None
    retcode: int
    comment: str
    raw: dict[str, Any]
    deal: int | None = None
    """The venue's deal ticket, or `None` when nothing executed.

    **A fill happened if and only if this is set.**

    Not the same as `ticket`, and the difference is the whole point: `ticket` is the *order*, which
    exists as soon as the venue accepts it, filled or resting. A deal is the execution.
    """

    def __post_init__(self) -> None:
        # The constructor refuses the exact lie the terminal tells, so that a hand-built
        # `Placement` in a test cannot tell it either. `_placement` derives both fields from the
        # same answer and never builds an inconsistent one, so this can only ever fire on a fake
        # — which is precisely where a divergence from the real terminal goes unnoticed.
        if self.filled_volume > 0 and self.deal is None:
            raise ValueError(
                f"a filled volume of {self.filled_volume} with no deal ticket: an order that "
                f"executed has a deal, and one that is merely resting has a volume the venue "
                f"echoed back from the request"
            )

    @property
    def resting(self) -> bool:
        """Accepted by the venue and waiting for the market. Not a fill, and not a refusal."""
        return self.accepted and self.deal is None

    @property
    def partial(self) -> bool:
        """⚠️ Its own question, and the caller must ask it. A partial fill leaves the rest of the
        order somewhere; reading it as filled is how a position ends up half the size a strategy
        believes it has."""
        return self.accepted and self.filled_volume > 0

    def is_short_of(self, asked: Decimal) -> bool:
        return self.accepted and 0 < self.filled_volume < asked


class OrderGateway(Protocol):
    """Send one order, and say what the venue answered. Nothing else.

    Narrow on purpose: the executor needs to place an order and read the account, and a protocol
    wider than that describes a client this code does not have. The lesson `LedgerView` recorded
    two PRs ago — a `Protocol` is a description of what the *caller* needs.
    """

    def send(self, order: OrderRequest, *, client_id: str) -> Placement: ...

    def balance(self) -> Decimal: ...

    def open_positions(self) -> int:
        """How many positions **this executor** holds, by its own magic number."""
        ...

    def realised_since(self, moment: dt.datetime) -> Decimal:
        """Closed P&L since `moment`, this executor's only. The daily loss cap's numerator."""
        ...


class MT5Gateway:
    """`OrderGateway` backed by a running MetaTrader 5 terminal.

    ⚠️ **Nothing in this class is exercised by the unit suite**, and that is not an oversight to
    fix with a mock. A mock of `order_send` proves that this file calls a function this file also
    describes — the `fake que diverge` failure this project has already paid for once. What
    protects it instead is being small, being the only such file, and having every *decision*
    that leads to it tested elsewhere.
    """

    def __init__(self, *, terminal: Any = None, deviation: int = 20) -> None:  # noqa: ANN401
        self._terminal = terminal
        self._mt5: Any = None
        self._deviation = deviation
        """Slippage the venue may take, in points. Not the engine's `slippage_ticks`: that one
        models a backtest's assumption, this one is a real instruction to a real broker."""

    def __enter__(self) -> Self:
        return self.connect()

    def __exit__(self, *_: object) -> None:
        self.close()

    def connect(self) -> Self:
        if self._terminal is not None:
            self._mt5 = self._terminal
            return self
        # `mt5` is the library's own conventional alias, used across this repo.
        import MetaTrader5 as mt5  # noqa: PLC0415, N813 — the library's own alias; Windows-only

        if not mt5.initialize():
            raise ConnectionError(f"could not attach to MetaTrader 5: {mt5.last_error()}")
        self._mt5 = mt5
        return self

    def close(self) -> None:
        if self._mt5 is not None and self._terminal is None:
            self._mt5.shutdown()
        self._mt5 = None

    def send(self, order: OrderRequest, *, client_id: str) -> Placement:
        """Place one order. **The only call in this project that reaches a venue.**

        ⚠️ `client_id` goes in the comment, not only in our own records. It is what makes a
        position on somebody else's screen traceable back to the zone that armed it — and the
        only thing that survives this process dying mid-flight.
        """
        mt5 = self._require()
        request = self._request(mt5, order, client_id=client_id)
        logger.info(
            "sending %s %s %s (%s)", order.side.value, order.volume, order.symbol, client_id
        )
        answer = mt5.order_send(request)
        return self._placement(mt5, answer)

    def balance(self) -> Decimal:
        info = self._require().account_info()
        if info is None:
            raise ConnectionError("the terminal did not answer with an account")
        return Decimal(str(info.balance))

    def open_positions(self) -> int:
        held = self._require().positions_get() or ()
        return sum(1 for position in held if getattr(position, "magic", None) == MAGIC)

    def realised_since(self, moment: dt.datetime) -> Decimal:
        """⚠️ Filtered by magic **and** by time. An account's deal history holds another advisor's
        trades and everything from before today; counting those would make the daily loss cap
        fire on somebody else's bad morning."""
        mt5 = self._require()
        deals = mt5.history_deals_get(moment, dt.datetime.now(dt.UTC)) or ()
        return sum(
            (Decimal(str(deal.profit)) for deal in deals if getattr(deal, "magic", None) == MAGIC),
            Decimal(0),
        )

    def _require(self) -> Any:  # noqa: ANN401 — MetaTrader5 ships no type stubs
        if self._mt5 is None:
            raise ConnectionError("the gateway is not connected; call connect() first")
        return self._mt5

    def _request(self, mt5: Any, order: OrderRequest, *, client_id: str) -> dict[str, Any]:  # noqa: ANN401
        resting = order.limit_price is not None
        request: dict[str, Any] = {
            "action": mt5.TRADE_ACTION_PENDING if resting else mt5.TRADE_ACTION_DEAL,
            "symbol": order.symbol,
            "volume": float(order.volume),
            "type": self._order_type(mt5, order),
            "magic": MAGIC,
            "comment": client_id[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if resting:
            request["price"] = float(order.limit_price)  # type: ignore[arg-type]
        else:
            request["deviation"] = self._deviation
        if order.stop_loss is not None:
            request["sl"] = float(order.stop_loss)
        if order.take_profit is not None:
            request["tp"] = float(order.take_profit)
        return request

    def _order_type(self, mt5: Any, order: OrderRequest) -> int:  # noqa: ANN401
        long = order.side is Side.LONG
        if order.limit_price is not None:
            return int(mt5.ORDER_TYPE_BUY_LIMIT if long else mt5.ORDER_TYPE_SELL_LIMIT)
        if order.intent is SignalKind.EXIT:
            # Closing is the opposite side of holding.
            return int(mt5.ORDER_TYPE_SELL if long else mt5.ORDER_TYPE_BUY)
        return int(mt5.ORDER_TYPE_BUY if long else mt5.ORDER_TYPE_SELL)

    def _placement(self, mt5: Any, answer: Any) -> Placement:  # noqa: ANN401
        if answer is None:
            code, message = mt5.last_error()
            return Placement(
                accepted=False,
                ticket=None,
                filled_volume=Decimal(0),
                price=None,
                retcode=int(code),
                comment=str(message),
                raw={"error": [code, message]},
            )
        raw = answer._asdict() if hasattr(answer, "_asdict") else dict(answer)
        accepted = int(answer.retcode) in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED)
        # ⚠️ **Both of these are read through `deal`, not from the fields that name them.** The
        # terminal echoes the requested `volume` and a `price` of `0.0` on a placement that has
        # not executed, and a reader taking those at face value manufactures a fill. `raw` keeps
        # the answer verbatim either way, so nothing is lost to the audit trail by not trusting
        # it here.
        deal = int(getattr(answer, "deal", 0) or 0) or None
        # ⚠️ **One gate, not one per field.** Written as two conditions — `... if executed else`
        # on the volume and `... if executed and price` on the price — the second `executed` is
        # dead logic: the terminal answers a resting order with `price=0.0`, which the falsiness
        # test already catches, so a mutant deleting it survives every test that can be written
        # against a real recording. A guard nothing can observe is a guard that is not there.
        volume, price = Decimal(0), None
        if accepted and deal is not None:
            volume = Decimal(str(getattr(answer, "volume", 0) or 0))
            price = Decimal(str(answer.price)) if getattr(answer, "price", None) else None
        return Placement(
            accepted=accepted,
            ticket=int(getattr(answer, "order", 0)) or None,
            filled_volume=volume,
            price=price,
            retcode=int(answer.retcode),
            comment=str(getattr(answer, "comment", "")),
            raw=_jsonable(raw),
            deal=deal,
        )


def _jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    """MT5 answers with numpy scalars and named tuples; JSONB takes neither.

    ⚠️ Stringified rather than coerced to `float`. This document is evidence, and a price that
    passed through a float on the way into the audit trail is evidence about a different number.
    """
    return {
        key: value if isinstance(value, int | str) else str(value) for key, value in payload.items()
    }
