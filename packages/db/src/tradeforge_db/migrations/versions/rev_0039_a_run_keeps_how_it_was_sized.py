"""a run keeps how it was sized, year by year

Revision ID: 0039
Revises: 0038
Create Date: 2026-09-29

`backtest_metrics.sizing_by_years`: per year of entry, the equity the run held when the year opened
and the smallest lot it entered at. `backtest_metrics.sizing_refusals`: the signals it turned away
because the lot came to zero. Together they say whether cutting `r_by_years` to a window of whole
years gives what a run of that window would (`year_cut`): measured on 29/09, a run whose equity fell
until the lot hit zero took other trades than a run started later, and the R of every trade the two
shared was identical. Both null for the runs recorded before.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("backtest_metrics", sa.Column("sizing_by_years", JSONB(), nullable=True))
    op.add_column("backtest_metrics", sa.Column("sizing_refusals", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "sizing_by_years_is_an_object",
        "backtest_metrics",
        "sizing_by_years IS NULL OR jsonb_typeof(sizing_by_years) = 'object'",
    )
    op.create_check_constraint(
        "sizing_refusals_non_negative",
        "backtest_metrics",
        "sizing_refusals IS NULL OR sizing_refusals >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("sizing_refusals_non_negative", "backtest_metrics", type_="check")
    op.drop_constraint("sizing_by_years_is_an_object", "backtest_metrics", type_="check")
    op.drop_column("backtest_metrics", "sizing_refusals")
    op.drop_column("backtest_metrics", "sizing_by_years")
