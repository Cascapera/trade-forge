"""The hand-over: what a warm-up carries into a session, and what it must not.

The first version of this module vetoed every order decided on history. It kept the account
clean and broke the strategy — a setup marks its armed order placed when it *emits* the signal,
so it crossed into the session believing an order rested at a venue that had never heard of it.
Measured on real EURUSD H1 bars with the CHoCH setup, four of five hand-over points produced
that ghost.

So the tests here are written against the thing that went wrong: a **real setup**, over **real
market shapes**, checked for agreement between what the strategy believes and what the broker
holds. A scripted strategy cannot fail that way — it has no bookkeeping to disagree with — and
would have passed the broken design.
"""

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal

import pytest

from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.costs import NoCostModel
from tradeforge_engine.domain import (
    AccountState,
    Candle,
    Context,
    InstrumentSpec,
    OrderRequest,
    OrderResult,
    RefusedBy,
    Side,
    Signal,
    SignalKind,
)
from tradeforge_engine.errors import EngineError
from tradeforge_engine.indicators import EMA, SMA
from tradeforge_engine.loop import iter_run
from tradeforge_engine.protocols import Broker
from tradeforge_engine.risk import PercentRiskManager
from tradeforge_engine.setup_factory import build_setup
from tradeforge_engine.testing import (
    EURUSD,
    HOUR,
    START,
    ImmediateFillBroker,
    ScriptedStrategy,
    arms_a_resting_limit,
    rising,
)
from tradeforge_engine.warmup import HandOver, hand_over, unwarmed_indicators

# The window this file measured lives in `tradeforge_engine.testing` now, because a second suite
# needs it: `apps/api` drives a whole session over it to check that a live session's row is on
# file *before* the hand-over re-submits what the warm-up left resting. Copying 175 bars is how
# one fixture becomes two that disagree — the same argument that module's own docstring makes.
a_structure_market = arms_a_resting_limit


def test_the_measured_window_is_handed_out_as_a_copy() -> None:
    """⚠️ Two suites in two packages now consume this fixture, and one of them slices it. A
    shared list would be a fixture each could hand the other in a state neither wrote — and the
    failure would land in whichever ran second, looking like a bug in the code under test.

    Pinned because the mutant survives otherwise: a module-level `_SHARED = list(...)` returned
    from the function passes all nineteen tests here. The docstring claims the protection, so
    something has to prove it.
    """
    assert arms_a_resting_limit() is not arms_a_resting_limit()
    assert arms_a_resting_limit() == list(arms_a_resting_limit())


CAPITAL = Decimal(10_000)


def a_broker() -> BacktestBroker:
    return BacktestBroker(instrument=EURUSD, initial_capital=CAPITAL, cost_model=NoCostModel())


RISK = PercentRiskManager(percent=Decimal(1))


def carry(warm: Broker, live: Broker, *, bars: int = 175) -> HandOver:
    """`hand_over` with the two seams a re-size needs, so a test names only what it varies.

    Typed on `Broker`, not `BacktestBroker`: one scenario deliberately hands it a broker that
    cannot enumerate its resting orders, and narrowing here would make that unwritable.
    """
    return hand_over(warm, live, symbol="EURUSD", bars=bars, risk=RISK, instrument=EURUSD)


def a_context(candle: Candle) -> Context:
    return Context(
        candle=candle,
        instrument=EURUSD,
        account=AccountState(balance=CAPITAL, equity=CAPITAL),
        position=None,
        fills=(),
    )


def warm(candles: list[Candle], strategy: object) -> BacktestBroker:
    """Run history through the real loop against a real broker — a backtest, deliberately."""
    broker = a_broker()
    for _ in iter_run(
        candles=candles,
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=strategy,  # type: ignore[arg-type]
        broker=broker,
        risk=PercentRiskManager(percent=Decimal(1)),
    ):
        pass
    return broker


# --------------------------------------------------------------------------- #
# The thing that broke: a real setup crossing the line                          #
# --------------------------------------------------------------------------- #


