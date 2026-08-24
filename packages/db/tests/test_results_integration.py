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
    ExitReason,
    Instrument,
    LiveSession,
    LiveSessionStatus,
    SessionMode,
    Strategy,
    Trade,
)
from tradeforge_db.results import close_trade_values, open_trade_row, to_rows
from tradeforge_engine.domain import AssetClass, ClosedTrade, EquityPoint, Position, Side
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


# --------------------------------------------------------------------------- #
# Rounding to the column scale, against the real NUMERIC(20, 8)                 #
# --------------------------------------------------------------------------- #
#
# The fixtures above deal in whole hundreds, and whole hundreds cannot show this: the engine
# computes money in unbounded `Decimal` (ten to twelve places, out of price, tick
# value and volume) and every money column holds eight. Postgres rounds each column independently on
# the way in, and independent rounding does not distribute over addition — so a row whose
# three numbers balance exactly in `Decimal` can still be rejected by the CHECK that says so.
#
# It is intermittent: only the minority of runs whose digits past the eighth place carry hit
# it, which is why every backtest before the AUDCAD acceptance run of PR-234 persisted fine.
# The numbers below are that minority, taken from the engine driven end to end rather than
# invented. Both tests read the row **back from Postgres** and compare its text: the identity
# holds in unbounded `Decimal`, so asserting it as a `Decimal` comparison would pass against
# the very defect these forbid. Only the scale on the wire separates them.


def _tailed_trade(*, side: Side, gross: str, costs: str, net: str, offset: int) -> ClosedTrade:
    return ClosedTrade(
        symbol="EURUSD",
        side=side,
        volume=Decimal("1"),
        entry_time=START + offset * HOUR,
        entry_price=Decimal("1.10000"),
        exit_time=START + (offset + 1) * HOUR,
        exit_price=Decimal("1.10200"),
        gross_pnl=Decimal(gross),
        costs=Decimal(costs),
        net_pnl=Decimal(net),
        reason="tp" if side is Side.LONG else "sl",
    )


def _persist(session: Session, trades: list[ClosedTrade]) -> tuple[BacktestMetrics, list[Trade]]:
    instrument = _instrument(session)
    backtest = _backtest(session, instrument.id)
    metrics_row, trade_rows = to_rows(
        trades=trades,
        metrics=compute_metrics(
            trades=trades, equity_curve=_curve(), initial_capital=Decimal("10000")
        ),
        backtest_id=backtest.id,
        instrument_id=instrument.id,
    )
    session.add(metrics_row)
    session.add_all(trade_rows)
    session.commit()
    session.expire_all()  # force a real read, not the identity map's copy of what we wrote

    summary = session.get(BacktestMetrics, backtest.id)
    assert summary is not None
    stored = list(
        session.scalars(
            select(Trade).where(Trade.backtest_id == backtest.id).order_by(Trade.entry_time)
        ).all()
    )
    return summary, stored


def test_metrics_whose_halves_round_apart_still_satisfy_the_check(session: Session) -> None:
    """`net_profit_balances` against a run where rounding the engine's own total gives
    90.98800910 while the rounded halves add to 90.98800909. Before the mapper rounded, this
    insert raised `IntegrityError` — which is how the AUDCAD acceptance run died."""
    summary, _ = _persist(
        session,
        [
            _tailed_trade(
                side=Side.LONG,
                gross="191.44801914480",
                costs="0",
                net="191.44801914480",
                offset=0,
            ),
            _tailed_trade(
                side=Side.SHORT,
                gross="-100.46001004600",
                costs="0",
                net="-100.46001004600",
                offset=1,
            ),
        ],
    )
    assert str(summary.gross_profit) == "191.44801914"
    assert str(summary.gross_loss) == "-100.46001005"
    assert str(summary.net_profit) == "90.98800909"


def test_a_trade_whose_legs_round_apart_still_satisfies_the_check(session: Session) -> None:
    """`net_pnl_balances`, the same defect one table over: rounding the engine's own net gives
    198.66520767 while the rounded legs give 198.66520766. Rarer than the metrics case — the
    cost has to land on the rounding boundary — but the same arithmetic."""
    _, stored = _persist(
        session,
        [
            _tailed_trade(
                side=Side.LONG,
                gross="199.0882702401",
                costs="0.423062575000",
                net="198.665207665100",
                offset=0,
            )
        ],
    )
    assert len(stored) == 1
    assert str(stored[0].gross_pnl) == "199.08827024"
    assert str(stored[0].costs) == "0.42306258"
    assert str(stored[0].net_pnl) == "198.66520766"


