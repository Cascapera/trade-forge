"""an instrument knows its broker

Revision ID: 0050
Revises: 0049
Create Date: 2026-10-08

ADR-0032: several brokers at once — forex, metals, indices and crypto at ActivTrades, US shares at
Tradeview, Brazilian shares at a third. `brokers` holds each one's MT5 server and clock;
`instruments` gains the broker it comes from and that broker's ticker, while `symbol` stays the
internal name everything keys on.

The two brokers in use are seeded with the clocks their series were collected at (ActivTrades +2 h
since 02/10, Tradeview +3 h on 08/10), and every existing instrument is tied to the broker its
clock and class say it came from: shares at +3 h are Tradeview's, the rest ActivTrades'. A row with
neither (a seed, or one built by hand) keeps no broker, which the column allows.
"""

import datetime as dt
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVTRADES = uuid.UUID("6a0c1d64-7d8e-4b6f-9a71-2f1e6c0b0a01")
TRADEVIEW = uuid.UUID("6a0c1d64-7d8e-4b6f-9a71-2f1e6c0b0a02")


def upgrade() -> None:
    brokers = op.create_table(
        "brokers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("server", sa.String(length=128), nullable=False),
        sa.Column("terminal_path", sa.Text(), nullable=True),
        sa.Column("server_offset", sa.Interval(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]*$'", name=op.f("ck_brokers_slug_is_a_slug")),
        sa.CheckConstraint(
            "server_offset BETWEEN interval '-14 hours' AND interval '14 hours'",
            name=op.f("ck_brokers_server_offset_is_a_clock"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_brokers")),
        sa.UniqueConstraint("slug", name=op.f("uq_brokers_slug")),
        sa.UniqueConstraint("server", name=op.f("uq_brokers_server")),
    )
    op.bulk_insert(
        brokers,
        [
            {
                "id": ACTIVTRADES,
                "slug": "activtrades",
                "name": "ActivTrades",
                "server": "ActivTradesCorp-Server",
                "terminal_path": None,
                "server_offset": dt.timedelta(hours=2),
            },
            {
                "id": TRADEVIEW,
                "slug": "tradeview",
                "name": "Tradeview",
                "server": "Tradeview-Demo",
                "terminal_path": r"C:\Program Files\MetaTrader 5\terminal64.exe",
                "server_offset": dt.timedelta(hours=3),
            },
        ],
    )

    op.add_column(
        "instruments",
        sa.Column("broker_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("instruments", sa.Column("broker_symbol", sa.String(length=32), nullable=True))
    op.create_foreign_key(
        op.f("fk_instruments_broker_id_brokers"),
        "instruments",
        "brokers",
        ["broker_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        op.f("uq_instruments_broker_id_broker_symbol"),
        "instruments",
        ["broker_id", "broker_symbol"],
    )
    op.create_check_constraint(
        "broker_symbol_needs_a_broker",
        "instruments",
        "broker_symbol IS NULL OR broker_id IS NOT NULL",
    )

    # Only rows with candles behind them were collected from a broker; a seed or a hand-built row
    # has none and keeps no broker.
    tie = (
        "UPDATE instruments SET broker_id = :broker, broker_symbol = symbol "
        "WHERE broker_id IS NULL AND server_offset = :clock "
        "AND EXISTS (SELECT 1 FROM datasets d WHERE d.instrument_id = instruments.id)"
    )
    connection = op.get_bind()
    connection.execute(
        sa.text(tie + " AND asset_class = 'stock'"),
        {"broker": TRADEVIEW, "clock": dt.timedelta(hours=3)},
    )
    connection.execute(sa.text(tie), {"broker": ACTIVTRADES, "clock": dt.timedelta(hours=2)})


def downgrade() -> None:
    op.drop_constraint("broker_symbol_needs_a_broker", "instruments", type_="check")
    op.drop_constraint(
        op.f("uq_instruments_broker_id_broker_symbol"), "instruments", type_="unique"
    )
    op.drop_constraint(op.f("fk_instruments_broker_id_brokers"), "instruments", type_="foreignkey")
    op.drop_column("instruments", "broker_symbol")
    op.drop_column("instruments", "broker_id")
    op.drop_table("brokers")
