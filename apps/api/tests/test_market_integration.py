"""The sessions of one market in one process, over real Postgres (ADR-0033).

The claim under test is the one the ADR rests on: **a session among others of its market does
exactly what it does alone**. Each setup here runs twice over the same golden bars — once by
itself through `run_session`, once beside the other through `Market` — and the two runs must post
the same signals and record the same trades. Then what being together must not change: a setup
that fails, one asked to stop, one still in the trade its warm-up ended in.
"""

# The paper suite's fixtures are imported by name, which is how pytest finds them; each test then
# takes them as parameters, which ruff reads as redefining the import.
# ruff: noqa: F811

import dataclasses
import datetime as dt
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_api.live.market import Market
from tradeforge_api.live.session import SessionOutcome, SessionPlan, run_session
from tradeforge_api.live.signals import SignalEvent
from tradeforge_db.models import Instrument, LiveSession, LiveSessionStatus, SessionMode, Strategy
from tradeforge_db.models import Trade as TradeRow
from tradeforge_engine.domain import Candle

from .test_live_session_acceptance import ma_cross_strategy
from .test_session_integration import (  # noqa: F401 — fixtures
    CUT,
    ListSource,
    _a_bar_holding_a_position,
    factory_of,
    parquet_root,
    plan,
    rows,
    split_at,
)

pytestmark = pytest.mark.integration


class Sink:
    """Where a session's signals go; `fails` makes posting raise, as a dead channel would."""

    def __init__(self, *, fails: bool = False) -> None:
        self.events: list[SignalEvent] = []
        self.fails = fails
        self._next = 0

    def emit(self, event: SignalEvent) -> None:
        if self.fails:
            raise RuntimeError("the channel refused the signal")
        self.events.append(event)

    def number(self) -> int:
        self._next += 1
        return self._next


def told(sink: Sink) -> list[SignalEvent]:
    """The signals, without their pictures (drawn per run, compared elsewhere)."""
    return [dataclasses.replace(event, image=None) for event in sink.events]


def trades_of(session: Session, session_id: uuid.UUID) -> list[tuple[object, ...]]:
    rows = session.scalars(
        select(TradeRow)
        .where(TradeRow.live_session_id == session_id)
        .order_by(TradeRow.entry_time, TradeRow.id)
    ).all()
    return [
        (
            t.direction,
            t.entry_time,
            t.entry_price,
            t.volume,
            t.exit_time,
            t.exit_price,
            t.r_multiple,
        )
        for t in rows
    ]


@pytest.fixture
def other(session: Session, rows: tuple[Strategy, Instrument]) -> Strategy:
    """A second setup on the same market: the golden risking 2% instead of 1%. Same entries and
    exits, twice the volume — so a broker shared by the two would show in every trade."""
    risk = {"sizing": {"type": "percent_risk", "params": {"percent": 2.0}}}
    strategy = Strategy(
        definition={**ma_cross_strategy(), "name": "MA cross, 2%", "risk": risk}, version=1
    )
    session.add(strategy)
    session.commit()
    return strategy


def signal_plan(plan: SessionPlan, strategy: Strategy | None = None) -> SessionPlan:
    return dataclasses.replace(
        plan,
        strategy_id=plan.strategy_id if strategy is None else strategy.id,
        mode=SessionMode.SIGNAL,
        session_id=uuid.uuid4(),
    )


def alone(
    session_factory: Callable[[], Session],
    plan: SessionPlan,
    parquet_root: Path,
    bars: tuple[list[Candle], list[Candle], dt.datetime],
) -> tuple[SessionOutcome, Sink]:
    history, live_bars, cut = bars
    sink = Sink()
    outcome = run_session(
        factory=factory_of(session_factory),
        source=ListSource(history, live_bars),
        plan=plan,
        parquet_root=parquet_root,
        stopping=lambda: False,
        now=lambda: cut,
        signals=sink,
    )
    return outcome, sink


def together(  # noqa: PLR0913 — the market's seams, each a knob one test turns
    session_factory: Callable[[], Session],
    plans: list[SessionPlan],
    parquet_root: Path,
    bars: tuple[list[Candle], list[Candle], dt.datetime],
    *,
    sinks: dict[uuid.UUID, Sink] | None = None,
    asked_to_stop: Callable[[uuid.UUID], bool] = lambda _session_id: False,
) -> tuple[Market, list[SessionOutcome], dict[uuid.UUID, Sink]]:
    history, live_bars, cut = bars
    sinks = sinks if sinks is not None else {}
    for one in plans:
        assert one.session_id is not None
        sinks.setdefault(one.session_id, Sink())
    market = Market(
        factory=factory_of(session_factory),
        plans=plans,
        parquet_root=parquet_root,
        signals_for=lambda one: sinks[one.session_id],  # type: ignore[index]
        signalled=lambda: False,
        asked_to_stop=asked_to_stop,
        now=lambda: cut,
    )
    return market, market.run(ListSource(history, live_bars)), sinks


