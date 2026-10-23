"""a sweep walks forward: run again fold by fold, its choices tested after each

Revision ID: 0032
Revises: 0031
Create Date: 2026-09-25

His ask (25/09, path B): at each fold the whole sweep runs again on a training window, its best
points are chosen by a reserved-window test's rule, and run on the window right after — so what
is judged is the way of choosing, not one choice. The folds' sweeps are ordinary sweeps (training)
and reserved-window tests (test); these tables tie them together.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS = sa.Enum(
    "queued",
    "running",
    "done",
    "failed",
    name="sweep_walk_forward_status",
    native_enum=False,
    create_constraint=True,
    length=16,
)
_TIME = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "sweep_walk_forwards",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "parent_sweep_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sweeps.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("start_year", sa.Integer(), nullable=False),
        sa.Column("train_years", sa.Integer(), nullable=False),
        sa.Column("test_years", sa.Integer(), nullable=False),
        sa.Column("anchored", sa.Boolean(), nullable=False),
        sa.Column("rule", JSONB(), nullable=False),
        sa.Column("status", _STATUS, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", _TIME, nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", _TIME, nullable=True),
    )
    op.create_check_constraint(
        "windows_are_whole_years", "sweep_walk_forwards", "train_years >= 1 AND test_years >= 1"
    )
    op.create_check_constraint(
        "a_walk_forward_rule_is_an_object", "sweep_walk_forwards", "jsonb_typeof(rule) = 'object'"
    )
    op.create_index(
        "ix_sweep_walk_forwards_parent_sweep_id", "sweep_walk_forwards", ["parent_sweep_id"]
    )
    op.create_table(
        "sweep_walk_forward_folds",
        sa.Column(
            "walk_forward_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sweep_walk_forwards.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("index", sa.Integer(), primary_key=True),
        sa.Column("train_from", _TIME, nullable=False),
        sa.Column("train_to", _TIME, nullable=False),
        sa.Column("test_from", _TIME, nullable=False),
        sa.Column("test_to", _TIME, nullable=False),
        sa.Column(
            "train_sweep_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sweeps.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "test_sweep_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sweeps.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "test_starts_at_training_end", "sweep_walk_forward_folds", "train_to = test_from"
    )
    op.create_check_constraint(
        "windows_run_forwards",
        "sweep_walk_forward_folds",
        "train_from < train_to AND test_from < test_to",
    )


def downgrade() -> None:
    op.drop_table("sweep_walk_forward_folds")
    op.drop_index("ix_sweep_walk_forwards_parent_sweep_id", table_name="sweep_walk_forwards")
    op.drop_table("sweep_walk_forwards")
