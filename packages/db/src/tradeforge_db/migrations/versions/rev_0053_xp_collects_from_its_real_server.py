"""xp collects from its real server

Revision ID: 0053
Revises: 0052
Create Date: 2026-10-08

His XP terminal moved from the demo account to a real one on 08/10, whose MT5 server is
`XPMT5-PRD`. Measured the same day: the same clock (UTC-3), the same history (from 08/10/2021, cut
at five years like the demo) and the same symbol tree — so the broker stays one row, `xp`, and only
the server the collector recognises it by changes. The account is real; the collector only reads.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_brokers = sa.table("brokers", sa.column("slug", sa.String), sa.column("server", sa.String))


def upgrade() -> None:
    op.execute(_brokers.update().where(_brokers.c.slug == "xp").values(server="XPMT5-PRD"))


def downgrade() -> None:
    op.execute(_brokers.update().where(_brokers.c.slug == "xp").values(server="XPMT5-DEMO"))
