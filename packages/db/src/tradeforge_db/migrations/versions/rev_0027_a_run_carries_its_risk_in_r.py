"""a run carries its risk and stability in R

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-25

Selection metrics, step one (25/09): the deepest drawdown in R, the longest losing streak (count
and R), R per calendar year and the share of years that ended positive — for every run, computed
by the worker while the trades are in memory (`r_metrics`). A sweep's losing run keeps no trades,
so these could not be recovered later for exactly the runs a choice is judged against.

Columns rather than one JSONB like `targets`, because a sweep's runs are ranked by them in SQL.
Null for every run recorded before this migration: nothing is backfilled, since the trades of most
of those runs were never kept.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RATIO = sa.Numeric(precision=18, scale=8)
_CHECKS = {
    "max_drawdown_r_non_negative": "max_drawdown_r IS NULL OR max_drawdown_r >= 0",
    "losing_streak_non_negative": "losing_streak IS NULL OR losing_streak >= 0",
    "losing_streak_r_non_positive": "losing_streak_r IS NULL OR losing_streak_r <= 0",
    "positive_year_share_is_a_fraction": (
        "positive_year_share IS NULL OR positive_year_share BETWEEN 0 AND 1"
    ),
    "yearly_r_is_an_object": "yearly_r IS NULL OR jsonb_typeof(yearly_r) = 'object'",
}


def upgrade() -> None:
    op.add_column("backtest_metrics", sa.Column("net_r", _RATIO, nullable=True))
    op.add_column("backtest_metrics", sa.Column("max_drawdown_r", _RATIO, nullable=True))
    op.add_column("backtest_metrics", sa.Column("losing_streak", sa.Integer(), nullable=True))
    op.add_column("backtest_metrics", sa.Column("losing_streak_r", _RATIO, nullable=True))
    op.add_column("backtest_metrics", sa.Column("positive_year_share", _RATIO, nullable=True))
    op.add_column("backtest_metrics", sa.Column("yearly_r", JSONB(), nullable=True))
    for name, condition in _CHECKS.items():
        op.create_check_constraint(name, "backtest_metrics", condition)


def downgrade() -> None:
    for name in reversed(list(_CHECKS)):
        op.drop_constraint(name, "backtest_metrics", type_="check")
    for column in (
        "yearly_r",
        "positive_year_share",
        "losing_streak_r",
        "losing_streak",
        "max_drawdown_r",
        "net_r",
    ):
        op.drop_column("backtest_metrics", column)
