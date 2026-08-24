"""The five seams (sdd.md §3.3.2).

Every one of these is a `Protocol`, not a base class, and the difference is the whole
design. With inheritance, `BacktestBroker` and `MT5Broker` would share an ancestor — and
the moment they do, the tempting shortcut appears: put a little logic in the base, and now
the engine's behaviour depends on which subclass it got. Structural typing gives the engine
an interface and nothing to inherit. It cannot reach into an implementation because there
is no implementation to reach into.

That is what ADR-04 buys in practice: the engine never learns where an order is executed.
The same strategy, the same loop, the same P&L arithmetic run against a simulated fill in a
backtest and a real one at a broker — the one property that stops "backtest result" and
"live result" from describing two different systems.

Which means these signatures have to be honest about **live**, not just convenient for the
backtest. `positions()` takes a symbol because a real MT5 account holds positions the
strategy never opened — another EA's, a manual trade, another symbol entirely.
"""

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from tradeforge_engine.domain import (
    AccountState,
    Candle,
    ClosedTrade,
    Context,
    EvalContext,
    Fill,
    InstrumentSpec,
    Money,
    OrderRequest,
    OrderResult,
    Position,
    Signal,
    Volume,
    ZoneMark,
)


@runtime_checkable
class Broker(Protocol):
    """Executes orders. Knows nothing about strategies.

    The split between `submit` and `on_bar` is the anti-lookahead rule in the type system:
    submitting an order does **not** execute it. `submit` queues; `on_bar` is the only place
    a `Fill` can ever be born, and it runs at the top of the *next* candle.

    Three obligations that the types cannot state, and that the engine checks or depends on:

    1. **`on_bar` must leave `account()` marked to market at the candle it was handed.**
       The loop reads equity straight after, once per bar. A broker that forgets to mark
       produces an equity curve that is really a balance curve — a flat line through a 50%
       drawdown, and a maximum drawdown of zero on a strategy that halved the account.
    2. **Fills returned from one `on_bar` are ordered exits before entries.** A reversal on
       a single bar closes before it opens; the other order makes the ledger refuse a second
       position, correctly, on a sequence that was merely mis-sorted.
    3. **A protective exit inherits the `decided_at` of the entry that placed it.** A stop
       hit intrabar was decided when the stop was set, not when price reached it — and that
       is what lets a stop trigger on the very bar the position opened without tripping the
       engine's lookahead guard.
    """

    def submit(self, order: OrderRequest) -> OrderResult:
        """Queue an order. Returns whether it was accepted, not whether it executed."""
        ...

    def cancel(self, client_id: str) -> bool:
        """Withdraw an order still waiting, by the name its strategy gave it. ADR-0014.

        Only a resting order — one with a `limit_price` or a `stop_price`, waiting for the
        market to reach it — can be withdrawn; a market order queued on the last bar has
        already executed by the time anyone could ask.

        **Returns false rather than raising when nothing is found.** The order may have
        filled on this very bar, or been cancelled already, or never have existed. In live
        that is a race, not a bug: the venue fills while the cancel is in flight, and a
        broker that raised would turn a normal execution into a dead session.

        The *reason* an order should stop existing is never the broker's to know. A limit
        order in this system is alive because a zone is alive, and zones are the strategy's
        vocabulary — the broker holding a rule like "cancel when the region is mitigated"
        would be the core learning what a region is (AGENTS.md §5.4, in spirit).
        """
        ...

    def modify_stop(self, symbol: str, stop_loss: Money, decided_at: dt.datetime) -> bool:
        """Move the protective stop of the open position in `symbol`. ADR-0018.

        The one level a strategy may change while a trade runs. Not the target, not the size,
        and not a resting order's planned stop — that one is cancelled and re-placed.

        **`decided_at` is the caller's, and the caller is the loop.** It is the opening
        instant of the bar whose close decided the new level, and it is what keeps the
        anti-lookahead guard armed on a level that now changes mid-trade: a stop decided on
        bar N must not be able to exit *inside* bar N. The loop's own ordering already
        prevents that — `on_bar` runs before the strategy sees the bar — but an honest stamp
        is what lets `loop._reject_lookahead` **prove** it instead of the ordering merely
        happening to hold. Inherit the entry's stamp instead and the guard goes quiet on the
        exact level that stopped being fixed.

        **The stop may only tighten.** A long's new stop must be at or above the current one,
        a short's at or below; equal is accepted, so a strategy recomputing the same level
        every bar is not punished for it. Loosening raises: the position was sized against
        the original stop (`RiskManager`), so moving it away adds risk nobody authorised, and
        a sign error in a strategy is exactly how that happens.

        **Returns false rather than raising when there is no position to protect.** Same
        argument as `cancel`: in live the trade may have closed while the instruction was in
        flight, and that is a race, not a bug. A position holding no protection at all *is*
        modifiable — arming a stop where there was none only reduces risk.

        **After a `True`, `positions()` must report the new level in `Position.stop_loss`.**
        An implementation that moves the stop at the venue and keeps handing back the entry's
        level publishes two answers to "where is my stop" and gives the strategy the stale
        one — which is then judged against the fresh one by the tightening rule above, so a
        trailing strategy asking the obvious question gets an `EngineError` it had no way to
        avoid. `initial_stop_loss` does not move: that is the risk the lot was sized against.
        """
        ...

    def on_bar(self, candle: Candle) -> Sequence[Fill]:
        """A new bar has arrived. Fill what is pending, check stops and targets, and mark
        the account to market at this candle's close.
        """
        ...

    def positions(self, symbol: str) -> Sequence[Position]:
        """What is open **in this symbol**, according to whoever is holding the risk.

        The argument is not decoration. A live MT5 account reports every position it holds:
        another expert advisor's, a manual trade, an entirely different instrument. A
        broker interface that returns "the positions" and lets the caller take the first is
        an interface that will one day close five lots of gold because a strategy trading
        EURUSD asked to exit.
        """
        ...

    def account(self) -> AccountState:
        """Balance and equity, from the same authority. Marked to market by `on_bar`."""
        ...

    def trades(self) -> Sequence[ClosedTrade]:
        """The round trips **this run** has closed. Not the account's history.

        The distinction is the same one `positions(symbol)` makes, and it matters for the
        same reason: a live MT5 account's deal history holds another expert advisor's trades,
        another symbol's, and everything from before this session started. Return those and
        the reconciliation property — `sum(net_pnl) == final equity - initial capital` — is
        false in live for reasons that have nothing to do with the strategy. An `MT5Broker`
        filters by its own magic number.

        The broker holds the ledger, so the broker reports the trades: the same authority
        that reports the account. PR-106's metrics are computed from these.
        """
        ...


