"""Test doubles for the things that plug into the engine.

Shipped with the package rather than hidden in a test folder, for two reasons. The engine's
tests need them; so will PR-104's and PR-105's, and a helper that three suites copy is a
helper that drifts into three versions. And anyone writing their own `Broker` or
`RiskManager` against these protocols gets a working reference implementation — which is
what an interface is *for*.

Nothing here inherits from anything. `ImmediateFillBroker` satisfies `Broker` by having the
right methods and no other relationship to the engine at all.
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from tradeforge_engine.domain import (
    ZERO,
    AccountState,
    AssetClass,
    Candle,
    ClosedTrade,
    Context,
    Fill,
    InstrumentSpec,
    Money,
    OrderRequest,
    OrderResult,
    Position,
    Side,
    Signal,
    SignalKind,
    Volume,
)
from tradeforge_engine.portfolio import Portfolio

EURUSD = InstrumentSpec(
    symbol="EURUSD",
    name="Euro vs US Dollar",
    asset_class=AssetClass.FOREX,
    currency_base="EUR",
    currency_quote="USD",
    tick_size=Decimal("0.00001"),
    tick_value=Decimal("1"),
    contract_size=Decimal("100000"),
    digits=5,
)

AAPL = InstrumentSpec(
    symbol="AAPL",
    name="Apple Inc.",
    asset_class=AssetClass.STOCK,
    exchange="NASDAQ",
    currency_quote="USD",
    tick_size=Decimal("0.01"),
    tick_value=Decimal("0.01"),
    contract_size=Decimal("1"),
    digits=2,
)

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)


def bar(
    index: int,
    *,
    open_: str,
    close: str,
    high: str | None = None,
    low: str | None = None,
) -> Candle:
    """One candle, `index` hours after the start."""
    body = [Decimal(open_), Decimal(close)]
    return Candle(
        time=START + index * HOUR,
        open=Decimal(open_),
        high=Decimal(high) if high else max(body),
        low=Decimal(low) if low else min(body),
        close=Decimal(close),
    )


BULLISH_START = [
    bar(-8, open_="92", close="91", high="92", low="90"),
    bar(-7, open_="91", close="90", high="91", low="89"),  # falling
    bar(-6, open_="90", close="89", high="90", low="88"),  # falling -> bottom 88
    bar(-5, open_="89", close="90", high="91", low="89"),  # up 1
    bar(-4, open_="90", close="91", high="92", low="90"),  # up 2 -> arms the bottom at 88
    bar(-3, open_="91", close="90", high="91", low="89"),
    bar(-2, open_="90", close="87", high="90", low="86"),  # close 87 < 88 -> bearish BOS
    bar(-1, open_="88", close="93", high="94", low="88"),  # close 93 > 92 -> bullish CHoCH
]
"""The toll a rising scenario has to pay before `MarketStructure` can read it at all.

`MarketStructure` is a transcription of the author's indicator, and it starts where the indicator
starts, at `DIR = -1`. So the only event a fresh machine can possibly report is a *bearish* BOS,
and a scenario that rises from bar 0 never leaves the starting gate — it confirms nothing, marks
no zone, and offers a setup nothing to qualify. That is not a bug to work around: an uptrend's
structure is only meaningful once there is an uptrend, and the machine insists on being shown one.

These eight bars are the shortest honest route into one: a bearish BOS on bar -2 (close 87 through
the armed bottom of 88), then a bullish CHoCH on bar -1 (close 93 through the 92 the down-move
came from). `test_the_bullish_start_is_a_bearish_bos_then_a_bullish_choch` pins exactly that, so
this is a stated fact and not hidden state.

Two properties make it safe to prepend to a scenario, and both are load-bearing:

* **The bars carry negative numbers.** `bar()` stamps `START + index * HOUR`, so these sit in the
  hour before bar 0 and every scenario keeps its own numbering. No golden had to be re-measured to
  accommodate the prefix, which is the whole reason it is shaped this way — a prefix that shifted
  the bars would mean rewriting the expectations, and a rewritten expectation proves nothing.
* **Nothing it leaves behind can decide a scenario's events.** Two residues could: the bearish
  CHoCH anchor at 86 and the running high at 94. The anchor is either out of reach or replaced by
  the scenario's own first break long before anything gets near it. The running high cannot be
  armed at all, and that one is structural rather than a matter of levels: arming reads `falling`,
  which requires `previous.high < before.high` — for bar 0 that is bar -1's 94 against bar -2's
  90, which is false. There is no path by which this prefix arms a bullish BOS of its own.

Lives here rather than in a test file because two suites need it, and by this module's own rule a
helper that two suites copy is a helper that drifts into two versions.
"""

BULLISH_START_EMA3 = [
    *BULLISH_START[:6],
    bar(-2, open_="90", close="87.75", high="90", low="86"),  # close below 88 -> bearish BOS
    bar(-1, open_="88", close="95", high="96", low="88"),  # close above 92 -> bullish CHoCH
]
"""`BULLISH_START`, re-closed so that it is invisible to a three-period moving average.

