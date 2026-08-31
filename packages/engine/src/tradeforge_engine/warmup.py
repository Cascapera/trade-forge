"""Handing a strategy the state a backtest would have had, and none of the money.

A live session starts cold. Its indicators read `None` until they have seen enough bars, and
its structure has no bias until the market has given it one — so a session that begins at the
next tick takes no trade it should have taken, for a while, and **looks exactly like a session
that found no setup**. Those two need opposite responses, and nothing on a screen tells them
apart.

**Warm-up is a backtest, and only the ledger is thrown away.** History is run through the real
loop against a real broker: orders fill, zones burn, stops move, and the strategy's own
bookkeeping ends up describing a market that actually happened to it. Then the session opens
with a *fresh* broker — so the money is untouched by construction, not by resetting anything —
and whatever was still resting is placed again.

**The alternative was measured, and it was worse.** The first version of this module vetoed
every order decided on history, through a `RiskManager` wrapper. It kept the account clean and
broke the strategy: a setup marks its armed order as placed the moment it *emits* the signal
(`setups.py`), and the loop never hands `OrderResult` back — so the strategy crossed into the
session believing an order rested at a venue that had never heard of it, and that region was
never traded again. Measured on EURUSD H1 with the CHoCH setup, at five hand-over points:
**four produced that ghost**. Without the veto, at the same five bars, the strategy and the
broker agreed every time. A cure that reproduces the disease is not a cure.

**Why a fresh broker rather than a reset.** A `reset_ledger()` would have to decide, one field
at a time, what survives — and every field somebody forgets is a warm-up trade leaking into a
live equity curve, silently. Building the live broker empty makes the money untouched by
construction: there is no code path that could carry a warm-up fill across, because there is no
carrying.

⚠️ **A session cannot open mid-trade.** If history ends with the strategy holding a position,
`hand_over` refuses. Inheriting it would mean a paper session reporting a trade it never took,
entered at a price from before it existed; flattening it would leave the strategy managing a
position the broker does not have — the same ghost, from the other side. Measured on EURUSD H1,
a hand-over lands mid-position **0.4% to 3% of bars**, against **35% to 73%** holding a resting
order. Refusing the rare case to keep the common one honest is the trade this makes.

⚠️ **What is still not reproduced.** `Context.position` and `Context.fills` are real during
warm-up now, so the setups branch as they would in a backtest. What a warmed session cannot
have is the *history of its own account*: its equity curve starts at the hand-over, and its
first trade is its first trade. That is the intended asymmetry — the strategy remembers the
market, the account does not remember trades nobody took.
"""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import localcontext
from typing import Protocol, runtime_checkable

from tradeforge_engine.domain import (
    ZERO,
    InstrumentSpec,
    OrderRequest,
    Refusal,
    RefusedBy,
    Signal,
    SignalKind,
)
from tradeforge_engine.errors import EngineError
from tradeforge_engine.loop import ENGINE_CONTEXT
from tradeforge_engine.protocols import Broker, Charted, Indicator, RiskManager

__all__ = ["HandOver", "RestingOrders", "hand_over", "unwarmed_indicators"]


@runtime_checkable
class RestingOrders(Protocol):
    """A broker that can enumerate the orders it is still holding.

    ⚠️ **Deliberately not part of `Broker`.** A live venue answers this over a network, and the
    engine's five seams are the ones the loop needs — this is a question only a hand-over asks.
    A `Protocol` rather than `getattr`, because `getattr` is invisible to `mypy --strict`: rename
    `resting()` and the hand-over would quietly carry nothing, with no error anywhere and a
    session that simply never trades the region it armed.
    """

    def resting(self) -> Sequence[OrderRequest]: ...


def unwarmed_indicators(strategy: object) -> tuple[str, ...]:
    """The names of the strategy's indicators that still read `None`, in drawing order.

    Empty for a strategy that charts nothing, and empty for one whose every indicator has a
    value. Those two are not the same fact, and a caller that cares has to ask whether the
    strategy is `Charted` at all — this returns what is *still cold*, which is the only thing a
    start-up decision needs.

    ⚠️ **Reads, never drives.** `Charted.overlays` hands back the strategy's own live indicator
    objects, and calling `update` on them would advance the very state the strategy reads — the
    protocol says so in as many words. `value()` is a question; `update()` would be an answer
    the strategy never gave.
    """
    if not isinstance(strategy, Charted):
        return ()
    overlays: Mapping[str, Indicator] = strategy.overlays()
    return tuple(name for name, indicator in overlays.items() if indicator.value() is None)


