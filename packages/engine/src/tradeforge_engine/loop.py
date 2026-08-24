"""The event loop (sdd.md §3.3.2). Fifty lines, and the whole system turns on their order.

    for each closed candle N:
        1. the broker fills what was decided on N-1, inside N
        2. the strategy sees N — closed, complete, final
        3. the risk manager sizes and vetoes
        4. the order is queued; it cannot fill until N+1
        5. the equity curve records N's close

Step 1 comes before step 2, and that is not a stylistic choice. It is the anti-lookahead
rule expressed as control flow: by the time the strategy is allowed to think about candle
N, everything it decided on N-1 has already executed at a price it could not have known.
Reverse those two lines and the backtest starts filling orders at prices the strategy had
already seen — the single most common way a backtesting engine reports a profit that does
not exist.

The loop does not *trust* the broker to honour any of this. It checks, on every fill, and
the check has both a floor and a ceiling — see `_reject_lookahead`.
"""

import datetime as dt
import logging
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, localcontext
from decimal import Context as DecimalContext
from typing import cast

from tradeforge_engine.domain import (
    SNAPSHOT_BARS_BEFORE,
    ZERO,
    AccountState,
    Candle,
    ClosedTrade,
    Context,
    EntrySnapshot,
    EquityPoint,
    Fill,
    InstrumentSpec,
    Money,
    OrderRequest,
    Position,
    Signal,
    SignalKind,
)
from tradeforge_engine.errors import EngineError, LookaheadError
from tradeforge_engine.protocols import Broker, RiskManager, Strategy

logger = logging.getLogger(__name__)

# The engine pins its own arithmetic. `decimal.getcontext()` is global and mutable: any
# library in the worker process that sets `getcontext().prec` — or `rounding` — would change
# every number the engine produces, without touching a line of it. And a determinism test
# running in that same process would not notice, because both runs would be corrupted
# identically.
#
# Built explicitly rather than copied. `localcontext()` with no argument *copies the ambient
# context*, so it would inherit whatever `rounding` the process happens to have set: with the
# same precision, ROUND_UP and ROUND_HALF_EVEN disagree in the last place. Today every tick
# size in the system is a power of ten and the divisions are exact — one instrument with a
# tick of 0.003 and that stops being true.
ENGINE_PRECISION = 28
ENGINE_CONTEXT = DecimalContext(prec=ENGINE_PRECISION, rounding=ROUND_HALF_EVEN)

# The window size lives with the type it describes (`domain.SNAPSHOT_BARS_BEFORE`), because the
# strategies size their own indicator buffers against it and a strategy reaching into the event
# loop for a constant would be the tail wagging the dog. Re-exported here, where the buffer that
# uses it is built.
#
# A *bounded* window, and that word is the whole design. `run()` takes an `Iterable` precisely
# so that ten years of M1 never sits in memory at once; a snapshot buffer that grew with the
# run would give that back, silently, and only on the long backtests where it matters. A
# `deque` with a `maxlen` cannot: it drops from the left as it fills, at constant cost.
# ⚠️ **One `__all__`, and it lives here.** There used to be a second one at the foot of the
# file, and the second one won — silently. Adding `iter_run` and `BarOutcome` to this list did
# nothing at all until the duplicate was removed, so the module's public surface disagreed with
# the list that claims to define it. Nothing broke, because every consumer imports from the
# module directly; a `from tradeforge_engine.loop import *` would have found the new names
# missing and no error anywhere to explain it.
__all__ = [
    "ENGINE_CONTEXT",
    "ENGINE_PRECISION",
    "SNAPSHOT_BARS_BEFORE",
    "BarOutcome",
    "RunResult",
    "iter_run",
    "run",
]


@dataclass(frozen=True, slots=True)
class RunResult:
    """What a run produced. Frozen, so a caller cannot doctor the record."""

    fills: tuple[Fill, ...]
    trades: tuple[ClosedTrade, ...]
    equity_curve: tuple[EquityPoint, ...]
    final_account: AccountState
    candles_processed: int


@dataclass(frozen=True, slots=True)
class BarOutcome:
    """What one bar produced, handed over the moment it is finished.

    The unit a live session works in. A backtest can afford to learn everything at the end,
    because the end arrives; a paper session's end is whenever somebody stops it, so anything
    it has not written down by then is lost. This is the record it writes.

    `fills` is **this bar's**, not the run's — the same tuple the strategy was shown in its
    `Context`. `equity` is the point step 5 appended, so a consumer that keeps every one of
    these holds exactly the curve `run()` would have built.
    """

    index: int
    candle: Candle
    fills: tuple[Fill, ...]
    equity: EquityPoint