Same eight bars, same two events, same levels — only the last two closes differ. They have to,
because a scenario judged against an average cannot afford a prefix that moves it, and every
golden of the Ponto Contínuo documents its average bar by bar.

The arithmetic that makes one variant enough: `EMA` seeds with the simple mean of the first
`period` closes and is a first-order recurrence thereafter, so the whole of a prefixed stream's
average chain depends on a **single** quantity — the value the average holds on bar -1. Left at
exactly 92 there, the ordinary updates on bars 0, 1 and 2 of a stream closing 100, 104, 108
reproduce 96, 100 and 104 — and 104 is precisely the seed that stream started from unprefixed. Bar
2 onward is then identical to the last digit. Closing bar -2 at 87.75 and bar -1 at 95 is what
lands it there, and both still do their structural job: 87.75 is under the armed bottom of 88, and
95 is over the 92 the down-move came from.

⚠️ **Tuned to a period of 3 and to a stream opening at those closes.** It is not a general-purpose
"average-safe" prefix, and under any other period it moves the average like the plain one does.
`test_the_structure_warm_up_leaves_the_average_alone` asserts the result rather than trusting the
arithmetic above, and is the thing to re-run if either ever changes.

One difference it does not hide: the average finishes **warming up** during the prefix, so a
prefixed stream has readings on bars 0 and 1 that the unprefixed one had none for. That is a curve
reaching further left, not a curve at different heights.
"""


GAPPING_IMPULSE = [
    bar(0, open_="122", close="122", high="123", low="120"),  # top 123
    bar(1, open_="119", close="119", high="122", low="118"),  # correction 1
    bar(2, open_="117", close="117", high="121", low="116"),  # correction 2 -> armed
    bar(3, open_="99", close="99", high="100", low="98"),  # impulse starts; origin low 98
    bar(4, open_="104", close="104", high="105", low="103"),
    bar(5, open_="108", close="108", high="110", low="102"),  # gap A
    bar(6, open_="113", close="113", high="115", low="107"),  # gap B
    bar(7, open_="112", close="112", high="117", low="110"),  # pause
    bar(8, open_="116", close="118", high="119", low="112"),  # pause; closes clear of 117
    bar(9, open_="124", close="124", high="125", low="120"),  # gap C, and close 124 > 123 -> BOS
]
"""The author's validated example: one impulse, gapping twice, that marks exactly two regions.

Prepend `BULLISH_START` and it is a complete order-block scenario — the shortest series in the
project that breaks structure *and* leaves something behind. Bars 0-2 leave a top at 123 and two
corrections that arm it; bars 3-9 are the impulse that breaks it, and inside that impulse the
gaps come in two events separated by a pause::

    bar 3  high 100  low  98  ┐
    bar 4  high 105  low 103  │ gap A: 100 < 102  (confirms on bar 5)
    bar 5  high 110  low 102  ┘
    bar 6  high 115  low 107    gap B: 105 < 107  (confirms on bar 6) -- adjacent to A, same event
    bar 7  high 117  low 110    no gap: 110 < 110 is not strict      -- THE PAUSE
    bar 8  high 118  low 112    no gap
    bar 9  high 125  low 120    gap C: 117 < 120  (confirms on bar 9) -- a second event

So **two** zones, not three: A and B are one continuous push, and both are marked on bar 9 by the
break that reveals them. The primary is drawn on bar 3 (100/98), the secondary on bar 7 (117/110).

⚠️ **Both regions are still standing when the last bar closes.** Price never returns to either
entry edge inside these ten bars, so a scenario that needs a *taken* region has to add its own
pullback — see `test_candles_integration._TAKES_THE_SECONDARY` for one.

