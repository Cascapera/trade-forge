"""The `Broker` the engine talks to when the venue is a real one, three processes away.

The whole problem in one sentence: **`Broker.submit` is synchronous and the executor is another
process.** That looks impossible until you read what the protocol actually promised, back in
phase 1 when no executor existed — *"queue an order; returns whether it was **accepted**, not
whether it **executed**"*. Accepted is a local fact. It is knowable the instant the order is on
the stream, and nothing about it requires the venue to have answered. The seam was designed for
this; it just had not met it yet.

So the split is:

* `submit` publishes to `orders.outbound` and says "accepted". Nothing waits.
* `on_bar` drains `fills.inbound` — **with this session's own consumer group** — and hands the
  loop the fills that arrived since the last bar. That is the same shape `BacktestBroker` has,
  where `on_bar` is also the only place a `Fill` can be born.

⚠️ **This process must never import `MetaTrader5`** (AGENTS.md §5.4), which decides more than it
first appears. `Broker.positions`, `account` and `trades` cannot be forwarded to the terminal,
so the answer has to come from somewhere else — and the choice made here is that **this broker
keeps its own ledger**, the engine's `Portfolio`, fed by the fills it sees.

That is not the obvious choice. The obvious one is to have the executor publish account
snapshots and make this a pure proxy. It was rejected for a reason the protocol states in its
own docstring: *"`on_bar` must leave `account()` marked to market at the candle it was handed"*.
A snapshot from another process is stamped with wall-clock time, not with a bar's close, so an
equity curve built from snapshots is a balance curve sampled at whatever moment the executor
happened to answer. `Portfolio.mark_to_market` is exact, uses the candle in hand, and is the
same arithmetic the backtest's golden test checks by hand.

The price of that choice is honest and worth stating: there are now **two answers** to "what is
my balance" — this ledger and the account at the venue — and they can drift. Reconciling them is
its own piece of work (PR-304-A4), and the drift it finds is a real signal. A proxy would have
had one answer that always agreed with itself and told you nothing.
"""

import datetime as dt
import logging
from collections.abc import Callable, Sequence
from typing import Protocol, cast

from redis.exceptions import ResponseError
from redis.typing import EncodableT, FieldT, GroupT, KeyT, StreamIdT

from tradeforge_engine.domain import (
    ZERO,
    AccountState,
    Candle,
    ClosedTrade,
    Fill,
    InstrumentSpec,
    Money,
    OrderRequest,
    OrderResult,
    Position,
    Refusal,
    RefusedBy,
    Side,
    SignalKind,
)
from tradeforge_engine.errors import EngineError
from tradeforge_engine.portfolio import Portfolio
from tradeforge_executor.snapshot import PUBLISH_EVERY
from tradeforge_executor.wire import (
    MAX_CLIENT_ID,
    VENUE_OUTCOMES,
    VENUE_STATE,
    VENUE_STATE_FRESH_FOR,
    WireFill,
    WireRefusal,
    cancel_fields,
    modify_stop_fields,
    order_fields,
    outcome_from_fields,
    stream_for,
    venue_state_from,
)

logger = logging.getLogger(__name__)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


__all__ = ["MT5Broker", "OrderWire"]

# Delivered to this consumer and never acknowledged. Read before the new ones, so a session that
# died between reading a fill and folding it into its ledger finds that fill again rather than
# trading on a position it does not know it holds.
_PENDING = "0"
_NEW = ">"

_BATCH = 500
"""How many entries one read may hand back. A ceiling, not an expectation: a bar normally brings
back nought or one. It exists so a session restarting against a stream with a long backlog does
not pull an unbounded answer into memory in a single call."""


class OrderWire(Protocol):
    """The four Redis calls this broker makes, and no more.

    ⚠️ **Spelled in redis-py's own type aliases, not in `str`.** `dict` is invariant, so a
    `streams: dict[str, str]` here would *not* be satisfied by a client whose parameter is
    `dict[KeyT, StreamIdT]` — the protocol would describe a client that does not exist, every
    double written against it would type-check, and the real thing would not. That is not a
    guess; `CandleStream.StreamReader` records the same scar, and the asymmetry below
    (`xreadgroup` takes plain `str` where `xgroup_create` takes the aliases) is redis-py's own.
    """

    def xadd(self, name: KeyT, fields: dict[FieldT, EncodableT]) -> object: ...

    def xgroup_create(
        self,
        name: KeyT,
        groupname: GroupT,
        id: StreamIdT,  # noqa: A002 — redis-py's parameter name; renaming it stops matching
        mkstream: bool,
    ) -> object: ...

    def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[KeyT, StreamIdT],
        count: int | None = None,
        block: int | None = None,
    ) -> object: ...

    def xack(self, name: KeyT, groupname: GroupT, *ids: StreamIdT) -> object: ...

    def get(self, name: KeyT) -> object: ...


