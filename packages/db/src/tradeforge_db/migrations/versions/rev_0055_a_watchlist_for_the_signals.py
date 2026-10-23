"""a watchlist for the signals

Revision ID: 0055
Revises: 0054
Create Date: 2026-10-08

Signals PR 4: the setups on the markets the live signals follow, each copied from a run that did
well. One active follow per (strategy, instrument, timeframe), or every signal would post twice.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "watch_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("strategy_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instrument_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("timeframe", sa.String(length=4), nullable=False),
        sa.Column("cost_model", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_backtest_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "no_target_r",
            sa.Numeric(precision=18, scale=8),
            server_default=sa.text("5"),
            nullable=False,
        ),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
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


def downgrade() -> None:
    op.drop_index("uq_watch_items_active", table_name="watch_items")
    op.drop_table("watch_items")
