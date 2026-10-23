"""a collection asks for the broker's ticker

Revision ID: 0052
Revises: 0051
Create Date: 2026-10-08

ADR-0032, an internal name apart from the broker's ticker: XP's continuous futures are `WIN$`,
`WDO$` and `BIT$`, and a `$` is no name for a Parquet folder (pyarrow writes it as `%24`) or a
URL. They are collected as `WIN`, `WDO` and `BIT`; the collection carries the ticker to ask the
terminal for. Null asks for the symbol itself, as every collection did before.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("collections", sa.Column("broker_symbol", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("collections", "broker_symbol")
