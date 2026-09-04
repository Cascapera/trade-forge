"""The vocabulary of the system.

Everything the engine reasons about lives here, and it lives here rather than in the
database or the collector on purpose: **the core owns the vocabulary, and the adapters
conform to it** (ADR-0012). A `Candle` defined by the storage layer would mean the
engine's model of a market is whatever happened to be convenient to persist.

Every type here is a **frozen dataclass**. Not style: an invariant. An indicator that has
already read a candle must be reading the same candle a thousand bars later, and a
`Position` that a risk check approved must be the position that gets filled. Mutable
domain objects turn "the same input produces the same output" into a hope, because any
holder of a reference can quietly change history. `slots=True` because there are millions
of these in a decade of M1 bars.

And every type here **validates itself**. The database has CHECK constraints; the engine,
which is where the arithmetic actually happens, needs the same. A `tick_size` of zero is a
division by zero at the bottom of the P&L; a negative cost is free money in the balance; a
naive datetime is a backtest silently shifted by hours. None of those raise on their own —
they produce plausible, wrong numbers — so they are refused at construction.

Money is `Decimal`, never `float` — see ADR-0011.
"""

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

# An amount of money, or a price. Exact decimal arithmetic: an equity curve is a sum of
# thousands of these, and binary floating point drifts through exactly that path.
type Money = Decimal

# Lots, contracts or shares — whatever the instrument's contract size says a unit is.
type Volume = Decimal

ZERO = Decimal(0)


def _require_utc(moment: dt.datetime, field: str) -> None:
    """A naive datetime is not an instant, and the failure it causes is silent.

    A backtest whose candles are naive and whose fills are aware does not crash on the
    happy path — it crashes ten years into a run, on the first comparison. And a backtest
    whose candles are all naive simply trades a market displaced by the broker's timezone,
    and reports a plausible result.
    """
    if moment.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware: {moment!r} means nothing on its own")


class Side(StrEnum):
    """Which way a position faces."""

    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        """+1 for a long, -1 for a short.

        Every P&L formula in the engine is written once, for a long, and multiplied by
        this. The alternative — an `if side is LONG` in each of them — is how a codebase
        ends up with a short-selling bug in one function and not the other.
        """
        return 1 if self is Side.LONG else -1

    @property
    def opposite(self) -> "Side":
        return Side.SHORT if self is Side.LONG else Side.LONG


class AssetClass(StrEnum):
    """What kind of thing is being traded.

    Domain, not metadata: a tick of EURUSD on one lot is worth a dollar and a tick of AAPL
    is worth a cent per share. The class is what tells the cost model and the sizing which
    arithmetic they are in.
    """

    FOREX = "forex"
    STOCK = "stock"
    INDEX = "index"
    FUTURE = "future"
    CRYPTO = "crypto"


class SignalKind(StrEnum):
    """Open a position, close the open one, withdraw a waiting order, or move a live stop.

    `CANCEL` and `MODIFY_STOP` are the odd ones out on purpose: they are the only kinds that
    never become an `OrderRequest`. Both act on something that already exists rather than
    asking for something new, so neither carries volume and neither can produce a fill.

    A resting order outlives the bar that placed it, so something has to be able to take it
    back — see `Signal.client_id` and `Broker.cancel` (ADR-0014). An open position outlives
    it too, and its stop is the one level a strategy may want to move while the trade runs —
    see `Broker.modify_stop` (ADR-0018).
    """

    ENTRY = "entry"
    EXIT = "exit"
    CANCEL = "cancel"
    MODIFY_STOP = "modify_stop"


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """A symbol and the numbers that turn a price move into money."""

    symbol: str
    name: str
    asset_class: AssetClass
    currency_quote: str
    tick_size: Money
    tick_value: Money
    contract_size: Money
    """Units of the base currency in one lot (100 000 for forex, 1 for a share).

    **Not used by `money_for`, deliberately.** The broker quotes `tick_value` *per lot*, so
    the contract size is already inside it; multiplying by it again would count the lot
    twice and inflate every P&L by five orders of magnitude. It is kept because MT5 reports
    it and margin calculations (phase 2) will need it — not because the P&L does.
    """

    digits: int
    exchange: str | None = None
    currency_base: str | None = None

    def __post_init__(self) -> None:
        if self.tick_size <= ZERO:
            raise ValueError(f"{self.symbol}: tick_size must be positive, got {self.tick_size}")
        if self.tick_value <= ZERO:
            raise ValueError(f"{self.symbol}: tick_value must be positive, got {self.tick_value}")
        if self.contract_size <= ZERO:
            raise ValueError(
                f"{self.symbol}: contract_size must be positive, got {self.contract_size}"
            )

    def money_for(self, price_move: Money, volume: Volume) -> Money:
        """Convert a price movement into profit or loss.

        `(move / tick_size) * tick_value * volume` — and that single line is the whole
        reason instruments are data rather than code. One tick of EURUSD (0.00001) on a
        standard lot is worth $1; one tick of AAPL (0.01) on one share is worth one cent.
        Same formula, entirely different numbers, and no `if asset_class ==` anywhere.
        """
        return (price_move / self.tick_size) * self.tick_value * volume


