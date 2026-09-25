"""a settled sweep keeps its summary

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-25

Measured 25/09: the summary a sweep's screen polls (`GET /sweeps/{id}?runs=none`) took 6.5 s on a
sweep of 51 840 runs — every run read whole to compute medians that no longer change once every
run has ended. `summary` keeps them, with the run counts they were computed at; it is served only
while the counts still match, so a run retried or deleted makes it be computed again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sweeps", sa.Column("summary", JSONB(), nullable=True))
    op.create_check_constraint(
        "a_summary_is_an_object", "sweeps", "summary IS NULL OR jsonb_typeof(summary) = 'object'"
    )


def downgrade() -> None:
    op.drop_constraint("a_summary_is_an_object", "sweeps", type_="check")
    op.drop_column("sweeps", "summary")
