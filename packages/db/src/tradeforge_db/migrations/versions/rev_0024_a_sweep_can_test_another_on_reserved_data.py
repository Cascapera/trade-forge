"""a sweep can be the reserved-window test of another

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-24

His ask (24/09): after a sweep, run its best points over a window none of them was chosen on.
Measured the same day by hand — 112 runs of four families over January to September 2026 — the
in-sample median of the best family fell from +50% over six years to +1.3%, and three of the four
went negative. That is the only honest second opinion a sweep can get (`Sweep`'s docstring), and
it had to be assembled one POST at a time.

The test is itself a sweep — the chosen points, on the reserved window — so it is queued, run,
read and exported by everything a sweep already has. What it adds is the link back:

* `holdout_of`: the sweep whose points it tests. `SET NULL` on delete, because a test that has
  run is a measurement in its own right; losing the sweep it came from costs the comparison,
  never the runs.
* `holdout_rule`: how the points were chosen — metric, how many per (entry, chart, market), the
  trade floor. Kept because "the best" means nothing without "by what", and a reader comparing two
  tests must be able to tell whether they chose alike.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sweeps", sa.Column("holdout_of", UUID(as_uuid=True), nullable=True))
    op.add_column("sweeps", sa.Column("holdout_rule", JSONB(), nullable=True))
    op.create_foreign_key(
        "fk_sweeps_holdout_of_sweeps",
        "sweeps",
        "sweeps",
        ["holdout_of"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_sweeps_holdout_of", "sweeps", ["holdout_of"])
    op.create_check_constraint(
        "a_holdout_rule_is_an_object",
        "sweeps",
        "holdout_rule IS NULL OR jsonb_typeof(holdout_rule) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint("a_holdout_rule_is_an_object", "sweeps", type_="check")
    op.drop_index("ix_sweeps_holdout_of", table_name="sweeps")
    op.drop_constraint("fk_sweeps_holdout_of_sweeps", "sweeps", type_="foreignkey")
    op.drop_column("sweeps", "holdout_rule")
    op.drop_column("sweeps", "holdout_of")