@dataclass(frozen=True, slots=True)
class Candle:
    """One closed OHLCV bar.

    `time` is the bar's **opening** instant, in **UTC**. Both halves matter. Labelling a
    bar by its close would make every off-by-one in the engine invisible; UTC because a
    broker's server clock is not a clock.

    Note what this means for the anti-lookahead rule: a strategy that "decides on the close
    of candle N" stamps its order with N's *opening* time, because that is the only time a
    candle has. The engine's guard is written against that fact rather than against a
    docstring — see `loop._reject_lookahead`.
    """

    time: dt.datetime
    open: Money
    high: Money
    low: Money
    close: Money
    tick_volume: int = 0
    spread: int = 0
    real_volume: int = 0

    def __post_init__(self) -> None:
        _require_utc(self.time, "Candle.time")

        # A high below the body is not a candle: it is a bar at which a stop would trigger
        # where price never went. Cheap to check, and it catches a corrupt feed at the
        # boundary instead of in the P&L.
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError(
                f"candle at {self.time} does not contain its own body: "
                f"O={self.open} H={self.high} L={self.low} C={self.close}"
            )


# How many bars before the decision an entry's snapshot carries (`EntrySnapshot`) — enough to
# read the swing the setup claims to be trading, and no more.
#
# It lives here, with the type it describes, rather than with the loop that fills the window:
# the strategies size their own indicator buffers against it, and a strategy reaching into the
# event loop for a constant would be the tail wagging the dog.
SNAPSHOT_BARS_BEFORE = 50


@dataclass(frozen=True, slots=True)
class SnapshotPoint:
    """One reading of an indicator, stamped with the bar it was read on."""

    time: dt.datetime
    value: Money

    def __post_init__(self) -> None:
        _require_utc(self.time, "SnapshotPoint.time")


@dataclass(frozen=True, slots=True)
class SnapshotSeries:
    """A line to draw across the bars: an indicator, as the strategy actually computed it.

    A moving average is not a level, it is a curve, and drawing it as the single number it held
    at the decision loses the shape the method is about — where price came back to it, at what
    angle, how far it had run.

    **Stamped with times, not aligned by position.** The strategy fills this buffer; the loop
    fills the bar window; they are two buffers and nothing forces them to agree. Indexed by
    position, a one-bar disagreement draws the whole curve shifted — plausible, silent, and
    wrong. Indexed by time, the same disagreement is a visible hole, and a reader can see that
    something is missing rather than believe something that is not there.

    **A leading gap is warmup**, not a fault: an average has no value until its period has
    filled, and the window can reach back before that. Points simply begin where the indicator
    did. A gap in the *middle* has no legitimate cause and is worth chasing.

    The series ends on the decision bar, even when the broker later extends the bars to a fill
    several bars away: only the strategy knows the average, and it stopped contributing when it
    emitted the signal. So the curve can stop short of the right edge — that is the arming
    window's extent, drawn honestly.
    """

    label: str
    points: tuple[SnapshotPoint, ...]

    def __post_init__(self) -> None:
        if not self.label:
            raise ValueError("a series needs a label; an unnamed curve is undrawable")
        if not self.points:
            raise ValueError(
                f"series {self.label} has no points; omit it rather than send it empty"
            )
        for earlier, later in zip(self.points, self.points[1:], strict=False):
            if later.time <= earlier.time:
                raise ValueError(
                    f"series {self.label} must ascend in time, got {earlier.time} then {later.time}"
                )


@dataclass(frozen=True, slots=True)
class SnapshotLevel:
    """A horizontal segment: one price, bounded at both ends in time.

    Where a region is a band that stays live and a series is a curve that moves, this is a level
    that *held and then broke* — the structure a break of structure broke. Drawn from the bar
    that set it to the bar that crossed it, its length is how long the structure stood, which is
    the difference between a break that means something and one that does not.

    Bounded at both ends, unlike `SnapshotRegion`, and that is the distinction: a zone is still
    in force after the entry, so it extends rightward; a broken level is over the moment it is
    crossed, and drawing it onward would show a structure still standing that is not.
    """

    label: str
    price: Money
    from_time: dt.datetime
    to_time: dt.datetime

    def __post_init__(self) -> None:
        _require_utc(self.from_time, "SnapshotLevel.from_time")
        _require_utc(self.to_time, "SnapshotLevel.to_time")
        if not self.label:
            raise ValueError("a level needs a label; an unnamed line is undrawable")
        # A level set after it was broken is not a level, it is a swapped pair — and it would
        # render as a segment of negative width, which most chart code silently normalises.
        if self.to_time < self.from_time:
            raise ValueError(
                f"level {self.label} is set at {self.from_time}, after it broke at {self.to_time}"
            )


@dataclass(frozen=True, slots=True)
class SnapshotRegion:
    """A rectangle to draw on the chart: a band of price with a left edge in time.

    A zone is not a pair of numbers. It is drawn from the candle that formed it, across the
    price range it covers, extended rightward until price comes back into it — which is how a
    reader sees *when* it was filled rather than merely that it was. Two scalars cannot say
    where the rectangle starts, so a chart drawn from them has to guess, and it guesses the
    left edge of the window, which is a lie about the age of the zone.

    `from_time` is the bar the region is drawn on — for an order block, the candle before the
    gap, which is `OrderBlock.time` and not `confirmed_at`. The two differ on purpose: the zone
    belongs to the past, and nothing knew it was a zone until the break revealed it.

    **It routinely falls before the snapshot's first bar.** A zone marked by a long impulse leg
    can be older than the window that carries it, and that is not an error to correct: the
    rectangle really does start there. A client draws from wherever `from_time` lands, clamping
    to the left edge of the chart — never *moving* it to the first visible bar, which would
    redraw the zone as younger than it is and lose the one thing the rectangle is for.

    No right edge. Where a region *stops* is a fact about what price did afterwards, and the
    snapshot is sealed at the fill — so the chart extends it to its own edge and lets the bars
    show the rest.
    """

    label: str
    top: Money
    bottom: Money
    from_time: dt.datetime

    def __post_init__(self) -> None:
        _require_utc(self.from_time, "SnapshotRegion.from_time")
        if not self.label:
            raise ValueError("a region needs a label; an unnamed rectangle is undrawable")
        # Inverted edges would render as a rectangle of negative height — which most chart
        # libraries silently normalise, leaving a zone that looks right and is upside down.
        if self.top < self.bottom:
            raise ValueError(f"region {self.label}: top {self.top} is below bottom {self.bottom}")


