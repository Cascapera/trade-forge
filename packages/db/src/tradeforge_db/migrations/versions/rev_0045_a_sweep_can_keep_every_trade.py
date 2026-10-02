"""a sweep can keep every run's trades, won or lost

Revision ID: 0045
Revises: 0044
Create Date: 2026-10-02

`sweeps.keep_all_trades`: every run of the sweep keeps its trades whatever it made — a base for
meta-labeling (ADR-0031). False for every sweep from before, which kept them only for the runs
that passed the bar.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sweeps",
        sa.Column("keep_all_trades", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("sweeps", "keep_all_trades")
