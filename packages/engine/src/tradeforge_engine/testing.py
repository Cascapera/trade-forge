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
    Refusal,
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


def bar(  # noqa: PLR0913 — not parameters, fields: this builds a `Candle`, which has six
    index: int,
    *,
    open_: str,
    close: str,
    high: str | None = None,
    low: str | None = None,
    spread: int = 0,
) -> Candle:
    """One candle, `index` hours after the start.

    `spread` defaults to `0`, the same default `Candle` itself carries — a hand-built bar
    makes no claim about what it cost to trade. `BarSpreadCostModel` refuses such a bar
    rather than pricing it as free, so a scenario that means to charge one has to say so.
    """
    body = [Decimal(open_), Decimal(close)]
    return Candle(
        time=START + index * HOUR,
        open=Decimal(open_),
        high=Decimal(high) if high else max(body),
        low=Decimal(low) if low else min(body),
        close=Decimal(close),
        spread=spread,
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
    client_id: str | None = None,
) -> Signal:
    """A signal to open. `client_id` names the order, which matters wherever a test is about
    **correlation** rather than about the order existing: a `Refusal` carries the name and
    nothing else of the order, so a test that leaves it unnamed cannot tell a refusal that
    identified the order from one that lost track of which order it was."""
    return Signal(
        kind=SignalKind.ENTRY,
        side=side,
        reference_price=Decimal(price),
        stop_loss=Decimal(stop) if stop else None,
        reason=reason,
        client_id=client_id,
    )


def close_out(
    side: Side = Side.LONG,
    *,
    price: str = "1.10100",
    reason: str = "test",
    client_id: str | None = None,
) -> Signal:
    """A signal to close. Nothing in the repo names an exit today — the DSL does not and the
    structure setups emit none — so `client_id` exists here for the one test that needs to see
    a *named* refusal come back, and it is the reason `RefusedBy.NO_POSITION` can carry one."""
    return Signal(
        kind=SignalKind.EXIT,
        side=side,
        reference_price=Decimal(price),
        reason=reason,
        client_id=client_id,
    )


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

        self.refuse_with: str | None = None
        """Set to a reason and every `submit` is declined with it, until it is set back to
        `None`. A real broker refuses for its own reasons — a duplicate name, an order it
        cannot rest — and none of them are reachable from a test that only has this broker;
        this is the switch that makes the refusal path drivable at all."""

        self.refused: list[OrderRequest] = []

    def submit(self, order: OrderRequest) -> OrderResult:
        if self.refuse_with is not None:
            # Refused orders are not appended to `submitted`: the list means "reached the
            # book", and a refusal is precisely the thing that did not.
            self.refused.append(order)
            return OrderResult(order=order, accepted=False, reason=self.refuse_with)
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

    def refusals(self) -> Sequence[Refusal]:
        """Nothing arrives out of band here either — `submit` answers on the spot.

        Refusals this broker makes (see `refuse_with`) travel the synchronous path, through
        `OrderResult`, and the loop turns them into `Refusal`s itself. This is the *other*
        channel, for verdicts that arrive later, and nothing in this file can produce one.
        """
        return ()


@dataclass
class ScriptedStrategy:
    """Emits signals from a script keyed by candle index. No indicators, no conditions."""

    script: dict[int, list[Signal]] = field(default_factory=dict)
    seen: list[Candle] = field(default_factory=list)
    positions_seen: list[Side | None] = field(default_factory=list)
    fills_seen: list[tuple[Fill, ...]] = field(default_factory=list)
    refusals_seen: list[tuple[Refusal, ...]] = field(default_factory=list)
    """What `Context.refusals` held on each bar. Recorded, like `fills_seen`, because the
    channel is only real if a strategy can be shown to have been handed something on it."""

    def on_bar(self, context: Context) -> Sequence[Signal]:
        index = len(self.seen)
        self.seen.append(context.candle)
        self.positions_seen.append(context.position.side if context.position else None)
        self.fills_seen.append(context.fills)
        self.refusals_seen.append(context.refusals)
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


# --------------------------------------------------------------------------- #
# A measured market, for the scenarios the synthetic ones cannot reach          #
# --------------------------------------------------------------------------- #