@dataclass(frozen=True, slots=True)
class ZoneMark:
    """One region over a whole run, with both ends of its life.

    The run-length sibling of `SnapshotRegion`, and the difference between them is the right
    edge. A snapshot is sealed at the fill, so it cannot know where a zone stopped and says
    nothing; a finished run knows exactly — the bar whose wick reached the entry edge and took
    it. Carrying that lets a chart draw the rectangle's *length* as how long the region stood,
    which is the one thing the picture is for.

    **Three instants, all different, none interchangeable.**

    * `from_time` — the bar the region is drawn on: the candle before the gap. It is where the
      rectangle begins, and it is routinely far older than the break that revealed it.
    * `confirmed_at` — the bar whose close broke structure and made the region *visible*. A
      strategy may only act from here. The stretch between the two is time price spent working
      a zone nothing yet knew was one: a median of **8 bars** over this project's 124 regions
      on real AAPL H1 data — long enough that collapsing the two would redraw most regions as
      much younger. ⚠️ The **mean** is 16 and the longest is 160, so quoting an average here
      would describe a typical region as twice its real age; the distribution has a long tail
      and the median is the honest summary of it.
    * `mitigated_at` — the bar that took it, or `None` for a region price never came back to.
      `None` is not "unknown": it is a region still standing when the run ended, and a chart
      extends it to its own right edge.

    Immutable, and deliberately not the detector's `TrackedZone`. That type is the detector's
    live bookkeeping — mutable, and its own business. What a reader needs is a record of what
    happened, and handing out the working copy would let a chart advance the machinery it is
    supposed to be describing.
    """

    kind: str
    """`demand` or `supply` — the side of the book, from `ZoneKind`."""

    top: Money
    bottom: Money
    from_time: dt.datetime
    confirmed_at: dt.datetime
    mitigated_at: dt.datetime | None
    primary: bool
    """First gap event of the impulse. Secondaries are only tradable with `allow_secondary`."""

    def __post_init__(self) -> None:
        _require_utc(self.from_time, "ZoneMark.from_time")
        _require_utc(self.confirmed_at, "ZoneMark.confirmed_at")
        if self.mitigated_at is not None:
            _require_utc(self.mitigated_at, "ZoneMark.mitigated_at")
        if self.top < self.bottom:
            raise ValueError(f"zone top {self.top} is below bottom {self.bottom}")
        # A region cannot be confirmed before it exists: the break that reveals a zone is always
        # later than the gap that marked it. Reversed, the rectangle would be drawn backwards in
        # time and a client clamping to the chart edge would silently render it as zero-width.
        if self.confirmed_at < self.from_time:
            raise ValueError(
                f"zone confirmed at {self.confirmed_at} before it was marked at {self.from_time}"
            )
        # And it cannot be taken before it is drawn. Taken *on* the marking bar is legal here,
        # and left legal on purpose even though **this engine never produces it**: measured
        # against the code, `OrderBlockDetector` advances the zones it already holds before
        # appending the ones a break revealed, and a region touched before that break is
        # filtered out of the leg — so from `StructureStrategy` the taking instant is strictly
        # later than the confirming one, always. Tightening this to `>= confirmed_at` would
        # write the backtest detector's bookkeeping order into the record type, and refuse a
        # live producer, where price can reach a region before the close that reveals it.
        # See `test_a_zone_taken_on_its_marking_bar_is_allowed`, which states both halves.
        if self.mitigated_at is not None and self.mitigated_at < self.from_time:
            raise ValueError(
                f"zone taken at {self.mitigated_at} before it was marked at {self.from_time}"
            )


