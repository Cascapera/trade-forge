"""a sweep names the markets it left out

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-18

His rule (18/09): told that a (market, chart) has no candles in the window and answering "do not
collect", the sweep skips that pair, runs the rest, and says which ones were left out. Until now
a sweep refused the whole launch instead, so that a map with holes could never pass for a map of
the whole space. The holes are allowed now; passing for complete is still not.

⚠️ **Stored, because the sweep is read back long after it is launched.** Its header prints the
markets and charts that were *asked* (`symbols`, `timeframes`), and the history, the dashboard
and the dataset all return to it later. Said once in the launch's answer and then forgotten, a
skipped pair would read on the next visit as a pair that was measured and happened to have no
row.

⚠️ **Not derivable from the runs.** A pair can also have no runs because the DSL refused every
point on that chart (an H4 filter cannot run on H4). Both absences look the same in `backtests`,
and they mean different things: one is data to collect, the other is a question that cannot be
asked.

`'[]'` for the sweeps already written is true of them: every one of them was launched under the
old rule, which refused rather than skipped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sweeps", sa.Column("skipped", JSONB(), nullable=False, server_default="[]"))
    op.create_check_constraint("skipped_is_a_list", "sweeps", "jsonb_typeof(skipped) = 'array'")


def downgrade() -> None:
    op.drop_constraint("skipped_is_a_list", "sweeps", type_="check")
    op.drop_column("sweeps", "skipped")