def iter_run(  # noqa: PLR0913 — keyword-only; see run()
    *,
    candles: Iterable[Candle],
    timeframe: dt.timedelta,
    instrument: InstrumentSpec,
    strategy: Strategy,
    broker: Broker,
    risk: RiskManager,
) -> Iterator[BarOutcome]:
    """The loop itself, one `BarOutcome` at a time, for as long as candles keep arriving.

    **This is the only implementation of the five ordered steps, and that is the point.** A
    live session needs the same order as a backtest — not a similar one. Written twice, the
    two would agree on the day they were written and drift on the first fix applied to one of
    them, and the drift would be invisible: both would keep producing plausible trades. So the
    backtest is not a sibling of the live loop, it is a *consumer* of it — `run()` below is
    the whole of the difference, and it is an accumulator.

    **Live comes for free from `candles` being an `Iterable`.** A backtest hands it a cursor
    over ten years of history; a paper session hands it a generator that blocks on a Redis
    stream and yields whenever a bar closes. Neither the loop nor the broker can tell, because
    there is nothing here to tell them with.

    ⚠️ **Each bar's work runs under `ENGINE_CONTEXT`; the `yield` happens outside it.** Both
    halves matter. A `with localcontext(...)` wrapped around the *creation* of a generator
    does nothing at all — the body runs on `next()`, long after that block exited — so pinning
    the arithmetic from the caller is not available here and the loop has to pin it itself.
    And holding the context across the `yield` would push the engine's precision and rounding
    onto whatever the consumer does with the bar, which for a live session is database writes:
    the engine would be silently reconfiguring code that is not the engine.
    """
    if timeframe <= dt.timedelta(0):
        raise ValueError(f"timeframe must be positive, got {timeframe}")

    # Validation eagerly, iteration lazily. A generator function's body does not run until it
    # is first advanced, so a `raise` written directly in it would arrive whenever the caller
    # happened to start iterating — a bad timeframe would surface from inside a live session's
    # first bar rather than from the line that configured it.
    return _iter_run(
        candles=candles,
        timeframe=timeframe,
        instrument=instrument,
        strategy=strategy,
        broker=broker,
        risk=risk,
    )


def run(  # noqa: PLR0913 — keyword-only; each one names a real axis of a backtest
    *,
    candles: Iterable[Candle],
    timeframe: dt.timedelta,
    instrument: InstrumentSpec,
    strategy: Strategy,
    broker: Broker,
    risk: RiskManager,
) -> RunResult:
    """Drive a strategy over a stream of closed candles, to the end, and report.

    `timeframe` is how long one bar lasts, and the engine needs it to police the broker: a
    fill belongs to the bar being processed, and "the bar being processed" is `[candle.time,
    candle.time + timeframe)`. Without it, the guard has no ceiling and a broker that fills
    a week late passes unnoticed.

    Keyword-only, because five positional objects is four chances to swap two of them and
    still have the backtest run.

    Takes an `Iterable`, not a list: ten years of M1 is five million bars, and the engine has
    no reason to hold them all at once.

    ⚠️ **To the end.** Handed a stream that never ends — a live one — this never returns, and
    the lists below grow without bound. That is not a defect to be patched here; it is what
    `iter_run` is for.
    """
    fills: list[Fill] = []
    equity_curve: list[EquityPoint] = []
    processed = 0

    for outcome in iter_run(
        candles=candles,
        timeframe=timeframe,
        instrument=instrument,
        strategy=strategy,
        broker=broker,
        risk=risk,
    ):
        fills.extend(outcome.fills)
        equity_curve.append(outcome.equity)
        processed = outcome.index + 1

    # The closing reads are pinned like every bar's work was. ⚠️ With the brokers in this repo
    # that is a no-op and provably so — `Portfolio.account()` reads fields already computed and
    # `trades()` hands back objects already built, so a mutant deleting this block survives. It
    # stays as a boundary, not as a fix: the arithmetic of totalling a ledger is a broker's to
    # place, and one that computed equity on demand would need it. The comment says which of
    # those is true today so nobody reads the block as proof of the other.
    with localcontext(ENGINE_CONTEXT):
        return RunResult(
            fills=tuple(fills),
            trades=tuple(broker.trades()),
            equity_curve=tuple(equity_curve),
            final_account=broker.account(),
            candles_processed=processed,
        )


