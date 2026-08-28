"""A live session, and the two things that separate one from a paper session.

**Which broker it trades through**, and **when its row exists**. Everything else — the warm-up,
the splice, the heartbeat, the per-bar writes — is the same code either way, and
`test_session_integration.py` already drives it. This file is about the seam.

⚠️ **The venue here is a probe, not a double.** It records *when* it was asked for things and
whether the database agreed the session existed at that moment; it does not model Redis, MT5 or
`MT5Broker`, which have 780 lines of their own tests next door. A fake large enough to model the
venue is a fake that drifts (`live/testing.py` says the same about `FakeRedisStreams`), and the
question this file asks is about ordering inside `session.py`, which no amount of venue fidelity
would answer.

The market is `arms_a_resting_limit` — the measured EURUSD H1 window from
`tradeforge_engine.testing`. It is here because the golden `ma_cross` document **cannot** ask
this question: probed at all 15 of its cuts, the warm-up leaves a resting order at none of them,
so `hand_over` would submit nothing and every assertion about "before the first order" would pass
against an implementation that sent the first order whenever it liked.
"""

import datetime as dt
import uuid
from collections.abc import Callable, Iterator, Sequence
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tradeforge_api.live.heartbeat import Heartbeat
from tradeforge_api.live.session import SessionOutcome, SessionPlan, run_session
from tradeforge_db.live_sessions import STALE_AFTER
from tradeforge_db.models import (
    Instrument,
    LiveSession,
    LiveSessionStatus,
    SessionMode,
    Strategy,
)
from tradeforge_engine import BacktestBroker, PercentRiskManager, compile_strategy
from tradeforge_engine.domain import (
    AccountState,
    AssetClass,
    Candle,
    ClosedTrade,
    Fill,
    InstrumentSpec,
    OrderRequest,
    OrderResult,
    Position,
    Refusal,
)
from tradeforge_engine.loop import iter_run
from tradeforge_engine.testing import EURUSD, arms_a_resting_limit
from tradeforge_executor.ledger import session_is_alive

from .test_live_session_acceptance import golden_candles, ma_cross_strategy
from .test_session_integration import ListSource, factory_of

pytestmark = pytest.mark.integration

SYMBOL = EURUSD.symbol

# Measured against the real loop, not chosen (`scratchpad/probe_choch.py`), over the 175 bars of
# `arms_a_resting_limit`:
#
#     cuts 100..109 and 174 end the warm-up flat AND holding exactly one resting limit
#     every other cut of the window ends with none
#
# 105 is taken from the middle of that band rather than at its edge — a cut at 100 or 109 proves
# the same thing and stops proving it the day the structure machine shifts by one bar — and it
# leaves 70 live bars, where cut 174 leaves one.
CUT = 105

# What a warm-up costs the clock, measured on the demo account on 2026-08-28: 39 204 EURUSD M15
# bars took **700 seconds**, against a `STALE_AFTER` of 60.
#
# ⚠️ **Declared, not spent.** Nothing here sleeps; the source below steps the clock by this once
# the last history bar has gone through. The number only has to be larger than `STALE_AFTER`,
# and it is the real one because the real one is what makes the failure legible in a message.
WARM_UP_COSTS = dt.timedelta(seconds=700)


class Clock:
    """A clock the warm-up can push forward, read by the session **and** by the venue.

    ⚠️ **One instance, two readers, and that is the point.** The bug this file grew to catch is
    a comparison between two instants — when the row says the session started, and when the
    session actually acted. A test whose clock is frozen makes those the same number and cannot
    see it: `run_live` froze at the cut for the six ordering tests that came before this one, and
    all six were **measured** to pass against the production bug in full — the hand-over back
    above the heartbeat, and the first beat back on the spawned thread.
    """

    def __init__(self, at: dt.datetime) -> None:
        self.at = at

    def __call__(self) -> dt.datetime:
        return self.at


