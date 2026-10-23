"""backtest candle provenance

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-03

`backtests.date_from`/`date_to` record what a run was *asked* for. Nothing recorded
what it actually read — so a request for two years over a dataset that starts eight
months in produced a run covering five months, with a row that still said two years.
These three columns close that gap.

All nullable, and deliberately so: rows written before this migration genuinely do not
know what they saw, and `0` would be a lie with the same shape as a fact. The CHECK
keeps the three in step, because provenance that is half filled in reads as
authoritative while being incomplete.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The *bare* name. Alembic applies `base.py`'s naming convention on top, so this becomes
# `ck_backtests_candle_provenance_complete` — exactly what the model's own constraint is
# called. Passing the full name here instead produces `ck_backtests_ck_backtests_...`, which
# is what rev_0001 accidentally did to every check on this table: measured, not assumed.
_PROVENANCE_CHECK = "candle_provenance_complete"


def upgrade() -> None:
    op.add_column("backtests", sa.Column("candles_seen", sa.Integer(), nullable=True))
    op.add_column("backtests", sa.Column("first_candle", sa.DateTime(timezone=True), nullable=True))
    op.add_column("backtests", sa.Column("last_candle", sa.DateTime(timezone=True), nullable=True))
    # Spelled out in full, like rev_0001: a migration builds its constraint on a bare
    # MetaData with no naming convention attached, so the name is taken literally. It must
    # match what the convention in `base.py` produces for the model, or `--autogenerate`
    # would report a phantom difference forever.
    op.create_check_constraint(
        _PROVENANCE_CHECK,
        "backtests",
        "(candles_seen IS NULL AND first_candle IS NULL AND last_candle IS NULL)"
        " OR (candles_seen > 0 AND first_candle IS NOT NULL AND last_candle IS NOT NULL"
        " AND last_candle >= first_candle)",
    )


def downgrade() -> None:
    op.drop_constraint(_PROVENANCE_CHECK, "backtests", type_="check")
    op.drop_column("backtests", "last_candle")
    op.drop_column("backtests", "first_candle")
    op.drop_column("backtests", "candles_seen")
