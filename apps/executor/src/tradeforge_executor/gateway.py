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
from tradeforge_executor.wire import MAX_CLIENT_ID, HeldPosition

logger = logging.getLogger(__name__)

__all__ = [
    "MAGIC",
    "MT5Gateway",
    "OrderGateway",
    "Placement",
    "VenueDeal",
    "deal_from",
    "spread_from",
]

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

    spread: Decimal | None = None
    """Ask minus bid at the instant of the send, in price units, or `None` if nothing executed.

    ⚠️ **The one number the venue will not tell you afterwards.** `order_send` answers with the
    price that traded; the quote either side of it is gone. The session needs it because MT5's
    bars are bid-based and a buy fills at the ask — see `WireFill.spread`.

    Zero is a real quote at a quiet hour, so it does not double as "missing": missing is `None`,
    and a fill that executed always carries a number, because `send` reads the quote before the
    order goes out and refuses the send if the terminal cannot answer."""

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
        # Same argument one field over: an execution nobody can price is an execution the
        # session would have to record at a made-up cost, and zero is the made-up cost that
        # looks most like a measurement.
        if self.filled_volume > 0 and self.spread is None:
            raise ValueError(
                f"deal {self.deal} filled {self.filled_volume} with no quote: a fill that "
                f"cannot say what crossing cost is a fill priced against a bid-based bar as "
                f"though it were free"
            )
        if self.spread is not None and self.spread < 0:
            raise ValueError(f"a spread is a magnitude, got {self.spread}")

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


@dataclass(frozen=True, slots=True)
class VenueDeal:
    """An execution the venue has already recorded, read back from its history.

    The sibling of `Placement` for the half a send cannot see. `Placement` is what the terminal
    answered *while the order was being placed*; this is what the terminal remembers afterwards,
    which is the only way a limit that filled an hour later can be noticed at all.

    ⚠️ **Not a `WireFill`, and the distance matters.** This is what MT5 reports; a `WireFill` is
    what the session is told. Between them sits the one thing history does not carry — the quote
    at the moment of the deal — and refusing to collapse the two types is what keeps that gap
    from being filled in silently. See `MT5Gateway.spread_at`.

    ⚠️ It does not live in `wire.py` because it never crosses a process boundary: the executor
    reads it, prices it, and publishes something else. `HeldPosition` is in `wire.py` precisely
    because it does cross one.
    """

    ticket: int
    """The **deal** ticket, not the order's. Unique per execution, and what the watermark counts."""

    order_ticket: int
    client_id: str
    """The name the strategy gave the order, read back out of the deal's comment.

    ⚠️ The comment is the only field that survives from our side into the venue's own history,
    which is why `send` writes it there and why `MAX_CLIENT_ID` exists. A deal whose comment is
    empty or unrecognisable is not ours to publish — see `deals_since`.
    """

    symbol: str
    at: dt.datetime
    price: Decimal
    volume: Decimal
    entry: int
    """MT5's `DEAL_ENTRY_*`: whether this execution opened, closed or reversed a position.

    Carried rather than interpreted here. The gateway's job is to report what the venue says;
    deciding what an entry code means for a session's ledger is a decision, and decisions do not
    belong in the file that talks to the terminal."""


def deal_from(raw: Any, *, server_offset: dt.timedelta) -> VenueDeal | None:  # noqa: ANN401
    """One record of MT5's deal history as a `VenueDeal`, or `None` if it is not ours to publish.

    ⚠️ **A pure function, and that is the point.** `MT5Gateway` has no unit tests on purpose — a
    mock of `history_deals_get` proves only that this file calls a function this file describes —
    and the protection its docstring names is *"having every decision that leads to it tested
    elsewhere"*. Reading a comment as a name and correcting a clock are decisions. Measured: two
    mutants that broke them both survived the whole suite while they lived inside the method.

    ⚠️ **The clock correction is the one that fails silently.** MT5 hands out **server time
    labelled as UTC** — measured 2026-08-31, `symbol_info_tick().time` read as UTC said `20:49`
    when UTC was `17:49`. Left uncorrected, every deal is stamped three hours into the future, the
    quote lookup searches three hours from the trade, finds nothing, and the executor reports a
    thin market. See `ExecutorSettings.server_offset`.

    ⚠️ **A deal with no readable name is `None`, never a guess.** The comment is the only field of
    ours that survives into the venue's own history. Correlating an unnamed deal to a zone by
    proximity would tell a session that a region it never armed had traded.
    """
    client_id = str(getattr(raw, "comment", "") or "").strip()
    if not client_id:
        logger.warning(
            "deal %s under magic %s carries no name; it cannot be correlated to a zone",
            getattr(raw, "ticket", "?"),
            MAGIC,
        )
        return None
    return VenueDeal(
        ticket=int(raw.ticket),
        order_ticket=int(raw.order),
        client_id=client_id,
        symbol=str(raw.symbol),
        # `time_msc` rather than `time`: two deals inside one second are two deals, and a
        # watermark counted in whole seconds would skip the second of them.
        at=dt.datetime.fromtimestamp(int(raw.time_msc) / 1000, dt.UTC) - server_offset,
        price=Decimal(str(raw.price)),
        volume=Decimal(str(raw.volume)),
        entry=int(raw.entry),
    )


