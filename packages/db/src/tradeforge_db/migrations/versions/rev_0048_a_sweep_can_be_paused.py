"""a sweep can be paused, its waiting runs kept here and queued again on resume

Revision ID: 0048
Revises: 0047
Create Date: 2026-10-03

`sweeps.paused_at`: when the sweep was paused. Its waiting runs leave the queue and stay `queued`
in `backtests`; the table, not the queue, is what a resume reads, so a pause outlives a machine
turned off and a job's expiry in Redis. Null for every sweep from before.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sweeps", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("sweeps", "paused_at")