def test_a_resting_order_survives_the_hand_over() -> None:
    """The whole point. 35% to 73% of bars leave one resting, measured on EURUSD H1 — so an
    order that does not cross is a region the session silently never trades."""
    strategy = build_setup({"type": "structure_choch"})
    warmed = warm(a_structure_market(), strategy)
    assert warmed.resting(), "the scenario armed nothing; it cannot show a hand-over"

    live = a_broker()
    result = carry(warmed, live)

    assert result.carried, "the resting order did not cross"
    assert result.refused == ()
    assert [order.client_id for order in live.resting()] == [
        order.client_id for order in warmed.resting()
    ]


def test_the_strategy_and_the_live_broker_agree_after_the_hand_over() -> None:
    """The ghost, stated directly: the strategy believes it placed an order, so the broker must
    be holding one. This is the assertion the vetoing design failed, four times in five."""
    strategy = build_setup({"type": "structure_choch"})
    warmed = warm(a_structure_market(), strategy)

    live = a_broker()
    carry(warmed, live)

    armed = getattr(strategy, "_armed", None)
    believes_placed = armed is not None and armed.placed
    assert believes_placed, "the scenario never armed anything; nothing to disagree about"
    assert live.resting(), "the strategy believes it placed an order the broker never got"


def test_the_money_does_not_cross() -> None:
    """The other half of the bargain. Warm-up is a backtest, so it moves the account — and the
    session must start at its initial capital regardless of how that backtest went."""
    strategy = build_setup({"type": "structure_choch"})
    warmed = warm(a_structure_market(), strategy)

    assert warmed.account().equity != CAPITAL, (
        "the warm-up did not move the account, so 'it did not cross' and 'it crossed an "
        "identical number' are the same fact and this test proves neither"
    )

    live = a_broker()
    carry(warmed, live)

    assert live.account().balance == CAPITAL
    assert live.account().equity == CAPITAL
    assert live.trades() == ()


def test_warm_up_really_does_trade() -> None:
    """⚠️ The separating test. Everything above is satisfied by a warm-up that did nothing at
    all — and the design before this one did exactly that, on purpose. If history stops producing
    fills, these tests stop meaning anything and this is the one that says so."""
    strategy = build_setup({"type": "structure_choch"})
    warmed = warm(a_structure_market(), strategy)

    assert warmed.trades(), "history closed no round trip; the design's premise is untested"
    assert warmed.resting(), "history left nothing armed; the hand-over has nothing to carry"
    assert warmed.account().equity != CAPITAL, "the account never moved"

    # ⚠️ **The numbers, not just their shape**, and the window moving into `src/` is what makes
    # that necessary: `apps/api` now drives a whole session over these same bars, so a drift here
    # would surface as a lot mismatch in another suite in another package.
    #
    # Measured, by removing each of the 175 bars in turn and re-running: **9 of them change the
    # outcome and 166 do not**. That is not a gap — most bars of any market decide nothing, and a
    # test that could tell them apart would be pinning noise. What the 9 do split into two groups,
    # and it takes both assertions below to cover them:
    #
    #     bars 33, 50, 109  ->  equity moves (9 900.24 / 9 900.04 / 9 968.50), the order does not
    #     bars 84..88       ->  the order moves (1.04241 @ 0.86 ... 1.03866 @ 0.48), equity does NOT
    #     bar 4             ->  nothing trades at all; the assertions above catch it
    #
    # So equity alone would leave five of the nine live bars unpinned. `9 901` is also not
    # decoration: it is what makes the re-sizing in `hand_over` observable — the carried order is
    # 1.08 lots against it, and the session's own 10 000 calls for 1.09.
    assert warmed.account().equity == Decimal("9901.00"), "the measured window drifted"
    resting = warmed.resting()[0]
    assert (resting.limit_price, resting.volume) == (Decimal("1.03893"), Decimal("1.08")), (
        "the order the hand-over carries drifted, at an equity that did not move"
    )