@dataclass(frozen=True, slots=True)
class EntrySnapshot:
    """The bars the strategy had seen when it decided to enter — the picture, not the numbers.

    Its whole job is to let a human look at a trade afterwards and answer "did this enter
    where the method says it should?". The levels that justified the entry are already
    recorded, in `Signal.context`; what no column carries is the *shape* price had at that
    moment, and no amount of scalars substitutes for seeing it.

    **Captured when the decision is made, never rebuilt afterwards.** Reconstructing the
    window later — from the Parquet, in the API — is the same class of mistake as computing
    an indicator after the fact: the bars on disk tomorrow are not necessarily the bars the
    strategy was looking at (the file gets recollected, extended, corrected), and the
    difference would show up as a chart that quietly disagrees with the trade drawn on it.
    Freezing it with the trade is the same argument as `Backtest.candles_seen`, one level down.

    The window answers **two** questions, and they are not the same one:

    * *Was it armed in the right place?* — the bars up to and including the decision bar,
      attached by the loop. `decided_at` is the last of them.
    * *Did it enter in the right place?* — the bars from the decision through the one that
      filled, attached by the broker, which is the only component that knows when a resting
      order stopped resting. `filled_at` is the last of them.

    The two coincide for an order that never rested. They separate for a stop or a limit,
    which can wait bars before price comes to it — and what price did while it waited is
    precisely how you tell a trigger that was reached from one that was run over by a gap.

    **The window is contiguous.** Every bar between the first and the last is present, with
    no holes: a chart with a bar silently missing is worse than a short chart, because
    nothing on it looks wrong. An order that rests longer than the broker keeps bars for
    therefore reports the arming window alone rather than a spliced one.
    """

    bars: tuple[Candle, ...]

    decided_at: dt.datetime
    """The opening instant of the bar the strategy decided on.

    Stored rather than derived, because once the broker extends the window past the fill the
    decision bar is no longer at either end of it — it is somewhere in the middle, and only
    this field says where. It is the same instant as `OrderRequest.decided_at`, which is what
    the anti-lookahead guard is written against."""

    regions: tuple[SnapshotRegion, ...] = ()
    """The rectangles to draw over those bars — the zones the entry was waiting at.

    Contributed by the **strategy**, unlike the bars: only it knows that a stretch of price is
    a region rather than a level, and where that region begins. Empty for a setup that has none
    — the swing setups enter off an average, which is a line and not a band."""

    levels: tuple[SnapshotLevel, ...] = ()
    """The horizontal segments to draw — the structure this entry was built on.

    A zone is only worth entering because a break of structure revealed it, and that break is
    otherwise invisible in the record: the bars show price crossing a price, with nothing saying
    which price mattered or how long it had held. See `SnapshotLevel`."""

    series: tuple[SnapshotSeries, ...] = ()
    """The curves to draw across those bars — the indicators, as the strategy computed them.

    Also the strategy's, and for a stronger reason than the regions: an indicator cannot be
    recovered from the bars here. A moving average carries the whole run's history in it, so a
    reader recomputing one from a fifty-bar window would get a different number from a different
    seed — a curve that looks right and does not pass through the value the entry was judged
    against. See `SnapshotSeries`."""

    def __post_init__(self) -> None:
        # An empty window is not a snapshot, it is a wiring bug that would reach the screen
        # as an empty chart and read as "this trade had no context".
        if not self.bars:
            raise ValueError("an entry snapshot needs at least the decision bar")
        _require_utc(self.decided_at, "EntrySnapshot.decided_at")
        # Out-of-order bars would be drawn as a chart folding back on itself. The loop
        # already refuses an out-of-order *stream* (`_reject_out_of_order`); this refuses an
        # out-of-order *window*, which is what a buffer filled from the wrong end produces.
        for earlier, later in zip(self.bars, self.bars[1:], strict=False):
            if later.time <= earlier.time:
                raise ValueError(
                    f"snapshot bars must ascend in time, got {earlier.time} then {later.time}"
                )
        # The decision bar has to be *in* the picture. Extending the window is a splice, and a
        # splice that lost the bar it was anchored to is one whose two halves may not adjoin —
        # the exact failure the contiguity rule above cannot see, because both halves ascend.
        if not any(bar.time == self.decided_at for bar in self.bars):
            raise ValueError(
                f"the decision bar {self.decided_at} is not in the snapshot window "
                f"[{self.bars[0].time} .. {self.bars[-1].time}]"
            )
        # A region drawn from a bar the strategy had not reached yet is a rectangle justifying
        # a decision with something that had not happened — lookahead, expressed as a drawing.
        # No producer can do this today (the only one passes `OrderBlock.time`, always past),
        # which is precisely why it is worth pinning: the next one has no market event to warn
        # it. Regions may start *before* the window and usually do; they may not start after
        # the decision.
        for region in self.regions:
            if region.from_time > self.decided_at:
                raise ValueError(
                    f"region {region.label} starts at {region.from_time}, after the decision "
                    f"at {self.decided_at}: a decision cannot be drawn from a later bar"
                )
        # The same rule for the curves, and it is the same rule: an indicator reading stamped
        # after the decision is a value the strategy had not seen, drawn as justification for
        # what it did. The broker extends the *bars* past the decision, to the fill, and must
        # never extend a series alongside them — this is what would catch it trying.
        for series in self.series:
            if series.points[-1].time > self.decided_at:
                raise ValueError(
                    f"series {series.label} reaches {series.points[-1].time}, past the decision "
                    f"at {self.decided_at}: an indicator cannot be read from a later bar"
                )

    @property
    def filled_at(self) -> dt.datetime:
        """The opening instant of the window's last bar.

        The bar the order filled on once the broker has extended the window, and the decision
        bar until then — an order that never filled has no trade and no row to reach."""
        return self.bars[-1].time


