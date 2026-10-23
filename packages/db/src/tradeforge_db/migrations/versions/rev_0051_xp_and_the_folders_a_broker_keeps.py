"""xp and the folders a broker keeps

Revision ID: 0051
Revises: 0050
Create Date: 2026-10-08

ADR-0032, the third broker: XP (`XPMT5-DEMO`) for Brazilian shares and B3 futures, its clock
measured on 08/10 at UTC-3 (Brasília). Its terminal lists 51 644 symbols, 44 838 of them options,
so `brokers` gains `catalogue_paths` — the tree folders a sync keeps; XP keeps the cash market and
the continuous futures (WIN, WDO, BIT). ActivTrades' terminal, installed beside the others on
08/10, gets its path.
"""

import datetime as dt
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVTRADES = uuid.UUID("6a0c1d64-7d8e-4b6f-9a71-2f1e6c0b0a01")
XP = uuid.UUID("6a0c1d64-7d8e-4b6f-9a71-2f1e6c0b0a03")


def upgrade() -> None:
    op.add_column(
        "brokers", sa.Column("catalogue_paths", postgresql.ARRAY(sa.Text()), nullable=True)
    )
    brokers = sa.table(
        "brokers",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("slug", sa.String),
        sa.column("name", sa.String),
        sa.column("server", sa.String),
        sa.column("terminal_path", sa.Text),
        sa.column("server_offset", sa.Interval),
        sa.column("catalogue_paths", postgresql.ARRAY(sa.Text())),
    )
    op.bulk_insert(
        brokers,
        [
            {
                "id": XP,
                "slug": "xp",
                "name": "XP Investimentos",
                "server": "XPMT5-DEMO",
                "terminal_path": r"C:\Program Files\BRASILMetaTrader 5\terminal64.exe",
                "server_offset": dt.timedelta(hours=-3),
                "catalogue_paths": [r"BOVESPA\A VISTA", r"BMF\SERIES CONTINUAS"],
            }
        ],
    )
    op.execute(
        brokers.update()
        .where(brokers.c.id == ACTIVTRADES)
        .values(terminal_path=r"C:\Program Files\FOREXMetaTrader 5\terminal64.exe")
    )


def downgrade() -> None:
    brokers = sa.table("brokers", sa.column("id"), sa.column("terminal_path"))
    op.execute(brokers.update().where(brokers.c.id == ACTIVTRADES).values(terminal_path=None))
    op.execute(brokers.delete().where(brokers.c.id == XP))
    op.drop_column("brokers", "catalogue_paths")
