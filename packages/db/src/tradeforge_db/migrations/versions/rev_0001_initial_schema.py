"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-07-14

The tables of sdd.md §5, plus the trigger that makes `strategies` append-only.

Constraint names are spelled out in full here rather than left to the database to
invent. They follow the convention in `base.py` exactly — which is what lets
`downgrade()` drop, by name, every object `upgrade()` created, and what keeps a
future `--autogenerate` from reporting a phantom difference between the models and
a database that is in fact identical to them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Rejecting the write outright, in the database, is the only version of this rule
# that survives contact with a maintenance script, a psql session at 2am, or an ORM
# bug. `restrict_violation` is chosen so that psycopg raises a specific, catchable
# error class rather than a generic one.
STRATEGIES_APPEND_ONLY_FUNCTION = """
CREATE FUNCTION strategies_reject_update() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'strategies are append-only: insert a new version instead of updating %', OLD.id
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

STRATEGIES_APPEND_ONLY_TRIGGER = """
CREATE TRIGGER strategies_no_update
    BEFORE UPDATE ON strategies
    FOR EACH ROW EXECUTE FUNCTION strategies_reject_update();
"""


def upgrade() -> None:
    op.create_table(
        "instruments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "asset_class",
            sa.Enum(
                "forex",
                "stock",
                "index",
                "future",
                "crypto",
                name="asset_class",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("exchange", sa.String(length=64), nullable=True),
        sa.Column("currency_base", sa.String(length=8), nullable=True),
        sa.Column("currency_quote", sa.String(length=8), nullable=False),
        sa.Column("tick_size", sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column("tick_value", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("contract_size", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("digits", sa.SmallInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("tick_size > 0", name="ck_instruments_tick_size_positive"),
        sa.CheckConstraint("tick_value > 0", name="ck_instruments_tick_value_positive"),
        sa.CheckConstraint("contract_size > 0", name="ck_instruments_contract_size_positive"),
        sa.CheckConstraint("digits BETWEEN 0 AND 10", name="ck_instruments_digits_range"),
        sa.PrimaryKeyConstraint("id", name="pk_instruments"),
        sa.UniqueConstraint("symbol", name="uq_instruments_symbol"),
    )

    op.create_table(
        "datasets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instrument_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("timeframe", sa.String(length=4), nullable=False),
        sa.Column("date_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("date_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("candle_count", sa.Integer(), nullable=False),
        sa.Column("parquet_path", sa.Text(), nullable=False),
        sa.Column(
            "collected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "timeframe IN ('M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1')",
            name="ck_datasets_timeframe",
        ),
        sa.CheckConstraint("date_to >= date_from", name="ck_datasets_date_range"),
        sa.CheckConstraint("candle_count >= 0", name="ck_datasets_candle_count_non_negative"),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name="fk_datasets_instrument_id_instruments",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_datasets"),
        sa.UniqueConstraint(
            "instrument_id", "timeframe", name="uq_datasets_instrument_id_timeframe"
        ),
    )

    op.create_table(
        "strategies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        # Generated columns: Postgres derives them from the JSONB on every write, so
        # they cannot drift from the document. The DSL remains the single definition
        # of what a strategy is called — this is just an indexable projection of it.
        sa.Column(
            "name",
            sa.Text(),
            sa.Computed("definition ->> 'name'", persisted=True),
            nullable=False,
        ),
        sa.Column(
            "description",
            sa.Text(),
            sa.Computed("definition ->> 'description'", persisted=True),
            nullable=True,
        ),
        sa.Column(
            "schema_version",
            sa.String(length=16),
            sa.Computed("definition ->> 'schema_version'", persisted=True),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version >= 1", name="ck_strategies_version_positive"),
        sa.CheckConstraint(
            "(version = 1) = (parent_version_id IS NULL)",
            name="ck_strategies_lineage_starts_at_version_1",
        ),
        sa.ForeignKeyConstraint(
            ["parent_version_id"],
            ["strategies.id"],
            name="fk_strategies_parent_version_id_strategies",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strategies"),
        sa.UniqueConstraint("name", "version", name="uq_strategies_name_version"),
    )

    op.execute(STRATEGIES_APPEND_ONLY_FUNCTION)
    op.execute(STRATEGIES_APPEND_ONLY_TRIGGER)

    op.create_table(
        "backtests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("strategy_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instrument_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("timeframe", sa.String(length=4), nullable=False),
        sa.Column("date_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("date_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("initial_capital", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("cost_model", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "done",
                "failed",
                name="backtest_status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "timeframe IN ('M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1')",
            name="ck_backtests_timeframe",
        ),
        sa.CheckConstraint("date_to >= date_from", name="ck_backtests_date_range"),
        sa.CheckConstraint("initial_capital > 0", name="ck_backtests_initial_capital_positive"),
        sa.CheckConstraint(
            "status <> 'failed' OR error IS NOT NULL", name="ck_backtests_failed_needs_error"
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR started_at IS NOT NULL",
            name="ck_backtests_finished_implies_started",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_backtests_finished_after_started",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name="fk_backtests_instrument_id_instruments",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id"],
            ["strategies.id"],
            name="fk_backtests_strategy_id_strategies",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_backtests"),
    )
    op.create_index("ix_backtests_status_created_at", "backtests", ["status", "created_at"])

    op.create_table(
        "backtest_metrics",
        sa.Column("backtest_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("net_profit", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("gross_profit", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("gross_loss", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("total_trades", sa.Integer(), nullable=False),
        sa.Column("long_trades", sa.Integer(), nullable=False),
        sa.Column("short_trades", sa.Integer(), nullable=False),
        sa.Column("win_rate", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("payoff", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("profit_factor", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("expectancy", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("max_drawdown_abs", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("max_drawdown_pct", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("max_dd_duration_days", sa.Integer(), nullable=False),
        sa.Column("sharpe", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("sortino", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("cagr", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("avg_trade_duration", sa.Interval(), nullable=True),
        sa.Column("equity_curve", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint("gross_profit >= 0", name="ck_backtest_metrics_gross_profit_sign"),
        sa.CheckConstraint("gross_loss <= 0", name="ck_backtest_metrics_gross_loss_sign"),
        sa.CheckConstraint(
            "net_profit = gross_profit + gross_loss",
            name="ck_backtest_metrics_net_profit_balances",
        ),
        sa.CheckConstraint(
            "total_trades = long_trades + short_trades",
            name="ck_backtest_metrics_trade_counts_balance",
        ),
        sa.CheckConstraint(
            "total_trades >= 0", name="ck_backtest_metrics_total_trades_non_negative"
        ),
        sa.CheckConstraint(
            "win_rate BETWEEN 0 AND 1", name="ck_backtest_metrics_win_rate_is_a_fraction"
        ),
        sa.CheckConstraint(
            "max_drawdown_abs >= 0", name="ck_backtest_metrics_max_drawdown_abs_non_negative"
        ),
        sa.CheckConstraint(
            "max_drawdown_pct BETWEEN 0 AND 1",
            name="ck_backtest_metrics_max_drawdown_pct_is_a_fraction",
        ),
        sa.CheckConstraint(
            "max_dd_duration_days >= 0", name="ck_backtest_metrics_max_dd_duration_non_negative"
        ),
        sa.ForeignKeyConstraint(
            ["backtest_id"],
            ["backtests.id"],
            name="fk_backtest_metrics_backtest_id_backtests",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("backtest_id", name="pk_backtest_metrics"),
    )

    op.create_table(
        "trades",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("backtest_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instrument_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "direction",
            sa.Enum(
                "long",
                "short",
                name="direction",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("entry_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_price", sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column("volume", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("exit_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_price", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column(
            "exit_reason",
            sa.Enum(
                "sl",
                "tp",
                "condition",
                "kill",
                "manual",
                name="exit_reason",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=True,
        ),
        sa.Column("stop_loss", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("take_profit", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("gross_pnl", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("costs", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("net_pnl", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("r_multiple", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column(
            "context",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint("entry_price > 0", name="ck_trades_entry_price_positive"),
        sa.CheckConstraint(
            "exit_price IS NULL OR exit_price > 0", name="ck_trades_exit_price_positive"
        ),
        sa.CheckConstraint("volume > 0", name="ck_trades_volume_positive"),
        sa.CheckConstraint(
            "(exit_time IS NULL) = (exit_price IS NULL)"
            " AND (exit_time IS NULL) = (exit_reason IS NULL)"
            " AND (exit_time IS NULL) = (net_pnl IS NULL)",
            name="ck_trades_exit_is_all_or_nothing",
        ),
        sa.CheckConstraint(
            "exit_time IS NULL OR exit_time >= entry_time", name="ck_trades_exit_after_entry"
        ),
        sa.CheckConstraint("costs IS NULL OR costs >= 0", name="ck_trades_costs_non_negative"),
        sa.CheckConstraint(
            "net_pnl IS NULL OR net_pnl = gross_pnl - costs", name="ck_trades_net_pnl_balances"
        ),
        sa.ForeignKeyConstraint(
            ["backtest_id"],
            ["backtests.id"],
            name="fk_trades_backtest_id_backtests",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name="fk_trades_instrument_id_instruments",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_trades"),
    )
    op.create_index("ix_trades_backtest_id_entry_time", "trades", ["backtest_id", "entry_time"])


def downgrade() -> None:
    """Unwind, children before parents.

    Dropping in the reverse order of creation is not cosmetic: `trades` holds a
    foreign key into `backtests`, and Postgres refuses to drop a table another one
    still points at. The trigger and its function go too — an orphaned function is
    exactly the kind of debris that makes the *next* upgrade fail with "already exists".
    """
    op.drop_index("ix_trades_backtest_id_entry_time", table_name="trades")
    op.drop_table("trades")
    op.drop_table("backtest_metrics")
    op.drop_index("ix_backtests_status_created_at", table_name="backtests")
    op.drop_table("backtests")
    op.execute("DROP TRIGGER IF EXISTS strategies_no_update ON strategies")
    op.execute("DROP FUNCTION IF EXISTS strategies_reject_update()")
    op.drop_table("strategies")
    op.drop_table("datasets")
    op.drop_table("instruments")
