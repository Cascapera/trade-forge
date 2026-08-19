"""broker symbols snapshot

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-19

The screen has always offered whatever is in `instruments`, which is whatever somebody
catalogued by hand through the collector's CLI. That is not "the assets you can test" —
it is "the assets you already bothered to collect", and the two diverge the moment the
question is *which* asset to test next.

This table is the broker's own catalogue, photographed. It is deliberately **not** more
columns on `instruments`, and the reason is destructive rather than tidy: `datasets` and
`backtests` point at `instruments` with `ondelete="RESTRICT"`, so a sync that owned that
table would have to delete rows with Parquet on disk the first time the account changed.
Measured on 19/08/2026, that is not hypothetical — the same terminal that once listed
9550 symbols including AAPL now lists 84 of forex and CFDs. Separate, a sync is free to
replace the whole snapshot, because nothing points at it.

`server` and `synced_at` sit on every row rather than in a one-row side table. A sync
replaces the snapshot inside a single transaction, so the denormalisation cannot drift,
and it lets the screen show provenance — a stale list with no date on it is exactly what
misleads somebody who switched accounts and forgot.

No foreign key to `instruments`. The overlap is a coincidence of names, not a relation:
a symbol can be in the snapshot and never collected, and an instrument can be collected
and then dropped by the broker. Joining them by symbol where both happen to exist is the
screen's job, and it is a join it can afford.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "broker_symbols",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("description", sa.String(256), nullable=True),
        sa.Column("path", sa.String(256), nullable=True),
        sa.Column("digits", sa.SmallInteger(), nullable=True),
        sa.Column("visible", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("server", sa.String(64), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        # ⚠️ Spelled out in full, and inside `create_table` rather than as a follow-up
        # `op.create_unique_constraint`. The naming convention in `base.py` lives on the
        # model's `MetaData`, and a standalone alembic op does not go through it — the first
        # version of this migration passed the bare name `"symbol"` and Postgres accepted a
        # constraint literally called `symbol`, while the model calls the same thing
        # `uq_broker_symbols_symbol`. Nothing breaks until somebody runs `--autogenerate`, or
        # a later migration tries to drop it by the name the model reports.
        sa.UniqueConstraint("symbol", name="uq_broker_symbols_symbol"),
    )

    # ⚠️ `text_pattern_ops`, not a plain btree. The whole table exists to answer `symbol LIKE
    # 'EUR%'`, and under a non-C collation a default btree index cannot serve a prefix match —
    # Postgres falls back to a sequential scan. At 84 rows that is invisible and at 9550 it is
    # still fast, which is exactly why the mistake would never be noticed: the index would look
    # present and do nothing. Declared correctly once, here, instead of being discovered later.
    #
    # ⚠️ Declared with `postgresql_ops` rather than as a raw `sa.text("symbol
    # varchar_pattern_ops")` expression. Both produce the same index, but alembic cannot
    # *compare* an operator clause buried in an expression — it warns and gives up, so the
    # drift test that checks the models against the migrated schema would stop covering this
    # index while still reporting green.
    op.create_index(
        "ix_broker_symbols_symbol_prefix",
        "broker_symbols",
        ["symbol"],
        postgresql_ops={"symbol": "varchar_pattern_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_broker_symbols_symbol_prefix", table_name="broker_symbols")
    op.drop_table("broker_symbols")