# --------------------------------------------------------------------------- #
# What a hand-over refuses                                                      #
# --------------------------------------------------------------------------- #


def test_a_session_cannot_open_mid_trade() -> None:
    """Inheriting a position would report a trade the session never took, entered before it
    existed. Measured at 0.4%-3% of bars on EURUSD H1 — rare enough to refuse."""
    strategy = ScriptedStrategy(script={2: [_entry_with_stop()]})
    warmed = warm(rising(8), strategy)
    assert warmed.positions("EURUSD"), "the scenario did not end holding a position"

    with pytest.raises(EngineError, match="cannot open mid-trade"):
        carry(warmed, a_broker(), bars=8)


def test_a_used_live_broker_is_refused() -> None:
    """Handed a broker that already traded, `hand_over` is being called twice — and the second
    call would quietly double the resting orders."""
    strategy = ScriptedStrategy(script={2: [_entry_with_stop()], 5: [_close_out()]})
    used = warm(rising(8), strategy)
    assert used.trades(), "the scenario left no trade; it cannot show a used broker"

    with pytest.raises(EngineError, match="not empty"):
        carry(a_broker(), used, bars=8)


def test_a_broker_that_cannot_list_its_orders_carries_nothing() -> None:
    """`resting()` is not on the `Broker` protocol. A broker without it must carry nothing —
    visibly wrong — rather than raise `AttributeError` on start-up."""
    warmed = ImmediateFillBroker(costs=Decimal(0))

    result = carry(warmed, a_broker(), bars=0)

    assert result.carried == ()
    assert result.refused == ()
    assert not hasattr(warmed, "resting"), "the double grew a resting(); the test is now vacuous"


def test_what_the_live_broker_refuses_is_reported_not_raised() -> None:
    """The session is otherwise fine, and the operator needs to know *which* region will not be
    traded — an exception would replace that with a stack trace."""

    class RefusesEverything(BacktestBroker):
        def submit(self, order: OrderRequest) -> OrderResult:
            return OrderResult(order=order, accepted=False, reason="no")

    strategy = build_setup({"type": "structure_choch"})
    warmed = warm(a_structure_market(), strategy)
    assert warmed.resting()

    live = RefusesEverything(instrument=EURUSD, initial_capital=CAPITAL, cost_model=NoCostModel())
    result = carry(warmed, live, bars=10)

    assert result.carried == ()
    assert result.refused, "a refusal happened and was not reported"
    assert result.refused[0].refused_by is RefusedBy.BROKER
    assert result.refused[0].detail == "no", (
        "the broker's own words were dropped; `detail` is the half a human reads, and the "
        "difference between 'a name I already hold' and 'an order I cannot rest' lives there"
    )


class _RefusesTheFirstOrderItIsHanded(BacktestBroker):
    """The shape production actually took, on the demo of 2026-08-28.

    The hand-over's order was refused and every order the session placed afterwards went
    through — so a broker that refuses *everything* cannot show this failure, because a session
    that can never place anything looks the same whether or not it knows.
    """

    submits = 0

    def submit(self, order: OrderRequest) -> OrderResult:
        self.submits += 1
        if self.submits == 1:
            return OrderResult(order=order, accepted=False, reason="the venue said no")
        return super().submit(order)


