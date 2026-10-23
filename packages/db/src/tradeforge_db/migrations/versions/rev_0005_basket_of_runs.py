"""basket of runs

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-11

A basket is one strategy launched over several symbols at once, so the question
"does this method survive outside the instrument it was designed on?" has somewhere
to live. Each symbol still runs as its own `backtests` row, unchanged: the engine
holds one instrument and one position per run by construction, and this migration
deliberately does not challenge that.

**Why a table rather than a list the screen remembers.** The runs already exist
independently; the only thing being added is the fact that they were launched
together with one intent. Keeping that fact in the browser would make a reload lose
it, and would repeat the defect that produces the 409 in the strategy builder today
— a screen deciding from state only it can see. A basket is a claim about the past
("these four runs are one experiment"), and claims about the past belong in the
database.

`backtests.basket_id` is nullable, and it stays nullable forever: every run made
before this migration was launched on its own, and so is every single-symbol run
made after it. NULL means "this run belongs to no basket", which is the truth for
the 45 rows already here — not a gap to backfill.

ON DELETE SET NULL rather than CASCADE. Deleting the grouping must not delete the
runs: the experiment's framing is disposable, the measurements are not. CASCADE here
would make "I no longer care about this comparison" silently mean "destroy four
backtests and their trades".
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out to match `base.MONEY` and the `timeframe` domain the backtests table already
# uses. A mismatch here would show up as a phantom difference on every `--autogenerate`.
_MONEY = sa.Numeric(precision=20, scale=8)
_TIMEFRAME = sa.String(length=4)

# The bare names: alembic applies `base.py`'s naming convention on top. Passing the full
# name yields `ck_baskets_ck_baskets_…`, the mistake rev_0001 made and rev_0002 documented.
# Verified against this project's own database, where rev_0001's checks really did land as
# `ck_backtests_ck_backtests_timeframe` while rev_0002's landed correctly.
_DATE_RANGE = "date_range"
_CAPITAL_POSITIVE = "initial_capital_positive"
_TIMEFRAME_CHECK = "timeframe"

# Duplicated from the DSL rather than imported: a migration is a historical record and must
# keep applying to an old database exactly as it did the day it was written. Importing the
# live list would silently rewrite history the day a ninth timeframe is added.
_TIMEFRAME_VALUES = "'M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1'"


def upgrade() -> None:
    op.create_table(
        "baskets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "strategy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategies.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("timeframe", _TIMEFRAME, nullable=False),
        sa.Column("date_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("date_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("initial_capital", _MONEY, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # The same three rules the backtests table enforces. A basket that is internally
    # impossible must not be storable just because the runs it spawns would each be caught
    # one layer down: by then four jobs are already queued.
    op.create_check_constraint(_DATE_RANGE, "baskets", "date_to >= date_from")
    op.create_check_constraint(_CAPITAL_POSITIVE, "baskets", "initial_capital > 0")
    op.create_check_constraint(_TIMEFRAME_CHECK, "baskets", f"timeframe IN ({_TIMEFRAME_VALUES})")

    op.add_column(
        "backtests",
        sa.Column("basket_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_backtests_basket_id_baskets",
        "backtests",
        "baskets",
        ["basket_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Reading a basket means "every run with this basket_id", which is the only query this
    # column exists to serve. Without the index it is a seq scan of every backtest ever run.
    op.create_index("ix_backtests_basket_id", "backtests", ["basket_id"])


def downgrade() -> None:
    op.drop_index("ix_backtests_basket_id", table_name="backtests")
    op.drop_constraint("fk_backtests_basket_id_baskets", "backtests", type_="foreignkey")
    op.drop_column("backtests", "basket_id")
    op.drop_table("baskets")
