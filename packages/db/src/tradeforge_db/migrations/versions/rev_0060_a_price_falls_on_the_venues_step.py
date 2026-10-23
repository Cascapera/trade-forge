"""a price falls on the venue's step

Revision ID: 0060
Revises: 0059
Create Date: 2026-10-14

XP's `WIN$` is the mini-index future made continuous by adjusting its past by a ratio at every
roll, so its prices fall on any point and the terminal reports a tick of 1, while every contract
trades in steps of 5 (`WDO$`: 0.001 against 0.5). A signal's levels on the adjusted series are not
prices anybody can type: signal #38's stop was 210 072. `price_step` is the venue's step when it is
not the series' tick, measured by the collector on the unadjusted sibling (`WIN$N`); NULL means the
tick is the step, as for every instrument quoted as it trades. The engine never reads it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The bare name: alembic applies `base.py`'s naming convention (see rev_0004).
_STEP_CHECK = "price_step_positive"
_PRICE = sa.Numeric(precision=20, scale=10)


def upgrade() -> None:
    op.add_column("instruments", sa.Column("price_step", _PRICE, nullable=True))
    op.create_check_constraint(_STEP_CHECK, "instruments", "price_step IS NULL OR price_step > 0")


def downgrade() -> None:
    op.drop_constraint(_STEP_CHECK, "instruments", type_="check")
    op.drop_column("instruments", "price_step")