@runtime_checkable
class Strategy(Protocol):
    """Turns a closed candle into intent.

    Returns `Signal`s, not orders: how big the position should be is not the strategy's
    business (see `Signal`). In PR-104 the DSL compiles into an implementation of this.
    """

    def on_bar(self, context: Context) -> Sequence[Signal]:
        """Decide. `context` holds exactly one candle — the one that just closed."""
        ...


@runtime_checkable
class Indicator(Protocol):
    """Incremental state, O(1) per candle.

    Not "recompute a 200-bar window on every bar": a backtest over a decade of M1 data is
    five million bars, and an O(n) update per bar makes the whole thing O(n²). The
    incremental form is also the only one that can run live, where there is no window to
    recompute — just the next candle.

    `value()` returns `None` until enough candles have been seen. That is not a nuisance; it
    is the warm-up period, and a strategy that trades on bar 3 of a 20-period average is
    trading on a number that does not exist yet.
    """

    def update(self, candle: Candle) -> None:
        """Fold one **closed** candle into the state."""
        ...

    def value(self) -> Money | None:
        """The current value, or `None` while still warming up."""
        ...


@runtime_checkable
class CompositeIndicator(Protocol):
    """One state machine, several readings — Bollinger's three bands, ADX's three lines.

    **Why not three ordinary `Indicator`s.** A document could then declare `bb_upper` with
    period 20 and `bb_lower` with period 50, and nothing anywhere would object: for the schema
    they are two independent, well-formed indicators. What comes out is a band whose rails
    describe different markets, and it looks exactly like a band. Making the three readings one
    object means the parameters cannot disagree, because there is only one set of them. The
    computation being shared instead of run three times is a consequence, not the reason.

    **Why a second protocol rather than widening `Indicator`.** Widening it would touch all six
    single-valued indicators and `Charted.overlays`, to give five of them a components mapping
    with one entry that no caller wants. Structural typing means the two can simply coexist:
    `build_indicator` returns either, and the compiler asks which it got — once, at compile
    time, never per bar.

    **The components warm up independently, and that is load-bearing.** ADX's `plus_di` is
    available `period - 1` bars before its `adx`, because the ADX line is a second Wilder
    smoothing *of* the DX built from the DI pair. So the mapping is `str -> Money | None`, not
    `None` for the whole object: a condition reading `adx_14.plus_di` must be answerable while
    `adx_14.adx` is still warming.

    ⚠️ **`update` must ignore a candle it has already folded**, identified by its time. The
    single-valued indicators never meet this: one object, one caller. A composite is reached
    through one channel per component, and the overlay reader drives *each channel it was
    handed* — so the same bar arrives once per component, and a state machine that counted it
    three times would report an ADX built from three times the bars anyone asked for. Folding
    the same closed bar twice is not two bars.
    """

    def update(self, candle: Candle) -> None:
        """Fold one **closed** candle into the state, unless this one is already in it."""
        ...

    def components(self) -> Mapping[str, Money | None]:
        """Each reading under its own name, `None` while that one is still warming up.

        The names are the second half of a DSL reference: `bb.upper` reaches the `"upper"` of
        the indicator declared as `bb`. They are fixed per indicator type and duplicated in the
        schema package, which cannot import this one — `apps/api` depends on both and pins them
        equal, because drift here is the silent kind: the schema would accept a reference the
        engine resolves to `None`, and a comparison against `None` is simply false for ever.
        """
        ...


