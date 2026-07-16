"""The mapper's rows, persisted against a real Postgres.

The unit tests in `test_results.py` prove the field mapping; this proves the rows the mapper
builds actually satisfy the table's CHECK constraints and that the JSONB columns round-trip.
A `net_pnl = gross_pnl - costs` that the engine and the database disagree about would only
ever surface here.

Run locally with:  docker compose up -d  &&  uv run pytest -m integration
"""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_db.models import (
    Backtest,
    BacktestMetrics,
    BacktestStatus,
    Instrument,
    Strategy,
    Trade,
)
from tradeforge_db.results import to_rows
from tradeforge_engine.domain import AssetClass, ClosedTrade, EquityPoint, Side
from tradeforge_engine.metrics import compute_metrics

pytestmark = pytest.mark.integration

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)


def _instrument(session: Session) -> Instrument:
    instrument = Instrument(
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
    session.add(instrument)
    session.flush()
    return instrument


def _backtest(session: Session, instrument_id: uuid.UUID) -> Backtest:
    definition: dict[str, Any] = {
        "schema_version": "1.0",
        "name": "MA Cross",
        "timeframe": "H1",
        "entry": {},
        "exit": {},
        "risk": {},
    }
    strategy = Strategy(definition=definition, version=1)
    session.add(strategy)
    session.flush()

    backtest = Backtest(
        strategy_id=strategy.id,
        instrument_id=instrument_id,
        timeframe="H1",
        date_from=START,
        date_to=START + 3 * HOUR,
        initial_capital=Decimal("10000"),
        cost_model={"type": "none"},
        status=BacktestStatus.DONE,
        engine_version="0.1.0",
    )
    session.add(backtest)
    session.flush()
    return backtest


def _a_win_and_a_loss() -> list[ClosedTrade]:
    win = ClosedTrade(
        symbol="EURUSD",
        side=Side.LONG,
        volume=Decimal("1"),
        entry_time=START,
        entry_price=Decimal("1.10000"),
        exit_time=START + HOUR,
        exit_price=Decimal("1.10200"),
        gross_pnl=Decimal("200"),
        costs=Decimal("0"),
        net_pnl=Decimal("200"),
        reason="tp",
        stop_loss=Decimal("1.09900"),
        take_profit=Decimal("1.10200"),
        r_multiple=Decimal("2"),
        context={"fast": Decimal("1.09925"), "slow": Decimal("1.09900")},
    )
    loss = ClosedTrade(
        symbol="EURUSD",
        side=Side.SHORT,
        volume=Decimal("1"),
        entry_time=START + HOUR,
        entry_price=Decimal("1.10200"),
        exit_time=START + 2 * HOUR,
        exit_price=Decimal("1.10300"),
        gross_pnl=Decimal("-100"),
        costs=Decimal("0"),
        net_pnl=Decimal("-100"),
        reason="sl",
        stop_loss=Decimal("1.10300"),
        take_profit=None,
        r_multiple=Decimal("-1"),
        context=None,
    )
    return [win, loss]


def _curve() -> tuple[EquityPoint, ...]:
    return (
        EquityPoint(time=START, equity=Decimal("10000")),
        EquityPoint(time=START + HOUR, equity=Decimal("10200")),
        EquityPoint(time=START + 2 * HOUR, equity=Decimal("10100")),
    )


def test_a_run_persists_and_reads_back(session: Session) -> None:
    instrument = _instrument(session)
    backtest = _backtest(session, instrument.id)
    trades = _a_win_and_a_loss()
    metrics = compute_metrics(
        trades=trades, equity_curve=_curve(), initial_capital=Decimal("10000")
    )

    metrics_row, trade_rows = to_rows(
        trades=trades,
        metrics=metrics,
        backtest_id=backtest.id,
        instrument_id=instrument.id,
    )
    session.add(metrics_row)
    session.add_all(trade_rows)
    session.commit()

    stored = session.scalars(
        select(Trade).where(Trade.backtest_id == backtest.id).order_by(Trade.entry_time)
    ).all()
    assert len(stored) == 2

    win, loss = stored
    assert win.direction is Side.LONG
    assert win.net_pnl == Decimal("200")
    assert win.r_multiple == Decimal("2")
    # JSONB round-trips as stored: decimals came back as the strings they went in as.
    assert win.context == {"fast": "1.09925", "slow": "1.09900"}
    assert loss.direction is Side.SHORT
    assert loss.context == {}  # a strategy with no indicators stored an empty object

    summary = session.get(BacktestMetrics, backtest.id)
    assert summary is not None
    # net_profit = gross_profit + gross_loss survived the DB CHECK.
    assert summary.net_profit == Decimal("100")
    assert summary.total_trades == 2
    assert summary.long_trades == 1
    assert summary.short_trades == 1
    assert [point["equity"] for point in summary.equity_curve] == ["10000", "10200", "10100"]


def test_deleting_the_backtest_cascades_to_its_rows(session: Session) -> None:
    """The rows are derived data: delete the run and both the trades and the metrics go with
    it, in one statement, by the ON DELETE CASCADE the mapper never has to know about."""
    instrument = _instrument(session)
    backtest = _backtest(session, instrument.id)
    trades = _a_win_and_a_loss()
    metrics = compute_metrics(
        trades=trades, equity_curve=_curve(), initial_capital=Decimal("10000")
    )
    metrics_row, trade_rows = to_rows(
        trades=trades, metrics=metrics, backtest_id=backtest.id, instrument_id=instrument.id
    )
    session.add(metrics_row)
    session.add_all(trade_rows)
    session.commit()

    session.delete(backtest)
    session.commit()
    # Drop the identity map so both checks hit the database, not SQLAlchemy's cache: a
    # `get` by primary key would hand back the stale in-memory row it still holds.
    session.expunge_all()

    assert session.scalars(select(Trade)).all() == []
    assert session.scalars(select(BacktestMetrics)).all() == []
