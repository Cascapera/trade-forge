"""`BothSides`: a long and a short of one swing setup over one account, one position at a time.

Two halves of tests, for the reason the class docstring measures. The real setups cannot reach the
referee's two rules — no pair of them is ever armed at once, and neither ever asks to move a stop on
the other's trade — so those rules are driven with **scripted** halves that say exactly what a half
would have to say to reach them. The real setups are then driven through the real loop, where what
has to hold is the opposite claim: until the other side trades, `both` is invisible.
"""

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, localcontext

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.domain import (
    ZERO,
    Candle,
    Context,
    Fill,
    OrderRequest,
    Position,
    Side,
    Signal,
    SignalKind,
)
from tradeforge_engine.indicators import EMA
from tradeforge_engine.loop import ENGINE_CONTEXT, RunResult, run
from tradeforge_engine.protocols import Indicator
from tradeforge_engine.risk import PercentRiskManager
from tradeforge_engine.swing import (
    BothSides,
    Mme9BreakoutStrategy,
    Mme9FailedTurnStrategy,
    Mme9PullbackStrategy,
    Mme9TurnStrategy,
    PontoContinuoStrategy,
)
from tradeforge_engine.testing import AAPL, HOUR, ImmediateFillBroker, bar

_ACCOUNT = ImmediateFillBroker(instrument=AAPL).account()

# --------------------------------------------------------------------------- #
# The referee, with scripted halves                                             #
# --------------------------------------------------------------------------- #


class _Scripted:
    """A half that says, on each bar, exactly what the test tells it to — and remembers what it
    was shown. Enough to reach the rules the real setups never reach."""

    def __init__(self, side: Side, script: Mapping[int, Sequence[Signal]] | None = None) -> None:
        self._side = side
        self._script = script or {}
        self._average = EMA(period=3, source="close")
        self.seen: list[Context] = []

    def overlays(self) -> Mapping[str, Indicator]:
        return {f"{self._side} EMA 3": self._average}

    def on_bar(self, context: Context) -> tuple[Signal, ...]:
        index = len(self.seen)
        self.seen.append(context)
        return tuple(self._script.get(index, ()))


def _entry(side: Side, client_id: str) -> Signal:
    stop = Decimal(90) if side is Side.LONG else Decimal(110)
    trigger = Decimal(101) if side is Side.LONG else Decimal(99)
    return Signal(
        kind=SignalKind.ENTRY,
        side=side,
        reference_price=Decimal(100),
        stop_loss=stop,
        stop_price=trigger,
        reason="entry.scripted",
        client_id=client_id,
    )


def _cancel(side: Side, client_id: str) -> Signal:
    return Signal(
        kind=SignalKind.CANCEL,
        side=side,
        reference_price=Decimal(100),
        reason="cancel.scripted",
        client_id=client_id,
    )


def _stop_move(side: Side) -> Signal:
    return Signal(
        kind=SignalKind.MODIFY_STOP,
        side=side,
        reference_price=Decimal(100),
        stop_loss=Decimal(95) if side is Side.LONG else Decimal(105),
        reason="trail.scripted",
    )


def _open(side: Side) -> Position:
    return Position(
        symbol=AAPL.symbol,
        side=side,
        volume=Decimal(1),
        entry_price=Decimal(100),
        entry_time=bar(0, open_="100", close="100").time,
    )


def _fill(client_id: str, side: Side) -> Fill:
    candle = bar(0, open_="100", close="100")
    order = OrderRequest(
        symbol=AAPL.symbol,
        side=side,
        intent=SignalKind.ENTRY,
        volume=Decimal(1),
        decided_at=candle.time,
        client_id=client_id,
    )
    return Fill(order=order, time=candle.time, price=candle.close, volume=Decimal(1), costs=ZERO)


def _drive(
    both: BothSides,
    bars: int,
    *,
    positions: Mapping[int, Position] | None = None,
    fills: Mapping[int, Sequence[Fill]] | None = None,
) -> list[list[Signal]]:
    """One context per bar, with the position and the fills the test says that bar carries."""
    positions = positions or {}
    fills = fills or {}
    out: list[list[Signal]] = []
    with localcontext(ENGINE_CONTEXT):
        for index in range(bars):
            context = Context(
                # Open and close apart, so a cancel stamped with the wrong one of the two is caught.
                candle=bar(index, open_="99", close="100"),
                instrument=AAPL,
                account=_ACCOUNT,
                position=positions.get(index),
                fills=tuple(fills.get(index, ())),
            )
            out.append(list(both.on_bar(context)))
    return out


def _withdrawals(signals: list[Signal]) -> list[tuple[Side, str | None]]:
    return [(s.side, s.client_id) for s in signals if s.reason == "cancel.other-side-filled"]


