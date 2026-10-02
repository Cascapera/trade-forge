"""a sweep keeps the years it cut from its window

Revision ID: 0047
Revises: 0046
Create Date: 2026-10-02

`sweeps.trimmed`: the pairs whose runs start later than the sweep's window, at the first year of
real bars — before it, a broker's intraday history can be one bar a day stored as the chart.
An empty list for every sweep from before, which cut nothing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sweeps",
        sa.Column(
            "trimmed", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
    )
    op.create_check_constraint("trimmed_is_a_list", "sweeps", "jsonb_typeof(trimmed) = 'array'")


def downgrade() -> None:
    op.drop_constraint("trimmed_is_a_list", "sweeps", type_="check")
    op.drop_column("sweeps", "trimmed")
