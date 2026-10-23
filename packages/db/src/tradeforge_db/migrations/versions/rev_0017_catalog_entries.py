"""a shelf a person can read, and the sweep saved beside the strategy

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-11

The screen that listed saved strategies made the problem visible: this database holds eleven of
them and not one has a name anybody wrote. They are `MME9-20260910-172055` and
`SCHOCH-20260910-162620`, because `strategies.name` is a generated column projected out of the
document and the builder stamps a time into it. Nobody noticed while the only place those names
appeared was a dropdown opened seconds after creating one.

So a catalogue needs a label of its own, and that is half of this table. The other half is the
**grid**, which until now existed only as something typed into the study screen and thrown away
at launch — `studies.grid` records what a study *did* search, and there was nowhere at all to
keep what somebody *intends* to search.

**Why the grid is not a column on `strategies`.** A strategy document is immutable per version
and it is the DSL: the engine reads every field of it. A grid is a question *about* a document,
the engine never sees it, and storing it inside would mean a `schema_version` bump plus a field
nulled on 267 existing rows. The cardinality disagrees too — one document can be swept by
several grids, and `9.1 across every average` and `9.1 across every stop` are two entries over
one strategy.

⚠️ **`strategy_id` pins a version, not a lineage**, and the FK is RESTRICT like every other
reference to a strategy in this schema. A grid is only meaningful against the document it
expands, because every path in it has to exist there. Following the lineage would let an edit to
the strategy leave an entry pointing at a document whose paths have moved, and nothing would say
so until a launch refused — long after the edit that caused it.

⚠️ **The CHECK is an invariant, not a policy** — the same line this schema has drawn twice
before (`rev_0016`, `order_audit`). That a grid is a JSON *object* is not a judgement anybody
will want to revisit: it is what `expand` means by a grid, and JSONB would otherwise happily
store a list or a bare number and let the code meet its first surprise inside the transaction
that is already writing runs. How *large* a grid may be is the opposite kind of rule — a budget
that changes with patience and with hardware — and it stays in the application, on `MAX_POINTS`.

The unique name is the same argument in a smaller key: two shelf entries called `9.1 com filtro`
is a catalogue nobody can speak about, and the failure it produces is a person launching the one
they did not mean.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "catalog_entries",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        # Nullable, and that is the honest shape: NULL means nobody wrote a description, which
        # is a different fact from an empty one. The same distinction the spread column makes.
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("strategy_id", sa.UUID(as_uuid=True), nullable=False),
        # `server_default`, so a row written by hand in psql gets a grid rather than a NULL that
        # every reader would then have to coalesce.
        sa.Column("grid", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_catalog_entries"),
        sa.UniqueConstraint("name", name="uq_catalog_entries_name"),
        sa.ForeignKeyConstraint(
            ["strategy_id"],
            ["strategies.id"],
            name="fk_catalog_entries_strategy_id_strategies",
            ondelete="RESTRICT",
        ),
        # ⚠️ The **bare** name. `ck` in the naming convention is
        # `ck_%(table_name)s_%(constraint_name)s`, so it substitutes whatever is passed here
        # *into* the template — a name already carrying the prefix comes out as
        # `ck_catalog_entries_ck_catalog_entries_a_grid_is_an_obje_01ca`, doubled and then
        # truncated to fit. Measured on the first run of this migration, not reasoned.
        sa.CheckConstraint("jsonb_typeof(grid) = 'object'", name="a_grid_is_an_object_of_axes"),
    )
    # Newest first is how the shelf is read, and it is the only ordering any caller asks for.
    op.create_index("ix_catalog_entries_created_at", "catalog_entries", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_catalog_entries_created_at", table_name="catalog_entries")
    op.drop_table("catalog_entries")