def test_an_order_the_hand_over_could_not_carry_is_not_left_believed_in() -> None:
    """The fifth silent point of ADR-0023, measured end to end — and it costs a trade.

    Warm-up and live bars are two loops around **one strategy object**, and the gap between them
    is the only place an order can be refused with no bar running to report it. `StructurePhase`
    marks its order placed the instant it emits the signal, so the strategy crosses believing a
    limit rests at a venue that refused it.

    Measured on `arms_a_resting_limit`, cut at bar 70 with the demand zone armed and resting:

    | | zone re-offered | closed trades |
    |---|---|---|
    | refusals not handed on | never | **0** |
    | handed on (this test)  | first live bar, as `-2` | **1** |

    So it is not a reporting nicety: the region the warm-up armed is silently never traded
    again, which is the sentence ADR-0023 uses and the reason the veto design was thrown out.

    ⚠️ **The re-arm carries a new name.** `_may_arm` mints it off `_armed_count`, and it has to:
    re-offering under the old name would be refused a second time as a duplicate, by a broker
    answering a different question, and the retry would never converge.
    """
    bars = a_structure_market()
    cut = 70
    strategy = build_setup({"type": "structure_choch"})
    warmed = warm(bars[:cut], strategy)
    [armed] = warmed.resting()
    assert not warmed.positions("EURUSD"), "the cut lands mid-trade; a hand-over would refuse"

    live = _RefusesTheFirstOrderItIsHanded(
        instrument=EURUSD, initial_capital=CAPITAL, cost_model=NoCostModel()
    )
    handover = carry(warmed, live, bars=cut)
    assert handover.carried == (), "the venue took it; there is no refusal to hand on"
    assert [r.client_id for r in handover.refused] == [armed.client_id]

    session = iter_run(
        candles=bars[cut:],
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=strategy,
        broker=live,
        risk=RISK,
        refusals=handover.refused,
    )

    next(session)
    reoffered = [order.client_id for order in live.resting()]
    assert reoffered, (
        "after the first live bar nothing rests: the strategy is still believing in "
        f"{armed.client_id}, which the venue refused, so it never offered the zone again"
    )
    assert reoffered != [armed.client_id], "it re-armed under the name the venue already refused"

    for _ in session:
        pass

    assert len(live.trades()) == 1, (
        "the zone the warm-up armed was never traded — the ghost ADR-0023 measured, arriving "
        "through the one hand-over point that was still silent"
    )


def test_the_hand_over_records_the_bars_it_was_told() -> None:
    """A fact a session stores. Nothing derives it, because only the caller knows how much
    history it actually found — a window can come back short."""
    result = carry(a_broker(), a_broker(), bars=417)

    assert result.bars == 417


def test_a_carried_order_is_resized_against_the_session_account() -> None:
    """The money blocker, stated as the two numbers that differ.

    `volume` is the one field on an order that is not a fact about the market: a
    `PercentRiskManager` computed it from the equity of the ledger this hand-over throws away.
    Measured on this window, the warm-up ends at 9 901 and the order it leaves resting carries
    **1.08** lots; the session's own account is 10 000 and calls for **1.09**. A 1% drift in an
    account nobody has becomes a 1% drift in the risk of the session's first trade.

    ⚠️ One percent is a small gap on purpose — it is what this window actually produces, and a
    test written against a comfortable gap would not have caught the rounding case. On a warm-up
    that ran 10 000 to 13 000 the same order sizes at 1.42 against 1.09.
    """
    strategy = build_setup({"type": "structure_choch"})
    warmed = warm(a_structure_market(), strategy)
    original = warmed.resting()[0]
    assert original.volume == Decimal("1.08"), "the window changed; re-measure before editing"

    live = a_broker()
    result = carry(warmed, live)

    assert result.carried[0].volume == Decimal("1.09"), "the order kept the discarded ledger's size"
    assert live.resting()[0].volume == Decimal("1.09")
    assert result.carried[0].client_id == original.client_id, "it stopped being the same order"
    assert result.carried[0].limit_price == original.limit_price
    assert result.carried[0].decided_at == original.decided_at, (
        "the decision instant moved; `loop._reject_lookahead` is armed by it, so a stamp "
        "refreshed to the hand-over would let the first live bar fill a decision made on it"
    )
    # ⚠️ The snapshot is the only one that will ever exist for this order. The live broker is
    # fresh, so its bar window cannot rebuild one — `_snapshot_through` would find nothing and
    # hand back the arming window as it came. Dropped here, the session's first trade charts
    # with no context and nothing complains.
    assert result.carried[0].snapshot == original.snapshot