def test_every_bar_reaches_both_halves_position_or_not() -> None:
    """The averages inside each half have to see every bar — one fed only on the bars that reached
    it is a different average. A position belongs to one half and still reaches the other."""
    long, short = _Scripted(Side.LONG), _Scripted(Side.SHORT)
    _drive(BothSides(long=long, short=short), 4, positions={1: _open(Side.LONG)})
    assert [c.candle.time for c in long.seen] == [c.candle.time for c in short.seen]
    assert len(long.seen) == 4
    # And the context is passed whole: the half that does not own the trade is shown it anyway,
    # which is what keeps it from arming beside it.
    assert short.seen[1].position is not None


@pytest.mark.parametrize(("owner", "other"), [(Side.LONG, Side.SHORT), (Side.SHORT, Side.LONG)])
def test_the_other_sides_order_is_withdrawn_when_a_position_opens(owner: Side, other: Side) -> None:
    """His rule (22/09): one side fills, the other side's order comes back. The other half has
    just forgotten it — it read the position as its own fill — so this is the only place left that
    knows the name."""
    halves = {
        owner: _Scripted(owner, {0: [_entry(owner, "own-1")]}),
        other: _Scripted(other, {0: [_entry(other, "other-1")]}),
    }
    out = _drive(
        BothSides(long=halves[Side.LONG], short=halves[Side.SHORT]),
        3,
        positions={1: _open(owner), 2: _open(owner)},
        fills={1: [_fill("own-1", owner)]},
    )
    assert _withdrawals(out[0]) == []  # flat: both orders rest, as they would at a venue
    assert _withdrawals(out[1]) == [(other, "other-1")]
    assert _withdrawals(out[2]) == []  # once, not on every bar the trade stays open
    # Stamped with this bar's close, the same reference `_withdraw` gives a half's own cancel.
    [cancel] = [s for s in out[1] if s.kind is SignalKind.CANCEL]
    assert cancel.reference_price == Decimal(100)


def test_the_owners_order_is_not_withdrawn_even_without_its_fill_on_the_bar() -> None:
    """A position the owner was never shown a fill for (the ADR-0015 fallback) is still its own
    order become a trade. Withdrawing that name would cancel what the trade already consumed."""
    long = _Scripted(Side.LONG, {0: [_entry(Side.LONG, "own-1")]})
    out = _drive(
        BothSides(long=long, short=_Scripted(Side.SHORT)), 3, positions={1: _open(Side.LONG)}
    )
    assert all(_withdrawals(signals) == [] for signals in out)


def test_an_order_its_half_already_withdrew_is_not_withdrawn_again() -> None:
    short = _Scripted(Side.SHORT, {0: [_entry(Side.SHORT, "s-1")], 1: [_cancel(Side.SHORT, "s-1")]})
    out = _drive(
        BothSides(long=_Scripted(Side.LONG), short=short), 3, positions={2: _open(Side.LONG)}
    )
    assert [s.client_id for s in out[1]] == ["s-1"]  # the half's own cancel passes through
    assert _withdrawals(out[2]) == []


def test_an_order_that_filled_is_forgotten_before_the_position_changes_hands() -> None:
    """The short fills and the trade dies inside one bar; later a long opens. The short's name was
    consumed by its own fill, so the long's position must not produce a cancel for it."""
    short = _Scripted(Side.SHORT, {0: [_entry(Side.SHORT, "s-1")]})
    out = _drive(
        BothSides(long=_Scripted(Side.LONG), short=short),
        3,
        positions={2: _open(Side.LONG)},
        fills={1: [_fill("s-1", Side.SHORT)]},
    )
    assert all(_withdrawals(signals) == [] for signals in out)


def test_a_trade_that_opened_and_died_inside_one_bar_leaves_the_other_order_resting() -> None:
    """The long fills and is stopped out inside bar 1, so bar 1's context is flat. The short half
    never saw a position and still believes in its order — withdrawing it here would leave it
    believing in one the broker no longer holds, the ADR-0023 phantom in reverse."""
    long = _Scripted(Side.LONG, {0: [_entry(Side.LONG, "l-1")]})
    short = _Scripted(Side.SHORT, {0: [_entry(Side.SHORT, "s-1")]})
    out = _drive(BothSides(long=long, short=short), 2, fills={1: [_fill("l-1", Side.LONG)]})
    assert _withdrawals(out[1]) == []


