"""a signal keeps its no-target R

Revision ID: 0058
Revises: 0057
Create Date: 2026-10-13

A signal with no target closes at its setup's `no_target_r` — read through `setup_id`, which a
deleted setup nulls (`ON DELETE SET NULL`). A trade under way when its setup was deleted lost
the R it should close at, and the orphan watch followed it to its stop alone. The signal now
keeps its own copy, as it already keeps its strategy version, market and timeframe; the signals
recorded so far take their setup's, where the setup is still there.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RATIO = sa.Numeric(precision=18, scale=8)


def upgrade() -> None:
    op.add_column("signals", sa.Column("no_target_r", _RATIO, nullable=True))
    op.execute(
        "UPDATE signals SET no_target_r = live_setups.no_target_r "
        "FROM live_setups WHERE live_setups.id = signals.setup_id"
    )


def downgrade() -> None:
    op.drop_column("signals", "no_target_r")