def test_an_order_that_resizes_to_nothing_does_not_cross() -> None:
    """The risk manager saying "not this trade" on the session's own terms. Carrying it anyway
    would overrule the one component whose job is to say no."""
    strategy = build_setup({"type": "structure_choch"})
    warmed = warm(a_structure_market(), strategy)
    resting = warmed.resting()[0]

    live = a_broker()
    result = hand_over(
        warmed,
        live,
        symbol="EURUSD",
        bars=175,
        risk=_NeverSizes(),
        instrument=EURUSD,
    )

    assert result.carried == ()
    assert live.resting() == ()

    assert [r.client_id for r in result.refused] == [resting.client_id]
    assert result.refused[0].refused_by is RefusedBy.SIZING, (
        "the gate that turned it away was not recorded; a strategy cannot tell "
        "'not with this much money' from 'never this order' if all three gates report the same"
    )
    assert result.refused[0].intent is resting.intent
    assert result.refused[0].reason == resting.reason
    # ⚠️ **The size that was refused, not the size the order carried.** `f"...{order.volume}"` is
    # the plausible neighbour and it survived until this line existed: a zero-sized refusal would
    # report *"sizing returned 1.09"*, which reads to whoever answers the incident as the
    # opposite of what happened.
    assert result.refused[0].detail == "sizing returned 0"


def test_handing_over_twice_is_refused() -> None:
    """⚠️ The guard the first version missed. A successful hand-over leaves no position and no
    trade — it leaves an **order**, which is exactly what a `positions or trades` check cannot
    see. Without this the second call passes the guard, and the only thing stopping a duplicate
    is `BacktestBroker` refusing a repeated `client_id` — reported as "this region will not be
    traded", which is a lie, because it is resting. A venue that does not deduplicate names
    would end up holding two limits on one zone.
    """
    strategy = build_setup({"type": "structure_choch"})
    warmed = warm(a_structure_market(), strategy)
    live = a_broker()
    assert carry(warmed, live).carried, "the first hand-over carried nothing"

    with pytest.raises(EngineError, match="not empty"):
        carry(warmed, live)

    assert len(live.resting()) == 1, "the second hand-over duplicated the order"


def test_a_live_broker_holding_a_position_is_refused() -> None:
    """The other clause of the same guard, which no test reached: a broker can hold a position
    without having closed a trade, and that one is not fresh either."""
    holding = warm(rising(8), ScriptedStrategy(script={2: [_entry_with_stop()]}))
    assert holding.positions("EURUSD")
    assert holding.trades() == (), "it closed a trade, so this exercises the other clause"

    with pytest.raises(EngineError, match="not empty"):
        carry(a_broker(), holding, bars=8)


def test_the_hand_over_records_what_the_warm_up_traded() -> None:
    """Recorded, never carried. The strategy crosses holding `_traded` for zones whose trades
    exist only in the discarded ledger, so "why did the session skip this region?" has an answer
    that lives nowhere in the session. This number is the smallest honest trace of it."""
    strategy = build_setup({"type": "structure_choch"})
    warmed = warm(a_structure_market(), strategy)
    assert warmed.trades(), "no trade to record"

    result = carry(warmed, a_broker())

    assert result.warm_trades == len(warmed.trades())
    assert result.warm_trades > 0


def test_every_resting_order_crosses_not_just_the_first() -> None:
    """`hand_over` loops, and a loop that carried only the head would pass every other test here
    — the setup has a single `_armed` slot, so two resting orders are unreachable through it.
    A double with two is the only way to hold the loop to what it is written as."""
    warmed = a_broker()
    first = _a_limit(client_id="zone-a", limit="1.03000")
    second = _a_limit(client_id="zone-b", limit="1.02500")
    assert warmed.submit(first).accepted
    assert warmed.submit(second).accepted
    assert len(warmed.resting()) == 2

    live = a_broker()
    result = carry(warmed, live)

    assert [order.client_id for order in result.carried] == ["zone-a", "zone-b"]
    assert len(live.resting()) == 2


