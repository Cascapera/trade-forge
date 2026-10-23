"""a run keeps the instrument it was executed with

Revision ID: 0031
Revises: 0030
Create Date: 2026-09-25

Measured 25/09: three USDCHF runs executed again — same strategy, window, 7 764 candles, costs
and engine — made the same trades and a different amount of money, because a collection in
between had rewritten the instrument's tick value with the exchange rate of that moment. The
worker now writes the specification it used on the run, the first time, and reads it back after.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("backtests", sa.Column("instrument_spec", JSONB(), nullable=True))
    op.create_check_constraint(
        "an_instrument_spec_is_an_object",
        "backtests",
        "instrument_spec IS NULL OR jsonb_typeof(instrument_spec) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint("an_instrument_spec_is_an_object", "backtests", type_="check")
    op.drop_column("backtests", "instrument_spec")
