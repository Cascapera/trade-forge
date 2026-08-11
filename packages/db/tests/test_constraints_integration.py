"""The constraints, proven to bite — against a real Postgres.

A CHECK constraint nobody ever violated on purpose is a CHECK constraint you are
merely hoping about. Every rule the schema claims to enforce is broken here, once,
and the database is expected to say no.

Run locally with:  docker compose up -d  &&  uv run pytest -m integration
"""

import datetime as dt
import uuid
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from tradeforge_db.instruments import CatalogueEntry, upsert_instruments
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
from tradeforge_db.seeds import INSTRUMENT_SEEDS, seed_instruments
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


def test_seeding_does_not_overwrite_a_catalogued_spread(session: Session) -> None:
    """The regression, and it is a report from production rather than a hypothetical.

    On 11-08-2026 the forex spreads were catalogued from a live terminal — EURUSD 8 ticks,
    GBPUSD 9 — and were NULL again minutes later. `docker compose up` runs the seed step on
    every start, the seeds went through the same upsert as the collector, and
    `default_spread_points` sits in `_INSTRUMENT_UPDATABLE`: the placeholder's silence was
    written straight over the measurement.

    A measured spread must outlive a stack restart, or the column is decorative.
    """
    spec = INSTRUMENT_SEEDS[0]
    upsert_instruments(session, (CatalogueEntry(spec, Decimal("8")),))
    session.commit()

    seed_instruments(session)
    session.commit()

    # Filtered, not `.scalar()`: seeding leaves four rows, and the bare scalar would read
    # whichever the database happened to return first — a NULL from one of the other three
    # would fail this test for the wrong reason, and an ordering fluke could pass it.
    stored = session.query(Instrument.default_spread_points).filter_by(symbol=spec.symbol)
    assert stored.scalar() == Decimal("8")


def test_seeding_does_not_overwrite_the_brokers_contract_specs(session: Session) -> None:
    """Not just the spread — the seeds had authority over all ten updatable columns.

    This is the half that was invisible, and worth a test precisely because nothing broke.
    The placeholders were written to match what MT5 reports for these four symbols, so
    rewriting `contract_size` with an identical `contract_size` changed nothing anyone could
    observe. The bug was always there; the spread was merely the first column where the two
    sources disagreed.

    So the catalogued value here is deliberately *different* from the seed's. A broker that
    resizes a contract, or any symbol whose placeholder was a guess, is this test.
    """
    seed = INSTRUMENT_SEEDS[0]
    from_broker = replace(seed, contract_size=Decimal("50000"), name="EURUSD as MT5 spells it")
    upsert_instruments(session, (CatalogueEntry(from_broker, Decimal("8")),))
    session.commit()

    seed_instruments(session)
    session.commit()

    row = session.query(Instrument).filter_by(symbol=seed.symbol).one()
    assert row.contract_size == Decimal("50000"), "the seed's placeholder reclaimed the row"
    assert row.name == "EURUSD as MT5 spells it"


def test_seeding_still_inserts_a_symbol_the_catalogue_lacks(session: Session) -> None:
    """Not overwriting must not become not writing — a fresh clone still needs its examples.

    The failure this guards against is a one-character one: `on_conflict_do_nothing` without
    `index_elements` swallows *every* conflict, and a seed run against an empty table would
    still work, so the suite would stay green while a real box got nothing.
    """
    assert session.query(Instrument).count() == 0

    written = seed_instruments(session)
    session.commit()

    assert written == len(INSTRUMENT_SEEDS)
    assert session.query(Instrument).count() == len(INSTRUMENT_SEEDS)


def test_re_seeding_a_populated_catalogue_reports_nothing_written(session: Session) -> None:
    """The count has to be rows written, not rows offered.

    `tradeforge-db seed` prints this number to the migrate log — the line an operator reads
    to learn what a deploy did. Returning `len(entries)` was harmless while every row was
    genuinely written; under DO NOTHING it would announce "seeded 4 instruments" having
    touched none, which is the same species of defect as a fresh spread under a stale date.
    """
    assert seed_instruments(session) == len(INSTRUMENT_SEEDS)
    session.commit()

    assert seed_instruments(session) == 0
    session.commit()

    assert session.query(Instrument).count() == len(INSTRUMENT_SEEDS)


# --------------------------------------------------------------------------- #
# The instrument's default spread                                               #
# --------------------------------------------------------------------------- #


