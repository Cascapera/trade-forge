"""The constraints, proven to bite — against a real Postgres.

A CHECK constraint nobody ever violated on purpose is a CHECK constraint you are
merely hoping about. Every rule the schema claims to enforce is broken here, once,
and the database is expected to say no.

Run locally with:  docker compose up -d  &&  uv run pytest -m integration
"""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from tradeforge_db.models import (
    Backtest,
    BacktestMetrics,
    BacktestStatus,
    Dataset,
    ExitReason,
    Instrument,
    Strategy,
    Trade,
)
from tradeforge_db.seeds import seed_instruments
from tradeforge_engine.domain import AssetClass, Side

pytestmark = pytest.mark.integration

JAN = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
FEB = dt.datetime(2024, 2, 1, tzinfo=dt.UTC)


def definition(name: str = "MA Cross") -> dict[str, Any]:
    """A strategy document.

    Only three keys matter to the database — it projects `name`, `description` and
    `schema_version` out of the JSONB and stores the rest opaquely. Validating that the
    document is a *legal strategy* is `tradeforge_schema`'s job, and deliberately not
    duplicated here (that is the two-layer validation from PR-004).
    """
    return {
        "schema_version": "1.0",
        "name": name,
        "description": "an example",
        "timeframe": "H1",
        "entry": {},
        "exit": {},
        "risk": {},
    }


def an_instrument(session: Session) -> Instrument:
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


def a_strategy(session: Session, name: str = "MA Cross", version: int = 1) -> Strategy:
    strategy = Strategy(definition=definition(name), version=version)
    session.add(strategy)
    session.flush()
    return strategy


def a_backtest(session: Session, **overrides: object) -> Backtest:
    values: dict[str, Any] = {
        "strategy_id": a_strategy(session).id,
        "instrument_id": an_instrument(session).id,
        "timeframe": "H1",
        "date_from": JAN,
        "date_to": FEB,
        "initial_capital": Decimal("10000"),
        "cost_model": {"type": "spread", "spread_points": 10},
        "status": BacktestStatus.DONE,
        "engine_version": "0.1.0",
    }
    values.update(overrides)
    backtest = Backtest(**values)
    session.add(backtest)
    session.flush()
    return backtest


# --------------------------------------------------------------------------- #
# Strategies are append-only                                                    #
# --------------------------------------------------------------------------- #


def test_a_strategy_cannot_be_updated(session: Session) -> None:
    """The trigger. This is the invariant the whole table exists for.

    Editing a saved strategy in place would make every backtest that points at it
    unexplainable: the row no longer describes the run that produced the numbers.
    """
    strategy = a_strategy(session)
    session.commit()

    with pytest.raises(IntegrityError, match="append-only"):
        session.execute(
            text("UPDATE strategies SET version = 2 WHERE id = :id"), {"id": strategy.id}
        )


def test_editing_means_inserting_the_next_version(session: Session) -> None:
    """The supported way to change a strategy: a new row, linked to its parent."""
    first = a_strategy(session, version=1)

    second = Strategy(definition=definition("MA Cross"), version=2, parent_version_id=first.id)
    session.add(second)
    session.commit()

    assert second.parent_version_id == first.id
    assert second.version == 2


def test_name_and_schema_version_are_read_out_of_the_definition(session: Session) -> None:
    """Generated columns: Postgres derives them, so they cannot contradict the document."""
    strategy = a_strategy(session, name="Breakout H1")
    session.commit()
    session.refresh(strategy)

    assert strategy.name == "Breakout H1"
    assert strategy.schema_version == "1.0"
    assert strategy.description == "an example"


def test_a_generated_column_cannot_be_written_by_hand(session: Session) -> None:
    """Not even raw SQL can set `name` to something the definition does not say."""
    with pytest.raises(ProgrammingError, match="cannot insert a non-DEFAULT value"):
        session.execute(
            text(
                "INSERT INTO strategies (id, definition, name, version)"
                " VALUES (:id, '{}'::jsonb, 'a lie', 1)"
            ),
            {"id": uuid.uuid4()},
        )


def test_two_rows_cannot_claim_the_same_name_and_version(session: Session) -> None:
    a_strategy(session, name="MA Cross", version=1)
    session.commit()

    session.add(Strategy(definition=definition("MA Cross"), version=1))

    with pytest.raises(IntegrityError, match="uq_strategies_name_version"):
        session.commit()