def spread_from(ticks: Any, at_ms: float) -> Decimal | None:  # noqa: ANN401
    """The crossing cost at `at_ms`, from the ticks around it — or `None` if none prices it.

    ⚠️ **Pure, and out here for the reason `deal_from` is.** `MT5Gateway` has no unit tests by
    policy; what protects it is *"having every decision that leads to it tested elsewhere"*. Two
    decisions live in this function, and both were measured surviving the whole suite while they
    sat inside the method: which tick counts as *the* tick, and whether a tick prices a crossing
    at all.

    ⚠️ **Nearest by time, not first in the window.** Ticks either side of a deal are both
    candidates and the one before it is often the better answer. A mutant taking the *furthest*
    tick in the window is the plausible neighbour here — it returns a real quote from a real tick,
    just the wrong one, and the further it is the more wrong the cost.

    ⚠️ **A tick with no book does not price a crossing, and zero is not the answer.** MT5
    publishes trade-only ticks with no quote attached. `ask == 0` makes `ask - bid` negative,
    which `Placement` refuses one layer down as *"a spread is a magnitude"* — but only after this
    had already called it an answer. And `bid == ask == 0` subtracts to a clean **zero**, which
    nothing downstream refuses at all: free execution, published as a measurement. Both are the
    absence of a quote, so both are `None`.
    """
    if ticks is None or len(ticks) == 0:
        return None
    nearest = min(ticks, key=lambda tick: abs(int(tick["time_msc"]) - at_ms))
    if nearest["ask"] <= 0 or nearest["bid"] <= 0:
        return None
    # Through `str`, never `Decimal(float)`: the venue quotes 1.16667 and the binary double
    # nearest it is not that number. Same reason `send` does it.
    spread = Decimal(str(nearest["ask"])) - Decimal(str(nearest["bid"]))
    return spread if spread >= 0 else None