def _iter_run(  # noqa: PLR0913 — see run()
    *,
    candles: Iterable[Candle],
    timeframe: dt.timedelta,
    instrument: InstrumentSpec,
    strategy: Strategy,
    broker: Broker,
    risk: RiskManager,
) -> Iterator[BarOutcome]:
    previous: Candle | None = None
    # The arming window. Holds the decision bar plus the bars before it, and no more — see
    # SNAPSHOT_BARS_BEFORE. The loop owns it because the loop is the only component that sees
    # the stream, in backtest and in live alike, and because it is here that `decided_at` is
    # stamped: the window and the instant the anti-lookahead guard checks come from one place.
    window: deque[Candle] = deque(maxlen=SNAPSHOT_BARS_BEFORE + 1)

    for index, candle in enumerate(candles):
        # The engine's arithmetic is pinned for the whole of a bar's work and released before
        # the hand-over — see `iter_run`. Everything below this line is the engine; everything
        # the consumer does with the `BarOutcome` is not.
        with localcontext(ENGINE_CONTEXT):
            _reject_out_of_order(previous, candle, timeframe)

            # Before anything else looks at this bar. The strategy is about to be shown it, so
            # it belongs in any window describing what the strategy had seen — and nothing
            # below can decide an entry without it being the window's last bar.
            window.append(candle)

            outcome = _step(
                index=index,
                candle=candle,
                window=window,
                timeframe=timeframe,
                instrument=instrument,
                strategy=strategy,
                broker=broker,
                risk=risk,
            )

        yield outcome

        previous = candle


def _step(  # noqa: PLR0913 — one bar of the loop; every argument is a seam or the bar itself
    *,
    index: int,
    candle: Candle,
    window: deque[Candle],
    timeframe: dt.timedelta,
    instrument: InstrumentSpec,
    strategy: Strategy,
    broker: Broker,
    risk: RiskManager,
) -> BarOutcome:
    """One bar, five ordered steps. The order is the anti-lookahead rule — see the module
    docstring. Runs inside `ENGINE_CONTEXT`; its caller is responsible for that."""
    # 1. The bar arrives. Whatever was decided on an earlier bar executes now, inside
    #    this one, at a price the strategy had not seen when it decided. This is the
    #    ONLY place a fill can be born.
    born: list[Fill] = []
    for fill in broker.on_bar(candle):
        _reject_lookahead(fill, candle, timeframe)
        _reject_foreign_symbol(fill, instrument)
        born.append(fill)

    # 2. Only now is the strategy allowed to look at this candle — and at nothing else.
    #    The account is read once: against a live terminal, two reads are two round
    #    trips that can disagree, and the equity curve would stop being what the
    #    strategy saw.
    account = broker.account()
    context = Context(
        candle=candle,
        instrument=instrument,
        account=account,
        position=_open_position(broker, instrument.symbol),
        # This bar's fills, not the run's: what step 1 just did is part of what the
        # bar revealed, and it is the only way a strategy learns of a trade that
        # opened and died inside one bar (ADR-0015).
        fills=tuple(born),
    )

    # 3-4. Intent -> size -> veto -> queue. Never intent -> fill.
    for signal in strategy.on_bar(context):
        # A cancel is the one intent that never becomes an order — it withdraws one. It
        # skips sizing and the veto for the same reason an exit skips sizing: there is
        # nothing to size, and a risk manager that could refuse a cancel would be a risk
        # manager that keeps an order alive after the strategy disowned it.
        if signal.kind is SignalKind.CANCEL:
            # `Signal.__post_init__` has already refused a cancel with no name, so this
            # is a `str`. Narrowing it again would add a branch no test could enter.
            broker.cancel(cast(str, signal.client_id))
            continue

        # A stop modification skips sizing and the veto for the same reasons, and adds one
        # of its own: **the decision instant is stamped here, not by the strategy**. It is
        # this candle's opening time, so a stop decided on this bar's close cannot exit
        # inside this bar — `_reject_lookahead` checks it on the way out. The loop already
        # guarantees that by ordering (`broker.on_bar` ran before the strategy saw the
        # bar), but ordering is a fact about these lines; the stamp is a fact the engine
        # can verify. See ADR-0018.
        if signal.kind is SignalKind.MODIFY_STOP:
            # `Signal.__post_init__` refuses a MODIFY_STOP with no level, same as a
            # nameless cancel — so this is a `Money`.
            moved = broker.modify_stop(
                instrument.symbol, cast(Money, signal.stop_loss), candle.time
            )
            # Refused. Usually a strategy trailing a stop onto an entry that has not
            # filled yet; but a broker holding a position it keeps no protective level for
            # refuses too, and from here the two are the same answer. So the line reports
            # the refusal and stops there — naming a cause the loop never checked is how a
            # log turns into a false lead. Silence is not an option either: it looks
            # exactly like a trailing rule that ran and worked, which is the one difference
            # the author is trying to see. Same reason `_to_order` logs an exit with no
            # open position.
            if not moved:
                logger.debug(
                    "broker refused the stop modification at %s (%s)",
                    candle.time,
                    signal.reason,
                )
            continue

        order = _to_order(signal, context, instrument, risk, window)
        if order is None:
            continue
        if not risk.allow(order, account):
            logger.debug("risk manager vetoed %s at %s", order.reason, candle.time)
            continue
        result = broker.submit(order)
        # A refusal is the broker declining to take the order at all — a duplicate name, an
        # order it cannot rest. Silence here would look exactly like a trade that simply
        # never triggered, which is the one failure the strategy author cannot debug.
        if not result.accepted:
            logger.debug("broker refused %s at %s: %s", order.reason, candle.time, result.reason)

    # 5. The account is worth what it is worth at the close of this bar.
    #
    # ⚠️ `account` is the one read in step 2, deliberately — not a fresh one taken here. The
    # curve is meant to be what the strategy was looking at when it decided, and against a
    # live terminal a second read is a second round trip that can disagree with the first.
    return BarOutcome(
        index=index,
        candle=candle,
        fills=tuple(born),
        equity=EquityPoint(time=candle.time, equity=account.equity),
    )


