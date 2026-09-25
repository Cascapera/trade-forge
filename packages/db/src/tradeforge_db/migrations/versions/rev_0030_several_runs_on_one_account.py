"""several finished runs can be replayed on one shared account

Revision ID: 0030
Revises: 0029
Create Date: 2026-09-25

His ask (25/09): a cluster — a portfolio of setups on shared capital. The engine runs one
strategy with one position at a time (ADR-0019), so the members' kept trades are replayed in time
on one balance instead (`cluster`), each sized by its risk %, under a limit of open positions and
of open risk, with open positions marked on their own bars. One row per cluster, the members and
their risk % as used, the answer in `result`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS = sa.Enum(
    "queued",
    "running",
    "done",
    "failed",
    name="cluster_status",
    native_enum=False,
    create_constraint=True,
    length=16,
)
_CHECKS = {
    "cluster_capital_positive": "initial_capital > 0",
    "at_least_one_open_position": "max_open_positions >= 1",
    "open_risk_is_a_percent": "max_open_risk_percent > 0 AND max_open_risk_percent <= 100",
    "members_are_a_list": "jsonb_typeof(members) = 'array'",
    "cluster_result_is_an_object": "result IS NULL OR jsonb_typeof(result) = 'object'",
}


def upgrade() -> None:
    op.create_table(
        "clusters",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("initial_capital", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("max_open_positions", sa.Integer(), nullable=False),
        sa.Column("max_open_risk_percent", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("members", JSONB(), nullable=False),
        sa.Column("status", _STATUS, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("result", JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    for name, condition in _CHECKS.items():
        op.create_check_constraint(name, "clusters", condition)
    op.create_index("ix_clusters_created_at", "clusters", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_clusters_created_at", table_name="clusters")
    op.drop_table("clusters")