@dataclass(frozen=True, slots=True)
class Signal:
    """A strategy's *intent*, before anyone decides how big it should be.

    Deliberately not an order. A strategy says "go long here, with the stop at 1.0950"; how
    many lots that is depends on the account, the risk budget and the instrument — none of
    which are the strategy's business. Keeping sizing out of the strategy is what lets the
    same strategy run on a $1 000 account and a $1 000 000 one without an edit, and it is
    the same separation as the cost model (ADR-07).
    """

    kind: SignalKind
    side: Side
    """For an EXIT, the side of the position being closed."""

    reference_price: Money
    """The close that triggered the decision. Sizing measures risk against it."""

    stop_loss: Money | None = None
    take_profit: Money | None = None
    reason: str = ""
    context: Mapping[str, Money | None] | None = None
    """What the indicators read at the instant this was decided.

    Captured here, at the entry, and carried untouched all the way to the `ClosedTrade` — it
    is the training material for the phase-3 analysis ("does this only work when ADX > 25?"),
    and recomputing it afterwards would mean re-running the engine and trusting nothing moved.
    None on an exit, and on any strategy with no indicators."""

    levels: tuple[SnapshotLevel, ...] = ()
    """The structural levels behind this decision, for the entry's picture.

    The strategy's, like the regions: only it knows which of the prices on the chart was the one
    that had to break. See `SnapshotLevel`."""

    series: tuple[SnapshotSeries, ...] = ()
    """The indicator curves behind this decision, for the entry's picture.

    The strategy is the only side that can supply them: an indicator carries the whole run's
    history, so nobody downstream can recover it from the bars. See `SnapshotSeries`."""

    regions: tuple[SnapshotRegion, ...] = ()
    """The bands of price this decision was made against, for the entry's picture.

    Separate from `context` because they are a different kind of fact: `context` is named
    scalars, exact and aggregatable, and it is what a later "does this only work when the zone
    is narrow?" reads. A region is a rectangle with a left edge in time, which is not a scalar
    and would have to be flattened into three keys nobody could reassemble. The engine carries
    both, and neither pretends to be the other. See `SnapshotRegion`."""

    limit_price: Money | None = None
    """Where the order should wait, or `None` to take the next open at market (ADR-0014).

    An indicator setup has no preferred price — "RSI crossed 30, buy" is answered by whatever
    the next open is. A structure setup is the opposite: it enters *at the edge of a region*,
    which is a price and not an instant, and filling it at the next open measures a trade the
    method would never have taken."""

    stop_price: Money | None = None
    """Where a **breakout** order waits, or `None` (ADR-0016). The mirror of `limit_price`.

    A limit rests on the side price has to come *back* to; a stop rests on the side price has to
    break *through*: a buy-stop above the market, a sell-stop below, filling as price runs into
    it. It is how the swing setups enter — "buy when price breaks the high of the candle the
    average turned on". At most one of `limit_price`/`stop_price` is set: an order is one or the
    other, never both."""

    client_id: str | None = None
    """The name this signal gives its order, so a later bar can take it back.

    A resting order outlives the bar that placed it, and only the strategy knows when it
    stopped making sense — its zone was mitigated, or aged out of the window. To say *that
    one*, it needs a name, and the name has to come from the strategy: the engine hands out
    no ids, because the engine is not the side that can carry one across bars. Required on a
    `CANCEL`, and on anything that rests."""

    def __post_init__(self) -> None:
        # A cancel that names nothing is a strategy asking the broker to guess which order it
        # meant. Refused here rather than downstream: by the time it reaches the broker the
        # bar that could have explained it is gone.
        if self.kind is SignalKind.CANCEL and self.client_id is None:
            raise ValueError("a CANCEL names the order it withdraws: client_id is required")
        # A stop modification with no level is the same shape of bug as a cancel with no name:
        # an intent that reaches the broker meaning nothing. Worse here, because the broker
        # would have to guess between "leave it" and "remove protection", and one of those
        # answers turns a stopped position into an unstopped one.
        if self.kind is SignalKind.MODIFY_STOP and self.stop_loss is None:
            raise ValueError("a MODIFY_STOP carries the new level: stop_loss is required")
        if self.limit_price is not None and self.limit_price <= ZERO:
            raise ValueError(f"limit price must be positive, got {self.limit_price}")
        if self.stop_price is not None and self.stop_price <= ZERO:
            raise ValueError(f"stop price must be positive, got {self.stop_price}")
        # An order is a limit *or* a stop, never both: the two say opposite things about where it
        # rests, so a price carrying both meanings is a bug, not a richer order.
        if self.limit_price is not None and self.stop_price is not None:
            raise ValueError("an order rests at a limit or a stop, not both")
        # A limit rests on the side price has to come back to: a buy waits *below*, a sell
        # *above*. The wrong side is not an exotic order, it is a sign error — and it does not
        # announce itself, because a buy limit above the market simply fills at the next open
        # while being sized against a price that never existed. The structure layer computes
        # these levels from zone edges, which is exactly where a top/bottom swap happens.
        if self.kind is SignalKind.ENTRY and self.limit_price is not None:
            wrong_side = (
                self.limit_price > self.reference_price
                if self.side is Side.LONG
                else self.limit_price < self.reference_price
            )
            if wrong_side:
                raise ValueError(
                    f"a {self.side.value} limit at {self.limit_price} is on the wrong side of "
                    f"{self.reference_price}: a buy limit rests below the market, a sell above"
                )
        # A stop is the mirror: a buy breaks *up* through a level above the market, a sell *down*
        # through one below. The wrong side is the worse sign error of the two — a buy stop below
        # the market is already triggered, so it fills at the next open as a silent market order,
        # sized against a level price never had to break.
        if self.kind is SignalKind.ENTRY and self.stop_price is not None:
            wrong_side = (
                self.stop_price < self.reference_price
                if self.side is Side.LONG
                else self.stop_price > self.reference_price
            )
            if wrong_side:
                raise ValueError(
                    f"a {self.side.value} stop at {self.stop_price} is on the wrong side of "
                    f"{self.reference_price}: a buy stop rests above the market, a sell below"
                )


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """A sized, submittable order: at market, or resting at a `limit_price` (ADR-0014).

    `decided_at` is the **opening instant of the candle the strategy was looking at** when
    it decided. It exists so the engine can *prove* the anti-lookahead rule rather than
    trust it: a fill may only land on a strictly later bar (see `loop._reject_lookahead`).
    That proof matters most to a limit order, which is the one fill in the engine priced
    *inside* a bar rather than at its open.

    One subtlety, and PR-105 depends on it: a protective exit (a stop or a target hit
    intrabar) inherits the `decided_at` of whatever **decided the level it came out at** —
    for a target, and for a stop that was never moved, the entry that placed it. That is
    honest, the level was decided then, and it is what lets a stop trigger on the very bar
    the position opened without tripping the lookahead guard.

    A stop that `modify_stop` has since moved carries the instant of the bar that moved it
    instead (ADR-0018), which is the same honesty applied to a level the entry no longer
    owns. The two live side by side in `_Protection`, and the exit takes the stamp belonging
    to the level that was actually hit.
    """

    symbol: str
    side: Side
    intent: SignalKind
    volume: Volume
    decided_at: dt.datetime
    stop_loss: Money | None = None
    take_profit: Money | None = None
    reason: str = ""
    context: Mapping[str, Money | None] | None = None
    """The indicator snapshot from the signal that produced this order. See `Signal.context`."""

    snapshot: EntrySnapshot | None = None
    """The bars the strategy had seen when it decided this. See `EntrySnapshot`.

    Attached by the **loop**, not by the strategy: the window is a fact about the stream, and
    the loop is the only component that sees the stream — in backtest and in live alike. A
    strategy keeping its own buffer would be four copies of one deque, each free to disagree
    about which bars it saw."""

    limit_price: Money | None = None
    """The price this order waits at, or `None` for "fill at the next open". See `Signal`."""

    stop_price: Money | None = None
    """The breakout level this order waits at, or `None`. Mirror of `limit_price` (ADR-0016)."""

    client_id: str | None = None
    """The strategy's name for this order, so it can be cancelled later. See `Signal`."""

    def __post_init__(self) -> None:
        _require_utc(self.decided_at, "OrderRequest.decided_at")
        if self.volume <= ZERO:
            raise ValueError(f"order volume must be positive, got {self.volume}")
        # A cancel withdraws an order; it is not one. It carries no volume, no side and no
        # fill, and letting it be built as an `OrderRequest` would put it in the queue the
        # broker fills — which is exactly the queue it is supposed to empty.
        if self.intent is SignalKind.CANCEL:
            raise ValueError("a cancel is not an order: withdraw it through Broker.cancel")
        # Same argument, same queue: a stop modification acts on a position that already
        # exists. Built as an order it would carry a volume nobody asked for and sit in the
        # queue waiting to open a *second* position (ADR-0018).
        if self.intent is SignalKind.MODIFY_STOP:
            raise ValueError("a stop modification is not an order: use Broker.modify_stop")
        if self.limit_price is not None and self.limit_price <= ZERO:
            raise ValueError(f"limit price must be positive, got {self.limit_price}")
        if self.stop_price is not None and self.stop_price <= ZERO:
            raise ValueError(f"stop price must be positive, got {self.stop_price}")
        if self.limit_price is not None and self.stop_price is not None:
            raise ValueError("an order rests at a limit or a stop, not both")