def _to_order(
    signal: Signal,
    context: Context,
    instrument: InstrumentSpec,
    risk: RiskManager,
    window: Iterable[Candle],
) -> OrderRequest | None:
    """Turn intent into a sized order, or into nothing.

    An exit is not sized: you close the position you have, all of it. Running an exit through
    the risk manager is how a stop-loss ends up rejected for exceeding the daily loss limit —
    the position closing *is* the thing that stops the loss.
    """
    if signal.kind is SignalKind.EXIT:
        position = context.position
        if position is None:
            logger.debug("exit signal with no open position at %s", context.candle.time)
            return None

        # An exit does not rest at a price: the broker's own protective levels are the only
        # thing that closes a position at a level, and two paths closing one position is where
        # the ledger stops adding up (`BacktestBroker._reject_resting`). Said out loud, because
        # an exit that quietly became a market order is a strategy measuring something else.
        if signal.limit_price is not None or signal.stop_price is not None:
            logger.debug(
                "exit signal at %s carries a resting price; exits fill at the open",
                context.candle.time,
            )

        return OrderRequest(
            symbol=instrument.symbol,
            side=position.side,
            intent=SignalKind.EXIT,
            volume=position.volume,
            decided_at=context.candle.time,
            reason=signal.reason,
        )

    volume = risk.size(signal, context.account, instrument)
    if volume <= ZERO:
        logger.debug("sizing returned %s at %s; no trade", volume, context.candle.time)
        return None

    return OrderRequest(
        symbol=instrument.symbol,
        side=signal.side,
        intent=SignalKind.ENTRY,
        volume=volume,
        decided_at=context.candle.time,
        stop_loss=signal.stop_loss,
        take_profit=signal.take_profit,
        reason=signal.reason,
        context=signal.context,
        # Entries only. An exit has already returned above, and neither a cancel nor a stop
        # modification ever becomes an order — so every window built here ends on the bar the
        # strategy is being shown right now, which is what `decided_at` says one line up.
        snapshot=EntrySnapshot(
            bars=tuple(window),
            decided_at=context.candle.time,
            regions=signal.regions,
            series=signal.series,
            levels=signal.levels,
        ),
        limit_price=signal.limit_price,
        stop_price=signal.stop_price,
        client_id=signal.client_id,
    )


def _open_position(broker: Broker, symbol: str) -> Position | None:
    """The position **in the symbol being traded**, and nothing else.

    A live account holds positions the strategy never opened: another expert advisor's, a
    manual trade, a different instrument entirely. Taking `positions()[0]` would build an
    exit order out of somebody else's gold short — with its side and its volume — and send
    it, to a real broker, as an order to close that many lots of EURUSD.
    """
    positions = [position for position in broker.positions(symbol) if position.symbol == symbol]
    if not positions:
        return None
    if len(positions) > 1:
        raise EngineError(
            f"{len(positions)} open positions in {symbol}; phase 1 holds one at a time"
        )
    return positions[0]


