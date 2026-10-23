"""index runs by strategy

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-14

`backtests.strategy_id` is a foreign key, and Postgres does **not** index one for
you — only the referenced side gets an index, from its primary key. Its two
siblings on this table were indexed when they were added (`basket_id`,
`study_id`) because reading a grouping means "every run pointing at it"; asking
which runs belong to a strategy is the same question and had no index at all.

What made it matter is the studies table one revision back. A strategy listing
has to leave out the strategies a *grid* generated — a hundred-point study writes
a hundred rows nobody would ever pick from a list — and the honest way to know
which those are is to ask whether a strategy's runs belong to a study. That is
derived rather than flagged, so nothing can go stale: a column saying "generated"
is a second place for the truth to live, and the day it disagrees with the runs
it is the column that is believed.

Derived costs a lookup per strategy, though, and without this index each one is a
sequential scan of every backtest ever run. At today's 49 runs that is nothing; a
few grids in, it is the listing getting slower every time somebody searches a
parameter space, for a reason nobody would connect to searching a parameter space.

An index and nothing else — no column, no data touched, and it reverses cleanly.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_backtests_strategy_id", "backtests", ["strategy_id"])


def downgrade() -> None:
    op.drop_index("ix_backtests_strategy_id", table_name="backtests")
