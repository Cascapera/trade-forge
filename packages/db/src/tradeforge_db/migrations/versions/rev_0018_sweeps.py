"""the product a study and a basket each refuse to take

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-11

A study varies the parameters and holds the market still; a basket varies the market and holds
the parameters still. Each exists because one number from one run cannot tell a method apart
from a lucky corner, and each attacks that from one direction. Neither takes the product, and
"run every variation I care about, over everything I have collected" is exactly the product —
plus a third axis, the timeframe.

⚠️ **The three list columns are documents, not join tables, and that is a trade rather than a
shortcut.** A join table would let the database refuse a sweep naming a catalogue entry that no
longer exists, and that is the wrong answer here: an entry is a *label*, removable by design
(`rev_0017`), and a finished sweep has to go on being readable after somebody tidies the shelf.
What the runs point at is the strategy document, which is immutable and which the RESTRICT
foreign key on `backtests.strategy_id` already refuses to let vanish. So the sweep records the
**question that was asked**, and the runs record what it resolved to.

⚠️ **`backtests.sweep_id` is a third independent grouping, not a replacement for the other two.**
A run can belong to a sweep and to nothing else, which is the common case; one `group_id` plus a
kind column would make every reader ask which kind before they could ask anything else, and
would forbid the coherent case of a run that is a point of a grid *and* a member of a sweep.
`SET NULL`, like `basket_id` and `study_id`: deleting the grouping must not delete the
measurements, which is a rule this schema has now stated three times.

The CHECKs say what the expansion means by an axis — a JSON **array**, never an object or a bare
value — and that a window runs forwards. Invariants, not policy: nobody will want to revisit
either, while *how many runs* a sweep may enqueue is a budget that changes with patience and
hardware, and it stays in the application beside `MAX_POINTS`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sweeps",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("entry_ids", JSONB(), nullable=False),
        sa.Column("symbols", JSONB(), nullable=False),
        sa.Column("timeframes", JSONB(), nullable=False),
        # Where each written document sits on the axes. Stored rather than recovered from the
        # strategy's name, which is a caption: splitting one works until an entry's name is a
        # prefix of another's, or until a value contains the separator.
        sa.Column("points", JSONB(), nullable=False, server_default="[]"),
        sa.Column("date_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("date_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("initial_capital", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sweeps"),
        # ⚠️ **Bare names.** `ck` in the naming convention is
        # `ck_%(table_name)s_%(constraint_name)s`, so a name already carrying the prefix comes
        # out doubled and then truncated — measured on `rev_0017`'s first run, not reasoned.
        sa.CheckConstraint("jsonb_typeof(entry_ids) = 'array'", name="entries_are_a_list"),
        sa.CheckConstraint("jsonb_typeof(symbols) = 'array'", name="symbols_are_a_list"),
        sa.CheckConstraint("jsonb_typeof(timeframes) = 'array'", name="timeframes_are_a_list"),
        sa.CheckConstraint("jsonb_typeof(points) = 'array'", name="points_are_a_list"),
        sa.CheckConstraint("date_to > date_from", name="a_window_runs_forwards"),
    )
    op.create_index("ix_sweeps_created_at", "sweeps", ["created_at"])

    op.add_column("backtests", sa.Column("sweep_id", sa.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_backtests_sweep_id_sweeps",
        "backtests",
        "sweeps",
        ["sweep_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_backtests_sweep_id", "backtests", ["sweep_id"])


def downgrade() -> None:
    op.drop_index("ix_backtests_sweep_id", table_name="backtests")
    op.drop_constraint("fk_backtests_sweep_id_sweeps", "backtests", type_="foreignkey")
    op.drop_column("backtests", "sweep_id")
    op.drop_index("ix_sweeps_created_at", table_name="sweeps")
    op.drop_table("sweeps")