def _reject_lookahead(fill: Fill, candle: Candle, timeframe: dt.timedelta) -> None:
    """The engine polices its own invariant, on every fill, whatever produced it.

    Two rules, and each catches a different bug that produces a plausible, wrong backtest:

    **A floor.** The order must have been decided on a bar strictly *before* the one being
    filled. `decided_at` is the opening instant of the candle the strategy saw, so a fill
    anywhere inside that same candle — including at its close, the price the strategy was
    literally looking at — is the classic "decide on the breakout, fill on the breakout".

    **A ceiling.** The fill must land inside the bar being processed. A broker with an
    off-by-one in its index — `candles[i+1].open` instead of `candles[i].open`, the single
    most common bug in a backtest broker — fills at a price from the future, and nothing
    about the resulting equity curve looks wrong. It is simply too good.
    """
    bar_end = candle.time + timeframe

    if fill.order.decided_at >= candle.time:
        raise LookaheadError(
            f"fill on the candle at {candle.time} for an order decided at "
            f"{fill.order.decided_at}: a decision taken on the close of a candle executes "
            f"at the open of the next one, never within the candle it was taken from "
            f"(AGENTS.md §5.1)"
        )

    if not (candle.time <= fill.time < bar_end):
        raise LookaheadError(
            f"fill timestamped {fill.time}, outside the candle being processed "
            f"[{candle.time}, {bar_end}): a fill belongs to the bar that produced it, and "
            f"a broker reaching past it is reading prices that have not happened"
        )

    # **A range.** A fill at a price the bar never traded at is the most expensive fantasy in
    # backtesting: "the stop always fills at the stop level". Price gaps below the stop at
    # the open, the broker books the fill at the stop anyway, and the backtest reports a loss
    # that the market would never have given you. This does not — cannot — demand
    # `price == candle.open`, because a stop hit intrabar legitimately fills somewhere else
    # inside the bar. It only demands that the price existed.
    if not (candle.low <= fill.price <= candle.high):
        raise LookaheadError(
            f"fill at {fill.price}, outside the bar's range "
            f"[{candle.low}, {candle.high}] at {candle.time}: nobody traded there"
        )


def _reject_foreign_symbol(fill: Fill, instrument: InstrumentSpec) -> None:
    """A fill for a symbol this run is not trading is somebody else's trade."""
    if fill.order.symbol != instrument.symbol:
        raise EngineError(
            f"fill for {fill.order.symbol} while running {instrument.symbol}: "
            f"this run's ledger is not the place for another instrument's trade"
        )


def _reject_out_of_order(previous: Candle | None, candle: Candle, timeframe: dt.timedelta) -> None:
    """Candles must arrive forward in time, **spaced by the timeframe they claim to be**.

    The first half is obvious: a replayed or duplicated bar lets a strategy act twice on the
    same information, which is lookahead wearing a different hat. Sorted here, a broken data
    source would keep working; refused, it gets fixed.

    The second half is not obvious, and it is what makes the lookahead ceiling worth
    anything. That ceiling is `[candle.time, candle.time + timeframe)` — so it is only as
    tight as `timeframe` is *true*. Hand `run()` an H1 timeframe over an M5 stream and the
    ceiling dilates to cover the next twelve bars: a broker filling one bar into the future
    sails straight through it, and the guard that was written not to trust the broker ends up
    trusting the caller instead — who is the one who actually gets this wrong. The worker in
    PR-107 will read the timeframe from a saved strategy and the candles from Parquet; those
    two are not the same source, and nothing but this line makes them agree.

    A gap must be a whole number of bars. That admits the weekend (Friday 21:00 to Monday
    00:00 is 51 hours — 51 H1 bars) and refuses a stream whose spacing the timeframe does not
    describe.
    """
    if previous is None:
        return

    if candle.time <= previous.time:
        raise LookaheadError(
            f"candle at {candle.time} arrived after {previous.time}: "
            f"the engine consumes a strictly increasing stream of closed candles"
        )

    gap = candle.time - previous.time
    if gap % timeframe != dt.timedelta(0):
        raise EngineError(
            f"candles {gap} apart under a {timeframe} timeframe: the stream and the "
            f"timeframe disagree, and the lookahead ceiling is only as tight as they agree"
        )
