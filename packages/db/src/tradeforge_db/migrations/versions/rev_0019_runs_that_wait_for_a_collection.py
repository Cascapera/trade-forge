"""a run that waits for the data it needs

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-17

His rule (17/09): told that a window is not collected, the person may answer "collect it" — and
then the run should happen by itself, over the whole window, once the download lands. That needs
one fact the schema did not hold: **which collections a run is waiting for**.

⚠️ **A link table, not a column on `backtests`.** One run can wait for several collections — a
window whose gaps fall either side of what is on disk is two of them, and a basket's run may wait
for one while its neighbours wait for others. A column would hold the first and forget the rest,
and the worker would start on a half-downloaded window.

⚠️ **The row is the wait, and it is kept afterwards.** Deleting the link once a collection
finishes would leave "why did this run start ten minutes late?" unanswerable, and would make the
worker's question ("is anything I am waiting for still running?") depend on rows that vanish
under it.

⚠️ **Both ends CASCADE**, which for `collections` is a choice worth naming: deleting a collection
takes the wait with it and the run then starts on the next wake-up, over whatever is on disk.
Nothing in the application deletes a collection — the table is append-only in practice — so the
alternative (RESTRICT, which would make a collection undeletable while a run waits for it) would
only ever be felt by somebody clearing rows by hand.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backtest_collections",
        sa.Column("backtest_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("collection_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # The pair is the identity: the same run waiting twice for the same collection is not a
        # second fact, and a duplicate would make the worker count one download as two waits.
        # ⚠️ Its index is also the only one this table needs: the worker asks "what is this run
        # waiting for?", and `backtest_id` leads the key, so Postgres uses that index. A second
        # index on `backtest_id` alone would be written on every insert and read by nothing.
        sa.PrimaryKeyConstraint("backtest_id", "collection_id", name="pk_backtest_collections"),
        sa.ForeignKeyConstraint(
            ["backtest_id"],
            ["backtests.id"],
            name="fk_backtest_collections_backtest_id_backtests",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["collections.id"],
            name="fk_backtest_collections_collection_id_collections",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("backtest_collections")
