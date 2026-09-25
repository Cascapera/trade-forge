"""a reserved-window test's points can be resampled, and the answer is kept

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-25

Selection metrics, step three (25/09): Monte Carlo on demand. Each tested point's out-of-sample
trades are drawn with replacement into many paths — the drawdown and losing streak to expect, and
how often the run ends below zero (`montecarlo`). One row per request, with the seed that makes
it repeatable; `CASCADE` from the test, like a slicing (`rev_0026`).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sweep_montecarlos",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "sweep_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sweeps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("paths", sa.Integer(), nullable=False),
        sa.Column("seed", sa.String(64), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_check_constraint(
        "paths_within_bounds", "sweep_montecarlos", "paths BETWEEN 100 AND 5000"
    )
    op.create_check_constraint(
        "a_montecarlo_result_is_an_object",
        "sweep_montecarlos",
        "jsonb_typeof(result) = 'object'",
    )
    op.create_index("ix_sweep_montecarlos_sweep_id", "sweep_montecarlos", ["sweep_id"])


def downgrade() -> None:
    op.drop_index("ix_sweep_montecarlos_sweep_id", table_name="sweep_montecarlos")
    op.drop_table("sweep_montecarlos")