class HandOver:
    """What crossed from warm-up into the session, and what did not.

    Frozen in the sense that matters: it records a hand-over that already happened, so a caller
    reporting "warmed with N bars, carried M orders" is reporting a fact rather than a plan.
    """

    __slots__ = ("bars", "carried", "refused", "warm_trades")

    def __init__(
        self,
        *,
        bars: int,
        carried: tuple[OrderRequest, ...],
        refused: tuple[Refusal, ...],
        warm_trades: int = 0,
    ) -> None:
        self.bars = bars
        """Bars of history the strategy was driven over. What a session records as its warm-up."""

        self.carried = carried
        """The resting orders re-placed on the live broker, in the order they were submitted."""

        self.refused = refused
        """The orders that did not cross, and which gate turned each one away. Empty is the
        expected answer.

        Reported rather than raised because the session is otherwise fine and the operator needs
        to know *which* region will not be traded — an exception would replace that with a stack
        trace.

        ⚠️ **`Refusal`s rather than names, and the caller is expected to hand them to the
        strategy** — `iter_run(refusals=...)`, which delivers them on the session's first live
        bar. This was a `tuple[str, ...]` until PR-304-B-D-2b, and the strings were the last
        silent hand-over point in ADR-0023: the strategy marks an order placed the instant it
        emits the signal, so a name that only ever reached a log left it holding an order the
        session's broker had refused. Four of five measured hand-over points produced that ghost.

        Three gates, and they are not interchangeable — see `RefusedBy`. `SIZING` and `RISK` are
        answers about *this account at this moment*; `BROKER` is an answer about the order
        itself. A strategy deciding whether to offer the zone again is asking exactly that, and
        a caller that only wanted to print them can still read `client_id`.
        """

        self.warm_trades = warm_trades
        """How many round trips the warm-up closed. Recorded, never carried.

        The strategy crosses holding `_traded`/`_spent` for zones whose trades exist only in the
        ledger that was thrown away — so "why did the session skip this region?" has an answer
        that lives nowhere in the session. This number is the smallest honest trace of it.
        """


def hand_over(  # noqa: PLR0913 — two brokers, the symbol, and the two seams a re-size needs
    warm: Broker,
    live: Broker,
    *,
    symbol: str,
    bars: int,
    risk: RiskManager,
    instrument: InstrumentSpec,
) -> HandOver:
    """Move what a warm-up learned onto a session's own broker, and nothing else.

    `warm` is the broker history was run against; `live` is the empty one the session will use.
    Nothing about the account crosses — that is what building `live` fresh already guarantees.

    Raises `EngineError` if `warm` still holds a position in `symbol`, or if `live` is not
    empty. Both are refusals to start rather than problems to work around: a session that
    inherits a position reports a trade it never took, and a session handed a broker that
    already has orders is being started twice.

    ⚠️ **Every carried order is re-sized against `live`, and that is not a detail.** Of all the
    fields on an order, `volume` is the only one that is not a fact about the market: a
    `PercentRiskManager` computed it from `account.equity`, and the equity that computed it
    belongs to the ledger this hand-over exists to throw away. Measured on the window the tests
    use — a warm-up that ends at 9 901 from 10 000 — the order carries 1.08 lots while the
    session's own account calls for 1.09. A 1% drift in the account becomes a 1% drift in the
    risk of the session's first trade, authorised by money that never existed. On a warm-up that
    ran the account to 13 000 it is 1.42 against 1.09, and the session's first trade risks 1.3%
    where the strategy asked for 1%.

    An order that re-sizes to zero does not cross, and is reported in `refused`. That is the
    risk manager saying "not this trade" on the session's own terms — an unstopped order, or an
    account too small for a lot step — and carrying it anyway would be overruling the one
    component whose job is to say no.

    It deliberately says nothing about whether the strategy is *ready* — `unwarmed_indicators`
    is the caller's to ask. "Start anyway" is a legitimate answer for a strategy whose average
    is still warming, and a hand-over that refused on its own would be deciding that.
    """
    if warm.positions(symbol):
        raise EngineError(
            f"warm-up ended with an open position in {symbol}; a session cannot open mid-trade. "
            "Seed a window that ends flat, or start once the strategy is out."
        )
    # ⚠️ `_resting_orders(live)` is in the guard, and it is the only clause that catches the
    # state a *successful* hand-over leaves behind. A second call finds no position and no
    # trade — it finds an order — so without this the guard passes and the only thing stopping
    # a duplicate is `BacktestBroker` refusing a repeated `client_id`. That refusal is then
    # reported as "this region will not be traded", which is a lie: it is resting. And a venue
    # that does not deduplicate names would end up holding two limits on one zone.
    if live.positions(symbol) or live.trades() or _resting_orders(live):
        raise EngineError("the live broker is not empty; hand_over expects a fresh one")

    account = live.account()
    carried: list[OrderRequest] = []
    refused: list[Refusal] = []
    # The engine pins its own arithmetic for the same reason `run()` does: `risk.size` divides
    # `Decimal`s, and `getcontext()` is process-global and mutable — any library in this process
    # that sets `prec` or `rounding` changes every number the engine produces.
    #
    # ⚠️ **A mutant that removes this line survives, and it is equivalent, not a test gap.**
    # Measured twice, independently: with `prec` from 28 down to 5 and `rounding` set to
    # ROUND_UP and ROUND_CEILING, the carried volume came back 1.09 every time, because
    # `PercentRiskManager` floors to the instrument's lot step and the step swallows the
    # difference. The pin stays for the same reason the one in `run()` does: it is an argument
    # about what the process *can* do to this arithmetic, not about what it has done. An
    # instrument with a finer step, or a risk manager that did not floor, and it stops being
    # equivalent — silently.
    with localcontext(ENGINE_CONTEXT):
        for order in _resting_orders(warm):
            signal = _as_signal(order)
            volume = risk.size(signal, account, instrument)
            if volume <= ZERO:
                # ⚠️ `client_id` and `reason` are separate fields, and used to be one. The old
                # `client_id or order.reason` put a *reason* into a slot every consumer reads as
                # a name: `StructurePhase._observe_refusal` matches `refusal.client_id` against
                # the name it is holding, so an unnamed order's reason arriving there is a string
                # that can only ever match by accident. An unnamed refusal is a log line, not a
                # correlation — `Refusal` says so, and this now says the same.
                refused.append(_refusal(order, RefusedBy.SIZING, f"sizing returned {volume}"))
                continue
            resized = replace(order, volume=volume)
            # ⚠️ **`allow` as well as `size`, and the split is the point.** `protocols.py` keeps
            # the veto out of sizing precisely so that a sizing bug cannot become a safety bug —
            # and a hand-over that only asked `size` would have made the reverse inversion, with
            # the arithmetic deciding whether an order exists. Today `PercentRiskManager.allow`
            # always says yes; the day a kill switch or a daily-loss limit lands, whoever writes
            # it will look for the calls to `allow`, and this has to be one of them
            # (AGENTS.md §5.7).
            if not risk.allow(resized, account):
                refused.append(_refusal(resized, RefusedBy.RISK, "the risk manager vetoed it"))
                continue
            result = live.submit(resized)
            if result.accepted:
                carried.append(resized)
            else:
                refused.append(_refusal(resized, RefusedBy.BROKER, result.reason))

    return HandOver(
        bars=bars,
        carried=tuple(carried),
        refused=tuple(refused),
        warm_trades=len(warm.trades()),
    )