@dataclass(frozen=True, slots=True)
class OrderResult:
    """What the broker said when it was handed an order."""

    order: OrderRequest
    accepted: bool
    reason: str = ""


class RefusedBy(StrEnum):
    """Which of the three gates between intent and the book turned an order away.

    They are not interchangeable, and the difference is what a strategy would act on:
    `SIZING` and `RISK` are answers about *this account at this moment* and change on their own
    as equity does, while `BROKER` is an answer about the order itself. Collapsing them into one
    "refused" would leave a strategy unable to tell "not with this much money" from "never this
    order", which are opposite instructions.

    ⚠️ The last two arrive from **outside this process**, after `submit` already answered
    `accepted` (ADR-0024). They are separate members rather than one "downstream" because they
    behave oppositely: `EXECUTOR` refuses on a condition that changes on its own — a session that
    resumes beating, a volume cap the next size clears — while `VENUE` is usually the same answer
    next bar. A strategy deciding whether to offer a zone again is asking exactly that.
    """

    NO_POSITION = "no_position"
    """An exit was asked for and there was nothing open to close.

    The ghost read from the other side: a strategy asking to close a position it does not hold
    is a strategy whose bookkeeping has already come apart from the broker's, and that is worth
    telling it rather than logging at debug."""

    SIZING = "sizing"
    """The risk manager sized the order at zero, or had no stop to size against."""

    RISK = "risk"
    """The risk manager vetoed an order it had already sized."""

    BROKER = "broker"
    """The broker declined to take it locally: a name it already holds, an order it cannot rest."""

    EXECUTOR = "executor"
    """A safeguard between here and the venue said no — the kill switch, the volume cap, a
    session that stopped beating. Conditions that change without anybody changing the order."""

    VENUE = "venue"
    """The terminal itself refused. Carries the venue's own retcode in `detail`, because that
    number is the only thing that distinguishes "your stop is too close" from "trading is off"."""


@dataclass(frozen=True, slots=True)
class Refusal:
    """An order the strategy asked for that **never reached the book**, and why.

    The sibling of `Fill`, and it exists for the sibling reason. `Context.fills` was added
    because a strategy that never learns its order became a trade goes on treating the order as
    resting; this is the same sentence with the other verb. A setup marks its armed order placed
    the instant it *emits* the signal (`setups.py`), so every gate that turns an order away
    silently leaves the strategy believing an order rests somewhere it does not — the ghost
    ADR-0023 measured at **four of five** hand-over points, arriving through a different door.

    ⚠️ **Correlated by `client_id`, not by holding the order.** `Fill` can carry its
    `OrderRequest` because a fill implies one was built; a refusal does not — the sizing gate
    turns a `Signal` away before any `OrderRequest` exists. The name is the one thing present at
    all three gates, and it is what the strategy keyed its own bookkeeping by.

    ⚠️ `client_id` is optional because a `Signal` need not carry one: a strategy that does not
    name its orders (the DSL's condition trees do not) still gets told it was refused, it just
    cannot match the refusal to a specific armed zone. An unnamed refusal is a log line, not a
    correlation — which is honest, and better than pretending to a name that was never chosen.
    """

    client_id: str | None
    intent: SignalKind | None
    """What the order was for, or `None` when whoever built this could not tell.

    ⚠️ **Optional for the same reason `client_id` is, and it was not always.** A refusal that
    travels home from another process (ADR-0024) is matched against the orders this session
    sent, and a session that restarted has forgotten them. Defaulting to `ENTRY` there produced
    a message asserting a fact it did not have — indistinguishable, to any reader, from one that
    did. `wire.py` refuses to guess a `kind` one layer down; this is the same rule, and a reader
    that branches on intent can now tell the unknown from the known."""

    refused_by: RefusedBy
    reason: str = ""
    """What the **strategy** called this order (`"entry.choch"`), not why it was refused."""

    detail: str = ""
    """Why the gate said no, in its own words. Free text: it is for a human reading a log or an
    incident, and the machine-readable half is `refused_by`."""


