"""A paper session end to end: the real engine, a real database, a real strategy.

Nothing here is a mock of the session. The candles come from the golden dataset — the one whose
every trade is derived by hand in `packages/engine/tests/golden/ma_cross_golden.md` — the
strategy is compiled from a real DSL document, and the broker is the one a backtest uses. What
is faked is only the *outside*: the bar source, so a test does not wait for a market.

The claim under test is the one the whole PR-302 rests on: **a session is the same engine with a
different candle source**, and the order the pieces go in is what makes the record honest.
"""

import datetime as dt
import threading
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tradeforge_api.live.session import SessionPlan, reconcile_on_start, run_session
from tradeforge_db.live_sessions import STALE_AFTER, open_session
from tradeforge_db.models import (
    Instrument,
    LiveSession,
    LiveSessionStatus,
    Strategy,
    Trade,
)
from tradeforge_engine import BacktestBroker, PercentRiskManager, compile_strategy
from tradeforge_engine.domain import AssetClass, Candle, InstrumentSpec
from tradeforge_engine.loop import iter_run

from .test_live_session_acceptance import golden_candles, ma_cross_strategy

pytestmark = pytest.mark.integration

HOUR = dt.timedelta(hours=1)
SYMBOL = "EURUSD"


class ListSource:
    """A `BarSource` over fixed lists: nothing waiting, and a stream that ends."""

    def __init__(self, backlog: list[Candle], live: list[Candle]) -> None:
        self._backlog = backlog
        self._live = live

    def ensure_group(self) -> bool:
        return True

    def backlog(self) -> Iterator[Candle]:
        yield from self._backlog

    def candles(self) -> Iterator[Candle]:
        yield from self._live


@pytest.fixture
def parquet_root(tmp_path: Path) -> Path:
    """An empty dataset directory. History is handed to the session through the backlog
    instead, so these tests do not depend on the collector's file layout."""
    return tmp_path


@pytest.fixture
def rows(session: Session) -> tuple[Strategy, Instrument]:
    instrument = Instrument(
        symbol=SYMBOL,
        name="Euro vs US Dollar",
        asset_class=AssetClass.FOREX,
        currency_base="EUR",
        currency_quote="USD",
        tick_size=Decimal("0.00001"),
        tick_value=Decimal("1"),
        contract_size=Decimal("100000"),
        digits=5,
    )
    strategy = Strategy(definition=ma_cross_strategy(), version=1)
    session.add_all([instrument, strategy])
    session.commit()
    return strategy, instrument


@pytest.fixture
def plan(rows: tuple[Strategy, Instrument]) -> SessionPlan:
    strategy, instrument = rows
    return SessionPlan(
        strategy_id=strategy.id,
        instrument_id=instrument.id,
        timeframe="H1",
        initial_capital=Decimal("10000"),
        cost_model={"type": "none"},
    )


def factory_of(session_factory: Callable[[], Session]) -> sessionmaker[Session]:
    return cast("sessionmaker[Session]", session_factory)


# Measured against the real loop, not chosen (`scratchpad/probe_golden.py`):
#
#     16 bars  ·  a position is held only on bar 4  ·  entries on bars 4 and 11
#     trades settle on bars 5 and 11
#
# So a cut at 6 puts the first round trip entirely in the warm-up and the second entirely in the
# session — and the second one opens *and* closes inside bar 11, which means this file drives the
# `to_insert_closed` path with the real engine rather than with a hand-built `ClosedTrade`.
CUT = 6


def split_at(index: int) -> tuple[list[Candle], list[Candle], dt.datetime]:
    """The golden bars cut in two, with the instant that makes the cut fall there.

    ⚠️ `candles[index].time` is that bar's **opening** instant, so the cut lands exactly where
    the previous bar closed: bars `0..index-1` are history and `index` onwards are live.
    """
    candles = golden_candles()
    return candles[:index], candles[index:], candles[index].time


