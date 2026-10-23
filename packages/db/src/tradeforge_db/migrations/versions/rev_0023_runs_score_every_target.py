"""every run scores the ladder of targets, whatever it kept

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-23

His plan of 23/09: a sweep runs without a target, and every target is scored from how far each
trade went (MFE). But a sweep's losing run keeps no trades (0021) — and a run that loses without a
target may win with one. So the worker scores the whole ladder while the trades are still in
memory, and the scores are kept on every run: a few hundred bytes, against the trades it replaces.

`NULL` for every run recorded before this: nobody scored them, and a ladder of zeros would claim
they had been.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("backtest_metrics", sa.Column("targets", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("backtest_metrics", "targets")
