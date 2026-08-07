"""instrument default spread

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-07

Every backtest so far ran with `{"type": "none"}` because that is what the screen
offered, and nothing anywhere knew what an instrument's spread actually was. Measured
against the real trades already in this database, that is not a rounding error on the
instruments it matters for: the median stop on EURUSD H1 is 0.00116, so a 12-tick
spread is **10% of one R**, paid on every round trip and reported nowhere. On AAPL H1
the same arithmetic gives 0.55%, which is genuinely noise. The difference between those
two numbers is the reason this is a per-instrument column and not a field somebody types.

Nullable, and the distinction carries meaning. NULL is *nobody has measured this
symbol* — the seeds, and every row catalogued before this column existed. Zero is a
claim that the instrument costs nothing to trade, which for a real broker is almost
never true; storing zero for the unmeasured ones would make "free" and "unknown"
indistinguishable, and the screen could no longer tell the reader which one it is.

The CHECK admits zero and refuses negatives: a broker can legitimately quote a zero
spread, but a negative one would pay the account for trading.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The *bare* name: alembic applies `base.py`'s naming convention on top, so this becomes
# `ck_instruments_default_spread_points_non_negative`, matching the model's own constraint.
# Passing the full name here yields `ck_instruments_ck_instruments_…` — the mistake rev_0001
# made on every check of the backtests table, and which rev_0002 documented.
_SPREAD_CHECK = "default_spread_points_non_negative"

# Spelled out to match `base.PRICE` exactly, which is what `tick_size` and every other price
# distance on this table already uses. A spread is a price distance, so it is stored the same
# way rather than as an integer count of ticks — a broker quoting a fractional spread is
# unusual, not impossible, and an integer column would round it away in silence.
#
# The precision has to match the model's or `--autogenerate` would report a phantom
# difference on every run from here on.
_PRICE = sa.Numeric(precision=20, scale=10)


def upgrade() -> None:
    op.add_column("instruments", sa.Column("default_spread_points", _PRICE, nullable=True))
    op.create_check_constraint(
        _SPREAD_CHECK,
        "instruments",
        "default_spread_points IS NULL OR default_spread_points >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(_SPREAD_CHECK, "instruments", type_="check")
    op.drop_column("instruments", "default_spread_points")