@runtime_checkable
class Charted(Protocol):
    """A strategy that can say which curves it was reading, for something to draw them.

    **Optional, and the engine never calls it.** Nothing in the event loop asks a strategy for
    this; a strategy that does not implement it simply charts as bars and trades. The protocol
    exists so a *reader* — the API serving a price chart — can ask a question the loop has no
    reason to ask.

    **The values are not persisted, and that is deliberate.** An indicator series is derivable
    from the candles and the parameters, and this project's rule is that derivable data is
    recomputed rather than stored: the equity curve of a single run already reaches 856 kB, and
    a series per indicator per run would grow the same way for something a chart reads once.

    **It returns the strategy's own indicators, not a description of them.** A description
    ("EMA, period 9, on close") would have to be turned back into an indicator by whoever drew
    it, and that reconstruction is a second place where "which average does this setup use"
    lives. The day a setup changed its average, the description path would keep drawing the old
    one — correct-looking, and wrong. Handing back the object removes the second place entirely.

    ⚠️ **The objects are live and mutable.** A caller that drives them with `update` advances
    the very indicators the strategy would read, so this must be asked of a strategy built for
    charting and used for nothing else — never of one that is executing a backtest. Driving
    them is exact for the setups here because each updates its average on **every** bar,
    position or not (see `Mme9BreakoutStrategy.on_bar`), so the standalone series and the one
    the run computed cannot differ.
    """

    def overlays(self) -> Mapping[str, Indicator]:
        """Label to indicator, in drawing order. Empty is a valid answer."""
        ...


