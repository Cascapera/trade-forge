"""a ruined account is recorded, not refused

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-02

`backtest_metrics.ruined_at`: the close at which the equity first reached zero or below. And the
drawdown as a fraction may now exceed 1: a run whose account went below zero — a crypto spread
wider than an M5 stop lost more than the account in one trade — failed on the old CHECK and was
never recorded. The new one keeps it non-negative only.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "backtest_metrics", sa.Column("ruined_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.drop_constraint("max_drawdown_pct_is_a_fraction", "backtest_metrics", type_="check")
    op.create_check_constraint(
        "max_drawdown_pct_non_negative", "backtest_metrics", "max_drawdown_pct >= 0"
    )


def downgrade() -> None:
    op.drop_constraint("max_drawdown_pct_non_negative", "backtest_metrics", type_="check")
    op.create_check_constraint(
        "max_drawdown_pct_is_a_fraction", "backtest_metrics", "max_drawdown_pct BETWEEN 0 AND 1"
    )
    op.drop_column("backtest_metrics", "ruined_at")
