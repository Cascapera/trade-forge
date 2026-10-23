"""a finished run can stand for a new one: the copy says where it came from

Revision ID: 0037
Revises: 0036
Create Date: 2026-09-28

His ask (28/09): a launch whose run is the very measurement an earlier run already made — same
strategy, market, chart, window, capital, costs, instrument and engine — takes that result instead
of running it again. The new run is a copy (`reused_from` names the original) so every reader of a
sweep keeps reading its own rows. The index is the lookup the worker makes before each run.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("backtests", sa.Column("reused_from", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_backtests_reused_from_backtests",
        "backtests",
        "backtests",
        ["reused_from"],
        ["id"],
        ondelete="SET NULL",
    )
    # A deleted original sets its copies' link to null: without this index, each deletion scans.
    op.create_index("ix_backtests_reused_from", "backtests", ["reused_from"])
    op.create_index(
        "ix_backtests_measurement",
        "backtests",
        ["strategy_id", "instrument_id", "timeframe", "date_from", "date_to"],
    )


def downgrade() -> None:
    op.drop_index("ix_backtests_measurement", table_name="backtests")
    op.drop_index("ix_backtests_reused_from", table_name="backtests")
    op.drop_constraint("fk_backtests_reused_from_backtests", "backtests", type_="foreignkey")
    op.drop_column("backtests", "reused_from")