class _ReportsWhateverItIsTold(BacktestBroker):
    """A warm broker reporting orders a `BacktestBroker` would refuse to rest.

    ⚠️ **Deliberate, and here is the argument for it.** `hand_over` reads `_resting_orders`
    through the `RestingOrders` **protocol** — a `Protocol` precisely so that something other
    than this class can answer it one day — and the two fields below cannot be observed through
    the class, measured: `submit` refuses an unnamed order (*"a resting order needs a client_id:
    nothing else can cancel it later"*) and refuses a resting exit (*"only an entry can rest at a
    level"*). So every fixture in this file hands the hand-over a **named entry**, and against
    those, a refusal that guessed both fields is indistinguishable from one that read them.

    Both guesses are plausible enough to have been written: `client_id or reason` is what this
    code said until PR-304-B-D-2b, and a hardcoded `ENTRY` is the exact defect review caught one
    layer down in PR-304-B-D-2a. Neither would fail visibly — a reason in a name slot simply
    never matches, and `StructurePhase` goes on holding an order that does not exist.
    """

    told: tuple[OrderRequest, ...] = ()

    def resting(self) -> Sequence[OrderRequest]:
        return self.told


def test_a_refusal_does_not_invent_the_two_fields_it_cannot_see() -> None:
    """An order with no name comes back with no name, and an exit comes back as an exit.

    `Refusal` says an unnamed refusal is a log line and not a correlation — which is honest, and
    strictly better than a name that was never chosen. The failure of the alternative is silent
    in both directions: a `reason` in the `client_id` slot is matched against the names a
    strategy holds and never matches one, and an intent that is guessed reads, to anybody
    downstream, exactly like an intent somebody knew.
    """
    warmed = _ReportsWhateverItIsTold(
        instrument=EURUSD, initial_capital=CAPITAL, cost_model=NoCostModel()
    )
    warmed.told = (
        replace(_a_limit(client_id="zone-a", limit="1.03000"), client_id=None),
        replace(_a_limit(client_id="flatten-1", limit="1.02500"), intent=SignalKind.EXIT),
    )
    live = a_broker()

    result = carry(warmed, live)

    assert result.carried == (), "the live broker rested what it says it will not rest"
    unnamed, exiting = result.refused
    assert unnamed.refused_by is RefusedBy.BROKER
    assert unnamed.client_id is None, (
        f"the refusal is named {unnamed.client_id!r}, which the order never was — a reason "
        "smuggled into the slot every consumer reads as a name"
    )
    assert unnamed.reason == "entry.test", "the reason was dropped along with the name"
    assert exiting.intent is SignalKind.EXIT, "the intent was assumed rather than read"
    assert exiting.client_id == "flatten-1"


class _NeverSizes:
    """A risk manager that always answers zero — "no trade", in the loop's own vocabulary."""

    def size(self, signal: Signal, account: AccountState, instrument: InstrumentSpec) -> Decimal:
        return Decimal(0)

    def allow(self, order: OrderRequest, account: AccountState) -> bool:
        return True


def _a_limit(*, client_id: str, limit: str) -> OrderRequest:
    return OrderRequest(
        symbol="EURUSD",
        side=Side.LONG,
        intent=SignalKind.ENTRY,
        volume=Decimal("1"),
        decided_at=START,
        stop_loss=Decimal(limit) - Decimal("0.00500"),
        reason="entry.test",
        limit_price=Decimal(limit),
        client_id=client_id,
    )