@dataclass(frozen=True, slots=True)
class Fill:
    """An order that actually executed, at a price, at a time, for a cost."""

    order: OrderRequest
    time: dt.datetime
    price: Money
    volume: Volume
    costs: Money

    def __post_init__(self) -> None:
        _require_utc(self.time, "Fill.time")
        if self.price <= ZERO:
            raise ValueError(f"fill price must be positive, got {self.price}")
        if self.volume <= ZERO:
            raise ValueError(f"fill volume must be positive, got {self.volume}")
        # Costs are a magnitude. A negative one is money appearing in the balance out of
        # nowhere — and it would look exactly like a profitable strategy.
        if self.costs < ZERO:
            raise ValueError(f"costs are a magnitude and cannot be negative, got {self.costs}")


@dataclass(frozen=True, slots=True)
class Position:
    """An open position. There is no `current_price` field, and that is deliberate.

    A position does not know what it is worth — marking to market needs a candle, and a
    position carrying a stale price is a position that will eventually be valued against a
    bar that has already gone.
    """

    symbol: str
    side: Side
    volume: Volume
    entry_price: Money
    entry_time: dt.datetime
    entry_costs: Money = ZERO
    stop_loss: Money | None = None
    """The stop protecting this position **right now** — the level it would exit at today.

    It moves. `Broker.modify_stop` tightens a stop mid-trade (ADR-0018) and this field moves
    with it, because where a position's stop sits is a fact about the *position*, not about
    the order that opened it. It is also what a real venue reports: MT5's `POSITION_SL` is
    the current stop, not the one the entry carried.

    **This is the field a trailing strategy reads** to decide whether a new level tightens.
    Reading `initial_stop_loss` for that is how a strategy asks the engine to loosen a stop
    it has already moved — and the engine refuses that with an `EngineError`.
    """
    initial_stop_loss: Money | None = None
    """The stop the position **opened** with, frozen at the fill and never touched again.

    The lot was sized against this distance (`PercentRiskManager`), so it is the money the
    position stood to lose when it was opened. That makes it the only honest denominator for
    an R multiple — measuring against a stop that has since been dragged to breakeven would
    report every trailing win as an infinite one — and it is what `ClosedTrade.stop_loss`
    records.

    `None` on a position whose entry carried no stop, *including* one that was later given a
    stop by `modify_stop`: nothing sized that lot against a level, so it has no R.

    Before ADR-0018 a stop could not move, so one field said both of these things at once and
    was right by accident. The moment it can move they are two different facts. `_Protection`
    split its decision instant for exactly the same reason, one level down.
    """
    take_profit: Money | None = None
    context: Mapping[str, Money | None] | None = None
    """The indicator snapshot from the entry that opened this position. See `Signal.context`."""

    snapshot: EntrySnapshot | None = None
    """The bars around the entry, arming window included. See `EntrySnapshot`.

    Carried on the position for the same reason `context` is: nothing else survives from the
    fill to the close, and the `ClosedTrade` is built at the close."""


@dataclass(frozen=True, slots=True)
class AccountState:
    """Where the money is.

    `balance` is settled: it only moves when a position closes. `equity` is balance plus
    what the open position is worth right now. The gap between them is the whole reason
    drawdown is measured on equity — an account can be one bad open trade away from a
    margin call while its balance still says everything is fine.

    `currency` is the account's deposit currency, and it comes from the account. It is
    **not** the instrument's quote currency: trade USDJPY on a USD account and the quote
    currency is the yen, while every number here is in dollars.
    """

    balance: Money
    equity: Money
    currency: str = "USD"


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """A round trip. `costs` is the **whole** round trip — both legs.

    Getting that wrong is not cosmetic. Every metric in PR-106 (expectancy, profit factor,
    R-multiple) is computed from this, and the property PR-105 must satisfy is that the
    trades reconcile with the equity curve: `sum(net_pnl) == final equity - initial`. Drop
    the entry's cost here and the table reports more profit than the account ever had — in
    forex, where the entry spread is usually the dominant cost, systematically so.
    """

    symbol: str
    side: Side
    volume: Volume
    entry_time: dt.datetime
    entry_price: Money
    exit_time: dt.datetime
    exit_price: Money
    gross_pnl: Money
    costs: Money
    net_pnl: Money
    reason: str = ""
    stop_loss: Money | None = None
    """The stop this trade was **sized against** — the position's `initial_stop_loss`.

    Not necessarily the level it exited at. A trade closed by a stop that `modify_stop` had
    trailed reports `reason='sl'` with an `exit_price` some distance from this number, and
    that is correct: this is the denominator `r_multiple` divides by, and a record whose
    risk and whose R came from different stops would contradict itself.
    """
    take_profit: Money | None = None
    r_multiple: Money | None = None
    """Net result in multiples of the risk taken: `net_pnl / (money risked at the stop)`. The
    unit that lets trades with different sizes and stops be compared — a +2R win is the same
    edge whether it made $200 or $2 000. None when the position carried no stop to measure
    risk against."""
    context: Mapping[str, Money | None] | None = None
    """The indicator snapshot from the entry. See `Signal.context`."""

    snapshot: EntrySnapshot | None = None
    """The bars around the entry, arming window included. See `EntrySnapshot`."""


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """The account's equity at the close of one candle."""

    time: dt.datetime
    equity: Money


