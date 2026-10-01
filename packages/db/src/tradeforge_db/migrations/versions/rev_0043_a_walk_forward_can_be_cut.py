"""a walk-forward can be answered by cutting the runs it walks

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-01

`backtest_metrics.trades_by_years`: how many trades make each cell of `r_by_years` (year of entry,
then year of exit) — what a cut of whole years needs to put a trade floor on. Null for the runs
recorded before. `sweep_walk_forwards.mode`: `rerun`, what every walk-forward so far did, or `cut`
— every fold answered from the parent sweep's own runs, nothing run again. `sweep_walk_forward_folds
.cut`: that answer, per fold.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("backtest_metrics", sa.Column("trades_by_years", JSONB(), nullable=True))
    op.create_check_constraint(
        "trades_by_years_is_an_object",
        "backtest_metrics",
        "trades_by_years IS NULL OR jsonb_typeof(trades_by_years) = 'object'",
    )
    # Every walk-forward kept so far re-ran its sweep: the default names them, and stays.
    op.add_column(
        "sweep_walk_forwards",
        sa.Column("mode", sa.String(8), nullable=False, server_default=sa.text("'rerun'")),
    )
    op.create_check_constraint(
        "a_walk_forward_mode_is_known", "sweep_walk_forwards", "mode IN ('rerun', 'cut')"
    )
    op.add_column("sweep_walk_forward_folds", sa.Column("cut", JSONB(), nullable=True))
    op.create_check_constraint(
        "a_fold_cut_is_an_object",
        "sweep_walk_forward_folds",
        "cut IS NULL OR jsonb_typeof(cut) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint("a_fold_cut_is_an_object", "sweep_walk_forward_folds", type_="check")
    op.drop_column("sweep_walk_forward_folds", "cut")
    op.drop_constraint("a_walk_forward_mode_is_known", "sweep_walk_forwards", type_="check")
    op.drop_column("sweep_walk_forwards", "mode")
    op.drop_constraint("trades_by_years_is_an_object", "backtest_metrics", type_="check")
    op.drop_column("backtest_metrics", "trades_by_years")