@runtime_checkable
class Zoned(Protocol):
    """A strategy that marks regions on the chart, for something to draw them.

    The rectangle half of `Charted`, and a separate protocol rather than another method on it
    because the two are genuinely independent: the swing setups read a curve and mark no zones,
    the structure setups mark zones and read no curve. One protocol with two methods would make
    every implementer answer a question it has no opinion on.

    **Immutable records, unlike `Charted`.** That asymmetry is deliberate. An indicator has to be
    handed over live, because the only faithful way to get its series is to drive it. A region is
    already history by the time anyone asks: the detector has been folding candles into it all
    run, and what is left is a fact, not a machine. Handing back `TrackedZone` would give a
    reader the detector's own mutable bookkeeping — advancing what it means to describe.
    """

    def zones(self) -> Sequence[ZoneMark]:
        """The regions the strategy still holds, oldest first. Empty is a valid answer.

        ⚠️ **Still holds, not "every one it ever marked".** A detector keeps a bounded history
        — `OrderBlockDetector` drops the oldest past `_MAX_ZONES` — so a long run's earliest
        regions are gone from this answer, and nothing in it says so. That bound is the
        strategy's own and predates any of this: what a reader gets here is what the strategy
        was actually carrying, which is the honest thing to draw. A reader wanting a complete
        history has to record marks as they happen; it cannot ask afterwards.
        """
        ...


@runtime_checkable
class Condition(Protocol):
    """One node of the strategy's expression tree, evaluated once per closed candle.

    Takes an `EvalContext`, not the loop's `Context` (sdd.md §3.3.2). A condition can name
    an indicator and reach a candle N bars back, and neither of those lives in the
    single-candle view the loop hands the strategy. Same anti-lookahead discipline, wider
    vocabulary — and the wider vocabulary is precisely why it needs its own context.
    """

    def evaluate(self, context: EvalContext) -> bool: ...


@runtime_checkable
class CostModel(Protocol):
    """What a trade costs. Plugged in, never hard-coded (ADR-07).

    Forex pays a spread and a swap; a stock pays a commission; an index future pays both. An
    `if asset_class == ...` inside the engine would spread that difference across every fill
    site in the codebase. A cost model is one object, and adding a new asset class is a new
    implementation of this protocol — not an edit to the core.

    **`candle` is the bar the fill is being born on, and it is a parameter for a reason.**
    A backtest charges a spread chosen once, in a configuration; a paper session charges the
    spread the venue was actually quoting, which arrives on every bar (`Candle.spread`, in
    points). That is one number that changes per fill, and there are exactly two shapes it
    could have taken: an argument, or a field on the model that somebody updates before each
    bar. The field is the cheaper diff and the worse design — nothing in the type system says
    who updates it or when, and a model holding the *previous* bar's spread charges a
    plausible number, silently, for ever. As an argument it cannot be stale: the only place a
    `Fill` is born is inside `Broker.on_bar`, which was handed this very candle.

    ⚠️ The whole candle, not `spread_points`. Naming the spread here would put "cost means
    spread" back into the seam that exists precisely to not know that — a commission model
    ignores it, and a model that one day charges by traded range needs the bar, not a number
    somebody picked out of it for them.
    """

    def entry_cost(
        self, order: OrderRequest, instrument: InstrumentSpec, price: Money, candle: Candle
    ) -> Money:
        """What it costs to open. A magnitude, never negative — the domain refuses one."""
        ...

    def exit_cost(
        self, order: OrderRequest, instrument: InstrumentSpec, price: Money, candle: Candle
    ) -> Money:
        """What it costs to close."""
        ...


@runtime_checkable
class RiskManager(Protocol):
    """How big, and whether at all.

    Two questions, deliberately separate. `size` is arithmetic: given this stop, this account
    and this instrument, how many lots put exactly 1% at risk. `allow` is a veto: the daily
    loss limit is hit, the kill switch is on, there are already three positions open. Sizing
    must not be where the kill switch lives, because a sizing bug would then be a safety bug.
    """

    def size(self, signal: Signal, account: AccountState, instrument: InstrumentSpec) -> Volume:
        """Lots (or shares). Zero means "do not take this trade"."""
        ...

    def allow(self, order: OrderRequest, account: AccountState) -> bool:
        """The veto. False means the order never reaches the broker."""
        ...