# One hundred and seventy-five real EURUSD H1 bars, starting 2025-01-01 21:00 UTC.
#
# ⚠️ Real data, and the window was **measured, not chosen**. The criterion matters and it took
# two tries. The first version asked for "one resting order, no trade, flat" — and got a warm-up
# that filled nothing and left the account at exactly its initial capital, which made two of the
# tests below vacuous: `the money does not cross` cannot separate 10 000 from 10 000, and
# `warm-up really does trade` was satisfied by the resting order the window was selected to
# guarantee.
#
# The criterion here is the one the design actually needs: **at least one fill**, at least one
# order still resting, flat at the end, and an account that **moved**. Measured on this window:
# 2 fills, 1 closed trade, 1 resting order, equity 10 000 -> 9 901. That last number is what
# makes the re-sizing observable — the carried order was sized at 1.08 lots against 9 901, and
# the session's own 10 000 calls for 1.09.
#
# The package's synthetic scenarios cannot do any of this: probed, `BULLISH_START`,
# `BULLISH_START_EMA3` and `GAPPING_IMPULSE` arm nothing at all, being far too short for the
# structure machine to confirm a break and mark a block.
#
# Timestamps are `bar()`'s hourly grid rather than the original instants. Only the shape matters:
# the setup reads highs, lows and closes, never the calendar.
ARMS_A_RESTING_LIMIT: tuple[Candle, ...] = (
    bar(0, open_="1.03515", close="1.03556", high="1.03579", low="1.03493"),
    bar(1, open_="1.03548", close="1.03508", high="1.03566", low="1.03464"),
    bar(2, open_="1.03508", close="1.03521", high="1.03565", low="1.03473"),
    bar(3, open_="1.03522", close="1.03579", high="1.03608", low="1.03443"),
    bar(4, open_="1.03579", close="1.03709", high="1.03712", low="1.03576"),
    bar(5, open_="1.03710", close="1.03715", high="1.03742", low="1.03681"),
    bar(6, open_="1.03716", close="1.03693", high="1.03750", low="1.03664"),
    bar(7, open_="1.03692", close="1.03637", high="1.03707", low="1.03618"),
    bar(8, open_="1.03639", close="1.03637", high="1.03649", low="1.03600"),
    bar(9, open_="1.03637", close="1.03628", high="1.03724", low="1.03623"),
    bar(10, open_="1.03628", close="1.03628", high="1.03709", low="1.03463"),
    bar(11, open_="1.03629", close="1.03517", high="1.03706", low="1.03495"),
    bar(12, open_="1.03516", close="1.03167", high="1.03570", low="1.03137"),
    bar(13, open_="1.03167", close="1.03322", high="1.03343", low="1.03150"),
    bar(14, open_="1.03322", close="1.03183", high="1.03343", low="1.03149"),
    bar(15, open_="1.03182", close="1.03134", high="1.03297", low="1.03114"),
    bar(16, open_="1.03135", close="1.03060", high="1.03239", low="1.03022"),
    bar(17, open_="1.03037", close="1.02742", high="1.03089", low="1.02725"),
    bar(18, open_="1.02741", close="1.02613", high="1.02763", low="1.02243"),
    bar(19, open_="1.02610", close="1.02649", high="1.02680", low="1.02514"),
    bar(20, open_="1.02652", close="1.02564", high="1.02662", low="1.02558"),
    bar(21, open_="1.02565", close="1.02513", high="1.02595", low="1.02480"),
    bar(22, open_="1.02511", close="1.02615", high="1.02645", low="1.02506"),
    bar(23, open_="1.02614", close="1.02565", high="1.02675", low="1.02556"),
    bar(24, open_="1.02569", close="1.02642", high="1.02661", low="1.02534"),
    bar(25, open_="1.02652", close="1.02678", high="1.02681", low="1.02644"),
    bar(26, open_="1.02679", close="1.02730", high="1.02735", low="1.02658"),
    bar(27, open_="1.02731", close="1.02676", high="1.02731", low="1.02664"),
    bar(28, open_="1.02675", close="1.02722", high="1.02747", low="1.02667"),
    bar(29, open_="1.02723", close="1.02708", high="1.02745", low="1.02687"),
    bar(30, open_="1.02706", close="1.02697", high="1.02710", low="1.02671"),
    bar(31, open_="1.02697", close="1.02717", high="1.02736", low="1.02646"),
    bar(32, open_="1.02717", close="1.02793", high="1.02794", low="1.02715"),
    bar(33, open_="1.02789", close="1.02808", high="1.02847", low="1.02687"),
    bar(34, open_="1.02808", close="1.02804", high="1.02881", low="1.02728"),
    bar(35, open_="1.02804", close="1.02903", high="1.02999", low="1.02803"),
    bar(36, open_="1.02903", close="1.02814", high="1.02939", low="1.02792"),
    bar(37, open_="1.02813", close="1.02973", high="1.03029", low="1.02813"),
    bar(38, open_="1.02972", close="1.02956", high="1.03010", low="1.02942"),
    bar(39, open_="1.02958", close="1.02932", high="1.03002", low="1.02914"),
    bar(40, open_="1.02931", close="1.03004", high="1.03024", low="1.02879"),
    bar(41, open_="1.02947", close="1.02892", high="1.03058", low="1.02731"),
    bar(42, open_="1.02891", close="1.02909", high="1.02999", low="1.02835"),
    bar(43, open_="1.02910", close="1.02980", high="1.03057", low="1.02909"),
    bar(44, open_="1.02979", close="1.02973", high="1.03021", low="1.02924"),
    bar(45, open_="1.02969", close="1.03012", high="1.03061", low="1.02938"),
    bar(46, open_="1.03011", close="1.03057", high="1.03075", low="1.02988"),
    bar(47, open_="1.03056", close="1.03067", high="1.03099", low="1.03046"),
    bar(48, open_="1.03018", close="1.03007", high="1.03060", low="1.02954"),
    bar(49, open_="1.03010", close="1.03049", high="1.03079", low="1.03009"),
    bar(50, open_="1.03051", close="1.03030", high="1.03053", low="1.02953"),
    bar(51, open_="1.03029", close="1.03121", high="1.03136", low="1.03012"),
    bar(52, open_="1.03121", close="1.03160", high="1.03176", low="1.03107"),
    bar(53, open_="1.03161", close="1.03164", high="1.03183", low="1.03131"),
    bar(54, open_="1.03165", close="1.03151", high="1.03176", low="1.03128"),
    bar(55, open_="1.03150", close="1.03089", high="1.03182", low="1.03084"),
    bar(56, open_="1.03089", close="1.03127", high="1.03166", low="1.03077"),
    bar(57, open_="1.03126", close="1.03272", high="1.03283", low="1.03099"),
    bar(58, open_="1.03272", close="1.03501", high="1.03524", low="1.03266"),
    bar(59, open_="1.03502", close="1.03555", high="1.03688", low="1.03401"),
    bar(60, open_="1.03556", close="1.03351", high="1.03580", low="1.03351"),
    bar(61, open_="1.03351", close="1.04219", high="1.04329", low="1.03286"),
    bar(62, open_="1.04216", close="1.04108", high="1.04307", low="1.03922"),
    bar(63, open_="1.04110", close="1.04167", high="1.04368", low="1.04044"),
    bar(64, open_="1.04168", close="1.03926", high="1.04237", low="1.03534"),
    bar(65, open_="1.03925", close="1.03934", high="1.03987", low="1.03713"),
    bar(66, open_="1.03936", close="1.03969", high="1.04029", low="1.03854"),
    bar(67, open_="1.03969", close="1.03874", high="1.03969", low="1.03792"),
    bar(68, open_="1.03874", close="1.03819", high="1.03892", low="1.03771"),
    bar(69, open_="1.03820", close="1.03883", high="1.03919", low="1.03788"),
    bar(70, open_="1.03883", close="1.03877", high="1.03920", low="1.03858"),
    bar(71, open_="1.03881", close="1.03886", high="1.03922", low="1.03861"),
    bar(72, open_="1.03888", close="1.03885", high="1.03917", low="1.03803"),
    bar(73, open_="1.03895", close="1.03834", high="1.03899", low="1.03816"),
    bar(74, open_="1.03835", close="1.03844", high="1.03853", low="1.03771"),
    bar(75, open_="1.03841", close="1.03811", high="1.03848", low="1.03761"),
    bar(76, open_="1.03809", close="1.03823", high="1.03854", low="1.03799"),
    bar(77, open_="1.03823", close="1.03928", high="1.03930", low="1.03801"),
    bar(78, open_="1.03927", close="1.03918", high="1.03944", low="1.03904"),
    bar(79, open_="1.03918", close="1.03968", high="1.04033", low="1.03909"),
    bar(80, open_="1.03968", close="1.03973", high="1.04042", low="1.03963"),
    bar(81, open_="1.03975", close="1.04146", high="1.04244", low="1.03975"),
    bar(82, open_="1.04146", close="1.04252", high="1.04252", low="1.04030"),
    bar(83, open_="1.04251", close="1.04276", high="1.04344", low="1.04241"),
    bar(84, open_="1.04263", close="1.04280", high="1.04345", low="1.04167"),
    bar(85, open_="1.04280", close="1.04134", high="1.04284", low="1.04052"),
    bar(86, open_="1.04134", close="1.03892", high="1.04171", low="1.03872"),
    bar(87, open_="1.03893", close="1.03965", high="1.04052", low="1.03866"),
    bar(88, open_="1.03965", close="1.03930", high="1.03975", low="1.03893"),
    bar(89, open_="1.03872", close="1.03727", high="1.03872", low="1.03552"),
    bar(90, open_="1.03729", close="1.03684", high="1.03815", low="1.03626"),
    bar(91, open_="1.03685", close="1.03670", high="1.03738", low="1.03538"),
    bar(92, open_="1.03670", close="1.03544", high="1.03694", low="1.03493"),
    bar(93, open_="1.03544", close="1.03553", high="1.03597", low="1.03522"),
    bar(94, open_="1.03551", close="1.03444", high="1.03560", low="1.03440"),
    bar(95, open_="1.03441", close="1.03393", high="1.03443", low="1.03384"),
    bar(96, open_="1.03378", close="1.03415", high="1.03424", low="1.03378"),
    bar(97, open_="1.03418", close="1.03415", high="1.03449", low="1.03391"),
    bar(98, open_="1.03416", close="1.03468", high="1.03494", low="1.03416"),
    bar(99, open_="1.03468", close="1.03530", high="1.03531", low="1.03453"),
    bar(100, open_="1.03530", close="1.03542", high="1.03549", low="1.03515"),
    bar(101, open_="1.03542", close="1.03478", high="1.03542", low="1.03462"),
    bar(102, open_="1.03477", close="1.03512", high="1.03521", low="1.03473"),
    bar(103, open_="1.03513", close="1.03550", high="1.03558", low="1.03494"),
    bar(104, open_="1.03549", close="1.03514", high="1.03575", low="1.03461"),
    bar(105, open_="1.03515", close="1.03324", high="1.03517", low="1.03284"),
    bar(106, open_="1.03322", close="1.03188", high="1.03396", low="1.03180"),
    bar(107, open_="1.03187", close="1.03195", high="1.03264", low="1.03154"),
    bar(108, open_="1.03192", close="1.03189", high="1.03214", low="1.03101"),
    bar(109, open_="1.03188", close="1.02977", high="1.03199", low="1.02732"),
    bar(110, open_="1.02978", close="1.02827", high="1.03002", low="1.02808"),
    bar(111, open_="1.02826", close="1.03034", high="1.03044", low="1.02809"),
    bar(112, open_="1.03035", close="1.02915", high="1.03075", low="1.02890"),
    bar(113, open_="1.02928", close="1.03016", high="1.03099", low="1.02926"),
    bar(114, open_="1.03016", close="1.03125", high="1.03145", low="1.02945"),
    bar(115, open_="1.03125", close="1.03053", high="1.03134", low="1.02982"),
    bar(116, open_="1.03053", close="1.03126", high="1.03159", low="1.03053"),
    bar(117, open_="1.03125", close="1.03090", high="1.03243", low="1.03083"),
    bar(118, open_="1.03093", close="1.03161", high="1.03167", low="1.03079"),
    bar(119, open_="1.03161", close="1.03170", high="1.03193", low="1.03139"),
    bar(120, open_="1.03079", close="1.03174", high="1.03174", low="1.03079"),
    bar(121, open_="1.03179", close="1.03132", high="1.03187", low="1.03132"),
    bar(122, open_="1.03132", close="1.03130", high="1.03147", low="1.03047"),
    bar(123, open_="1.03131", close="1.03143", high="1.03174", low="1.03114"),
    bar(124, open_="1.03144", close="1.03165", high="1.03199", low="1.03141"),
    bar(125, open_="1.03165", close="1.03110", high="1.03214", low="1.03109"),
    bar(126, open_="1.03110", close="1.03091", high="1.03122", low="1.03056"),
    bar(127, open_="1.03092", close="1.03067", high="1.03125", low="1.03053"),
    bar(128, open_="1.03067", close="1.03000", high="1.03082", low="1.02969"),
    bar(129, open_="1.03007", close="1.02910", high="1.03083", low="1.02876"),
    bar(130, open_="1.02909", close="1.03078", high="1.03078", low="1.02835"),
    bar(131, open_="1.03077", close="1.03058", high="1.03175", low="1.03032"),
    bar(132, open_="1.03060", close="1.03031", high="1.03125", low="1.02996"),
    bar(133, open_="1.03030", close="1.03011", high="1.03044", low="1.02968"),
    bar(134, open_="1.03011", close="1.03034", high="1.03051", low="1.02961"),
    bar(135, open_="1.03033", close="1.03080", high="1.03187", low="1.03010"),
    bar(136, open_="1.03080", close="1.03074", high="1.03148", low="1.03051"),
    bar(137, open_="1.03074", close="1.02994", high="1.03096", low="1.02948"),
    bar(138, open_="1.02993", close="1.02973", high="1.03011", low="1.02910"),
    bar(139, open_="1.02973", close="1.02974", high="1.03019", low="1.02937"),
    bar(140, open_="1.02972", close="1.03001", high="1.03025", low="1.02940"),
    bar(141, open_="1.03001", close="1.02980", high="1.03021", low="1.02978"),
    bar(142, open_="1.02981", close="1.03011", high="1.03037", low="1.02981"),
    bar(143, open_="1.03012", close="1.02995", high="1.03012", low="1.02991"),
    bar(144, open_="1.02992", close="1.03005", high="1.03006", low="1.02956"),
    bar(145, open_="1.03012", close="1.02964", high="1.03019", low="1.02955"),
    bar(146, open_="1.02965", close="1.02954", high="1.03004", low="1.02921"),
    bar(147, open_="1.02952", close="1.03020", high="1.03046", low="1.02936"),
    bar(148, open_="1.03020", close="1.03019", high="1.03038", low="1.02982"),
    bar(149, open_="1.03017", close="1.02984", high="1.03033", low="1.02976"),
    bar(150, open_="1.02983", close="1.02949", high="1.03000", low="1.02935"),
    bar(151, open_="1.02951", close="1.02855", high="1.02974", low="1.02846"),
    bar(152, open_="1.02856", close="1.02818", high="1.02884", low="1.02811"),
    bar(153, open_="1.02816", close="1.02970", high="1.02972", low="1.02812"),
    bar(154, open_="1.02971", close="1.03026", high="1.03119", low="1.02927"),
    bar(155, open_="1.03026", close="1.02999", high="1.03058", low="1.02990"),
    bar(156, open_="1.03001", close="1.03022", high="1.03059", low="1.02998"),
    bar(157, open_="1.03020", close="1.03018", high="1.03053", low="1.02989"),
    bar(158, open_="1.03017", close="1.03055", high="1.03094", low="1.02991"),
    bar(159, open_="1.03054", close="1.02565", high="1.03118", low="1.02128"),
    bar(160, open_="1.02565", close="1.02532", high="1.02728", low="1.02444"),
    bar(161, open_="1.02531", close="1.02504", high="1.02782", low="1.02363"),
    bar(162, open_="1.02502", close="1.02375", high="1.02512", low="1.02274"),
    bar(163, open_="1.02375", close="1.02445", high="1.02506", low="1.02328"),
    bar(164, open_="1.02444", close="1.02415", high="1.02460", low="1.02358"),
    bar(165, open_="1.02416", close="1.02453", high="1.02489", low="1.02350"),
    bar(166, open_="1.02454", close="1.02445", high="1.02460", low="1.02371"),
    bar(167, open_="1.02444", close="1.02436", high="1.02489", low="1.02426"),
    bar(168, open_="1.02316", close="1.02454", high="1.02455", low="1.02316"),
    bar(169, open_="1.02464", close="1.02431", high="1.02468", low="1.02387"),
    bar(170, open_="1.02432", close="1.02424", high="1.02450", low="1.02365"),
    bar(171, open_="1.02425", close="1.02417", high="1.02499", low="1.02392"),
    bar(172, open_="1.02417", close="1.02401", high="1.02452", low="1.02386"),
    bar(173, open_="1.02399", close="1.02159", high="1.02404", low="1.02127"),
    bar(174, open_="1.02154", close="1.02118", high="1.02200", low="1.02075"),
)


def arms_a_resting_limit() -> list[Candle]:
    """The measured window above, as the loop wants it.

    A function rather than the tuple, because a caller slices and a shared mutable list is
    a fixture two suites can hand each other in a state neither wrote.
    """
    return list(ARMS_A_RESTING_LIMIT)