def test_a_second_version_must_name_its_parent(session: Session) -> None:
    """Lineage with a hole in it is not lineage."""
    session.add(Strategy(definition=definition("Orphan"), version=2))

    with pytest.raises(IntegrityError, match="lineage_starts_at_version_1"):
        session.commit()


# --------------------------------------------------------------------------- #
# Referential integrity                                                         #
# --------------------------------------------------------------------------- #


def test_an_instrument_with_history_cannot_be_deleted(session: Session) -> None:
    """RESTRICT. Deleting a symbol must not orphan the Parquet it catalogues."""
    instrument = an_instrument(session)
    session.add(
        Dataset(
            instrument_id=instrument.id,
            timeframe="H1",
            date_from=JAN,
            date_to=FEB,
            candle_count=744,
            parquet_path="data/EURUSD/H1/2024.parquet",
        )
    )
    session.commit()

    session.delete(instrument)

    with pytest.raises(IntegrityError, match="fk_datasets_instrument_id_instruments"):
        session.commit()


def test_deleting_a_backtest_takes_its_results_with_it(session: Session) -> None:
    """CASCADE. Metrics and trades are derived data: they mean nothing without the run."""
    backtest = a_backtest(session)
    session.add(
        Trade(
            backtest_id=backtest.id,
            instrument_id=backtest.instrument_id,
            direction=Side.LONG,
            entry_time=JAN,
            entry_price=Decimal("1.10000"),
            volume=Decimal("0.1"),
            exit_time=FEB,
            exit_price=Decimal("1.12000"),
            exit_reason=ExitReason.TAKE_PROFIT,
            gross_pnl=Decimal("200"),
            costs=Decimal("5"),
            net_pnl=Decimal("195"),
        )
    )
    session.commit()

    session.execute(text("DELETE FROM backtests WHERE id = :id"), {"id": backtest.id})
    session.commit()

    assert session.query(Trade).count() == 0
    assert session.query(BacktestMetrics).count() == 0


def test_one_dataset_row_per_instrument_and_timeframe(session: Session) -> None:
    """What makes the collector's backfill idempotent instead of ever-growing."""
    instrument = an_instrument(session)
    for _ in range(2):
        session.add(
            Dataset(
                instrument_id=instrument.id,
                timeframe="H1",
                date_from=JAN,
                date_to=FEB,
                candle_count=744,
                parquet_path="data/EURUSD/H1/2024.parquet",
            )
        )

    with pytest.raises(IntegrityError, match="uq_datasets_instrument_id_timeframe"):
        session.commit()


# --------------------------------------------------------------------------- #
# Rules about numbers                                                           #
# --------------------------------------------------------------------------- #


def test_a_failed_backtest_must_say_why(session: Session) -> None:
    # `a_backtest` flushes, so the database rejects the row inside this call.
    with pytest.raises(IntegrityError, match="failed_needs_error"):
        a_backtest(session, status=BacktestStatus.FAILED, error=None)


def test_an_unknown_timeframe_is_rejected(session: Session) -> None:
    """The list comes from the DSL. `M2` is not in it, so it does not exist."""
    with pytest.raises(IntegrityError, match="ck_backtests_timeframe"):
        a_backtest(session, timeframe="M2")


def test_net_profit_must_equal_gross_profit_plus_gross_loss(session: Session) -> None:
    """The sign convention, enforced.

    `gross_loss` is negative and `net_profit` is the sum — not the difference. Every
    backtesting codebase eventually grows a bug where two functions disagree about that,
    and the symptom is a P&L that is off by exactly twice the losses.
    """
    backtest = a_backtest(session)
    session.add(
        BacktestMetrics(
            backtest_id=backtest.id,
            net_profit=Decimal("500"),  # a lie: 1000 + (-800) is 200
            gross_profit=Decimal("1000"),
            gross_loss=Decimal("-800"),
            total_trades=10,
            long_trades=6,
            short_trades=4,
            win_rate=Decimal("0.6"),
            max_drawdown_abs=Decimal("120"),
            max_drawdown_pct=Decimal("0.012"),
            max_dd_duration_days=3,
            equity_curve=[{"t": "2024-01-01T00:00:00Z", "equity": "10000"}],
        )
    )

    with pytest.raises(IntegrityError, match="net_profit_balances"):
        session.commit()