def test_a_stop_move_for_the_other_sides_trade_is_dropped_and_the_owners_passes() -> None:
    long = _Scripted(Side.LONG, {0: [_stop_move(Side.LONG)]})
    short = _Scripted(Side.SHORT, {0: [_stop_move(Side.SHORT)]})
    [out] = _drive(BothSides(long=long, short=short), 1, positions={0: _open(Side.LONG)})
    assert [(s.kind, s.side) for s in out] == [(SignalKind.MODIFY_STOP, Side.LONG)]


def test_a_stop_move_with_no_position_at_all_is_dropped() -> None:
    """Nobody's trade: the loop would have nothing to move, and neither half conducts while flat —
    a half that did would be asking for a stop on a position that does not exist."""
    long = _Scripted(Side.LONG, {0: [_stop_move(Side.LONG)]})
    [out] = _drive(BothSides(long=long, short=_Scripted(Side.SHORT)), 1)
    assert out == []


def test_entries_and_cancels_pass_through_untouched_while_flat() -> None:
    long = _Scripted(Side.LONG, {0: [_entry(Side.LONG, "l-1")], 1: [_cancel(Side.LONG, "l-1")]})
    short = _Scripted(Side.SHORT, {0: [_entry(Side.SHORT, "s-1")]})
    out = _drive(BothSides(long=long, short=short), 2)
    assert [s.client_id for s in out[0]] == ["l-1", "s-1"]  # long first, then short, every bar
    assert [s.client_id for s in out[1]] == ["l-1"]


def test_the_curves_are_the_long_halfs() -> None:
    """Both halves read the same averages under the same labels, so a chart is drawn from one of
    them — the long, always, so the answer does not depend on which half traded."""
    long, short = _Scripted(Side.LONG), _Scripted(Side.SHORT)
    assert list(BothSides(long=long, short=short).overlays()) == ["long EMA 3"]


def test_one_object_cannot_be_both_halves() -> None:
    half = _Scripted(Side.LONG)
    with pytest.raises(ValueError, match="two instances"):
        BothSides(long=half, short=half)


# --------------------------------------------------------------------------- #
# The real setups, through the real loop                                        #
# --------------------------------------------------------------------------- #


def _run(strategy: object, candles: list[Candle]) -> RunResult:
    broker = BacktestBroker(
        instrument=AAPL, initial_capital=Decimal(100_000), take_profit_rr=Decimal(2)
    )
    return run(
        candles=candles,
        timeframe=HOUR,
        instrument=AAPL,
        strategy=strategy,  # type: ignore[arg-type]
        broker=broker,
        risk=PercentRiskManager(percent=Decimal(1)),
    )


def _breakout(side: Side, name: str = "mme9") -> Mme9BreakoutStrategy:
    return Mme9BreakoutStrategy(side=side, period=3, name=name)


def _both_breakouts() -> BothSides:
    return BothSides(
        long=_breakout(Side.LONG, "mme9-long"), short=_breakout(Side.SHORT, "mme9-short")
    )


# The long scenario of `test_swing.py` (entry at 101.3, conducted out at 101.90 on bar 7), run on
# for two bars so the short side has room to trade once the account is flat again.
_UP_THEN_DOWN = [
    bar(0, open_="100", close="100", high="100.5", low="99.5"),
    bar(1, open_="100", close="99", high="100.2", low="98.8"),
    bar(2, open_="99", close="98", high="99.2", low="97.5"),  # MME3 seeds at 99
    bar(3, open_="98", close="101", high="101.3", low="97.9"),  # long reference
    bar(4, open_="101", close="103", high="103.5", low="100.8"),  # long fills at 101.3
    bar(5, open_="103", close="105", high="105.2", low="102.9"),
    bar(6, open_="105", close="102", high="105.4", low="101.9"),  # under the MME3: stop to 101.90
    bar(7, open_="102", close="97", high="102.2", low="96.9"),  # long out; short reference
    bar(8, open_="97", close="95", high="97.1", low="94.8"),  # short fills at 96.9
    bar(9, open_="95", close="94", high="95.3", low="93.5"),
]


def _shape(fill: Fill) -> tuple[object, ...]:
    """A fill without its name — the halves are renamed, and the name is not the trade."""
    order = fill.order
    return (fill.time, order.intent, order.side, fill.price, order.stop_loss, order.reason)


