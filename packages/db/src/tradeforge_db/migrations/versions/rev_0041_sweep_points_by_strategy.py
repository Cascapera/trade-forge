"""sweep points are found by their strategy

Revision ID: 0041
Revises: 0040
Create Date: 2026-09-29

The strategy picker leaves out every strategy a sweep expanded, and it asks `sweep_points` by
`strategy_id` to know which (29/09). The table is keyed by (sweep, position) and indexed by (sweep,
strategy), neither of which answers "is this strategy a point of any sweep": measured over 7.4
million points, the picker took 1.5 s, and 0.2 s with this index.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_sweep_points_strategy_id", "sweep_points", ["strategy_id"])


def downgrade() -> None:
    op.drop_index("ix_sweep_points_strategy_id", table_name="sweep_points")
