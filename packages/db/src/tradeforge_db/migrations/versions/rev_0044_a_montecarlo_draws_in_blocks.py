"""a Monte Carlo draws in blocks too, and can resample a sweep's ranking

Revision ID: 0044
Revises: 0043
Create Date: 2026-10-01

`sweep_montecarlos.block_trades`: the block asked for when each point is also drawn in blocks of
trades in a row — null takes each point's own. `rank_by` and `top_n`: which runs of an ordinary
sweep's ranking were resampled, null for a reserved-window test. Every row from before keeps all
three null; the draw in blocks lives in `result`, per point, and old points simply lack it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sweep_montecarlos", sa.Column("block_trades", sa.Integer(), nullable=True))
    op.add_column("sweep_montecarlos", sa.Column("rank_by", sa.String(16), nullable=True))
    op.add_column("sweep_montecarlos", sa.Column("top_n", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "a_block_is_two_to_fifty_trades",
        "sweep_montecarlos",
        "block_trades IS NULL OR block_trades BETWEEN 2 AND 50",
    )
    op.create_check_constraint(
        "a_ranking_is_named_with_its_size",
        "sweep_montecarlos",
        "(rank_by IS NULL) = (top_n IS NULL)",
    )
    op.create_check_constraint(
        "top_n_within_bounds", "sweep_montecarlos", "top_n IS NULL OR top_n BETWEEN 1 AND 20"
    )


def downgrade() -> None:
    op.drop_constraint("top_n_within_bounds", "sweep_montecarlos", type_="check")
    op.drop_constraint("a_ranking_is_named_with_its_size", "sweep_montecarlos", type_="check")
    op.drop_constraint("a_block_is_two_to_fifty_trades", "sweep_montecarlos", type_="check")
    op.drop_column("sweep_montecarlos", "top_n")
    op.drop_column("sweep_montecarlos", "rank_by")
    op.drop_column("sweep_montecarlos", "block_trades")