def by_id(outcomes: list[SessionOutcome]) -> dict[uuid.UUID, SessionOutcome]:
    return {outcome.session_id: outcome for outcome in outcomes}


def test_each_session_does_beside_the_other_exactly_what_it_does_alone(
    session: Session,
    session_factory: Callable[[], Session],
    plan: SessionPlan,
    other: Strategy,
    parquet_root: Path,
) -> None:
    bars = split_at(CUT)
    golden_alone, golden_told = alone(session_factory, signal_plan(plan), parquet_root, bars)
    near_alone, near_told = alone(session_factory, signal_plan(plan, other), parquet_root, bars)

    golden, near = signal_plan(plan), signal_plan(plan, other)
    _, outcomes, sinks = together(session_factory, [golden, near], parquet_root, bars)
    ended = by_id(outcomes)
    assert golden.session_id is not None
    assert near.session_id is not None

    assert golden_told.events, "the golden bars must signal inside the session to prove anything"
    assert told(sinks[golden.session_id]) == told(golden_told)
    assert told(sinks[near.session_id]) == told(near_told)
    assert trades_of(session, golden.session_id) == trades_of(session, golden_alone.session_id)
    assert trades_of(session, near.session_id) == trades_of(session, near_alone.session_id)
    assert trades_of(session, golden.session_id) != trades_of(session, near.session_id), (
        "two setups that trade alike cannot tell a shared broker from two"
    )
    for mine, theirs in ((golden, golden_alone), (near, near_alone)):
        assert mine.session_id is not None
        assert ended[mine.session_id].bars == theirs.bars
        assert ended[mine.session_id].warmup_bars == theirs.warmup_bars
        assert ended[mine.session_id].error is None


def test_a_session_that_fails_fails_alone(
    session: Session,
    session_factory: Callable[[], Session],
    plan: SessionPlan,
    other: Strategy,
    parquet_root: Path,
) -> None:
    bars = split_at(CUT)
    _, golden_told = alone(session_factory, signal_plan(plan), parquet_root, bars)
    golden, broken = signal_plan(plan), signal_plan(plan, other)
    assert golden.session_id is not None
    assert broken.session_id is not None

    market, outcomes, sinks = together(
        session_factory,
        [golden, broken],
        parquet_root,
        bars,
        sinks={broken.session_id: Sink(fails=True)},
    )
    ended = by_id(outcomes)

    assert ended[broken.session_id].error == "RuntimeError: the channel refused the signal"
    assert ended[golden.session_id].error is None
    assert told(sinks[golden.session_id]) == told(golden_told), "the other went on as alone"
    rows = {row.id: row for row in session.scalars(select(LiveSession)).all()}
    assert rows[broken.session_id].status is LiveSessionStatus.FAILED
    assert rows[golden.session_id].status is LiveSessionStatus.STOPPED
    assert market.failures == 1


def test_a_session_asked_to_stop_leaves_and_the_other_stays(
    session: Session,
    session_factory: Callable[[], Session],
    plan: SessionPlan,
    other: Strategy,
    parquet_root: Path,
) -> None:
    bars = split_at(CUT)
    golden, leaving = signal_plan(plan), signal_plan(plan, other)
    assert leaving.session_id is not None
    assert golden.session_id is not None
    asked: set[uuid.UUID] = set()

    def asked_to_stop(session_id: uuid.UUID) -> bool:
        return session_id in asked

    history, live_bars, cut = bars
    market = Market(
        factory=factory_of(session_factory),
        plans=[golden, leaving],
        parquet_root=parquet_root,
        signals_for=lambda _one: Sink(),
        signalled=lambda: False,
        asked_to_stop=asked_to_stop,
        now=lambda: cut,
    )

    class Asking(ListSource):
        """Asks `leaving` to stop as the second live bar is read — and asks the market between
        reads, as the stream does, to see it leave before any further bar."""

        def candles(self) -> Iterator[Candle]:
            for index, candle in enumerate(live_bars):
                if index == 1:
                    asked.add(leaving.session_id)  # type: ignore[arg-type]
                    assert not market.should_stop(), "the other is still running"
                    assert market.sessions == 1, "left between reads, not at the next bar"
                yield candle

    ended = by_id(market.run(Asking(history, live_bars)))

    assert ended[leaving.session_id].bars == 1
    assert ended[leaving.session_id].error is None
    assert ended[golden.session_id].bars == len(live_bars)


