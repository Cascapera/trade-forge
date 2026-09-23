"""trades record how far they went: MFE and MAE, as prices and in R

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-23

His plan of 23/09: a sweep runs without a target and every target is derived afterwards from how
far each trade went in its favour. That needs the maximum favourable and adverse excursions on the
row — as the prices reached, which hold even for a trade with no stop, and in R of the stop the
trade was sized against, which is what a target and a model read.

Read against the trade when a bar is ambiguous (the engine's `Position.best_price`), so the
favourable side can only err low. NULL is "not measured": every trade recorded before this one.
The R columns are distances, so they are checked non-negative — which way is the column's name.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PRICE = sa.Numeric(precision=20, scale=10)
_RATIO = sa.Numeric(precision=18, scale=8)


def upgrade() -> None:
    op.add_column("trades", sa.Column("mfe_price", _PRICE, nullable=True))
    op.add_column("trades", sa.Column("mae_price", _PRICE, nullable=True))
    op.add_column("trades", sa.Column("mfe_r", _RATIO, nullable=True))
    op.add_column("trades", sa.Column("mae_r", _RATIO, nullable=True))
    op.create_check_constraint("mfe_r_non_negative", "trades", "mfe_r IS NULL OR mfe_r >= 0")
    op.create_check_constraint("mae_r_non_negative", "trades", "mae_r IS NULL OR mae_r >= 0")


def downgrade() -> None:
    op.drop_constraint("mae_r_non_negative", "trades", type_="check")
    op.drop_constraint("mfe_r_non_negative", "trades", type_="check")
    op.drop_column("trades", "mae_r")
    op.drop_column("trades", "mfe_r")
    op.drop_column("trades", "mae_price")
    op.drop_column("trades", "mfe_price")