def test_a_session_warms_on_history_and_trades_the_rest(
    session: Session,
    session_factory: Callable[[], Session],
    plan: SessionPlan,
    parquet_root: Path,
) -> None:
    """The whole life of a session, in order.

    ⚠️ The number that matters is `warmup_bars`: it is a **fact about what the seed used**, not
    a plan. A session that recorded the bars it *asked* for would report reassurance rather than
    measurement — and the warm-up of a structure strategy has no formula to ask against
    (ADR-0023).
    """
    history, live_bars, cut = split_at(CUT)

    outcome = run_session(
        factory=factory_of(session_factory),
        source=ListSource(history, live_bars),
        plan=plan,
        parquet_root=parquet_root,
        stopping=lambda: False,
        now=lambda: cut,
    )

    assert outcome.warmup_bars == len(history)
    assert outcome.bars == len(live_bars)
    assert outcome.error is None

    session.expire_all()
    row = session.get(LiveSession, outcome.session_id)
    assert row is not None
    assert row.status is LiveSessionStatus.STOPPED
    assert row.warmup_bars == len(history)
    assert row.stopped_at is not None
    assert row.last_bar_time == live_bars[-1].time, "the bar stamp did not follow the loop"


def test_the_warm_up_ledger_is_thrown_away(
    session: Session,
    session_factory: Callable[[], Session],
    plan: SessionPlan,
    parquet_root: Path,
) -> None:
    """⚠️ ADR-0023's whole point, checked where it would actually leak. History is run through
    the real loop against a real broker — orders fill, zones burn — and then the session opens
    with a *fresh* broker. Any trade recorded from before the cut is warm-up money that got into
    a live equity curve.
    """
    history, live_bars, cut = split_at(CUT)

    outcome = run_session(
        factory=factory_of(session_factory),
        source=ListSource(history, live_bars),
        plan=plan,
        parquet_root=parquet_root,
        stopping=lambda: False,
        now=lambda: cut,
    )

    recorded = list(
        session.execute(select(Trade).where(Trade.live_session_id == outcome.session_id)).scalars()
    )
    assert recorded, "the scenario recorded no trades at all; it cannot separate anything"
    warm_trade_entry = golden_candles()[4].time
    assert warm_trade_entry < cut, "the fixture stopped separating: the warm-up takes no trade"
    assert all(trade.entry_time >= cut for trade in recorded), (
        "a trade from the warm-up was written to the session's ledger"
    )


def test_a_session_asked_to_stop_finishes_the_bar_and_stops(
    session: Session,
    session_factory: Callable[[], Session],
    plan: SessionPlan,
    parquet_root: Path,
) -> None:
    """⚠️ Stopping is not abandoning. The bar in hand is recorded, the row is marked `stopped`
    rather than `failed`, and `stopped_at` is written — because a session somebody ended on
    purpose has to be distinguishable from one that died, which is the whole reason
    `reconcile_stale` writes `failed` and not `stopped`.
    """
    history, live_bars, cut = split_at(CUT)
    stopping = threading.Event()

    def stop_after_one() -> bool:
        was_set = stopping.is_set()
        stopping.set()
        return was_set

    outcome = run_session(
        factory=factory_of(session_factory),
        source=ListSource(history, live_bars),
        plan=plan,
        parquet_root=parquet_root,
        stopping=stop_after_one,
        now=lambda: cut,
    )

    assert outcome.bars == 2, f"stopped after {outcome.bars} bars, not the one in hand plus one"
    assert outcome.error is None

    session.expire_all()
    row = session.get(LiveSession, outcome.session_id)
    assert row is not None
    assert row.status is LiveSessionStatus.STOPPED, "an orderly stop was recorded as a failure"
    assert row.last_bar_time == live_bars[1].time


def test_nothing_is_written_when_the_warm_up_refuses(
    session: Session,
    session_factory: Callable[[], Session],
    plan: SessionPlan,
    parquet_root: Path,
) -> None:
    """⚠️ A session whose warm-up raised gets **no row at all**, and that is the honest outcome:
    nothing ran. A row written before the warm-up would have to be updated with `warmup_bars`
    afterwards — a second write that can fail on its own — and a failed warm-up would leave a
    `running` row for a process that never started.

    Reached with a cut that lands mid-position, which `hand_over` refuses (ADR-0023): inheriting
    it would have the session report a trade it never took.
    """
    candles = golden_candles()
    mid_position = _a_bar_holding_a_position(candles)

    with pytest.raises(Exception, match=r"(?i)position"):
        run_session(
            factory=factory_of(session_factory),
            source=ListSource(candles[:mid_position], candles[mid_position:]),
            plan=plan,
            parquet_root=parquet_root,
            stopping=lambda: False,
            now=lambda: candles[mid_position].time,
        )

    session.expire_all()
    assert session.execute(select(LiveSession)).scalars().all() == [], "a row was written anyway"


