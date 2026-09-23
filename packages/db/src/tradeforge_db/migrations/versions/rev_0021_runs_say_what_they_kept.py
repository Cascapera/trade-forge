"""a run says what it kept, and a sweep's run keeps no equity curve

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-23

His call (22 and 23/09), for sweeps across hundreds of assets: a sweep's run keeps its metrics
always, its trades only when it passed the bar (profit above zero and a floor of trades that
depends on the chart), and never the entry pictures or the equity curve. Measured on 22/09,
those two were 95% of a run's ~549 kB, and a sweep reads neither — its question is the aggregate.

Two changes:

* `backtests.recorded` — `full`, `trades` or `metrics` — is **written by the worker** when the run
  finishes. ⚠️ Not derived from `sweep_id` and the metrics: the bar can change, and a run judged
  under the old one must go on saying what it actually stored. `'full'` for the runs already
  written is true of them: every one kept everything.
* `backtest_metrics.equity_curve` becomes nullable, and `NULL` is "not kept". Not `'[]'`, which
  would say the run saw no bars.

⚠️ **Downgrade refuses to invent curves.** Putting NOT NULL back needs a value in every row, and
there is no honest one for a run that never stored its curve: `'[]'` would claim it saw no bars.
So the downgrade deletes nothing and fails loudly if any such run exists — re-run them or delete
them first.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RECORDED = ("full", "trades", "metrics")


def upgrade() -> None:
    op.add_column(
        "backtests",
        sa.Column("recorded", sa.String(16), nullable=False, server_default="full"),
    )
    op.create_check_constraint("backtest_recorded", "backtests", f"recorded IN {_RECORDED}")
    op.alter_column("backtest_metrics", "equity_curve", nullable=True)


def downgrade() -> None:
    missing = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM backtest_metrics WHERE equity_curve IS NULL"))
        .scalar_one()
    )
    if missing:
        raise RuntimeError(
            f"{missing} runs kept no equity curve; there is no honest value to put back. "
            "Re-run or delete them before downgrading past 0021."
        )
    op.alter_column("backtest_metrics", "equity_curve", nullable=False)
    op.drop_constraint("backtest_recorded", "backtests", type_="check")
    op.drop_column("backtests", "recorded")
