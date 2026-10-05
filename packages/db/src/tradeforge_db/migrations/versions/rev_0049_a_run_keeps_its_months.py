"""a run keeps its months

Revision ID: 0049
Revises: 0048
Create Date: 2026-10-05

`backtest_metrics.monthly_r` and `positive_month_share`: R per calendar month of entry and the share
of months that ended above zero (his ask, 05/10: "positive months beside the years"). Computed by
the worker from the trades while they are in memory; null for every run recorded before, which no
migration can fill — a sweep's losing run kept no trades.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "backtest_metrics",
        sa.Column("monthly_r", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "backtest_metrics",
        sa.Column("positive_month_share", sa.Numeric(18, 8), nullable=True),
    )
    op.create_check_constraint(
        "monthly_r_is_an_object",
        "backtest_metrics",
        "monthly_r IS NULL OR jsonb_typeof(monthly_r) = 'object'",
    )
    op.create_check_constraint(
        "positive_month_share_is_a_fraction",
        "backtest_metrics",
        "positive_month_share IS NULL OR positive_month_share BETWEEN 0 AND 1",
    )


def downgrade() -> None:
    op.drop_constraint("positive_month_share_is_a_fraction", "backtest_metrics", type_="check")
    op.drop_constraint("monthly_r_is_an_object", "backtest_metrics", type_="check")
    op.drop_column("backtest_metrics", "positive_month_share")
    op.drop_column("backtest_metrics", "monthly_r")