def test_a_session_still_in_its_warm_up_trade_waits_while_the_other_is_live(
    session: Session,
    session_factory: Callable[[], Session],
    plan: SessionPlan,
    other: Strategy,
    parquet_root: Path,
) -> None:
    """The golden holds a position across some cut; at that cut it follows its trade quietly
    while a setup flat at the same instant is already trading — each as it would alone."""
    candles = split_at(CUT)[0] + split_at(CUT)[1]
    holding_from = _a_bar_holding_a_position(candles)
    bars = (candles[:holding_from], candles[holding_from:], candles[holding_from].time)
    flat = Strategy(
        definition={
            **ma_cross_strategy(),
            "name": "never",
            # A cross of an average with itself: valid, and never true.
            "entry": {
                "long": {"op": "crosses_above", "left": {"ref": "fast"}, "right": {"ref": "fast"}},
                "short": None,
            },
        },
        version=1,
    )
    session.add(flat)
    session.commit()

    golden_alone, golden_told = alone(session_factory, signal_plan(plan), parquet_root, bars)
    golden, idle = signal_plan(plan), signal_plan(plan, flat)
    assert golden.session_id is not None
    assert idle.session_id is not None
    _, outcomes, sinks = together(session_factory, [golden, idle], parquet_root, bars)
    ended = by_id(outcomes)

    assert golden_alone.bars < len(bars[1]), "the golden must start inside a trade here"
    assert ended[golden.session_id].bars == golden_alone.bars
    assert told(sinks[golden.session_id]) == told(golden_told)
    assert ended[idle.session_id].bars == len(bars[1]), "the flat one opened at once"


def test_a_market_is_one_instrument_on_one_timeframe_of_signal_sessions(
    plan: SessionPlan, parquet_root: Path
) -> None:
    def market(plans: list[SessionPlan]) -> Market:
        return Market(
            factory=None,  # type: ignore[arg-type]
            plans=plans,
            parquet_root=parquet_root,
            signals_for=lambda _one: Sink(),
            signalled=lambda: False,
            asked_to_stop=lambda _session_id: False,
        )

    one = signal_plan(plan)
    with pytest.raises(ValueError, match="one market per process"):
        market([one, dataclasses.replace(signal_plan(plan), timeframe="M15")])
    with pytest.raises(ValueError, match="signal sessions only"):
        market([dataclasses.replace(one, mode=SessionMode.PAPER)])
    with pytest.raises(ValueError, match="needs its id"):
        market([dataclasses.replace(one, session_id=None)])


def test_a_market_asked_during_its_warm_up_whether_it_is_done_is_not(
    session: Session,
    session_factory: Callable[[], Session],
    plan: SessionPlan,
    parquet_root: Path,
) -> None:
    """The stream asks before its first read — inside the warm-up, which reads one bar past the
    cut to find it — when no session has opened yet. "Nobody left" is not "nobody yet": answered
    yes, the market would end before its first live bar."""
    history, live_bars, cut = split_at(CUT)
    golden = signal_plan(plan)
    market = Market(
        factory=factory_of(session_factory),
        plans=[golden],
        parquet_root=parquet_root,
        signals_for=lambda _one: Sink(),
        signalled=lambda: False,
        asked_to_stop=lambda _session_id: False,
        now=lambda: cut,
    )
    answers: list[bool] = []

    class Asking(ListSource):
        def candles(self) -> Iterator[Candle]:
            answers.append(market.should_stop())  # as `CandleStream` does, before reading
            yield from live_bars

    (outcome,) = market.run(Asking(history, live_bars))

    assert answers == [False]
    assert outcome.bars == len(live_bars)


def test_the_market_tells_the_supervisor_what_it_carries(
    session: Session,
    session_factory: Callable[[], Session],
    plan: SessionPlan,
    other: Strategy,
    parquet_root: Path,
) -> None:
    """What the fleet counts sessions by: one that left without being asked has failed."""
    history, live_bars, cut = split_at(CUT)
    golden, leaving = signal_plan(plan), signal_plan(plan, other)
    assert golden.session_id is not None
    assert leaving.session_id is not None
    said: list[tuple[str, frozenset[uuid.UUID]]] = []
    asked: set[uuid.UUID] = set()
    market = Market(
        factory=factory_of(session_factory),
        plans=[golden, leaving],
        parquet_root=parquet_root,
        signals_for=lambda _one: Sink(),
        signalled=lambda: False,
        asked_to_stop=lambda session_id: session_id in asked,
        now=lambda: cut,
        publish=lambda phase, sessions: said.append((phase, frozenset(sessions))),
    )

    class Asking(ListSource):
        def candles(self) -> Iterator[Candle]:
            for index, candle in enumerate(live_bars):
                if index == 2:
                    asked.add(leaving.session_id)  # type: ignore[arg-type]
                yield candle

    market.run(Asking(history, live_bars))

    both = frozenset({golden.session_id, leaving.session_id})
    assert said[0] == ("warming", both)
    assert said[1] == ("live", both)
    assert ("live", frozenset({golden.session_id})) in said
    assert said[-1] == ("live", frozenset({golden.session_id}))


