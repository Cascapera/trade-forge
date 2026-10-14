"""a share split is written down

Revision ID: 0059
Revises: 0058
Create Date: 2026-10-14

Tradeview's US shares come unadjusted: eleven splits across nine of them left a gap a backtest
read as a crash. `stock_splits` records each one the collector adjusts the stored bars for —
every bar before `effective_on` divided by `shares_after / shares_before`.
"""

import datetime as dt
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)


def _created_at() -> sa.Column[dt.datetime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "stock_splits",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("instrument_id", _UUID, nullable=False),
        sa.Column("effective_on", sa.Date(), nullable=False),
        sa.Column("shares_after", sa.Integer(), nullable=False),
        sa.Column("shares_before", sa.Integer(), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "shares_after > 0 AND shares_before > 0",
            name=op.f("ck_stock_splits_split_shares_positive"),
        ),
        sa.CheckConstraint(
            "shares_after <> shares_before", name=op.f("ck_stock_splits_split_changes_the_count")
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_stock_splits_instrument_id_instruments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_splits")),
        sa.UniqueConstraint(
            "instrument_id",
            "effective_on",
            name=op.f("uq_stock_splits_instrument_id_effective_on"),
        ),
    )


def downgrade() -> None:
    op.drop_table("stock_splits")