def _a_bar_holding_a_position(candles: list[Candle]) -> int:
    """The first cut at which the warm-up ends mid-trade.

    ⚠️ Probed against the real loop rather than guessed. Picking a number and hoping is how a
    test ends up proving something other than what its name says — measured on this dataset, a
    hand-over lands mid-position on a small minority of bars (ADR-0023 puts it at 0.4-3%).
    """
    spec = InstrumentSpec(
        symbol=SYMBOL,
        name="Euro vs US Dollar",
        asset_class=AssetClass.FOREX,
        currency_quote="USD",
        currency_base="EUR",
        tick_size=Decimal("0.00001"),
        tick_value=Decimal("1"),
        contract_size=Decimal("100000"),
        digits=5,
    )
    broker = BacktestBroker(instrument=spec, initial_capital=Decimal("10000"))
    strategy = compile_strategy(ma_cross_strategy())
    risk = PercentRiskManager(percent=Decimal("1"))

    for outcome in iter_run(
        candles=candles,
        timeframe=HOUR,
        instrument=spec,
        strategy=strategy,
        broker=broker,
        risk=risk,
    ):
        if broker.positions(SYMBOL):
            return outcome.index + 1
    pytest.skip("this dataset never holds a position; the scenario cannot separate anything")


def test_reconcile_on_start_settles_what_a_dead_session_left(
    session: Session, session_factory: Callable[[], Session], rows: tuple[Strategy, Instrument]
) -> None:
    """⚠️ Housekeeping about **other** sessions. The thing that would have marked those rows
    `stopped` is the thing that died, so nobody but the next process to come up will ever do
    it — and a panel is meanwhile reporting a session that has not existed since Tuesday."""
    strategy, instrument = rows
    abandoned = open_session(
        session,
        strategy_id=strategy.id,
        instrument_id=instrument.id,
        timeframe="H1",
        initial_capital=Decimal("10000"),
        cost_model={"type": "none"},
        engine_version="0.1.0",
        warmup_bars=10,
        at=dt.datetime.now(dt.UTC) - STALE_AFTER * 10,
    )
    session.commit()

    marked = reconcile_on_start(factory_of(session_factory))

    assert marked == [abandoned.id]
    session.expire_all()
    settled = session.get(LiveSession, abandoned.id)
    assert settled is not None
    assert settled.status is LiveSessionStatus.FAILED
    assert settled.error is not None


def test_a_running_session_says_it_is_alive(
    session: Session,
    session_factory: Callable[[], Session],
    plan: SessionPlan,
    parquet_root: Path,
) -> None:
    """⚠️ Without this the whole heartbeat is deletable and every test still passes — which is
    exactly what a mutation run said. And the consequence of it not running is not a quiet gap:
    `reconcile_stale` marks a working session `failed` sixty seconds in, and the operator sees a
    session that died for a reason nobody can name.

    Asserted on the row rather than on the thread, because the row is what anybody reads.
    """
    history, live_bars, cut = split_at(CUT)

    outcome = run_session(
        factory=factory_of(session_factory),
        source=ListSource(history, live_bars),
        plan=plan,
        parquet_root=parquet_root,
        stopping=lambda: False,
        now=lambda: cut,
    )

    session.expire_all()
    row = session.get(LiveSession, outcome.session_id)
    assert row is not None
    assert row.heartbeat_at is not None, "the session never said it was alive"
    assert row.stopped_at is not None
    assert row.heartbeat_at <= row.stopped_at, (
        "a beat landed after the session stopped; the heartbeat outlived the row"
    )


def test_the_process_refuses_an_instrument_that_does_not_exist(
    session_factory: Callable[[], Session],
) -> None:
    """⚠️ Refused **before** a consumer group is created. The stream is keyed on the symbol, and
    only the database knows which symbol an instrument id is — so a wrong id would otherwise
    create a group on a stream that will never carry anything, and the session would sit there
    looking patient.

    Exit code 2 rather than 1: 1 is a session that ran and failed, and this one never started.
    """
    from tradeforge_api.live.process import main  # noqa: PLC0415 — the process entry point

    code = main(
        [
            "--strategy",
            "11111111-1111-1111-1111-111111111111",
            "--instrument",
            str(uuid.uuid4()),
            "--timeframe",
            "H1",
            "--capital",
            "10000",
        ]
    )

    assert code == 2