def test_the_trade_counts_must_add_up(session: Session) -> None:
    backtest = a_backtest(session)
    session.add(
        BacktestMetrics(
            backtest_id=backtest.id,
            net_profit=Decimal("200"),
            gross_profit=Decimal("1000"),
            gross_loss=Decimal("-800"),
            total_trades=10,
            long_trades=6,
            short_trades=3,  # 6 + 3 is not 10
            win_rate=Decimal("0.6"),
            max_drawdown_abs=Decimal("120"),
            max_drawdown_pct=Decimal("0.012"),
            max_dd_duration_days=3,
            equity_curve=[],
        )
    )

    with pytest.raises(IntegrityError, match="trade_counts_balance"):
        session.commit()


def test_a_trade_cannot_be_half_closed(session: Session) -> None:
    """An exit price with no exit time is not a trade — it is free money in the metrics."""
    backtest = a_backtest(session)
    session.add(
        Trade(
            backtest_id=backtest.id,
            instrument_id=backtest.instrument_id,
            direction=Side.LONG,
            entry_time=JAN,
            entry_price=Decimal("1.10000"),
            volume=Decimal("0.1"),
            exit_price=Decimal("1.12000"),  # ...with no exit_time, reason or P&L
        )
    )

    with pytest.raises(IntegrityError, match="exit_is_all_or_nothing"):
        session.commit()


def test_an_open_trade_is_allowed(session: Session) -> None:
    """The other half of the same rule: *no* exit columns at all is a position still open."""
    backtest = a_backtest(session)
    session.add(
        Trade(
            backtest_id=backtest.id,
            instrument_id=backtest.instrument_id,
            direction=Side.SHORT,
            entry_time=JAN,
            entry_price=Decimal("1.10000"),
            volume=Decimal("0.1"),
            stop_loss=Decimal("1.11000"),
        )
    )
    session.commit()

    assert session.query(Trade).count() == 1


def test_prices_survive_the_round_trip_exactly(session: Session) -> None:
    """NUMERIC, not float. `0.00001` comes back as `0.00001`, not as `1.0000000000000001e-05`.

    This is the whole reason the money columns are decimals: an error of one part in
    10^16 is nothing on one fill and a visible drift after ten thousand of them.
    """
    instrument = an_instrument(session)
    session.commit()
    session.refresh(instrument)

    assert instrument.tick_size == Decimal("0.00001")
    assert instrument.tick_size.as_tuple() == Decimal("0.0000100000").as_tuple()


def test_a_tick_size_of_zero_is_rejected(session: Session) -> None:
    """It would divide by zero in position sizing — better to never let it in."""
    session.add(
        Instrument(
            symbol="BROKEN",
            name="Broken",
            asset_class=AssetClass.STOCK,
            currency_quote="USD",
            tick_size=Decimal("0"),
            tick_value=Decimal("1"),
            contract_size=Decimal("1"),
            digits=2,
        )
    )

    with pytest.raises(IntegrityError, match="tick_size_positive"):
        session.commit()


def test_an_unknown_asset_class_is_rejected(session: Session) -> None:
    """The enum is a CHECK constraint in the database, not just a Python class."""
    with pytest.raises(IntegrityError, match="ck_instruments_asset_class"):
        session.execute(
            text(
                "INSERT INTO instruments"
                " (id, symbol, name, asset_class, currency_quote,"
                "  tick_size, tick_value, contract_size, digits)"
                " VALUES (:id, 'X', 'X', 'nft', 'USD', 0.01, 0.01, 1, 2)"
            ),
            {"id": uuid.uuid4()},
        )


# --------------------------------------------------------------------------- #
# Seeds                                                                         #
# --------------------------------------------------------------------------- #


def test_seeding_twice_leaves_one_copy_of_each_instrument(session: Session) -> None:
    """Idempotent by construction — seeds get re-run every time someone rebuilds a dev box."""
    seed_instruments(session)
    seed_instruments(session)
    session.commit()

    symbols = session.query(Instrument.symbol).all()

    assert len(symbols) == len(set(symbols))
    assert ("EURUSD",) in symbols