def test_the_risk_manager_can_still_veto_a_carried_order() -> None:
    """⚠️ `allow` as well as `size`, and the split is what `protocols.py` insists on.

    Sizing is arithmetic; `allow` is the veto, and the veto is where a kill switch or a daily
    loss limit will live. A hand-over that asked only `size` would have made the inversion the
    protocol exists to prevent — the arithmetic deciding whether an order exists. Today nothing
    refuses, so this is the only place that says the question is asked at all.
    """
    strategy = build_setup({"type": "structure_choch"})
    warmed = warm(a_structure_market(), strategy)
    resting = warmed.resting()[0]

    live = a_broker()
    result = hand_over(
        warmed, live, symbol="EURUSD", bars=175, risk=_VetoesEverything(), instrument=EURUSD
    )

    assert result.carried == ()
    assert live.resting() == (), "a vetoed order reached the broker"

    # ⚠️ **`RISK`, and the sibling test above asserts `SIZING` on the same window and the same
    # order.** Until PR-304-B-D-2b both said `== (resting.client_id,)` — the same assertion,
    # passing for two different reasons, which is a fixture where the branches coincide. A
    # hand-over that reported one constant gate for everything satisfied both.
    assert [r.client_id for r in result.refused] == [resting.client_id]
    assert result.refused[0].refused_by is RefusedBy.RISK
    assert result.refused[0].detail == "the risk manager vetoed it", (
        "this gate can say the broker's words with nobody noticing — `detail` is free text, so "
        "the only thing keeping each gate speaking for itself is a test that reads it"
    )


class _VetoesEverything:
    """Sizes normally and refuses everything — a kill switch, in the shape one will have."""

    def size(self, signal: Signal, account: AccountState, instrument: InstrumentSpec) -> Decimal:
        return PercentRiskManager(percent=Decimal(1)).size(signal, account, instrument)

    def allow(self, order: OrderRequest, account: AccountState) -> bool:
        return False


# --------------------------------------------------------------------------- #
# Reading what is still cold                                                    #
# --------------------------------------------------------------------------- #


class Charting:
    """A strategy that charts two averages of different periods and trades nothing."""

    def __init__(self) -> None:
        self._fast = EMA(period=3)
        self._slow = SMA(period=9)

    def on_bar(self, context: Context) -> tuple[Signal, ...]:
        self._fast.update(context.candle)
        self._slow.update(context.candle)
        return ()

    def overlays(self) -> dict[str, EMA | SMA]:
        return {"fast": self._fast, "slow": self._slow}


def test_unwarmed_indicators_names_them_in_drawing_order() -> None:
    """Two overlays with different periods, so order is observable: with one, a reversed tuple
    and a correct one are the same tuple."""
    strategy = Charting()

    assert unwarmed_indicators(strategy) == ("fast", "slow")

    for candle in rising(3):
        strategy.on_bar(a_context(candle))

    assert unwarmed_indicators(strategy) == ("slow",)

    for candle in rising(9):
        strategy.on_bar(a_context(candle))

    assert unwarmed_indicators(strategy) == ()


def test_a_strategy_that_charts_nothing_reports_nothing_cold() -> None:
    """⚠️ Empty here means "nothing to warm", not "everything warm". A caller has to ask
    `isinstance(strategy, Charted)` to tell them apart; this pins the behaviour so nobody reads
    the empty tuple as a clean bill of health."""
    assert unwarmed_indicators(ScriptedStrategy(script={})) == ()


def test_reading_the_overlays_does_not_advance_them() -> None:
    """`Charted.overlays` hands back live objects. Asking twice must not warm anything."""
    strategy = Charting()
    for candle in rising(2):
        strategy.on_bar(a_context(candle))

    first = unwarmed_indicators(strategy)
    assert first == ("fast", "slow")
    assert unwarmed_indicators(strategy) == first
    assert unwarmed_indicators(strategy) == first, "reading it warmed it"


# --------------------------------------------------------------------------- #


def _entry_with_stop() -> Signal:
    return Signal(
        kind=SignalKind.ENTRY,
        side=Side.LONG,
        reference_price=Decimal("1.10000"),
        stop_loss=Decimal("1.09000"),
        reason="test.entry",
    )


def _close_out() -> Signal:
    return Signal(
        kind=SignalKind.EXIT,
        side=Side.LONG,
        reference_price=Decimal("1.10100"),
        reason="test.exit",
    )
