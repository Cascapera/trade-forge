"""sweeps can be read together, as one

Revision ID: 0034
Revises: 0033
Create Date: 2026-09-26

His ask (26/09): run a template's markets one at a time — some today, others tomorrow — then
read them together for the second phase. A combination is a sweep with no runs of its own whose
`combines` names the sweeps whose runs it reads.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sweeps", sa.Column("combines", JSONB(), nullable=True))
    op.create_check_constraint(
        "a_combination_is_a_list", "sweeps", "combines IS NULL OR jsonb_typeof(combines) = 'array'"
    )


def downgrade() -> None:
    op.drop_constraint("a_combination_is_a_list", "sweeps", type_="check")
    op.drop_column("sweeps", "combines")