def _refusal(order: OrderRequest, refused_by: RefusedBy, detail: str) -> Refusal:
    """The order the hand-over turned away, in the shape the strategy is shown refusals in.

    ⚠️ **`intent` is read off the order, never assumed**, even though every order a hand-over
    carries is resting and only entries rest today. That same guess was made one layer down in
    PR-304-B-D-2a and had to be taken back in review: a default that is usually right is
    indistinguishable, to whoever reads the record, from a fact somebody actually had.
    """
    return Refusal(
        client_id=order.client_id,
        intent=order.intent,
        refused_by=refused_by,
        reason=order.reason,
        detail=detail,
    )


def _as_signal(order: OrderRequest) -> Signal:
    """The order as the intent that produced it, so a `RiskManager` can size it again.

    ⚠️ `reference_price` is the **resting level**, not a close: sizing measures the distance
    from entry to stop, and for a limit order the entry *is* the limit. Handing the price of
    the hand-over bar instead would size against a distance the trade will never have.

    A resting order always carries one of the two levels — the domain refuses an order with
    both, and `_resting_orders` only ever returns orders that are resting — so the fallback is
    unreachable rather than defensive. It raises instead of guessing, because a signal sized
    against a `None` would be sized against nothing at all.

    ⚠️ **Some of what this fills in is redundant for `PercentRiskManager`, and that is a fact
    about that class, not about the seam.** It sizes from `abs(entry - stop)`, so it never reads
    `side` or `take_profit`, and it falls back to `reference_price` when both levels are `None` —
    which makes several fields here unobservable today. A `RiskManager` that sized a limit
    differently from a market order, or refused a signal with no target, would see a `Signal`
    that differs from the one the strategy emitted. Fill it in as completely as the order allows;
    the redundancy is on the safe side.

    `context` and `snapshot` are not carried onto the `Signal`: they were the original signal's
    payload, and the `OrderRequest` keeps its own — which is the copy that matters, because a
    fresh broker's bar window cannot rebuild one.
    """
    level = order.limit_price or order.stop_price
    if level is None:
        raise EngineError(f"order {order.client_id or order.reason!r} rests at no level")
    return Signal(
        kind=SignalKind.ENTRY,
        side=order.side,
        reference_price=level,
        stop_loss=order.stop_loss,
        take_profit=order.take_profit,
        reason=order.reason,
        limit_price=order.limit_price,
        stop_price=order.stop_price,
        client_id=order.client_id,
    )


def _resting_orders(broker: Broker) -> tuple[OrderRequest, ...]:
    """The orders a broker is still holding, or nothing if it cannot say.

    A broker that does not implement `RestingOrders` carries none across — wrong, but *visibly*
    wrong (the session trades nothing in that region) rather than an `AttributeError` on
    start-up. In this repository `warm` is always a `BacktestBroker`, because history is never
    replayed against a venue.
    """
    if not isinstance(broker, RestingOrders):
        return ()
    return tuple(broker.resting())
