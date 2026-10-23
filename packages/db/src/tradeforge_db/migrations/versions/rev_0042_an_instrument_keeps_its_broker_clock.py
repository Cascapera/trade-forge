"""an instrument keeps its broker's clock

Revision ID: 0042
Revises: 0041
Create Date: 2026-09-30

The bars above a chart are cut on the broker's server clock, and since 30/09 that clock is the
instrument's rather than each strategy's (`htf_offset`, his rule: *"vamos adotar o horário do
servidor mt5 como real"*). The rows found are filled with three hours: measured the same day on
every stored series (UKOIL, EURUSD, AUDUSD, 2009 to 2026), the D1 bar opens at 21:00 UTC summer and
winter alike — the collector shifted everything by a fixed three hours. The default is dropped
once they are filled, so a row written afterwards states its own clock.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "instruments",
        sa.Column(
            "server_offset",
            sa.Interval(),
            nullable=False,
            server_default=sa.text("interval '3 hours'"),
        ),
    )
    op.alter_column("instruments", "server_offset", server_default=None)
    op.create_check_constraint(
        "server_offset_is_a_clock",
        "instruments",
        "server_offset BETWEEN interval '-14 hours' AND interval '14 hours'",
    )


def downgrade() -> None:
    op.drop_constraint("server_offset_is_a_clock", "instruments", type_="check")
    op.drop_column("instruments", "server_offset")
