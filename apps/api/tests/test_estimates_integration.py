"""Time estimates read from a real Postgres: the medians, what they leave out, and the two places
that serve them — the sweep's preview and the collection plan (his call, 22/09)."""

import datetime as dt
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.estimates import backtest_rate, backtests_time, collection_rates
from tradeforge_api.main import create_app
from tradeforge_db.models import Backtest, BacktestStatus, Collection, Instrument, Strategy
from tradeforge_engine.domain import AssetClass

pytestmark = pytest.mark.integration

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
FINISHED = dt.datetime(2026, 9, 20, 12, tzinfo=dt.UTC)


def _instrument(session: Session) -> Instrument:
    instrument = Instrument(
        symbol=f"E{uuid.uuid4().hex[:6].upper()}",
        name="test market",
        asset_class=AssetClass.FOREX,
        currency_base="EUR",
        currency_quote="USD",
        tick_size=Decimal("0.00001"),
        tick_value=Decimal(1),
        contract_size=Decimal(100000),
        digits=5,
    )
    session.add(instrument)
    session.flush()
    return instrument


def _strategy(session: Session) -> Strategy:
    strategy = Strategy(
        definition={"schema_version": "1.0", "name": f"timed {uuid.uuid4()}"}, version=1
    )
    session.add(strategy)
    session.flush()
    return strategy


def ran(  # noqa: PLR0913 — keyword-only; each is one column the estimate reads
    session: Session,
    *,
    seconds: float,
    days: int = 1,
    timeframe: str = "H1",
    status: BacktestStatus = BacktestStatus.DONE,
    finished: dt.datetime = FINISHED,
) -> None:
    """A run over `days` of `timeframe` that took `seconds`, finished at `finished`."""
    session.add(
        Backtest(
            strategy_id=_strategy(session).id,
            instrument_id=_instrument(session).id,
            timeframe=timeframe,
            date_from=START,
            date_to=START + dt.timedelta(days=days),
            initial_capital=Decimal(10000),
            cost_model={"type": "none"},
            status=status,
            error="broke" if status is BacktestStatus.FAILED else None,
            engine_version="0.1.0",
            started_at=finished - dt.timedelta(seconds=seconds),
            finished_at=finished,
        )
    )
    session.flush()


def downloaded(
    session: Session,
    *,
    seconds: float,
    years: int,
    timeframe: str = "H1",
    status: BacktestStatus = BacktestStatus.DONE,
) -> None:
    session.add(
        Collection(
            symbol="EURUSD",
            timeframe=timeframe,
            date_from=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
            date_to=dt.datetime(2020 + years - 1, 12, 31, tzinfo=dt.UTC),
            status=status,
            years_total=years,
            years_done=years,
            error="no" if status is BacktestStatus.FAILED else None,
            started_at=FINISHED - dt.timedelta(seconds=seconds),
            finished_at=FINISHED,
        )
    )
    session.flush()


class TestTheBacktestRate:
    def test_no_finished_run_is_no_rate(self, session: Session) -> None:
        assert backtest_rate(session) is None
        assert backtests_time(session, [(START, START + dt.timedelta(days=1), "H1")]) is None

    def test_the_median_of_seconds_per_calendar_bar_and_how_many_runs_it_rests_on(
        self, session: Session
    ) -> None:
        # One day of H1 is 24 bars: 24 s, 48 s and 240 s are 1, 2 and 10 s per bar. The median is
        # 2 — the slow outlier moves a mean to 4.33 and a median not at all.
        for seconds in (24, 48, 240):
            ran(session, seconds=seconds)
        # A failed run stopped early and says nothing about how long a run takes.
        ran(session, seconds=1, status=BacktestStatus.FAILED)
        session.commit()

        assert backtest_rate(session) == (2.0, 3)

    def test_the_estimate_multiplies_the_bars_asked_for(self, session: Session) -> None:
        ran(session, seconds=48)  # 2 s per H1 bar
        session.commit()

        # Two runs: a day of H1 (24 bars) and a day of M15 (96 bars) — 120 bars at 2 s.
        estimate = backtests_time(
            session,
            [
                (START, START + dt.timedelta(days=1), "H1"),
                (START, START + dt.timedelta(days=1), "M15"),
            ],
        )
        assert estimate is not None
        assert (estimate.seconds, estimate.based_on) == (240.0, 1)

    def test_the_estimate_is_shared_by_the_workers_running_side_by_side(
        self, session: Session
    ) -> None:
        """Twelve workers, one core each: 240 s of runs take a twelfth of the wall clock. The rate
        is each run's own duration, which already paid for sharing the machine with the others."""
        ran(session, seconds=48)  # 2 s per H1 bar
        session.commit()

        estimate = backtests_time(
            session,
            [
                (START, START + dt.timedelta(days=1), "H1"),
                (START, START + dt.timedelta(days=1), "M15"),
            ],
            workers=12,
        )
        assert estimate is not None
        assert estimate.seconds == 20.0

    def test_only_the_most_recent_two_hundred_count(self, session: Session) -> None:
        # The engine changes; a median over every run ever made keeps answering for the old one.
        for minute in range(200):
            ran(session, seconds=24, finished=FINISHED + dt.timedelta(minutes=minute))  # 1 s/bar
        ran(session, seconds=24_000, finished=FINISHED - dt.timedelta(days=1))  # old and slow
        session.commit()

        assert backtest_rate(session) == (1.0, 200)


class TestTheCollectionRates:
    def test_per_year_by_timeframe_and_pooled_and_only_what_finished_well(
        self, session: Session
    ) -> None:
        downloaded(session, seconds=300, years=3, timeframe="M15")  # 100 s a year
        downloaded(session, seconds=60, years=2, timeframe="H1")  # 30 s a year
        downloaded(session, seconds=5, years=1, timeframe="H1", status=BacktestStatus.FAILED)
        session.commit()

        rates = collection_rates(session)
        assert rates is not None
        by_timeframe, pooled = rates
        assert by_timeframe == {"M15": (100.0, 1), "H1": (30.0, 1)}
        assert pooled == (65.0, 2)

    def test_no_finished_download_is_no_rate(self, session: Session) -> None:
        assert collection_rates(session) is None


class TestWhatTheScreensAreServed:
    @pytest.fixture
    def client(self, session_factory: Callable[[], Session]) -> Iterator[TestClient]:
        class _Queue:
            async def enqueue_job(self, *args: object, **options: object) -> None:
                return None

        app: Any = create_app(
            settings=Settings(), session_factory=session_factory, arq_pool=_Queue()
        )
        with TestClient(app) as opened:
            yield opened

    def test_the_plan_says_how_long_each_pair_would_take(
        self, client: TestClient, session_factory: Callable[[], Session]
    ) -> None:
        body = {
            "symbols": ["EURUSD"],
            "timeframes": ["H1"],
            "date_from": "2019-06-01T00:00:00Z",
            "date_to": "2021-06-30T00:00:00Z",
        }
        # No download has finished yet: no estimate, and said as `null` rather than zero.
        [before] = client.post("/collections/plan", json=body).json()
        assert before["time"] is None

        with session_factory() as session:
            downloaded(session, seconds=60, years=2)  # 30 s a year of H1
            session.commit()

        # 2019 to 2021 is three calendar years.
        [after] = client.post("/collections/plan", json=body).json()
        assert after["time"] == {"seconds": 90.0, "based_on": 1}
