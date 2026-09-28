"""a run warms up before its window; its R is kept by year of entry and of exit

Revision ID: 0038
Revises: 0037
Create Date: 2026-09-28

ADR-0030. `backtests.warmup_bars`: the bars a run read before `date_from` to warm up, traded on as
shadow and never booked — recorded because a run that starts where the history starts warms up
on less than it asks. `backtest_metrics.r_by_years`: the R of the booked trades by year of entry,
then year of exit, so a longer run answers a window of whole years with the trades a run of that
window would have closed inside it. Both null for the runs recorded before.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("backtests", sa.Column("warmup_bars", sa.Integer(), nullable=True))
    op.add_column("backtest_metrics", sa.Column("r_by_years", JSONB(), nullable=True))
    op.create_check_constraint(
        "r_by_years_is_an_object",
        "backtest_metrics",
        "r_by_years IS NULL OR jsonb_typeof(r_by_years) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint("r_by_years_is_an_object", "backtest_metrics", type_="check")
    op.drop_column("backtest_metrics", "r_by_years")
    op.drop_column("backtests", "warmup_bars")
