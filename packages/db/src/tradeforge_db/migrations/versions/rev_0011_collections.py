"""collections requested from the screen

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-20

The screen can now find any symbol the broker offers and say how much history it has. This is
the row that lets somebody act on that: a request to go and fetch a range, addressable while it
runs.

⚠️ **Separate from `datasets`, which answers a different question.** `datasets` says what
exists — one row per (instrument, timeframe), overwritten whenever the picture changes. This
says what was asked for and how it went, one row per request, kept afterwards. Folded together,
a failed collection would either erase the catalogue's account of data already on disk or leave
no trace at all.

⚠️ **The row is written before the work starts.** The API returns 202 and an id; the host agent
picks the job up seconds — or, on a cold H4, minutes — later. Without a row at request time
`GET /collections/{id}` has nothing to answer with, and the screen cannot tell "queued behind
another job" from "the agent is not running", which are exactly the two states somebody needs
to tell apart while nothing appears to be happening.

No foreign key to `instruments`, and that is the load-bearing one: a collection is *how* a
symbol gets into that table. Requiring the row to exist first would make the first collection
of any new symbol impossible to record — which is the entire point of the feature.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from tradeforge_schema.models import TIMEFRAMES

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ⚠️ Read from the DSL rather than retyped — same reason as rev_0010: a migration carrying its
# own copy of the legal timeframes stops agreeing the first time one is added.
_TIMEFRAME_VALUES = ", ".join(f"'{timeframe}'" for timeframe in TIMEFRAMES)


def upgrade() -> None:
    op.create_table(
        "collections",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(4), nullable=False),
        sa.Column("date_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("date_to", sa.DateTime(timezone=True), nullable=False),
        # Nullable, and NULL is an answer: it means the symbol's tree path already said what
        # this is. A value here is provenance for a class a *person* decided, which happens on
        # the 24 of this broker's 84 symbols filed under CFDs, Crypto Currency or Metals.
        sa.Column(
            "asset_class",
            sa.Enum(
                "forex",
                "stock",
                "index",
                "future",
                "crypto",
                name="asset_class",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "done",
                "failed",
                name="collection_status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("years_done", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("years_total", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("candles", sa.Integer(), nullable=True),
        sa.Column("gaps", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        # ⚠️ Checks take **bare** names and uniques take full ones — the asymmetry rev_0010 was
        # bitten by. Inside `create_table` the convention in `base.py` applies, and `ck` is
        # `ck_%(table_name)s_%(constraint_name)s`, which *prepends* to whatever is given.
        sa.CheckConstraint(f"timeframe IN ({_TIMEFRAME_VALUES})", name="timeframe"),
        sa.CheckConstraint("date_to >= date_from", name="date_range"),
        sa.CheckConstraint("candles IS NULL OR candles >= 0", name="candles_non_negative"),
        sa.CheckConstraint("gaps IS NULL OR gaps >= 0", name="gaps_non_negative"),
        sa.CheckConstraint("years_done >= 0", name="years_done_non_negative"),
        sa.CheckConstraint("years_done <= years_total", name="years_done_within_total"),
    )
    op.create_index("ix_collections_requested_at", "collections", ["requested_at"])


def downgrade() -> None:
    op.drop_index("ix_collections_requested_at", table_name="collections")
    op.drop_table("collections")