def test_a_market_that_cannot_tell_the_supervisor_still_trades(
    session: Session,
    session_factory: Callable[[], Session],
    plan: SessionPlan,
    parquet_root: Path,
) -> None:
    history, live_bars, cut = split_at(CUT)
    golden = signal_plan(plan)

    def redis_down(_phase: str, _sessions: object) -> None:
        raise ConnectionError("Error 10061 connecting to localhost:6379")

    market = Market(
        factory=factory_of(session_factory),
        plans=[golden],
        parquet_root=parquet_root,
        signals_for=lambda _one: Sink(),
        signalled=lambda: False,
        asked_to_stop=lambda _session_id: False,
        now=lambda: cut,
        publish=redis_down,
    )

    (outcome,) = market.run(ListSource(history, live_bars))

    assert outcome.error is None
    assert outcome.bars == len(live_bars)


def test_a_market_asked_inside_its_warm_up_names_the_sessions_it_is_warming(
    session: Session,
    session_factory: Callable[[], Session],
    plan: SessionPlan,
    other: Strategy,
    parquet_root: Path,
) -> None:
    """The stream asks before its first read, inside the warm-up. A word sent from there naming
    nobody is read by the fleet as every session gone unasked — failed, launched again elsewhere,
    while the first ones went on (found before it ever ran, 13/10)."""
    history, live_bars, cut = split_at(CUT)
    golden, near = signal_plan(plan), signal_plan(plan, other)
    said: list[tuple[str, frozenset[uuid.UUID]]] = []
    market = Market(
        factory=factory_of(session_factory),
        plans=[golden, near],
        parquet_root=parquet_root,
        signals_for=lambda _one: Sink(),
        signalled=lambda: False,
        asked_to_stop=lambda _session_id: False,
        now=lambda: cut,
        publish=lambda phase, sessions: said.append((phase, frozenset(sessions))),
    )

    class Asking(ListSource):
        def candles(self) -> Iterator[Candle]:
            market.should_stop()  # as `CandleStream` does, before reading
            yield from live_bars

    market.run(Asking(history, live_bars))

    both = frozenset({golden.session_id, near.session_id})
    warming = [sessions for phase, sessions in said if phase == "warming"]
    assert warming, "the warm-up said nothing"
    assert all(sessions == both for sessions in warming), said


def test_a_session_asked_to_stop_in_its_warm_up_leaves_it_and_the_last_one_ends_the_market(
    session: Session,
    session_factory: Callable[[], Session],
    plan: SessionPlan,
    other: Strategy,
    parquet_root: Path,
) -> None:
    """13/10: a restart pressed on WIN M5 at 20h, the B3 closed, would have waited for the next
    morning — the warm-up ends only at the first bar past the cut, and nobody warming heard a stop
    while the stream waited for it."""
    history, live_bars, cut = split_at(CUT)
    first, second = signal_plan(plan), signal_plan(plan, other)
    assert first.session_id is not None
    assert second.session_id is not None
    asked: set[uuid.UUID] = set()
    said: list[tuple[str, frozenset[uuid.UUID]]] = []
    market = Market(
        factory=factory_of(session_factory),
        plans=[first, second],
        parquet_root=parquet_root,
        signals_for=lambda _one: Sink(),
        signalled=lambda: False,
        asked_to_stop=lambda session_id: session_id in asked,
        now=lambda: cut,
        publish=lambda phase, sessions: said.append((phase, frozenset(sessions))),
    )
    answers: list[bool] = []

    class Closed(ListSource):
        """A market at night: reads that come back empty, the stream asking between them as
        `CandleStream` does, and ending when the answer is yes."""

        def candles(self) -> Iterator[Candle]:
            for leaving in (first, second):
                asked.add(leaving.session_id)  # type: ignore[arg-type]
                answers.append(market.should_stop())
                if answers[-1]:
                    return
            yield from live_bars

    outcomes = market.run(Closed(history, live_bars))

    assert answers == [False, True], "the other was still warming; then nobody was"
    assert said[-2:] == [("warming", frozenset({second.session_id})), ("warming", frozenset())]
    assert outcomes == []
    assert market.failures == 0, "asked to leave, not failed"
    opened = session.scalars(
        select(LiveSession).where(LiveSession.id.in_([first.session_id, second.session_id]))
    ).all()
    assert opened == [], "never opened: no row to leave behind"
