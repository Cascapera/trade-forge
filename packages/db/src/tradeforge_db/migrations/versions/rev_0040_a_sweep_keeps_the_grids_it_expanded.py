"""a sweep keeps the grids it expanded

Revision ID: 0040
Revises: 0039
Create Date: 2026-09-29

`sweeps.entry_grids`: each catalogue entry's grid as the sweep expanded it, keyed by entry id. An
entry's grid becomes editable in place (29/09), and a sweep kept only the entry ids — so two sweeps
of one template launched before and after an edit would read as the same question and be combined
over two different grids. Filled for every sweep already written from the entries as they stand:
exact, because until this revision no grid could be edited. An entry deleted since has no key.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sweeps", sa.Column("entry_grids", JSONB(), nullable=True))
    op.execute(
        """
        UPDATE sweeps AS s
        SET entry_grids = (
            SELECT coalesce(jsonb_object_agg(e.id::text, e.grid), '{}'::jsonb)
            FROM catalog_entries AS e
            WHERE e.id::text IN (SELECT jsonb_array_elements_text(s.entry_ids))
        )
        """
    )
    op.create_check_constraint(
        "entry_grids_are_an_object",
        "sweeps",
        "entry_grids IS NULL OR jsonb_typeof(entry_grids) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint("entry_grids_are_an_object", "sweeps", type_="check")
    op.drop_column("sweeps", "entry_grids")