class MT5Broker:
    """A `Broker` whose venue is an executor at the other end of two Redis streams."""

    def __init__(  # noqa: PLR0913 — one client and four keyword-only knobs
        self,
        client: OrderWire,
        *,
        session_id: str,
        instrument: InstrumentSpec,
        initial_capital: Money,
        consumer: str = "session",
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._client = client
        self._now = now or _utcnow
        self._session_id = session_id
        self._instrument = instrument
        self._portfolio = Portfolio(initial_capital=initial_capital, instrument=instrument)
        # ⚠️ The group is the **session's**, so this session sees every fill on the stream.
        # `fills.inbound` is fan-out, the opposite of `orders.outbound`: an order must be handled
        # by exactly one executor, a fill must reach the session that placed it. Sharing a group
        # here would mean a session silently never learns that its own order filled.
        self._group = f"session-{session_id}"
        self._consumer = consumer
        self._sent: dict[str, OrderRequest] = {}

        # ⚠️ **A mailbox, not a log.** Refusals arrive between bars, from another process
        # (ADR-0024); the loop drains this once a bar and folds it into the next
        # `Context.refusals`. Draining is what makes it news exactly once — a refusal handed
        # over twice would have `StructurePhase` forget an order it had since armed.
        self._refused: list[Refusal] = []
        """Every order this broker put on the wire, by the name it went out under.

        ⚠️ **This is the other half of every fill**, and why the request does not travel home
        on the wire. A `Fill` needs its `OrderRequest`, and sending the same document twice
        invites the two copies to disagree — about the stop, about `decided_at`, about the
        snapshot the loop attached. The `client_id` is the whole correlation (ADR-0014)."""

        self._minted = 0

    # ------------------------------------------------------------------ sending

    def start(self) -> None:
        """Make this broker usable, or refuse to let the session begin.

        Two things, and the order is deliberate: **look at the venue first**, then subscribe. A
        broker that created its consumer group and only then discovered an orphaned position
        would leave a group behind for a session that never ran.

        ⚠️ **The venue may not be holding anything.** This ledger starts empty by construction,
        and it has no way to notice a position it did not open — so a session starting over one
        left by a predecessor would size its next trade against an account already committed,
        and never know. A session id is new on every process (`open_session` writes a new row),
        so the dead session's fills are filtered out by `_read` and vanish silently. Nothing else
        in this system is watching for that.

        ⚠️ **Absent and stale are refusals too.** "I cannot tell what the venue holds" is not
        "the venue holds nothing", and only one of them authorises trading. Same argument the
        kill switch makes about a layer that cannot be read: the cost of refusing wrongly is a
        session that does not start, and the cost of allowing wrongly is money.
        """
        self._refuse_unless_venue_is_flat()
        self.ensure_group()

    def _refuse_unless_venue_is_flat(self) -> None:
        raw = self._client.get(VENUE_STATE)
        if raw is None:
            raise EngineError(
                f"no venue snapshot at {VENUE_STATE!r}: the executor publishes one every "
                f"{PUBLISH_EVERY.total_seconds():.0f}s, so this means it is not running — and "
                f"not knowing what the account holds is not the same as it holding nothing"
            )
        # ⚠️ `str` or `bytes`, because that is the client's decision and not this module's: a
        # `Redis` built with `decode_responses=True` hands back text and one without hands back
        # bytes. A broker that understood only one would pass every test here and fail on the box
        # where somebody configured the other.
        if isinstance(raw, str):
            document = raw
        elif isinstance(raw, bytes | bytearray | memoryview):
            document = bytes(raw).decode()
        else:
            raise EngineError(
                f"the venue snapshot at {VENUE_STATE!r} came back as {type(raw).__name__}, "
                f"which is neither text nor bytes"
            )
        try:
            state = venue_state_from(document)
        except Exception as error:
            raise EngineError(
                f"the venue snapshot at {VENUE_STATE!r} is unreadable: {error}"
            ) from error

        now = self._now()
        if state.is_stale(now=now):
            raise EngineError(
                f"the venue snapshot is stamped {state.at.isoformat()} and it is now "
                f"{now.isoformat()}: nothing has looked at the account for longer than "
                f"{VENUE_STATE_FRESH_FOR.total_seconds():.0f}s"
            )
        if state.positions:
            held = "; ".join(
                f"{position.symbol} {position.side.value} {position.volume} @ "
                f"{position.price_open} (ticket {position.ticket})"
                for position in state.positions
            )
            raise EngineError(
                f"the venue is already holding {len(state.positions)} position(s) under this "
                f"project's magic number: {held}. This ledger starts empty and cannot account "
                f"for them, so close them or hand them over before starting a session"
            )

    def ensure_group(self) -> bool:
        """Create this session's consumer group. `True` if this call created it.

        Must be called before the first `on_bar`, and `session.py` does it at start-up rather
        than lazily: a group created on the first bar would begin reading at that moment, and
        any fill that arrived while the session was warming up would be dropped on the floor —
        a real position, at the venue, that the ledger never hears about.

        `mkstream` because the session may legitimately start before any executor has published
        anything. The already-exists case is read off the error rather than pre-empted with a
        check, because asking and then writing is two round trips with a race between them.
        """
        try:
            self._client.xgroup_create(
                name=VENUE_OUTCOMES, groupname=self._group, id=_PENDING, mkstream=True
            )
        except ResponseError as error:
            if "BUSYGROUP" in str(error).upper():
                return False
            raise
        return True

    def submit(self, order: OrderRequest) -> OrderResult:
        """Put the order on `orders.outbound`. Accepted means queued, never executed.

        **A refusal here is local and immediate**: the wire would not carry the order faithfully,
        or Redis would not take it. Neither is the venue's opinion — the venue has not been asked
        yet, and will answer into `order_audit` and `fills.inbound` in its own time.
        """
        if order.stop_price is not None:
            # ⚠️ Refused rather than sent, because `order_fields` does not carry `stop_price` and
            # `MT5Gateway._order_type` only branches on `limit_price`. A breakout entry
            # (ADR-0016) would therefore reach the venue as a **market order**, filling at once
            # at whatever price is quoted instead of waiting for the level to break — a
            # different trade, taken silently, at a price the strategy never authorised.
            return OrderResult(
                order=order,
                accepted=False,
                reason=(
                    "a stop entry cannot cross this wire yet: `order_fields` carries no "
                    "`stop_price`, so the venue would take it as a market order"
                ),
            )

        client_id = order.client_id or self._mint()
        if len(client_id) > MAX_CLIENT_ID:
            # ⚠️ Refused rather than truncated, and the difference is not cosmetic. Every name in
            # this system ends in the part that distinguishes it — a counter, a bar's minute — so
            # the venue dropping the tail does not shorten a name, it **merges** names: two orders
            # arrive at the account with the same comment and nobody looking at that screen can
            # tell which is which. A refusal is loud, recorded, and fixable; a merge is silent.
            return OrderResult(
                order=order,
                accepted=False,
                reason=(
                    f"the name {client_id!r} is {len(client_id)} characters and the venue keeps "
                    f"{MAX_CLIENT_ID}: it would reach the account merged with every other name "
                    f"sharing that prefix"
                ),
            )

        try:
            self._client.xadd(
                stream_for(self._session_id),
                cast(
                    "dict[FieldT, EncodableT]",
                    order_fields(order, session_id=self._session_id, client_id=client_id),
                ),
            )
        except Exception as error:  # an unsendable order is a refusal, not a crash
            logger.exception("could not queue %s", client_id)
            return OrderResult(order=order, accepted=False, reason=f"the wire refused it: {error}")

        # ⚠️ Remembered **after** the write, not before. An order recorded here and never sent
        # would leave the broker waiting for a fill that cannot come; the reverse — sent and not
        # remembered — is caught loudly on arrival, by `_request_for`.
        self._sent[client_id] = order
        logger.info("queued %s %s %s (%s)", order.side.value, order.volume, order.symbol, client_id)
        return OrderResult(order=order, accepted=True)

    def _mint(self) -> str:
        """A name for an order the strategy did not name.

        Market orders need no handle — nothing will ever cancel one — but the wire correlates by
        `client_id` and the audit trail is indexed by it, so every order gets one. Derived from
        the session and a counter rather than random: two identical entries on the same session
        are then distinguishable in `order_audit` by something an operator can put in order.

        ⚠️ **The session id is abbreviated, and it has to be.** A full uuid is 36 characters, so
        `f"{session_id}:{n}"` is 38 and the venue keeps 31 — which cuts off the counter, the only
        part that distinguishes one minted name from the next, and lands every order of a session
        at the account under one identical comment. Measured, on names this method produced.

        Eight hex characters are not a guarantee against two sessions colliding, and they do not
        need to be: `session_id` travels beside the name on the wire and sits in its own column in
        `order_audit`. What this prefix is for is the one place the full id cannot go — a human
        reading the account, wanting to know which session put that order there.
        """
        self._minted += 1
        return f"s{self._session_id.replace('-', '')[:8]}-{self._minted}"

    def cancel(self, client_id: str) -> bool:
        """Withdraw a resting order by the name its strategy gave it (ADR-0014).

        ⚠️ **`True` means the instruction is on its way, not that anything was withdrawn.** The
        same asymmetry `submit` has, and for the same reason: the venue is another process, and
        what it finds when it looks is not knowable here. The protocol's `False` is reserved for
        the one thing this end *can* answer — an order this broker never sent.

        That is a narrower `False` than `BacktestBroker`'s, which really does know whether it was
        holding the order. The difference is honest: in live, "the venue had nothing under that
        name" is a race with a fill, and a broker that reported it as a failed cancel would have
        a strategy re-arming a zone that had just been entered.
        """
        if client_id not in self._sent:
            logger.info("nothing to cancel under %s; this session never sent it", client_id)
            return False
        return self._publish(
            cancel_fields(session_id=self._session_id, client_id=client_id),
            what=f"cancel {client_id}",
        )

    def modify_stop(self, symbol: str, stop_loss: Money, decided_at: dt.datetime) -> bool:
        """Move the protective stop of the open position in `symbol` (ADR-0018).

        ⚠️ **The executor refuses this today**, with a stated rule in `order_audit`: it cannot yet
        read the position and check for itself that the move *tightens*, and it does not take
        this process's word for that. A sign error here would arrive there looking exactly like a
        tightening and be waved past every limit (PR-304-A3-B).

        It is published anyway rather than raised on, and the difference matters. Raising kills
        the session; publishing puts the instruction, the level and the instant it was decided
        into the trail, where the refusal is visible and countable. A strategy conducting a trade
        still must not run live until the far end can act on it — but that is a decision for
        whoever starts the session, made from evidence, not a crash.

        `decided_at` travels because it is the anti-lookahead stamp. It is not the executor's to
        use, and it is not the executor's to lose either.
        """
        if self._portfolio.position is None:
            # The protocol's own `False`: nothing to protect. Answered here rather than three
            # processes away because this ledger is the thing that knows.
            return False
        return self._publish(
            modify_stop_fields(
                session_id=self._session_id,
                client_id=self._mint(),
                symbol=symbol,
                stop_loss=stop_loss,
                decided_at=decided_at,
            ),
            what=f"move the stop of {symbol} to {stop_loss}",
        )

    def _publish(self, fields: dict[str, str], *, what: str) -> bool:
        """One instruction onto `orders.outbound`. `False` if the wire would not take it."""
        try:
            self._client.xadd(
                stream_for(self._session_id), cast("dict[FieldT, EncodableT]", fields)
            )
        except Exception:  # an unsendable instruction is a refusal, not a crash
            logger.exception("could not queue %s", what)
            return False
        logger.info("queued %s", what)
        return True

    # ------------------------------------------------------------------ receiving

    def on_bar(self, candle: Candle) -> Sequence[Fill]:
        """Fold in whatever the venue did since the last bar, then value the account at this one.

        ⚠️ **Exits before entries**, which the protocol requires and the ledger enforces. A
        reversal that closed and reopened inside one bar arrives as two entries on a stream whose
        order is arrival, not causality; applied the wrong way round, the ledger refuses the
        second position — correctly, on a sequence that was merely mis-sorted.

        ⚠️ **`mark_to_market` runs even when nothing arrived.** The loop reads equity straight
        after this call, once per bar. A broker that only marked on bars where something happened
        would draw a flat line through every drawdown it was not trading in.
        """
        arrived = self._drain()
        # `SignalKind.EXIT` sorts before `ENTRY` because `False < True`; `sorted` is stable, so
        # two fills of the same kind keep the order the venue reported them in.
        born: list[Fill] = []
        for wire in sorted(arrived, key=lambda fill: fill.intent_is_entry):
            fill = self._fill_from(wire)
            self._portfolio.apply(fill)
            born.append(fill)
        self._portfolio.mark_to_market(candle)
        return tuple(born)

    def _drain(self) -> list["_Arrived"]:
        """Everything on `fills.inbound` for this session, oldest first, without blocking.

        ⚠️ **Every entry is acknowledged, including the ones belonging to other sessions.** The
        stream is fan-out, so this group is offered every fill any executor publishes. Leaving
        another session's entries unacknowledged would grow this group's pending list without
        bound, for ever, and make the pending read above slower on every bar.

        Not blocking: the bar has already closed, so what is here is what there is. The fill for
        an order submitted on the previous bar was published seconds after that bar, a whole
        timeframe ago.
        """
        arrived: list[_Arrived] = []
        for start in (_PENDING, _NEW):
            answer = self._client.xreadgroup(
                groupname=self._group,
                consumername=self._consumer,
                streams={VENUE_OUTCOMES: start},
                count=_BATCH,
            )
            for _stream, entries in cast(
                "list[tuple[object, list[tuple[str, dict[str, str]]]]]", answer or []
            ):
                for entry_id, fields in entries:
                    mine = self._read(entry_id, fields)
                    if mine is not None:
                        arrived.append(mine)
                    self._client.xack(VENUE_OUTCOMES, self._group, entry_id)
        return arrived

    def _read(self, entry_id: str, fields: dict[str, str]) -> "_Arrived | None":
        """One entry off the stream, or `None` if it is not this session's business.

        ⚠️ **An unreadable entry is skipped, not raised on.** It is acknowledged either way by
        the caller: a malformed entry that is never acknowledged is redelivered on every bar for
        the life of the session, and the second bar fails exactly like the first. The audit trail
        holds what the venue actually said.
        """
        try:
            outcome = outcome_from_fields(fields)
        except (KeyError, ValueError, ArithmeticError):
            logger.exception("unreadable outcome %s; acknowledged and skipped", entry_id)
            return None
        if outcome.session_id != self._session_id:
            return None
        if isinstance(outcome, WireRefusal):
            # ⚠️ **Not returned to the caller**, because a refusal is not something `on_bar`
            # folds into the ledger — it is something the *strategy* has to be told. It goes to
            # the mailbox the loop drains, and this method answers `None` the same way it does
            # for another session's entry: nothing here for the fills path.
            self._refused.append(self._refusal_from(outcome))
            return None
        return _Arrived(wire=outcome, order=self._request_for(outcome))

    def _refusal_from(self, wire: WireRefusal) -> Refusal:
        """The wire's refusal as the engine's, which is a narrowing and worth naming.

        ⚠️ **`by_venue` decides the gate, and the two are not interchangeable.** `VENUE` means
        the terminal said no — usually the same answer next bar; `EXECUTOR` means a safeguard in
        between did, on a condition that changes by itself. A strategy deciding whether to offer
        the zone again is asking exactly that, so collapsing them would hand it one word for two
        instructions.

        ⚠️ **The order is not looked up.** Unlike a fill — which raises when this session cannot
        attribute it, because an unattributed fill means the ledger is wrong — a refusal is about
        an order that does **not** exist anywhere. There is nothing to be wrong about, and a
        refusal for a name this session does not recognise is still worth passing on: the
        strategy keys its own bookkeeping by that name, not this dictionary.
        """
        detail = wire.reason
        if wire.retcode is not None:
            detail = f"{detail} (retcode {wire.retcode})"
        order = self._sent.get(wire.client_id)
        return Refusal(
            client_id=wire.client_id,
            intent=order.intent if order is not None else SignalKind.ENTRY,
            refused_by=RefusedBy.VENUE if wire.by_venue else RefusedBy.EXECUTOR,
            reason=order.reason if order is not None else "",
            detail=detail,
        )

    def refusals(self) -> Sequence[Refusal]:
        """Everything that arrived saying an order was never placed, and never twice.

        Drained rather than read: see `_refused`. The loop asks once a bar, including on bars
        where nothing was submitted — which is most of them, and exactly when a refusal for the
        previous bar's order lands.
        """
        drained, self._refused = self._refused, []
        return tuple(drained)

    def _request_for(self, wire: WireFill) -> OrderRequest:
        """The order this fill belongs to, or a stop.

        ⚠️ **Raises rather than skipping.** A fill this broker cannot attribute is a real
        position at a real venue that the ledger is about to be wrong about — and every number
        the session reports from here on, including the equity the risk manager sizes against,
        is computed from a ledger that has lost a trade. Stopping is the cheap failure; the
        expensive one is a session that keeps trading against a fiction.

        It is also the shape a restart takes: this dictionary lives in memory, so a session that
        died holding an order comes back not knowing about it. That is reconciliation's problem
        (PR-304-A4), and until it exists the honest thing is to refuse rather than to guess.
        """
        order = self._sent.get(wire.client_id)
        if order is None:
            raise EngineError(
                f"fill for {wire.client_id} at {wire.price}, which this session never sent: "
                f"the position is real and this ledger cannot account for it"
            )
        return order

    def _fill_from(self, arrived: "_Arrived") -> Fill:
        """Turn what the venue did into what the engine can account for.

        **Two things change on the way in, and both are the same decision.** The venue prices a
        buy at the ask and a sell at the bid; MT5's bars are bid-based. So a buy's fill price
        sits above the bar it happened in — measured on EURUSD M15, outside `candle.high` in
        7.5% of bars — and `loop._reject_lookahead` rightly refuses a price the bar never traded
        at. `SpreadCostModel` settled the doctrine in phase 1: *crossing the spread is a cost,
        never a worse price*.

        So the ask is converted back to the bid it was quoted from, and the difference becomes
        `Fill.costs`:

            price = venue price - spread   (only on the leg that buys)
            costs = that same distance, in money

        ⚠️ **The whole spread, on the leg that crosses — not half on each.** The backtest's
        models charge half a leg because a backtest has one price series and no way to know
        which side it was standing on. Here it is known exactly, and charging by the convention
        instead would leave the ledger disagreeing with the account statement on every trade,
        which turns reconciliation from a signal into noise.

        The leg that buys is the long's entry and the short's *exit*: `side is LONG` exactly when
        `intent is ENTRY`.
        """
        wire, order = arrived.wire, arrived.order
        crossed = wire.spread if arrived.buys else ZERO
        price = wire.price - crossed
        if price <= ZERO:
            raise EngineError(
                f"fill for {wire.client_id} at {wire.price} with a spread of {wire.spread} "
                f"prices to {price}: a quote wider than the price itself is not a quote"
            )
        return Fill(
            order=order,
            time=wire.at,
            price=price,
            volume=wire.volume,
            costs=self._instrument.money_for(crossed, wire.volume),
        )

    # ------------------------------------------------------------------ reading

    def positions(self, symbol: str) -> Sequence[Position]:
        """What this session holds in `symbol`, according to its own ledger.

        ⚠️ The argument is not decoration even here, where the ledger holds at most one position
        at a time: a session trading EURUSD must not be handed a position in anything else, and
        the day this ledger holds two the filter is already written.
        """
        position = self._portfolio.position
        if position is None or position.symbol != symbol:
            return ()
        return (position,)

    def account(self) -> AccountState:
        """Balance and equity from this broker's ledger — **not from the terminal**.

        See the module docstring: the terminal cannot be asked from this process, and a snapshot
        forwarded from the executor would not be marked to the bar the loop is on.
        """
        return self._portfolio.account()

    def trades(self) -> Sequence[ClosedTrade]:
        """The round trips **this session** has closed, which is what the ledger holds anyway.

        The filtering an `MT5Broker` reading a terminal would have to do by magic number is done
        here by construction: nothing but this session's own fills was ever folded in.
        """
        return self._portfolio.trades


class _Arrived:
    """One fill off the wire, paired with the order it belongs to.

    A small object rather than a tuple because three call sites read it and `arrived[1]` is how
    the pairing quietly gets swapped.
    """

    __slots__ = ("order", "wire")

    def __init__(self, *, wire: WireFill, order: OrderRequest) -> None:
        self.wire = wire
        self.order = order

    @property
    def intent_is_entry(self) -> bool:
        return self.order.intent is SignalKind.ENTRY

    @property
    def buys(self) -> bool:
        """Did this leg pay the ask? A long's entry and a short's exit do."""
        return (self.order.side is Side.LONG) is self.intent_is_entry
