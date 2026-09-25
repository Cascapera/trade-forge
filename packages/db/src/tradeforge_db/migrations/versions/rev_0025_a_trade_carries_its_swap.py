"""a trade carries its swap

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-24

His account pays a swap on every night a forex position is held (24/09): GBPUSD -5 USD per lot per
night on both sides, three nights on Wednesday, none over the weekend. A swap can also be a credit,
so it is a signed column of its own rather than part of `costs`, which the table holds to be a
magnitude (`costs_non_negative`).

`net_pnl_balances` becomes `net_pnl = gross_pnl - costs + swap`. Every row already written has a
swap of 0, so every one of them still balances.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trades",
        sa.Column("swap", sa.Numeric(20, 8), nullable=False, server_default="0"),
    )
    op.drop_constraint("net_pnl_balances", "trades", type_="check")
    op.create_check_constraint(
        "net_pnl_balances", "trades", "net_pnl IS NULL OR net_pnl = gross_pnl - costs + swap"
    )


def downgrade() -> None:
    op.drop_constraint("net_pnl_balances", "trades", type_="check")
    op.create_check_constraint(
        "net_pnl_balances", "trades", "net_pnl IS NULL OR net_pnl = gross_pnl - costs"
    )
    op.drop_column("trades", "swap")
