"""xp back on its demo server

Revision ID: 0054
Revises: 0053
Create Date: 2026-10-08

Back to `XPMT5-DEMO` the same day (his call, 08/10): the real account adds no history — the same
five years, bar for bar — and a demo terminal cannot be sent a real order by anyone. The collector
only reads, but the safest real account is the one not logged in.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_brokers = sa.table("brokers", sa.column("slug", sa.String), sa.column("server", sa.String))


def upgrade() -> None:
    op.execute(_brokers.update().where(_brokers.c.slug == "xp").values(server="XPMT5-DEMO"))


def downgrade() -> None:
    op.execute(_brokers.update().where(_brokers.c.slug == "xp").values(server="XPMT5-PRD"))
