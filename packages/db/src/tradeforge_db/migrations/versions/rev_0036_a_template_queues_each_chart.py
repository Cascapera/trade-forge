"""a template's queue runs one chart of one market at a time

Revision ID: 0036
Revises: 0035
Create Date: 2026-09-26

His ask (26/09): a market over every chart was one sweep whose preparation grew with the number
of charts — the minutes a single process spends at full boost, where this machine's CPU fails.
Each queued market now becomes one item per chart, lightest first, each its own small sweep.
`timeframe` is null on the items queued before: those still run every chart of the template.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sweep_template_items", sa.Column("timeframe", sa.String(8), nullable=True))


def downgrade() -> None:
    op.drop_column("sweep_template_items", "timeframe")
