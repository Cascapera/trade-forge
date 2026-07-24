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
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, localcontext
from decimal import Context as DecimalContext
from typing import cast

from tradeforge_engine.domain import (
    ZERO,
    AccountState,
    Candle,
    ClosedTrade,
    Context,
    EquityPoint,
    Fill,
    InstrumentSpec,
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


@dataclass(frozen=True, slots=True)
class RunResult:
    """What a run produced. Frozen, so a caller cannot doctor the record."""

    fills: tuple[Fill, ...]
    trades: tuple[ClosedTrade, ...]
    equity_curve: tuple[EquityPoint, ...]
    final_account: AccountState
    candles_processed: int


def run(  # noqa: PLR0913 — keyword-only; each one names a real axis of a backtest
    *,
    candles: Iterable[Candle],
    timeframe: dt.timedelta,
    instrument: InstrumentSpec,
    strategy: Strategy,
    broker: Broker,
    risk: RiskManager,
) -> RunResult:
    """Drive a strategy over a stream of closed candles.

    `timeframe` is how long one bar lasts, and the engine needs it to police the broker: a
    fill belongs to the bar being processed, and "the bar being processed" is `[candle.time,
    candle.time + timeframe)`. Without it, the guard has no ceiling and a broker that fills
    a week late passes unnoticed.

    Keyword-only, because five positional objects is four chances to swap two of them and
    still have the backtest run.

    Takes an `Iterable`, not a list: ten years of M1 is five million bars, and the engine has
    no reason to hold them all at once.
    """
    if timeframe <= dt.timedelta(0):
        raise ValueError(f"timeframe must be positive, got {timeframe}")

    with localcontext(ENGINE_CONTEXT):
        return _run(
            candles=candles,
            timeframe=timeframe,
            instrument=instrument,
            strategy=strategy,
            broker=broker,
            risk=risk,
        )


def _run(  # noqa: PLR0913 — see run()
    *,
    candles: Iterable[Candle],
    timeframe: dt.timedelta,
    instrument: InstrumentSpec,
    strategy: Strategy,
    broker: Broker,
    risk: RiskManager,
) -> RunResult:
    fills: list[Fill] = []
    equity_curve: list[EquityPoint] = []
    processed = 0
    previous: Candle | None = None

    for candle in candles:
        _reject_out_of_order(previous, candle, timeframe)

        # 1. The bar arrives. Whatever was decided on an earlier bar executes now, inside
        #    this one, at a price the strategy had not seen when it decided. This is the
        #    ONLY place a fill can be born.
        born: list[Fill] = []
        for fill in broker.on_bar(candle):
            _reject_lookahead(fill, candle, timeframe)
            _reject_foreign_symbol(fill, instrument)
            born.append(fill)
        fills.extend(born)

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

            order = _to_order(signal, context, instrument, risk)
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
                logger.debug(
                    "broker refused %s at %s: %s", order.reason, candle.time, result.reason
                )

        # 5. The account is worth what it is worth at the close of this bar.
        equity_curve.append(EquityPoint(time=candle.time, equity=account.equity))

        previous = candle
        processed += 1

    return RunResult(
        fills=tuple(fills),
        trades=tuple(broker.trades()),
        equity_curve=tuple(equity_curve),
        final_account=broker.account(),
        candles_processed=processed,
    )


def _to_order(
    signal: Signal,
    context: Context,
    instrument: InstrumentSpec,
    risk: RiskManager,
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


__all__ = ["ENGINE_CONTEXT", "ENGINE_PRECISION", "RunResult", "run"]
