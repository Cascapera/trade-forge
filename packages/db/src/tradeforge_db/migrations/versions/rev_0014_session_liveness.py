"""what a session was warmed with, and whether anything is still running it

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-24

Two columns, and both exist because a `live_sessions` row can outlive the process that wrote it.

**`warmup_bars`** is how much history the session was driven over before it opened. A fact, not a
plan: the amount available can come back short — a gap in the series, a symbol collected late —
and the number that matters afterwards is the one it *used*.

⚠️ It deliberately does not record whether the strategy was *ready*. Indicator warm-up is
calculable and structure warm-up is not: measured on this project's own data, the first CHoCH
arrives after 38 bars on AUDCAD H1 and after **730** on BTCUSD H1. There is no `N`, so there is
no "enough" to store. What a reader can do with this column is compare it against a session that
took no trade — which is the question, and the honest way to ask it.

**`heartbeat_at`** is the last time the process said it was alive.

⚠️ Its whole reason for existing is that `last_bar_time` cannot answer that. A session on H4
finishes a bar every four hours, so a stamp four hours old is indistinguishable from a process
that died — and `status` stays `running` for ever, because the thing that would have changed it
is the thing that died. This project already has that failure once: the collector agent's arq
health check refreshes on a 3600-second default and does not move when a job completes, so a
frozen reading means nothing within the hour (`specs/backlog.md`). A liveness signal that is only
true on the scale of hours is worse than none, because it looks like a liveness signal.

So this one is written on a cadence the session chooses deliberately and reports often enough to
be read as liveness. Nullable, because a row is written before the loop starts and a session that
never got that far should say so rather than claim a heartbeat it never had.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "live_sessions",
        sa.Column("warmup_bars", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "live_sessions",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Bare name inside `create_check_constraint`'s sibling — see rev_0012's note. The convention
    # in `base.py` prepends `ck_live_sessions_`, and passing the full name gets it twice.
    op.create_check_constraint("warmup_bars_non_negative", "live_sessions", "warmup_bars >= 0")
    # "Which sessions look alive?" is the reconciliation query, and it reads this column across
    # every running row. Postgres does not index a column for you.
    op.create_index("ix_live_sessions_heartbeat_at", "live_sessions", ["heartbeat_at"])


def downgrade() -> None:
    op.drop_index("ix_live_sessions_heartbeat_at", table_name="live_sessions")
    op.drop_constraint("warmup_bars_non_negative", "live_sessions", type_="check")
    op.drop_column("live_sessions", "heartbeat_at")
    op.drop_column("live_sessions", "warmup_bars")
