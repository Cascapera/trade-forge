"""trade entry snapshot

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-04

`trades.context` records the levels that justified an entry — a flat map of named
scalars, which is the shape a later "does this only work when ADX > 25?" needs. What
no column carried is the *shape* price had around the entry, and no number substitutes
for seeing it: the question "did this enter where the method says it should?" is
answered by looking, not by aggregating.

A separate column rather than a bigger `context`, because the two are different kinds
of fact. `context` is scalars, stringified for exactness, and it is the training
material for the phase-3 analysis; this is a time series, read once by a human and a
chart. Folding bars into `context` would mean every consumer of it learns to skip a
key that is not a number.

NOT NULL with an empty-object default, like `context`: a trade whose engine did not
record a window has `{}`, not a NULL to be distinguished from "recorded nothing".
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trades",
        sa.Column(
            "snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("trades", "snapshot")
