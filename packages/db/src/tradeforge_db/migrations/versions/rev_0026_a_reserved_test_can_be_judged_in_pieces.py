"""a reserved-window test can be judged in pieces, and the judgement is kept

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-25

His ask (25/09): after a reserved-window test (`rev_0024`), cut each tested point's
out-of-sample run by calendar year or into blocks of N trades, count how many pieces made money,
and keep the answer. Started by hand, never automatically: a bad sweep needs no second look.

One row per slicing asked for. The rule (`mode`, `block_trades`, `pass_share`) is stored beside
the result because it is chosen *before* the result is seen — with two ways to cut, a stored
rule is the record of which one was decided on first. `CASCADE` from the test: a slicing of a
test that is gone describes nothing anybody can open.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MODE = sa.Enum(
    "calendar",
    "trades",
    name="slice_mode",
    native_enum=False,
    create_constraint=True,
    length=16,
)


def upgrade() -> None:
    op.create_table(
        "sweep_slicings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "sweep_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sweeps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("mode", _MODE, nullable=False),
        sa.Column("block_trades", sa.Integer(), nullable=True),
        sa.Column("pass_share", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_check_constraint(
        "blocks_are_sized_only_when_cutting_by_trades",
        "sweep_slicings",
        "(mode = 'trades') = (block_trades IS NOT NULL)",
    )
    op.create_check_constraint(
        "a_block_holds_five_trades_or_more",
        "sweep_slicings",
        "block_trades IS NULL OR block_trades >= 5",
    )
    op.create_check_constraint(
        "the_bar_is_a_share_above_zero",
        "sweep_slicings",
        "pass_share > 0 AND pass_share <= 1",
    )
    op.create_check_constraint(
        "a_result_is_an_object", "sweep_slicings", "jsonb_typeof(result) = 'object'"
    )
    op.create_index("ix_sweep_slicings_sweep_id", "sweep_slicings", ["sweep_id"])


def downgrade() -> None:
    op.drop_index("ix_sweep_slicings_sweep_id", table_name="sweep_slicings")
    op.drop_table("sweep_slicings")
