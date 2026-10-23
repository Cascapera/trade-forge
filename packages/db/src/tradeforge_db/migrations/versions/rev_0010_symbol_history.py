"""symbol history probe

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-19

The screen can now offer every symbol the broker has, and the obvious next question — how much
history is there — turned out to have no single answer. Measured on this broker: the terminal
caps the series at whatever `Max bars in chart` says, the oldest bars of EURUSD are prices
nobody traded going back to 1971, and the spread before 2009 is one number written across each
whole year. Three bounds, three different remedies, and a reader can only act on the one that
binds them.

So the row keeps them apart rather than folding them into an "available from" date. That date
is derivable and lossy: it cannot tell somebody to raise a setting, or to start later, or to
distrust the costs.

⚠️ `bar_count` counts positions including the bar still forming, because that is how `maxbars`
counts and the two have to be comparable. Storing closed bars would leave every capped series
one short of its ceiling and reading as uncapped.

No foreign key, like `broker_symbols` next door: a probe is about a symbol *name*, and both the
snapshot and the instruments table are free to be replaced under it when the account changes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from tradeforge_schema.models import TIMEFRAMES

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ⚠️ Read from the DSL rather than retyped. The set of legal timeframes is owned by the schema
# package, and a migration carrying its own copy is a copy that stops agreeing the first time
# one is added — the model's own CHECK is built from this same import.
_TIMEFRAME_VALUES = ", ".join(f"'{timeframe}'" for timeframe in TIMEFRAMES)


def upgrade() -> None:
    op.create_table(
        "symbol_history",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(4), nullable=False),
        sa.Column("oldest", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bar_count", sa.Integer(), nullable=False),
        sa.Column("terminal_maxbars", sa.Integer(), nullable=False),
        sa.Column(
            "bar_count_is_a_ceiling",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("last_fabricated", sa.SmallInteger(), nullable=True),
        sa.Column("first_measured_cost", sa.SmallInteger(), nullable=True),
        sa.Column("probed_at", sa.DateTime(timezone=True), nullable=False),
        # ⚠️ **The unique gets its full name and the checks get bare ones, and that asymmetry
        # is not a slip.** Inside `create_table` the convention in `base.py` applies, and the two
        # templates read the given name differently: `uq` is built from the table and columns and
        # ignores it, while `ck` is `ck_%(table_name)s_%(constraint_name)s` and *prepends* to it.
        # Spelling a check in full yields `ck_symbol_history_ck_symbol_history_bar_count…`, which
        # is what the first version of this migration created — the same mistake rev_0001 made on
        # every check of the backtests table and rev_0004 left a comment about.
        sa.UniqueConstraint("symbol", "timeframe", name="uq_symbol_history_symbol_timeframe"),
        sa.CheckConstraint(f"timeframe IN ({_TIMEFRAME_VALUES})", name="timeframe"),
        sa.CheckConstraint("bar_count >= 0", name="bar_count_non_negative"),
        sa.CheckConstraint("terminal_maxbars >= 0", name="terminal_maxbars_non_negative"),
    )


def downgrade() -> None:
    op.drop_table("symbol_history")