def test_a_catalogued_spread_is_stored_and_re_reading_it_updates_in_place(
    session: Session,
) -> None:
    """The upsert converges, which is what makes re-running `catalogue` safe.

    A broker re-quotes a spread far more often than history is re-downloaded, so this path
    runs repeatedly against a symbol that already exists. It has to land on the new number,
    not fail on the conflict and not leave the stale one behind.
    """
    spec = INSTRUMENT_SEEDS[0]

    upsert_instruments(session, (CatalogueEntry(spec, Decimal("12")),))
    session.commit()
    assert session.query(Instrument.default_spread_points).scalar() == Decimal("12")

    upsert_instruments(session, (CatalogueEntry(spec, Decimal("16")),))
    session.commit()
    assert session.query(Instrument.default_spread_points).scalar() == Decimal("16")


def test_a_source_with_nothing_to_say_writes_null_rather_than_zero(session: Session) -> None:
    """Unknown and free are different claims, and the column has to keep them apart.

    Zero here would tell the screen this instrument costs nothing to trade — a statement
    about a market that no seeded row is entitled to make. NULL says nobody measured it,
    which is what lets the screen fall back to charging nothing *and say why*.
    """
    upsert_instruments(session, (INSTRUMENT_SEEDS[0],))
    session.commit()

    stored = session.query(Instrument.default_spread_points).scalar()
    assert stored is None
    assert stored != Decimal("0")


def test_a_measured_spread_survives_a_later_backfill_that_knows_nothing(
    session: Session,
) -> None:
    """⚠️ It does not — and this test exists to make that visible rather than surprising.

    `default_spread_points` is in the updatable set, so a catalogue run from a source that
    cannot quote a spread overwrites a measured one with NULL. That is the correct reading
    of an upsert whose whole job is to converge on what the source currently says: a broker
    that stopped quoting a spread has genuinely stopped, and silently keeping yesterday's
    number would be the catalogue asserting something nobody told it.
    """
    spec = INSTRUMENT_SEEDS[0]
    upsert_instruments(session, (CatalogueEntry(spec, Decimal("12")),))
    session.commit()

    upsert_instruments(session, (spec,))
    session.commit()

    assert session.query(Instrument.default_spread_points).scalar() is None


def test_a_zero_spread_is_allowed_but_a_negative_one_is_refused(session: Session) -> None:
    """Zero is a real quote on some instruments; negative would pay you for trading."""
    spec = INSTRUMENT_SEEDS[0]

    upsert_instruments(session, (CatalogueEntry(spec, Decimal("0")),))
    session.commit()
    assert session.query(Instrument.default_spread_points).scalar() == Decimal("0")

    # The upsert issues the INSERT itself, so the constraint bites on this call rather than
    # on a later commit — which is the point of asserting on it directly.
    with pytest.raises(IntegrityError, match="default_spread_points"):
        upsert_instruments(session, (CatalogueEntry(spec, Decimal("-1")),))


def test_re_cataloguing_a_symbol_moves_its_updated_at(session: Session) -> None:
    """A floating spread means nothing without the instant it was read at.

    `Instrument.updated_at` declares `onupdate=func.now()`, but that is an ORM hook and this
    write is a Core `INSERT ... ON CONFLICT DO UPDATE`: the SET clause is used exactly as
    written, so a column absent from it simply keeps its old value. The stamp therefore has
    to be set here, next to the columns it dates.

    Measured on the real catalogue before the fix: re-reading GBPUSD's spread stored the new
    number under a timestamp a week old. Both halves of the row were true on their own and
    the pair was a lie — which is worse than a missing value, because it reads as a
    measurement. See the sibling `upsert_dataset`, which has always stamped `collected_at`.

    ⚠️ Asserting merely that `updated_at` is *recent* would pass without the fix — the row
    is created moments earlier by `server_default`. Only comparing the stamp across two
    committed upserts can tell a column that tracks its row from one that never moves.
    """
    spec = INSTRUMENT_SEEDS[0]

    upsert_instruments(session, (CatalogueEntry(spec, Decimal("8")),))
    session.commit()
    first = session.query(Instrument.updated_at).scalar()

    # Separate transactions, deliberately: `now()` in Postgres is the *transaction's* start
    # time, so two upserts sharing one would share a stamp however correct the code was.
    upsert_instruments(session, (CatalogueEntry(spec, Decimal("9")),))
    session.commit()
    second = session.query(Instrument.updated_at).scalar()

    assert first is not None
    assert second is not None
    assert second > first, f"updated_at did not move: {first} -> {second}"