Lives here rather than in a test file for the same reason `BULLISH_START` does: the engine's
golden and the API's route test both assert against it, and a fixture two suites copy is a
fixture that drifts into two versions. Copied, a change to the author's marking rule would fail
the golden while the route test went on asserting prices nobody marks any more.
"""


def rising(count: int, *, start: str = "1.10000", step: str = "0.00100") -> list[Candle]:
    """A market that only goes up. Boring on purpose — the loop is what is under test."""
    price = Decimal(start)
    delta = Decimal(step)

    bars: list[Candle] = []
    for index in range(count):
        bars.append(bar(index, open_=str(price), close=str(price + delta)))
        price += delta
    return bars


def falling(count: int, *, start: str = "1.20000", step: str = "0.00100") -> list[Candle]:
    """A market that only goes down — so that shorts get exercised, not just longs."""
    return rising(count, start=start, step=f"-{step}")


def entry(
    side: Side = Side.LONG,
    *,
    price: str = "1.10100",
    stop: str | None = None,
    reason: str = "test",
) -> Signal:
    return Signal(
        kind=SignalKind.ENTRY,
        side=side,
        reference_price=Decimal(price),
        stop_loss=Decimal(stop) if stop else None,
        reason=reason,
    )


def close_out(side: Side = Side.LONG, *, price: str = "1.10100", reason: str = "test") -> Signal:
    return Signal(kind=SignalKind.EXIT, side=side, reference_price=Decimal(price), reason=reason)


def modify_stop(
    *,
    stop: str,
    side: Side = Side.LONG,
    price: str = "1.10100",
    reason: str = "test",
) -> Signal:
    """Move the open position's stop to `stop` (ADR-0018).

    `price` is along for the ride: `Signal` requires a `reference_price` and this intent has no
    use for one — the broker reads the position's own side and level. Same as `CANCEL`.
    """
    return Signal(
        kind=SignalKind.MODIFY_STOP,
        side=side,
        reference_price=Decimal(price),
        stop_loss=Decimal(stop),
        reason=reason,
    )


class ImmediateFillBroker:
    """Fills whatever is pending at the next bar's open. The honest minimum.

    **Not** `BacktestBroker` — that is PR-105, with slippage, a cost model and intrabar
    stops. This exists so the loop has something to talk to, and so the tests can assert
    *when* a fill happens without a fill model muddying the question.

    It honours the three obligations in the `Broker` protocol: it marks to market before
    returning, it sorts exits before entries, and it never invents a `decided_at`.
    """

    def __init__(
        self,
        *,
        initial_capital: Money = Decimal(10_000),
        instrument: InstrumentSpec = EURUSD,
        costs: Money = ZERO,
    ) -> None:
        self._instrument = instrument
        self._portfolio = Portfolio(initial_capital=initial_capital, instrument=instrument)
        self._costs = costs
        self._pending: list[OrderRequest] = []
        self.submitted: list[OrderRequest] = []

    def submit(self, order: OrderRequest) -> OrderResult:
        self._pending.append(order)
        self.submitted.append(order)
        return OrderResult(order=order, accepted=True)

    def cancel(self, client_id: str) -> bool:  # noqa: ARG002
        """Nothing ever rests here: everything pending fills at the very next open, so by the
        time anyone could withdraw an order it has already executed. Always false, which is
        the same answer the real broker gives for an order it cannot find."""
        return False

    def modify_stop(
        self,
        symbol: str,  # noqa: ARG002
        stop_loss: Money,  # noqa: ARG002
        decided_at: dt.datetime,  # noqa: ARG002
    ) -> bool:
        """There is no protective stop here to move. This broker fills what is pending at the
        next open and nothing else — no intrabar stops, no targets — so a position it holds
        carries no level a modification could reach. Always false, the same answer the real
        broker gives when there is nothing to protect (ADR-0018)."""
        return False

    def on_bar(self, candle: Candle) -> Sequence[Fill]:
        # Exits before entries: a reversal on one bar has to close before it opens, or the
        # ledger refuses the second position on a sequence that was merely mis-sorted.
        pending = sorted(self._pending, key=lambda order: order.intent is SignalKind.ENTRY)
        self._pending.clear()

        fills = [
            Fill(
                order=order,
                time=candle.time,
                price=candle.open,
                volume=order.volume,
                costs=self._costs,
            )
            for order in pending
        ]

        for fill in fills:
            self._portfolio.apply(fill)

        # The protocol requires it: without this, the equity curve is a balance curve and
        # the maximum drawdown of a strategy that halved the account comes out as zero.
        self._portfolio.mark_to_market(candle)
        return fills

    def positions(self, symbol: str) -> Sequence[Position]:
        position = self._portfolio.position
        return (position,) if position and position.symbol == symbol else ()

    def account(self) -> AccountState:
        return self._portfolio.account()

    def trades(self) -> Sequence[ClosedTrade]:
        return self._portfolio.trades


@dataclass
class ScriptedStrategy:
    """Emits signals from a script keyed by candle index. No indicators, no conditions."""

    script: dict[int, list[Signal]] = field(default_factory=dict)
    seen: list[Candle] = field(default_factory=list)
    positions_seen: list[Side | None] = field(default_factory=list)
    fills_seen: list[tuple[Fill, ...]] = field(default_factory=list)

    def on_bar(self, context: Context) -> Sequence[Signal]:
        index = len(self.seen)
        self.seen.append(context.candle)
        self.positions_seen.append(context.position.side if context.position else None)
        self.fills_seen.append(context.fills)
        return self.script.get(index, [])


class FixedRisk:
    """Always the same size, always allowed (unless told otherwise).

    The real sizing arithmetic — percent-risk against the stop distance — is PR-105. Here the
    point is only that the loop asks, and honours the answer.
    """

    def __init__(self, *, volume: Volume = Decimal(1), allow_all: bool = True) -> None:
        self._volume = volume
        self._allow = allow_all
        self.sized: list[Signal] = []
        self.vetoed: list[OrderRequest] = []

    def size(self, signal: Signal, account: AccountState, instrument: InstrumentSpec) -> Volume:  # noqa: ARG002
        self.sized.append(signal)
        return self._volume

    def allow(self, order: OrderRequest, account: AccountState) -> bool:  # noqa: ARG002
        if not self._allow:
            self.vetoed.append(order)
        return self._allow