@dataclass(frozen=True, slots=True)
class Context:
    """Everything a strategy is allowed to see, and nothing else.

    This is the anti-lookahead rule made structural. The strategy is not handed the list of
    candles and asked politely not to look ahead in it — it is handed *one* candle, the one
    that just closed. There is no future in this object to peek at.
    """

    candle: Candle
    instrument: InstrumentSpec
    account: AccountState
    position: Position | None = None

    fills: tuple[Fill, ...] = ()
    """The fills born inside this bar, before the strategy saw its close (ADR-0015).

    Not a relaxation of the anti-lookahead rule: these are bar-N events handed to a
    strategy deciding on bar N's close — the same thing a live terminal does when it
    pushes a fill notification the moment it happens. The field exists because
    `position` alone cannot report one real outcome: a limit order that fills and is
    stopped out inside a single bar opens a position that is already gone by the time
    this object is built, and a strategy that never learns its order became a trade
    will treat the order as still resting.
    """

    refusals: tuple[Refusal, ...] = ()
    """Orders asked for on the **previous** bar that never reached the book (see `Refusal`).

    ⚠️ **One bar behind `fills`, and the asymmetry is structural rather than a delay somebody
    settled for.** A fill is born in step 1, before the strategy is shown the bar, so it belongs
    to the bar it is handed with. A refusal is born in steps 3-4, *after* the strategy has
    spoken — it cannot exist any earlier, because there was nothing to refuse. Handing bar N's
    refusals to bar N's own `Context` would mean building the context after the decision it is
    supposed to inform, which is the anti-lookahead rule read backwards.

    So the earliest honest place is here, and it is early enough: the strategy learns before it
    decides anything else. What it must not do is treat this as "nothing happened for a bar" —
    an order refused on bar N was never resting *during* bar N either.
    """


@dataclass(frozen=True, slots=True)
class EvalContext:
    """What a DSL condition is allowed to read on one bar — and how far back.

    Richer than `Context`, which hands the strategy a single candle. A condition speaks the
    DSL's reference grammar, and that grammar reaches named indicators and closed candles N
    bars back — neither of which fits in a one-candle view. But it is the *same*
    anti-lookahead rule, not a relaxation of it: `candles[0]` is the bar that just closed,
    and there is no index into this object that reaches a bar which has not.

    **Newest-first**, deliberately. `candles[0]` is the current bar N, `candles[1]` is N-1,
    and `candle[-1]` in the DSL is `candles[1]` here. Indicator values follow the same
    convention: `indicator_values["sma_fast"][0]` is this bar's value, `[1]` the previous
    bar's, and `None` anywhere the indicator was still warming up.

    Holding only resolved *values* — candles and decimals, never indicator objects — is what
    keeps the domain free of the indicator machinery. The strategy owns the indicators and
    the rolling windows; by the time a condition sees an `EvalContext`, everything is a plain
    number the anti-lookahead rule has already vouched for.
    """

    candles: tuple[Candle, ...]
    indicator_values: Mapping[str, tuple[Money | None, ...]]
    position: Position | None = None

    def candle_at(self, offset: int) -> Candle | None:
        """The bar `offset` steps back from the current one (0 = now), or `None` past the
        edge of what has been seen. A ref reaching past the horizon is not an error — early
        in a run there simply is no candle there, and the condition that asked is false."""
        if 0 <= offset < len(self.candles):
            return self.candles[offset]
        return None

    def indicator_at(self, indicator_id: str, offset: int) -> Money | None:
        """This indicator's value `offset` bars back (0 = now), or `None` if it is unknown —
        the indicator was warming up, or there is not yet that much history to look back on.
        An unknown id resolves to `None`; the compiler is what proves ids exist, not this."""
        history = self.indicator_values.get(indicator_id)
        if history is None or not (0 <= offset < len(history)):
            return None
        return history[offset]


def to_tick(price: Money, tick: Money, rounding: str) -> Money:
    """Snap a computed level onto the instrument's price grid.

    A stop is a price someone has to be able to place. Ten percent of a zone's width is not
    generally a multiple of the tick, and a stop at 1.094375 on a five-digit pair is a level
    that does not exist — it would fill in the backtest and be rejected by the venue.

    ⚠️ **The direction is the caller's, and it is never "nearest".** Every level in this system
    is rounded the way that costs the trade rather than flatters it: an entry away from the
    price it hoped for, a stop no nearer than the rule said. `ROUND_HALF_EVEN` would be right
    for a measurement and wrong for a level, because a level is a promise about where money
    changes hands.

    It lives here, with `Money`, because two modules now ask for it and a third copy is how the
    setups file's own warning comes true — *"written twice they agree on every round number and
    part company on the first zone whose half falls between ticks, and nothing downstream would
    report the disagreement"*.
    """
    return (price / tick).to_integral_value(rounding=rounding) * tick