class OrderGateway(Protocol):
    """Send one order, and say what the venue answered. Nothing else.

    Narrow on purpose: the executor needs to place an order and read the account, and a protocol
    wider than that describes a client this code does not have. The lesson `LedgerView` recorded
    two PRs ago — a `Protocol` is a description of what the *caller* needs.
    """

    def send(self, order: OrderRequest, *, client_id: str) -> Placement: ...

    def withdraw(self, client_id: str) -> Placement:
        """Remove the resting order named `client_id`, if the venue still has it.

        ⚠️ **Nothing found is not an error.** The order may have filled while the cancel was in
        flight, been cancelled already, or never have reached the venue at all. In live that is a
        race, not a fault — `Broker.cancel` says the same from the other end — so it comes back
        as an accepted instruction that withdrew nothing, and the trail records that it withdrew
        nothing. A raised exception would turn a normal execution into a dead session.
        """
        ...

    def held(self, symbol: str) -> HeldPosition | None:
        """The position **this executor** holds in `symbol`, by its own magic number.

        ⚠️ The filter is not decoration. A live account reports a manual trade, another expert
        advisor's position, an entirely different instrument — and the one instruction that acts
        on a position by ticket is the one below.
        """
        ...

    def holdings(self) -> tuple[HeldPosition, ...]:
        """Everything this executor holds, across every symbol. For the venue snapshot."""
        ...

    def tighten(self, ticket: int, stop_loss: Decimal) -> Placement:
        """Move the protective stop of position `ticket`.

        ⚠️ **Named for what the caller is allowed to ask for, not for what the venue will do.**
        Measured on 26/08: MT5 accepts a stop moved *further* from price — `retcode=10009`, and
        the position comes back loosened. The venue has no opinion on the direction, so the only
        thing enforcing "a stop may only tighten" (ADR-0018) is the code that calls this. The name
        is a reminder that the check has already happened by the time this runs.
        """
        ...

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

    def __init__(
        self,
        *,
        terminal: Any = None,  # noqa: ANN401 — MetaTrader5 ships no type stubs
        deviation: int = 20,
        server_offset: dt.timedelta = dt.timedelta(0),
    ) -> None:
        self._terminal = terminal
        self._mt5: Any = None
        self._server_offset = server_offset
        """How far the terminal's clock is ahead of UTC. Applied at this boundary and nowhere
        else, so everything above this file speaks UTC — see `ExecutorSettings.server_offset`."""

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

        ⚠️ **The quote is read before the order goes, and an unreadable one refuses the send.**
        The spread cannot be recovered afterwards — `order_send` answers with the price that
        traded and nothing else, and the quote either side of it is gone a second later — but
        the session needs it, because MT5's bars are bid-based and a buy fills at the ask. Read
        it *after* the deal instead and there is a failure with no honest answer: a fill that
        happened, that nobody can price. Reading it first turns that into a refusal, which the
        router already knows how to record, and a terminal that cannot quote a symbol is exactly
        a terminal that should not be trading it.
        """
        mt5 = self._require()
        quote = mt5.symbol_info_tick(order.symbol)
        if quote is None:
            raise ConnectionError(f"the terminal did not quote {order.symbol}")
        # Through `str`, never `Decimal(float)`: the venue quotes 1.16667 and the binary double
        # nearest it is not that number. The engine prices in `Decimal` for exactly this reason.
        spread = Decimal(str(quote.ask)) - Decimal(str(quote.bid))
        request = self._request(mt5, order, client_id=client_id)
        logger.info(
            "sending %s %s %s (%s)", order.side.value, order.volume, order.symbol, client_id
        )
        answer = mt5.order_send(request)
        return self._placement(mt5, answer, spread=spread)

    def withdraw(self, client_id: str) -> Placement:
        """Find the resting order by the name the strategy gave it, and remove it.

        ⚠️ **Found by the comment, at the venue, rather than by a ticket from `order_audit`.**
        The venue is the authority on what it is still holding: an order the trail says exists
        may have filled a second ago, and acting on the trail's ticket would be acting on a
        memory. Asking the terminal makes "already filled", "already cancelled" and "never got
        there" the same answer — nothing found — which is exactly the answer `Broker.cancel`
        needs.

        Safe to match on the comment only because a name is now guaranteed to survive it whole:
        `MT5Broker.submit` refuses a `client_id` longer than `MAX_CLIENT_ID`. Before that, two
        names could reach the account truncated to the same 31 characters, and this method would
        have cancelled whichever it found first.
        """
        mt5 = self._require()
        resting = mt5.orders_get() or ()
        mine = [
            order
            for order in resting
            if getattr(order, "magic", None) == MAGIC
            and str(getattr(order, "comment", "")) == client_id[:MAX_CLIENT_ID]
        ]
        if not mine:
            logger.info("nothing to withdraw for %s; it filled, or was never placed", client_id)
            return Placement(
                accepted=True,
                ticket=None,
                filled_volume=Decimal(0),
                price=None,
                retcode=0,
                comment="nothing was waiting under that name",
                raw={"withdrew": 0, "client_id": client_id},
            )
        answers = [
            mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": int(order.ticket)})
            for order in mine
        ]
        return self._withdrawal(mt5, client_id, mine, answers)

    def _withdrawal(
        self,
        mt5: Any,  # noqa: ANN401
        client_id: str,
        removed: list[Any],
        answers: list[Any],
    ) -> Placement:
        """⚠️ **Every answer has to be `DONE`, not just the first.** One name should match one
        order, but "should" is a fact about the strategy, not about the account — and a partial
        withdrawal reported as a success leaves an order live at the venue that the session has
        already written off."""
        codes = [int(getattr(answer, "retcode", 0) or 0) for answer in answers]
        done = all(code == mt5.TRADE_RETCODE_DONE for code in codes)
        raw = {
            "withdrew": len(removed) if done else 0,
            "client_id": client_id,
            "tickets": [int(order.ticket) for order in removed],
            "retcodes": codes,
            "comments": [str(getattr(answer, "comment", "")) for answer in answers],
        }
        return Placement(
            accepted=done,
            ticket=int(removed[0].ticket),
            filled_volume=Decimal(0),
            price=None,
            retcode=codes[0] if codes else 0,
            comment=str(getattr(answers[0], "comment", "")) if answers else "",
            raw=_jsonable(raw),
        )

    def held(self, symbol: str) -> HeldPosition | None:
        """The one position this executor holds in `symbol`, or `None`.

        ⚠️ **More than one is an error, not a choice.** Phase 1 holds a single position at a time
        by construction, so two under this magic number means something has gone wrong upstream —
        and picking the first would move the stop of whichever the terminal happened to list
        first, silently, on the one instruction whose whole job is to reduce risk.
        """
        held = [
            position
            for position in (self._require().positions_get(symbol=symbol) or ())
            if getattr(position, "magic", None) == MAGIC
        ]
        if not held:
            return None
        if len(held) > 1:
            raise ConnectionError(
                f"{len(held)} positions in {symbol} under magic {MAGIC}; this executor holds one "
                f"at a time and will not guess which stop to move"
            )
        return _held(held[0])

    def holdings(self) -> tuple[HeldPosition, ...]:
        """**Everything** this executor holds, across every symbol, for the snapshot.

        The plural sibling of `held()`, and deliberately not built out of it: `held()` answers
        "which position do I act on in this symbol" and refuses to guess when there are two,
        because acting on the wrong one moves a stop that is not ours to move. This one answers
        "what is out there", and two is a thing it must be able to *report* rather than refuse —
        a session that must not start because the venue is holding something needs to be told
        what, not handed an exception about how many.
        """
        return tuple(
            _held(position)
            for position in (self._require().positions_get() or ())
            if getattr(position, "magic", None) == MAGIC
        )

    def tighten(self, ticket: int, stop_loss: Decimal) -> Placement:
        """Send the stop move. The direction was checked by the caller — see the protocol.

        ⚠️ The request carries **no magic and no symbol**; measured on 26/08, the terminal echoes
        `magic=0, symbol=''` back. The position ticket is the entire identity, which is why
        `held()` above is the thing that establishes the position is ours, and why this method
        takes a ticket that came from it rather than a symbol it would look up again.
        """
        mt5 = self._require()
        answer = mt5.order_send(
            {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": ticket,
                "sl": float(stop_loss),
            }
        )
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

    def deals_since(self, moment: dt.datetime) -> tuple[VenueDeal, ...]:
        """Every execution under this project's magic since `moment`, oldest first.

        ⚠️ **The half `send` cannot see.** A limit comes back `retcode=10009` with `deal=0` —
        accepted and resting — and the execution happens minutes or hours later, when the entry
        that placed it has long been acknowledged and nobody in that loop is watching. Read from
        history is the only way it is ever noticed.

        ⚠️ **Filtered by magic *and* by a name we recognise**, and both halves are load-bearing.
        The magic keeps another advisor's trades and the account's manual ones out — the same
        argument `realised_since` makes. The comment is what says *which* order this was: it is
        the only field of ours that survives into the venue's own history, which is why `send`
        writes `client_id` there. A deal of ours with a comment we cannot read is reported and
        skipped, never guessed at: correlating it to the wrong zone is worse than not
        correlating it, because a session would then believe a different region had traded.

        Oldest first because the caller advances a watermark through them, and a watermark that
        moves through unsorted history skips whatever arrives behind it.
        """
        mt5 = self._require()
        # ⚠️ Both bounds go **in server time**: `history_deals_get` interprets its arguments in
        # the terminal's clock, so a UTC window would ask about three hours ago and answer
        # confidently about the wrong interval.
        since = moment + self._server_offset
        until = dt.datetime.now(dt.UTC) + self._server_offset
        found = mt5.history_deals_get(since, until) or ()
        ours = (deal for deal in found if getattr(deal, "magic", None) == MAGIC)
        deals = [
            shaped
            for shaped in (deal_from(deal, server_offset=self._server_offset) for deal in ours)
            if shaped is not None
        ]
        deals.sort(key=lambda deal: (deal.at, deal.ticket))
        return tuple(deals)

    def spread_at(self, symbol: str, at: dt.datetime, *, within: dt.timedelta) -> Decimal | None:
        """Ask minus bid at the tick nearest `at`, or `None` if none is near enough.

        ⚠️ **`None` is a refusal to guess, and the caller must treat it as one.** The quote at a
        deal is the one number the venue does not keep — `send` reads it before the order goes
        precisely because it cannot be recovered from the answer. It *can* be recovered from the
        tick stream, and that is what this does; what it must not do is answer with whatever tick
        is closest regardless of how far. Measured on a real deal from a thin evening, the
        nearest tick was **3 438 ms** away, and a spread from 3.4 seconds later charged as the
        deal's own is a made-up cost wearing a measurement's clothes.

        So the window is the caller's to declare (`ExecutorSettings.deal_quote_window`) and this
        answers within it or not at all.

        ⚠️ Nearest by time, not first in the window: ticks either side of a deal are both
        candidates, and the one before it is often the better answer.
        """
        mt5 = self._require()
        # In server time, like every other instant this API takes — see `deals_since`.
        at_server = at + self._server_offset
        ticks = mt5.copy_ticks_range(
            symbol, at_server - within, at_server + within, mt5.COPY_TICKS_ALL
        )
        return spread_from(ticks, at_server.timestamp() * 1000)

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
            # ⚠️ Still sliced, and still logged when the slice bites. `MT5Broker.submit` refuses
            # an over-long name before it ever reaches here, so this cannot normally fire — but
            # this is the last place before the venue, and the failure it guards is a *silent*
            # merge of two names into one. A guard that costs a comparison and turns a silent
            # loss into a line in the log is worth keeping even where it should be unreachable.
            "comment": self._comment(client_id),
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

    def _comment(self, client_id: str) -> str:
        """As much of the name as the venue will keep, and a complaint if that is not all of it."""
        if len(client_id) > MAX_CLIENT_ID:
            logger.error(
                "client_id %r is %d characters; the venue keeps %d, so it will reach the "
                "account as %r and be indistinguishable from every other name sharing that "
                "prefix",
                client_id,
                len(client_id),
                MAX_CLIENT_ID,
                client_id[:MAX_CLIENT_ID],
            )
        return client_id[:MAX_CLIENT_ID]

    def _order_type(self, mt5: Any, order: OrderRequest) -> int:  # noqa: ANN401
        long = order.side is Side.LONG
        if order.limit_price is not None:
            return int(mt5.ORDER_TYPE_BUY_LIMIT if long else mt5.ORDER_TYPE_SELL_LIMIT)
        if order.intent is SignalKind.EXIT:
            # Closing is the opposite side of holding.
            return int(mt5.ORDER_TYPE_SELL if long else mt5.ORDER_TYPE_BUY)
        return int(mt5.ORDER_TYPE_BUY if long else mt5.ORDER_TYPE_SELL)

    def _placement(
        self,
        mt5: Any,  # noqa: ANN401
        answer: Any,  # noqa: ANN401
        *,
        spread: Decimal | None = None,
    ) -> Placement:
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
        volume, price, crossed = Decimal(0), None, None
        if accepted and deal is not None:
            volume = Decimal(str(getattr(answer, "volume", 0) or 0))
            price = Decimal(str(answer.price)) if getattr(answer, "price", None) else None
            crossed = spread
        return Placement(
            accepted=accepted,
            ticket=int(getattr(answer, "order", 0)) or None,
            filled_volume=volume,
            price=price,
            retcode=int(answer.retcode),
            comment=str(getattr(answer, "comment", "")),
            raw=_jsonable(raw),
            deal=deal,
            spread=crossed,
        )


def _held(position: Any) -> HeldPosition:  # noqa: ANN401 — whatever `positions_get` reports
    """One MT5 position, read into the shared type.

    ⚠️ `sl` of `0.0` becomes `None`. MT5 reports an absent stop that way, and a stop *at* zero is
    not a level anybody set — collapsed, "unprotected" and "protected at nothing" become the same
    answer, and only one of them makes arming a stop a tightening.
    """
    level = Decimal(str(position.sl))
    return HeldPosition(
        ticket=int(position.ticket),
        symbol=str(position.symbol),
        side=Side.LONG if int(position.type) == 0 else Side.SHORT,
        volume=Decimal(str(position.volume)),
        price_open=Decimal(str(position.price_open)),
        stop_loss=level if level > 0 else None,
    )


def _jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    """MT5 answers with numpy scalars and named tuples; JSONB takes neither.

    ⚠️ Stringified rather than coerced to `float`. This document is evidence, and a price that
    passed through a float on the way into the audit trail is evidence about a different number.

    ⚠️ **Lists keep their shape.** A withdrawal touches one order per name in the ordinary case
    and more than one when something has gone wrong, so its evidence is a list of tickets and a
    list of retcodes — and those are exactly the rows an incident filters on. Flattened to
    `"[10009, 10013]"` they are still readable and no longer queryable, which is the half of
    "evidence" that only matters once somebody needs it.
    """
    return {key: _value(value) for key, value in payload.items()}


def _value(value: Any) -> Any:  # noqa: ANN401 — whatever the terminal put in the answer
    if isinstance(value, int | str):
        return value
    if isinstance(value, list | tuple):
        return [_value(item) for item in value]
    return str(value)
