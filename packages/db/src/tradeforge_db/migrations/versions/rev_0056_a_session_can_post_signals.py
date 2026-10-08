"""a session can post signals

Revision ID: 0056
Revises: 0055
Create Date: 2026-10-08

Signals PR 5: `live_sessions.mode` gains `signal` — simulated exactly like paper, its moments
posted as signals. The paper-first trigger (rev_0016) already lets every mode but `live` through,
and counts only `paper` rows, so a signal session neither needs a promotion nor earns one.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "session_mode"  # the convention prefixes it: ck_live_sessions_session_mode


def upgrade() -> None:
    op.drop_constraint(op.f("ck_live_sessions_session_mode"), "live_sessions", type_="check")
    op.create_check_constraint(_NAME, "live_sessions", "mode IN ('paper', 'live', 'signal')")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_live_sessions_session_mode"), "live_sessions", type_="check")
    op.create_check_constraint(_NAME, "live_sessions", "mode IN ('paper', 'live')")
