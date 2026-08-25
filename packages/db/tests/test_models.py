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
    # The broker's own catalogue, photographed. Deliberately beside `instruments`
    # rather than merged into it: nothing references this table, which is what lets a
    # sync replace it wholesale when the account changes broker (ADR-0021).
    "broker_symbols",
    # What a probe found about one (symbol, timeframe), including the bounds on the answer.
    "symbol_history",
    "datasets",
    # One request to fetch a range, kept after it finishes. Beside `datasets` and not folded
    # into it: that one says what exists, this one says what was asked for and how it went.
    "collections",
    "strategies",
    "baskets",
    "studies",
    "walk_forwards",
    "walk_forward_folds",
    "backtests",
    "backtest_metrics",
    # A run that has not ended: no `date_to`, a `last_bar_time` that advances, and a status
    # that stays `running` for days. Beside `backtests` rather than a flag on it, because the
    # two answer different questions and a nullable half of each would answer neither.
    "live_sessions",
    "trades",
    # Every order the executor was asked to send, and what became of it. Append-only, enforced
    # by a trigger (rev_0015) — the one table here that exists for a reader who does not trust
    # the others, and the only one whose contents survive a `TRUNCATE` aimed at the schema.
    "order_audit",
}

# Anything holding a price, a quantity or an amount of money. If a column below ever
# becomes a float, `0.1 + 0.2` stops being `0.3` and the equity curve starts lying.
MONETARY_COLUMNS = {
    ("instruments", "tick_size"),
    ("instruments", "tick_value"),
    ("instruments", "contract_size"),
    ("backtests", "initial_capital"),
    # The two groupings carry the capital their runs are launched with. `baskets` was missing
    # here since it was added — caught while adding `studies`, and worth closing rather than
    # matching: this list is the specific check that a money column is an exact decimal of the
    # right scale, and a rule this file calls absolute cannot have two rows exempt from it.
    ("baskets", "initial_capital"),
    ("studies", "initial_capital"),
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


def test_trades_cascade_from_their_parent_and_restrict_their_instrument() -> None:
    """Delete a run and its trades go with it. Delete a symbol and the database says no.

    Derived data cascades; referenced history does not. Getting these two backwards is
    how a cleanup script silently deletes six months of results.

    Since rev_0012 there are **two** parents a trade can have, and both cascade on the same
    argument: the trades of a run are part of the run, whether that run finished or is still
    going. The instrument is neither's — it is referenced history, and it restricts.
    """
    foreign_keys = {fk.column.table.name: fk.ondelete for fk in table("trades").foreign_keys}

    assert foreign_keys == {
        "backtests": "CASCADE",
        "live_sessions": "CASCADE",
        "instruments": "RESTRICT",
    }


def test_a_trade_has_exactly_one_parent() -> None:
    """Both parent columns are nullable now, so the rule that stops an orphan — or a trade
    claiming two runs — is a CHECK and not a NOT NULL. Asserted here as well as against a real
    Postgres, because the models are what every unit test in this package reads."""
    trades = table("trades")
    checks = {constraint.name for constraint in trades.constraints if constraint.name}

    assert "ck_trades_exactly_one_parent" in checks
    assert trades.c.backtest_id.nullable
    assert trades.c.live_session_id.nullable


def test_a_live_session_restricts_the_strategy_and_the_instrument_it_ran() -> None:
    """Neither RESTRICT is decoration: deleting a strategy must not erase the record of it
    having traded, which is exactly what PR-304's promotion gate reads to decide whether a
    strategy has enough paper behind it to go real."""
    foreign_keys = {fk.column.table.name: fk.ondelete for fk in table("live_sessions").foreign_keys}

    assert foreign_keys == {"strategies": "RESTRICT", "instruments": "RESTRICT"}


def test_metrics_are_keyed_by_their_backtest() -> None:
    """One row per run, enforced by the primary key rather than by a unique index."""
    metrics = table("backtest_metrics")
    primary_key = [column.name for column in metrics.primary_key]

    assert primary_key == ["backtest_id"]
    assert {fk.column.table.name for fk in metrics.foreign_keys} == {"backtests"}