# --------------------------------------------------------------------------- #
# A live trade's two writes, against the CHECKs that judge them (rev_0012)      #
# --------------------------------------------------------------------------- #


def _live_session(session: Session, instrument_id: uuid.UUID) -> LiveSession:
    strategy = Strategy(
        definition={
            "schema_version": "1.0",
            "name": "Paper one",
            "description": "a session",
            "timeframe": "H1",
            "entry": {},
            "exit": {},
            "risk": {},
        },
        version=1,
    )
    session.add(strategy)
    session.flush()
    live = LiveSession(
        strategy_id=strategy.id,
        instrument_id=instrument_id,
        timeframe="H1",
        mode=SessionMode.PAPER,
        status=LiveSessionStatus.RUNNING,
        initial_capital=Decimal("10000"),
        cost_model={"type": "bar_spread"},
        engine_version="0.1.0",
    )
    session.add(live)
    session.flush()
    return live


def test_a_live_trade_opens_then_closes_and_the_checks_hold(session: Session) -> None:
    """The lifecycle PR-302-B exists for, judged by the database rather than by the mapper.

    Two writes, and each one has to satisfy `exit_is_all_or_nothing` on its own: the first with
    all four exit columns absent, the second with all four present. A translator that filled in
    three of them would be refused here and nowhere else — every unit test reads the models,
    and the models do not enforce a CHECK.
    """
    instrument = _instrument(session)
    live = _live_session(session, instrument.id)

    position = Position(
        symbol="EURUSD",
        side=Side.LONG,
        volume=Decimal("0.5"),
        entry_price=Decimal("1.10000"),
        entry_time=START,
        entry_costs=Decimal("5"),
        stop_loss=Decimal("1.09500"),
        initial_stop_loss=Decimal("1.09000"),
        take_profit=Decimal("1.12000"),
        context={"ema9": Decimal("1.10500"), "adx": None},
    )
    session.add(open_trade_row(position, live.id, instrument.id))
    session.commit()

    opened = session.execute(select(Trade)).scalar_one()
    assert opened.exit_time is None
    assert opened.net_pnl is None
    assert opened.stop_loss == Decimal("1.09000")
    assert opened.context == {"ema9": "1.10500", "adx": None}

    closed = ClosedTrade(
        symbol="EURUSD",
        side=Side.LONG,
        volume=Decimal("0.5"),
        entry_time=START,
        entry_price=Decimal("1.10000"),
        exit_time=START + 3 * HOUR,
        exit_price=Decimal("1.11000"),
        gross_pnl=Decimal("500"),
        costs=Decimal("11.25"),
        net_pnl=Decimal("488.75"),
        reason="tp",
        stop_loss=Decimal("1.09000"),
        take_profit=Decimal("1.12000"),
        r_multiple=Decimal("2"),
    )
    # Found by the correlation key, which is what the partial unique index guarantees.
    found = session.execute(
        select(Trade).where(Trade.live_session_id == live.id, Trade.entry_time == closed.entry_time)
    ).scalar_one()
    for column, value in close_trade_values(closed).items():
        setattr(found, column, value)
    live.last_bar_time = closed.exit_time
    session.commit()

    persisted = session.execute(select(Trade)).scalar_one()
    assert persisted.exit_reason is ExitReason.TAKE_PROFIT
    # Asserted present before being arithmetic on: the columns are nullable precisely because
    # the first write leaves them so, and `None - None` would fail as a type error rather than
    # as the missing-exit this is checking for.
    assert persisted.gross_pnl is not None
    assert persisted.costs is not None
    assert persisted.net_pnl == Decimal("488.75")
    assert persisted.net_pnl == persisted.gross_pnl - persisted.costs
    # ⚠️ Untouched by the close. The entry was settled at the fill, and a close that could move
    # it would move the denominator every R multiple is divided by.
    assert persisted.entry_price == Decimal("1.10000")
    assert persisted.stop_loss == Decimal("1.09000")
    assert persisted.context == {"ema9": "1.10500", "adx": None}
    reloaded = session.get(LiveSession, live.id)
    assert reloaded is not None
    assert reloaded.last_bar_time == closed.exit_time
