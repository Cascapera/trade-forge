"""a sweep is run market by market from a kept template

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-26

His ask (26/09): a sweep over thirteen markets was a day and a half of queue. A template keeps
what makes sweeps comparable — entries, charts, window, capital — and a queue runs one market's
sweep after another, today or another day; `sweeps.template_id` ties each back.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ITEM_STATUS = sa.Enum(
    "waiting",
    "launched",
    "failed",
    "removed",
    name="template_item_status",
    native_enum=False,
    create_constraint=True,
    length=16,
)
_TIME = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "sweep_templates",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("entry_ids", JSONB(), nullable=False),
        sa.Column("timeframes", JSONB(), nullable=False),
        sa.Column("date_from", _TIME, nullable=False),
        sa.Column("date_to", _TIME, nullable=False),
        sa.Column("initial_capital", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("created_at", _TIME, nullable=False, server_default=sa.func.now()),
    )
    for name, condition in {
        "template_entries_are_a_list": "jsonb_typeof(entry_ids) = 'array'",
        "template_charts_are_a_list": "jsonb_typeof(timeframes) = 'array'",
        "a_template_window_runs_forwards": "date_to > date_from",
        "template_capital_positive": "initial_capital > 0",
    }.items():
        op.create_check_constraint(name, "sweep_templates", condition)

    op.create_table(
        "sweep_template_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "template_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sweep_templates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("cost_model", JSONB(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", _ITEM_STATUS, nullable=False),
        sa.Column(
            "sweep_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sweeps.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", _TIME, nullable=False, server_default=sa.func.now()),
    )
    op.create_check_constraint(
        "item_costs_are_an_object", "sweep_template_items", "jsonb_typeof(cost_model) = 'object'"
    )
    op.create_index(
        "ix_sweep_template_items_template_id",
        "sweep_template_items",
        ["template_id", "position"],
    )

    op.add_column("sweeps", sa.Column("template_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_sweeps_template_id_sweep_templates",
        "sweeps",
        "sweep_templates",
        ["template_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_sweeps_template_id", "sweeps", ["template_id"])


def downgrade() -> None:
    op.drop_index("ix_sweeps_template_id", table_name="sweeps")
    op.drop_constraint("fk_sweeps_template_id_sweep_templates", "sweeps", type_="foreignkey")
    op.drop_column("sweeps", "template_id")
    op.drop_table("sweep_template_items")
    op.drop_table("sweep_templates")