def test_both_trades_the_long_exactly_as_long_only_and_the_short_once_flat() -> None:
    """Golden numbers from feeding these bars to the engine.

    The long is the long-only run's trade to the cent, conducted stop included. The short is
    **not** the short-only run's: alone, the short arms on bar 6 (the first close under the average)
    and fills at 101.9 on bar 7. Under `both`, bar 6 finds the long still open, so nothing arms
    beside it — his one-position-at-a-time rule — and the short waits for bar 7's close, the first
    the account is flat on, and enters at its low.
    """
    with localcontext(ENGINE_CONTEXT):
        both = _run(_both_breakouts(), _UP_THEN_DOWN)
        long_only = _run(_breakout(Side.LONG), _UP_THEN_DOWN)
        short_only = _run(_breakout(Side.SHORT), _UP_THEN_DOWN)

    assert [_shape(f)[:5] for f in both.fills[:2]] == [_shape(f)[:5] for f in long_only.fills]
    [_, _, short_entry] = both.fills
    assert short_entry.order.side is Side.SHORT
    assert short_entry.time == _UP_THEN_DOWN[8].time
    assert short_entry.price == Decimal("96.9")
    assert short_entry.order.stop_loss == Decimal("102.20")

    [alone, _] = short_only.fills
    assert alone.time == _UP_THEN_DOWN[7].time
    assert alone.price == Decimal("101.9")


def test_each_half_names_its_orders_with_its_own_name() -> None:
    with localcontext(ENGINE_CONTEXT):
        both = _run(_both_breakouts(), _UP_THEN_DOWN)
    entries = [f.order for f in both.fills if f.order.intent is SignalKind.ENTRY]
    assert [(o.side, o.reason) for o in entries] == [
        (Side.LONG, "entry.mme9-long"),
        (Side.SHORT, "entry.mme9-short"),
    ]
    assert all(o.client_id is not None for o in entries)
    assert entries[0].client_id.startswith("mme9-long-")  # type: ignore[union-attr]
    assert entries[1].client_id.startswith("mme9-short-")  # type: ignore[union-attr]


# Every swing setup, built as the factory builds a half: one class, one side, one name.
_SETUPS: dict[str, Callable[[Side, str], object]] = {
    "mme9_breakout": lambda side, name: Mme9BreakoutStrategy(side=side, period=3, name=name),
    "mme9_turn": lambda side, name: Mme9TurnStrategy(side=side, period=3, name=name),
    "mme9_failed_turn": lambda side, name: Mme9FailedTurnStrategy(side=side, period=3, name=name),
    "mme9_pullback": lambda side, name: Mme9PullbackStrategy(side=side, period=3, name=name),
    "ponto_continuo": lambda side, name: PontoContinuoStrategy(side=side, period=3, name=name),
}


@st.composite
def _random_candles(draw: st.DrawFn) -> list[Candle]:
    """Valid candles, long enough for several turns of an MME3 in both directions."""
    count = draw(st.integers(min_value=8, max_value=60))
    price = st.decimals(min_value="80", max_value="120", places=1)
    wick = st.decimals(min_value="0", max_value="3", places=1)
    candles: list[Candle] = []
    for index in range(count):
        open_, close = draw(price), draw(price)
        high = max(open_, close) + draw(wick)
        low = min(open_, close) - draw(wick)
        candles.append(bar(index, open_=str(open_), close=str(close), high=str(high), low=str(low)))
    return candles


@pytest.mark.parametrize("kind", list(_SETUPS))
@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@settings(max_examples=60, deadline=None)
@given(candles=_random_candles())
def test_both_is_invisible_until_the_other_side_trades(
    kind: str, side: Side, candles: list[Candle]
) -> None:
    """Until the other half's first entry fills, a `both` run is the one-sided run, fill for fill.

    This is the whole claim the composition makes about everything *before* the two sides meet:
    the other half sees every bar and says nothing that reaches the account — no order that fills,
    no stop moved on a trade it does not own, no name that collides. Any leak shows up as a first
    divergent fill. After the other side's entry the runs part company, as they must: from then on
    one position at a time is doing its job.
    """
    make = _SETUPS[kind]
    other = Side.SHORT if side is Side.LONG else Side.LONG
    halves = {side: make(side, f"{kind}-{side}"), other: make(other, f"{kind}-{other}")}
    with localcontext(ENGINE_CONTEXT):
        both = _run(BothSides(long=halves[Side.LONG], short=halves[Side.SHORT]), candles)  # type: ignore[arg-type]
        alone = _run(make(side, kind), candles)

    # The first fill of the other side, **whatever its intent**: a trade that opens and is stopped
    # inside one bar lists its exit before its entry (exits are settled first on a bar), so cutting
    # at the other side's first *entry* would keep its exit on the "before" side of the line.
    crossed = next((i for i, f in enumerate(both.fills) if f.order.side is other), len(both.fills))
    before = [_shape(f)[:5] for f in both.fills[:crossed]]
    assert before == [_shape(f)[:5] for f in alone.fills[: len(before)]]
    if crossed == len(both.fills):
        # The other side never traded: the runs must be the same run, to the last fill.
        assert len(both.fills) == len(alone.fills)
