"""Invariants of the schema itself, checked without a database.

These are not "does SQLAlchemy work" tests. Each one guards a rule that, if broken,
produces wrong numbers rather than an error — the worst failure mode a backtesting
system has, because a wrong number is indistinguishable from a good strategy.
"""

import pytest
from sqlalchemy import DateTime, Float, Numeric, Table
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from tradeforge_db.base import Base
from tradeforge_schema.models import TIMEFRAMES


def table(name: str) -> Table:
    """The `Table` behind a model. `Model.__table__` is typed as a `FromClause`."""
    return Base.metadata.tables[name]


def ddl(name: str) -> str:
    """The CREATE TABLE that Postgres would actually receive."""
    # SQLAlchemy ships no annotations for the DDL compiler.
    statement = CreateTable(table(name))
    return str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]


EXPECTED_TABLES = {
    "instruments",
    "datasets",
    "strategies",
    "backtests",
    "backtest_metrics",
    "trades",
}

# Anything holding a price, a quantity or an amount of money. If a column below ever
# becomes a float, `0.1 + 0.2` stops being `0.3` and the equity curve starts lying.
MONETARY_COLUMNS = {
    ("instruments", "tick_size"),
    ("instruments", "tick_value"),
    ("instruments", "contract_size"),
    ("backtests", "initial_capital"),
    ("backtest_metrics", "net_profit"),
    ("backtest_metrics", "gross_profit"),
    ("backtest_metrics", "gross_loss"),
    ("trades", "entry_price"),
    ("trades", "exit_price"),
    ("trades", "volume"),
    ("trades", "gross_pnl"),
    ("trades", "costs"),
    ("trades", "net_pnl"),
}


def test_every_expected_table_is_registered() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_no_column_anywhere_is_a_float() -> None:
    """The rule is absolute, so the test is too: not one float in the whole schema.

    Stated over the metadata rather than over a list of columns, because the failure
    this prevents arrives in a *future* column that someone types as `float` without
    thinking about it.
    """
    floats = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, Float)
    ]

    assert floats == []


@pytest.mark.parametrize(("table_name", "column_name"), sorted(MONETARY_COLUMNS))
def test_monetary_columns_are_exact_decimals(table_name: str, column_name: str) -> None:
    column = Base.metadata.tables[table_name].columns[column_name]

    assert isinstance(column.type, Numeric)
    assert column.type.scale is not None
    assert column.type.scale >= 8


def test_every_timestamp_carries_a_timezone() -> None:
    """A naive timestamp is a bug waiting for a server to move.

    "The candle closed at 09:00" is not a fact until you say 09:00 *where*. Postgres
    stores TIMESTAMPTZ as an absolute instant; TIMESTAMP stores whatever the writer
    happened to mean, and the reader guesses.
    """
    naive = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, DateTime) and not column.type.timezone
    ]

    assert naive == []


def test_constraints_are_named_by_the_convention() -> None:
    """Unnamed constraints cannot be dropped by a downgrade.

    Postgres invents a name; Alembic then has nothing to write in `op.drop_constraint`.
    The convention in `base.py` makes every name derivable from its columns — which is
    what keeps every migration reversible.
    """
    unnamed = [
        f"{table.name}: {constraint!r}"
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if constraint.name is None
        or not str(constraint.name).startswith(("pk_", "uq_", "ck_", "fk_"))
    ]

    assert unnamed == []


def test_the_timeframe_check_is_derived_from_the_dsl() -> None:
    """The database's list of timeframes *is* the DSL's list, not a copy of it."""
    datasets = ddl("datasets")

    for timeframe in TIMEFRAMES:
        assert f"'{timeframe}'" in datasets
    assert len(TIMEFRAMES) == 8


def test_strategy_name_is_generated_from_the_definition() -> None:
    """`name` is a projection of the JSONB, so the two can never disagree."""
    strategies = ddl("strategies")

    assert "GENERATED ALWAYS AS (definition ->> 'name') STORED" in strategies
    assert "GENERATED ALWAYS AS (definition ->> 'schema_version') STORED" in strategies


def test_enums_are_check_constraints_not_native_types() -> None:
    """VARCHAR + CHECK, so a migration can change the allowed set in three lines."""
    instruments = ddl("instruments")

    assert "CREATE TYPE" not in instruments
    assert "ck_instruments_asset_class" in instruments
    assert "'forex'" in instruments


def test_trades_cascade_from_their_backtest_and_restrict_their_instrument() -> None:
    """Delete a run and its trades go with it. Delete a symbol and the database says no.

    Derived data cascades; referenced history does not. Getting these two backwards is
    how a cleanup script silently deletes six months of results.
    """
    foreign_keys = {fk.column.table.name: fk.ondelete for fk in table("trades").foreign_keys}

    assert foreign_keys == {"backtests": "CASCADE", "instruments": "RESTRICT"}


def test_metrics_are_keyed_by_their_backtest() -> None:
    """One row per run, enforced by the primary key rather than by a unique index."""
    metrics = table("backtest_metrics")
    primary_key = [column.name for column in metrics.primary_key]

    assert primary_key == ["backtest_id"]
    assert {fk.column.table.name for fk in metrics.foreign_keys} == {"backtests"}
