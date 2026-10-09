"""live setups and their signals

Revision ID: 0057
Revises: 0056
Create Date: 2026-10-09

His ask of 09/10: a setup is followed on several markets, added or dropped at any time, and keeps
its signals as history. `watch_items` (one setup x one market, empty when replaced) becomes
`live_setups` + `live_setup_markets`; `signals` keeps each signal's life, one row per number.
"""

import datetime as dt
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)
_PRICE = sa.Numeric(precision=20, scale=10)
_RATIO = sa.Numeric(precision=18, scale=8)


def _created_at() -> sa.Column[dt.datetime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.drop_index("uq_watch_items_active", table_name="watch_items")
    op.drop_table("watch_items")

    op.create_table(
        "live_setups",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("strategy_id", _UUID, nullable=False),
        sa.Column("timeframe", sa.String(length=4), nullable=False),
        sa.Column("cost_model", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("no_target_r", _RATIO, server_default=sa.text("5"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("source_backtest_id", _UUID, nullable=True),
        _created_at(),
        sa.CheckConstraint("no_target_r > 0", name=op.f("ck_live_setups_no_target_r_is_positive")),
        sa.CheckConstraint(
            "jsonb_typeof(cost_model) = 'object'",
            name=op.f("ck_live_setups_setup_costs_are_an_object"),
        ),
        sa.CheckConstraint("length(name) > 0", name=op.f("ck_live_setups_setup_has_a_name")),
        sa.ForeignKeyConstraint(
            ["strategy_id"],
            ["strategies.id"],
            name=op.f("fk_live_setups_strategy_id_strategies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_backtest_id"],
            ["backtests.id"],
            name=op.f("fk_live_setups_source_backtest_id_backtests"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_live_setups")),
    )
    op.create_table(
        "live_setup_markets",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("setup_id", _UUID, nullable=False),
        sa.Column("instrument_id", _UUID, nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["setup_id"],
            ["live_setups.id"],
            name=op.f("fk_live_setup_markets_setup_id_live_setups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_live_setup_markets_instrument_id_instruments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_live_setup_markets")),
        sa.UniqueConstraint(
            "setup_id", "instrument_id", name=op.f("uq_live_setup_markets_setup_id_instrument_id")
        ),
    )
    op.create_table(
        "signals",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("setup_id", _UUID, nullable=True),
        sa.Column("strategy_id", _UUID, nullable=True),
        sa.Column("instrument_id", _UUID, nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=4), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("order_type", sa.String(length=16), nullable=True),
        sa.Column("entry", _PRICE, nullable=True),
        sa.Column("stop", _PRICE, nullable=True),
        sa.Column("target", _PRICE, nullable=True),
        sa.Column("exit_price", _PRICE, nullable=True),
        sa.Column("result_r", _RATIO, nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("armed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("session_id", _UUID, nullable=True),
        sa.CheckConstraint(
            "status IN ('armed', 'triggered', 'cancelled', 'closed')",
            name=op.f("ck_signals_signal_status"),
        ),
        sa.CheckConstraint("side IN ('long', 'short')", name=op.f("ck_signals_signal_side")),
        sa.ForeignKeyConstraint(
            ["setup_id"],
            ["live_setups.id"],
            name=op.f("fk_signals_setup_id_live_setups"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id"],
            ["strategies.id"],
            name=op.f("fk_signals_strategy_id_strategies"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_signals_instrument_id_instruments"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signals")),
        sa.UniqueConstraint("number", name=op.f("uq_signals_number")),
    )
    op.create_index(op.f("ix_signals_setup_id"), "signals", ["setup_id"])
    op.create_index("ix_signals_setup_id_number", "signals", ["setup_id", "number"])


def downgrade() -> None:
    op.drop_index("ix_signals_setup_id_number", table_name="signals")
    op.drop_index(op.f("ix_signals_setup_id"), table_name="signals")
    op.drop_table("signals")
    op.drop_table("live_setup_markets")
    op.drop_table("live_setups")
    op.create_table(
        "watch_items",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("strategy_id", _UUID, nullable=False),
        sa.Column("instrument_id", _UUID, nullable=False),
        sa.Column("timeframe", sa.String(length=4), nullable=False),
        sa.Column("cost_model", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_backtest_id", _UUID, nullable=True),
        sa.Column("no_target_r", _RATIO, server_default=sa.text("5"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        _created_at(),
        sa.CheckConstraint("no_target_r > 0", name=op.f("ck_watch_items_no_target_r_is_positive")),
        sa.CheckConstraint(
            "jsonb_typeof(cost_model) = 'object'",
            name=op.f("ck_watch_items_watch_costs_are_an_object"),
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id"],
            ["strategies.id"],
            name=op.f("fk_watch_items_strategy_id_strategies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_watch_items_instrument_id_instruments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_backtest_id"],
            ["backtests.id"],
            name=op.f("fk_watch_items_source_backtest_id_backtests"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watch_items")),
    )
    op.create_index(
        "uq_watch_items_active",
        "watch_items",
        ["strategy_id", "instrument_id", "timeframe"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