class WarmUpTakesTime(ListSource):
    """A `ListSource` whose warm-up costs the clock what a real one costs.

    The step lands after the last history bar and before the first live one, because that is
    where `chain(history(), backlog(), candles())` exhausts this generator — which is exactly
    where the real cost falls: `opened_at` is stamped before the first bar of history, and the
    session's first order goes out after the last.
    """

    def __init__(
        self,
        backlog: list[Candle],
        live: list[Candle],
        *,
        clock: Clock,
        costs: dt.timedelta,
    ) -> None:
        super().__init__(backlog, live)
        self._clock = clock
        self._costs = costs

    def backlog(self) -> Iterator[Candle]:
        yield from super().backlog()
        # Reached when the chain asks for one bar past the last: the warm-up is over.
        self._clock.at += self._costs


class Moment(NamedTuple):
    """One thing the session asked of the venue, and what the database said at that instant.

    Two questions, because they fail apart. `on_file` is whether the row exists — PR-304-B-B.
    `alive` is `session_is_alive`, **the executor's own function**, which is the question that
    actually decides whether an order is sent or refused. A session can be perfectly on file and
    still be refused for having said nothing since before its warm-up.
    """

    what: str
    on_file: bool
    alive: bool


def choch_strategy() -> dict[str, object]:
    """A setup document (ADR-0019), which is what arms a limit at all.

    ⚠️ A condition-tree strategy cannot reach this test. `ma_cross` enters at market on the bar
    it decides, so nothing of it is ever left resting — measured, at every cut of the golden.
    """
    return {
        "schema_version": "1.0",
        "name": "CHoCH continuation",
        "timeframe": "H1",
        "setup": {"type": "structure_choch"},
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


class RecordingVenue:
    """A `Broker` that answers plausibly and writes down the order it was asked in.

    Every method that a session calls appends to `timeline`, and every entry carries the one
    fact this file is about: **did the session's row exist in the database at that moment?** The
    question is asked with a connection of its own, so it sees committed state — which is the
    same thing the executor sees when it looks up `live_session_id`, and therefore the same
    answer that decides whether an audit row is orphaned.
    """

    def __init__(
        self,
        factory: sessionmaker[Session],
        session_id: uuid.UUID,
        now: Callable[[], dt.datetime],
    ) -> None:
        self._factory = factory
        self._now = now
        self.session_id = session_id
        self.timeline: list[Moment] = []
        self.submitted: list[OrderRequest] = []
        self.started = False
        self._position: Position | None = None
        self._note("built")

    def _note(self, what: str) -> None:
        db = self._factory()
        try:
            on_file = db.get(LiveSession, self.session_id) is not None
            # ⚠️ The executor's own function, not a re-implementation of it. A local copy of the
            # rule would be a copy this test could keep passing while the real one refused —
            # which is precisely how the demo run got through every suite and then failed.
            alive = session_is_alive(db, str(self.session_id), now=self._now())
        finally:
            db.close()
        self.timeline.append(Moment(what, on_file, alive))

    def start(self) -> None:
        self.started = True
        self._note("start")

    # ---------------------------------------------------------------- Broker
    def submit(self, order: OrderRequest) -> OrderResult:
        self._note("submit")
        self.submitted.append(order)
        return OrderResult(order=order, accepted=True)

    def cancel(self, client_id: str) -> bool:
        self._note("cancel")
        return True

    def modify_stop(self, symbol: str, stop_loss: Decimal, decided_at: dt.datetime) -> bool:
        self._note("modify_stop")
        return False

    def on_bar(self, candle: Candle) -> Sequence[Fill]:
        return ()

    def positions(self, symbol: str) -> Sequence[Position]:
        return () if self._position is None else (self._position,)

    def account(self) -> AccountState:
        # The session's own capital, not the warm-up's. `hand_over` re-sizes every carried order
        # against this, which is the whole reason it asks (ADR-0023).
        return AccountState(balance=Decimal("10000"), equity=Decimal("10000"))

    def trades(self) -> Sequence[ClosedTrade]:
        return ()

    def refusals(self) -> Sequence[Refusal]:
        """Nothing arrives out of band at this probe. It records *when* the session asked things
        of a venue (`timeline`); a refusal travelling home from a real executor is
        `test_broker.py`'s subject, and a double large enough to model it would drift."""
        return ()


@pytest.fixture
def parquet_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def rows(session: Session) -> tuple[Strategy, Instrument]:
    instrument = Instrument(
        symbol=SYMBOL,
        name=EURUSD.name,
        asset_class=AssetClass.FOREX,
        currency_base="EUR",
        currency_quote="USD",
        tick_size=EURUSD.tick_size,
        tick_value=EURUSD.tick_value,
        contract_size=EURUSD.contract_size,
        digits=EURUSD.digits,
    )
    strategy = Strategy(definition=choch_strategy(), version=1)
    session.add_all([instrument, strategy])
    session.commit()
    return strategy, instrument


def paper_evidence(session: Session, strategy: Strategy, instrument: Instrument, days: int) -> None:
    """Enough finished paper sessions to satisfy the promotion gate, on `days` distinct dates.

    ⚠️ Written straight to the table rather than run, and that is the honest shape of the
    fixture: the gate counts **distinct dates on which a paper session completed a bar**
    (`promotion.paper_days`), so five real sessions would still be one day of evidence unless the
    test also moved a clock. What is under test here is the live path, not the counting — which
    `test_promotion.py` owns.
    """
    for offset in range(days):
        session.add(
            LiveSession(
                strategy_id=strategy.id,
                instrument_id=instrument.id,
                timeframe="H1",
                mode=SessionMode.PAPER,
                status=LiveSessionStatus.STOPPED,
                initial_capital=Decimal("10000"),
                cost_model={"type": "none"},
                engine_version="test",
                warmup_bars=0,
                started_at=dt.datetime(2024, 1, 1, tzinfo=dt.UTC) + dt.timedelta(days=offset),
                last_bar_time=dt.datetime(2024, 1, 1, tzinfo=dt.UTC) + dt.timedelta(days=offset),
            )
        )
    session.commit()


@pytest.fixture
def live_plan(session: Session, rows: tuple[Strategy, Instrument]) -> SessionPlan:
    strategy, instrument = rows
    paper_evidence(session, strategy, instrument, days=5)
    return SessionPlan(
        strategy_id=strategy.id,
        instrument_id=instrument.id,
        timeframe="H1",
        initial_capital=Decimal("10000"),
        cost_model={"type": "none"},
        mode=SessionMode.LIVE,
    )


def split() -> tuple[list[Candle], list[Candle], dt.datetime]:
    """The measured window cut at `CUT`. `candles[CUT].time` is that bar's *opening* instant, so
    bars `0..CUT-1` are history and `CUT` onwards are live."""
    candles = arms_a_resting_limit()
    return candles[:CUT], candles[CUT:], candles[CUT].time


class VenueFactory:
    """Builds one `RecordingVenue` and remembers what it was handed."""

    def __init__(self, factory: sessionmaker[Session], now: Callable[[], dt.datetime]) -> None:
        self._factory = factory
        self._now = now
        self.calls: list[tuple[uuid.UUID, InstrumentSpec]] = []
        self.built: RecordingVenue | None = None

    def __call__(self, session_id: uuid.UUID, spec: InstrumentSpec) -> RecordingVenue:
        self.calls.append((session_id, spec))
        self.built = RecordingVenue(self._factory, session_id, self._now)
        self.built.start()
        return self.built


def run_live(
    session_factory: Callable[[], Session],
    plan: SessionPlan,
    parquet_root: Path,
    *,
    venue: VenueFactory | None = None,
    warm_up_costs: dt.timedelta = dt.timedelta(0),
) -> tuple[SessionOutcome, VenueFactory]:
    """One live session over the measured window, with the venue watching.

    `warm_up_costs` is zero by default, which keeps the eight ordering tests reading a still
    clock — they are about *sequence*, and a moving clock would add a variable none of them are
    asking about. The one test that is about duration passes `WARM_UP_COSTS`.
    """
    factory = factory_of(session_factory)
    history, live_bars, cut = split()
    clock = Clock(cut)
    made = venue if venue is not None else VenueFactory(factory, clock)
    outcome = run_session(
        factory=factory,
        source=WarmUpTakesTime(history, live_bars, clock=clock, costs=warm_up_costs),
        plan=plan,
        parquet_root=parquet_root,
        stopping=lambda: False,
        venue=made,
        now=clock,
    )
    return outcome, made


# --------------------------------------------------------------------------- #
# The fixture has to be able to say something                                   #
# --------------------------------------------------------------------------- #


def test_the_warm_up_really_does_leave_an_order_resting() -> None:
    """⚠️ **First, because every claim below is vacuous without it.** A window where the warm-up
    arms nothing would let "the row exists before the first order" pass against an implementation
    that opened the row last — there would be no first order to be before.

    Probed at every cut of the golden `ma_cross` document: **zero** resting, all fifteen. That is
    why this file carries a second market rather than reusing the one next door.
    """
    history, live_bars, _ = split()
    broker = BacktestBroker(instrument=EURUSD, initial_capital=Decimal("10000"))
    for _outcome in iter_run(
        candles=iter(history),
        timeframe=dt.timedelta(hours=1),
        instrument=EURUSD,
        strategy=compile_strategy(choch_strategy()),
        broker=broker,
        risk=PercentRiskManager(percent=Decimal("1")),
    ):
        pass

    assert len(broker.resting()) == 1, "the cut stopped arming a limit; the file proves nothing"
    assert not broker.positions(SYMBOL), "the cut lands mid-position; hand_over would refuse"
    assert live_bars, "nothing left to trade after the cut"


# --------------------------------------------------------------------------- #
# The row is on file before anything reaches the venue                          #
# --------------------------------------------------------------------------- #


def test_the_row_exists_before_the_hand_over_sends_its_first_order(
    session: Session,
    session_factory: Callable[[], Session],
    live_plan: SessionPlan,
    parquet_root: Path,
) -> None:
    """⚠️ **The reason `open_session` moved.** `hand_over` re-submits what the warm-up left
    resting, and in live those go to the venue — so a row opened *after* it would leave the first
    orders of a session, placed in its riskiest instant, with nothing for `order_audit` to point
    at. The executor's `_known_session` degrades to NULL rather than losing the audit row, so the
    damage is silent: the trail keeps the `client_id` and forgets whose it was.

    Asked with a connection of the venue's own, so it sees committed state — which is exactly
    what the executor sees when it does the lookup for real.
    """
    _outcome, made = run_live(session_factory, live_plan, parquet_root)
    venue = made.built
    assert venue is not None

    submits = [moment for moment in venue.timeline if moment.what == "submit"]
    assert submits, "no order was ever submitted; this test proved nothing"
    assert all(moment.on_file for moment in submits), (
        f"an order went out before the session was on file: {venue.timeline}"
    )
    assert venue.submitted, "the hand-over carried nothing"


def test_a_long_warm_up_still_leaves_the_session_alive_when_it_places_its_first_order(
    session: Session,
    session_factory: Callable[[], Session],
    live_plan: SessionPlan,
    parquet_root: Path,
) -> None:
    """**Measured on the demo account, 2026-08-28, and it refused the order.**

    The session warmed over 39 204 bars, opened its row, and `hand_over` put its limit on the
    wire. The executor answered *"the core is not answering; no new orders while it is silent"*
    and wrote `refused` into `order_audit`. Nothing was broken: `open_session` leaves
    `heartbeat_at` NULL on purpose, `silence()` falls back to `started_at` when there is no
    beat, and `started_at` is stamped **before** the warm-up. Three correct decisions summing to
    a session that is declared dead in the instant it first acts.

    ⚠️ **It is systematic, and it gets worse with the thing that makes a session better.** The
    more history a session warms over, the longer it has been "silent" when it places its first
    order — 700 s here against a limit of 60.

    ⚠️ **What this asserts is the silence at the instant of the submit, not a count of beats.**
    `test_a_running_session_says_it_is_alive` next door reads `heartbeat_at` off the finished
    row, and passes against the bug: by the time a session stops, it has beaten plenty. The
    question is whether it had beaten *yet* when the order went out.
    """
    _outcome, made = run_live(session_factory, live_plan, parquet_root, warm_up_costs=WARM_UP_COSTS)
    venue = made.built
    assert venue is not None

    # The fixture has to be able to say no. A warm-up that cost nothing, or a limit longer than
    # it, would make every assertion below pass against a session that never beat at all.
    assert WARM_UP_COSTS > STALE_AFTER, "the warm-up is shorter than the limit; nothing is proved"

    submits = [moment for moment in venue.timeline if moment.what == "submit"]
    assert submits, "no order was ever submitted; this test proved nothing"
    assert all(moment.alive for moment in submits), (
        "the executor would refuse this session's own orders — it had not said it was alive "
        f"when it placed them: {venue.timeline}"
    )


def test_a_session_that_cannot_write_its_first_heartbeat_hands_nothing_over(
    session: Session,
    session_factory: Callable[[], Session],
    live_plan: SessionPlan,
    parquet_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**How the ordering above fails if it fails — the question a new guard has to answer.**

    `Heartbeat.start()` swallows what its beat raises, on purpose: a transient failure mid-session
    must not stop a heartbeat for good. But that means "the heartbeat is open" is not the same
    claim as "the row says the session is alive", and if they came apart here the session would
    walk straight back into the demo failure — placing orders the executor refuses, while the
    strategy crosses over believing they rest at the venue (ADR-0023's ghost, from the side
    nobody was watching).

    So it refuses, loudly, and **before anything reaches the venue**. The row is left `running`
    with no beat, which is what `reconcile_stale` exists to settle — the same shape a hand-over
    that refuses already leaves.
    """

    def a_heartbeat_that_cannot_reach_the_database(*_a: object, **_k: object) -> Heartbeat:
        def beat() -> None:
            raise RuntimeError("the database is gone")

        return Heartbeat(beat, every=dt.timedelta(seconds=30), name="heartbeat-refusing")

    monkeypatch.setattr(
        "tradeforge_api.live.session.session_heartbeat", a_heartbeat_that_cannot_reach_the_database
    )

    made = VenueFactory(factory_of(session_factory), lambda: split()[2])
    with pytest.raises(Exception, match=r"(?i)heartbeat"):
        run_live(session_factory, live_plan, parquet_root, venue=made)

    venue = made.built
    assert venue is not None, "the venue was never built; the refusal came too early to prove this"
    assert venue.submitted == [], (
        "an order reached the venue from a session that could not say it was alive"
    )


def test_the_venue_is_built_and_refused_before_the_row_is_written(
    session: Session,
    session_factory: Callable[[], Session],
    live_plan: SessionPlan,
    parquet_root: Path,
) -> None:
    """The other half of the same ordering, and it pulls the opposite way.

    `MT5Broker.start()` refuses over a position the venue is already holding, or a snapshot that
    is absent, stale or unreadable — and a refusal has to mean **no row**, the same as a warm-up
    that would not run. So the broker is built and started *before* `open_session`, and the row
    lands between that and the hand-over. Both edges are load-bearing and they are one bar apart.
    """
    _outcome, made = run_live(session_factory, live_plan, parquet_root)
    venue = made.built
    assert venue is not None
    assert venue.started

    before_the_row = [moment.what for moment in venue.timeline if not moment.on_file]
    assert before_the_row == ["built", "start"], (
        f"the venue was started after the row was written: {venue.timeline}"
    )


def test_the_venue_is_built_with_the_id_the_row_is_written_under(
    session: Session,
    session_factory: Callable[[], Session],
    live_plan: SessionPlan,
    parquet_root: Path,
) -> None:
    """⚠️ The id is minted before the row and handed to both, so this is the assertion that it is
    **one** id and not two. Everything that correlates a live session across the three processes
    hangs off it: the consumer group, the envelope's tag, the `client_id` prefix the account
    displays, and `order_audit.live_session_id`. A broker built with an id nothing was recorded
    under would place real orders that no trail can be joined to.
    """
    outcome, made = run_live(session_factory, live_plan, parquet_root)

    assert [called for called, _spec in made.calls] == [outcome.session_id]
    assert [spec.symbol for _called, spec in made.calls] == [SYMBOL]

    session.expire_all()
    row = session.get(LiveSession, outcome.session_id)
    assert row is not None, "the session ran under an id no row was written for"
    assert row.mode is SessionMode.LIVE


# --------------------------------------------------------------------------- #
# Paper never reaches a venue, and live never runs without one                  #
# --------------------------------------------------------------------------- #


def test_a_paper_session_never_touches_the_venue_even_when_handed_one(
    session: Session,
    session_factory: Callable[[], Session],
    rows: tuple[Strategy, Instrument],
    parquet_root: Path,
) -> None:
    """⚠️ **The branch is on the mode, not on whether a venue was passed**, and this is the half
    of that decision that costs money if it is wrong. `venue if venue is not None else ...` reads
    as the same function: it would make a session the operator called paper send real orders the
    moment a caller wired one up by mistake.
    """
    strategy, instrument = rows
    plan = SessionPlan(
        strategy_id=strategy.id,
        instrument_id=instrument.id,
        timeframe="H1",
        initial_capital=Decimal("10000"),
        cost_model={"type": "none"},
        mode=SessionMode.PAPER,
    )

    outcome, made = run_live(session_factory, plan, parquet_root)

    assert made.calls == [], "a paper session asked for a venue"
    assert made.built is None
    assert outcome.error is None

    session.expire_all()
    row = session.get(LiveSession, outcome.session_id)
    assert row is not None
    assert row.mode is SessionMode.PAPER


def test_a_live_session_with_no_venue_refuses_before_the_warm_up(
    session: Session,
    session_factory: Callable[[], Session],
    live_plan: SessionPlan,
    parquet_root: Path,
) -> None:
    """The other half, and it fails the other way: a live session that fell through to a
    `BacktestBroker` would report imaginary fills as real ones, on an account that never moved.

    ⚠️ **Refused before the warm-up**, which the counted source proves. Warming a real session
    reads tens of thousands of bars — 38 987 on the last real run — and spending all of it to say
    a thing that was knowable at the start is the same mistake the promotion gate avoids.
    """
    history, live_bars, cut = split()
    source = CountingSource(history, live_bars)

    with pytest.raises(Exception, match=r"(?i)venue"):
        run_session(
            factory=factory_of(session_factory),
            source=source,
            plan=live_plan,
            parquet_root=parquet_root,
            stopping=lambda: False,
            venue=None,
            now=lambda: cut,
        )

    assert source.reads == 0, "the warm-up ran before the refusal"
    session.expire_all()
    assert (
        session.execute(select(LiveSession).where(LiveSession.mode == SessionMode.LIVE))
        .scalars()
        .all()
        == []
    ), "a row was written for a session that never started"


class CountingSource(ListSource):
    """A `ListSource` that says whether anybody read it."""

    def __init__(self, backlog: list[Candle], live: list[Candle]) -> None:
        super().__init__(backlog, live)
        self.reads = 0

    def backlog(self) -> Iterator[Candle]:
        self.reads += 1
        yield from super().backlog()

    def candles(self) -> Iterator[Candle]:
        self.reads += 1
        yield from super().candles()


# --------------------------------------------------------------------------- #
# A live session is otherwise the same session                                  #
# --------------------------------------------------------------------------- #


def test_a_live_session_records_its_bars_like_any_other(
    session: Session,
    session_factory: Callable[[], Session],
    live_plan: SessionPlan,
    parquet_root: Path,
) -> None:
    """Nothing about the mode changes the record. The row advances, stops cleanly, and reports
    the warm-up it actually used — which is the claim that the venue is a *seam* and not a second
    implementation of a session."""
    history, live_bars, _cut = split()

    outcome, _made = run_live(session_factory, live_plan, parquet_root)

    assert outcome.warmup_bars == len(history)
    assert outcome.bars == len(live_bars)
    assert outcome.error is None

    session.expire_all()
    row = session.get(LiveSession, outcome.session_id)
    assert row is not None
    assert row.status is LiveSessionStatus.STOPPED
    assert row.warmup_bars == len(history)
    assert row.last_bar_time == live_bars[-1].time


def test_a_live_session_is_refused_without_the_paper_days(
    session: Session,
    session_factory: Callable[[], Session],
    rows: tuple[Strategy, Instrument],
    parquet_root: Path,
) -> None:
    """⚠️ The promotion gate still runs, and it runs **first**. Wiring the venue did not move it,
    and a live session for a strategy nobody has watched must not reach the point where a broker
    is built — never mind where an order is sent.
    """
    strategy, instrument = rows
    plan = SessionPlan(
        strategy_id=strategy.id,
        instrument_id=instrument.id,
        timeframe="H1",
        initial_capital=Decimal("10000"),
        cost_model={"type": "none"},
        mode=SessionMode.LIVE,
    )

    with pytest.raises(Exception, match=r"(?i)paper"):
        run_live(session_factory, plan, parquet_root)

    session.expire_all()
    assert session.execute(select(LiveSession)).scalars().all() == []


def test_the_warm_up_holding_a_position_writes_no_row_and_builds_no_venue(
    session: Session,
    session_factory: Callable[[], Session],
    rows: tuple[Strategy, Instrument],
    parquet_root: Path,
) -> None:
    """⚠️ **The refusal that had to be hoisted out of `hand_over`.** With `open_session` now
    running before the hand-over, leaving this to `hand_over` alone would write a `running` row
    for a session that never opened — and, in live, would have started a broker at the venue for
    it. `hand_over` keeps its own copy of the guard, because that is its invariant and it has
    other callers; what this pins is that `session.py` asks *first*.

    ⚠️ **A different market and a different strategy, because this one has to be measured too.**
    Probed at all 175 cuts, `arms_a_resting_limit` with `structure_choch` holds a position at
    **none** of them — its one trade opens and closes inside a single bar. So the mid-position
    scenario is built on the golden `ma_cross` window, where it exists.
    """
    _strategy, instrument = rows
    ma_cross = Strategy(definition=ma_cross_strategy(), version=1)
    session.add(ma_cross)
    session.commit()
    paper_evidence(session, ma_cross, instrument, days=5)

    candles = golden_candles()
    mid = a_bar_holding_a_position(candles)
    made = VenueFactory(factory_of(session_factory), lambda: candles[mid].time)

    with pytest.raises(Exception, match=r"(?i)position"):
        run_session(
            factory=factory_of(session_factory),
            source=ListSource(candles[:mid], candles[mid:]),
            plan=SessionPlan(
                strategy_id=ma_cross.id,
                instrument_id=instrument.id,
                timeframe="H1",
                initial_capital=Decimal("10000"),
                cost_model={"type": "none"},
                mode=SessionMode.LIVE,
            ),
            parquet_root=parquet_root,
            stopping=lambda: False,
            venue=made,
            now=lambda: candles[mid].time,
        )

    assert made.calls == [], "a broker was built at the venue for a session that never opened"
    session.expire_all()
    assert (
        session.execute(select(LiveSession).where(LiveSession.mode == SessionMode.LIVE))
        .scalars()
        .all()
        == []
    ), "a row was written anyway"


def a_bar_holding_a_position(candles: list[Candle]) -> int:
    """The first cut at which the golden's warm-up ends mid-trade.

    ⚠️ Probed against the real loop rather than guessed, the same way `CUT` was. Picking a number
    and hoping is how a test ends up proving something other than what its name says.
    """
    broker = BacktestBroker(instrument=EURUSD, initial_capital=Decimal("10000"))
    strategy = compile_strategy(ma_cross_strategy())
    risk = PercentRiskManager(percent=Decimal("1"))
    for index, _outcome in enumerate(
        iter_run(
            candles=iter(candles),
            timeframe=dt.timedelta(hours=1),
            instrument=EURUSD,
            strategy=strategy,
            broker=broker,
            risk=risk,
        )
    ):
        if broker.positions(SYMBOL):
            return index + 1
    raise AssertionError("this window never holds a position; the scenario cannot be built")
