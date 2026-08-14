"""study of a grid

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-14

A study is one strategy launched over a grid of its own parameters, so the question
"is this result a property of the method, or of the corner I happened to pick?" has
somewhere to live. The sibling of `baskets`: that one varies the **market** and holds
the parameters still, this one varies the **parameters** and holds the market still.
Same shape for the same reason — each point still runs as its own `backtests` row,
and the engine is not touched.

**Every point is its own stored strategy, and that is why there is no `params`
column here.** A backtest points at a `strategy_id`, and that document is what it
executed; reproducing a run is reading one row. The tempting alternative — one
strategy plus an override carried on each run — would make "what did this run
actually execute" a question with two answers joined by merge logic, and nothing
would raise the day the two disagreed.

`grid` is kept as declared: the axes and their values, in order. It is not
derivable from the runs afterwards. The strategy names carry each point's values as
text, and reading a heatmap's axes back out of parsed names is exactly the kind of
reconstruction that works until a value contains a comma.

`backtests.study_id` is nullable and stays that way: every run made before this
migration was launched on its own, and so is every ordinary run made after it. NULL
means "this run belongs to no study", which is the truth for the 48 rows already
here — not a gap to backfill.

ON DELETE SET NULL, matching `basket_id` and for the same reason: deleting the
grouping must not delete the runs. The framing of an experiment is disposable, the
measurements are not, and CASCADE here would turn "I am done looking at this grid"
into "destroy a hundred backtests and their trades".

A run may carry both a `basket_id` and a `study_id`; nothing here forbids it. The
two are independent groupings and a run that is genuinely part of both is not a
contradiction — a constraint against it would only prejudge a combination (a grid
across a basket of markets) that this migration has no reason to rule out.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Spelled out to match `base.MONEY` and the `timeframe` domain the other tables use. A
# mismatch here shows up as a phantom difference on every `--autogenerate`.
_MONEY = sa.Numeric(precision=20, scale=8)
_TIMEFRAME = sa.String(length=4)

# Bare names: alembic applies `base.py`'s naming convention on top. Passing the full name
# yields `ck_studies_ck_studies_…`, the mistake rev_0001 made and rev_0002 documented.
_DATE_RANGE = "date_range"
_CAPITAL_POSITIVE = "initial_capital_positive"
_TIMEFRAME_CHECK = "timeframe"
_GRID_NOT_EMPTY = "grid_not_empty"

# Duplicated from the DSL rather than imported: a migration is a historical record and must
# keep applying to an old database exactly as it did the day it was written.
_TIMEFRAME_VALUES = "'M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1'"


def upgrade() -> None:
    op.create_table(
        "studies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # The lineage the grid was built from. RESTRICT, like every other reference to a
        # strategy: a stored run must never be able to point at a document that is gone.
        sa.Column(
            "strategy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategies.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # One market, unlike a basket. Holding the instrument still is what makes the points
        # of a grid comparable to each other at all.
        sa.Column(
            "instrument_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("instruments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("timeframe", _TIMEFRAME, nullable=False),
        sa.Column("date_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("date_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("initial_capital", _MONEY, nullable=False),
        # `{"setup.params.period": [5, 9, 20], ...}` — paths into the strategy document and
        # the values tried at each. JSONB rather than a child table: it is read whole, never
        # queried into, and a table of axes and a table of values would be two joins to
        # reassemble a literal that arrived as one.
        sa.Column("grid", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # The same three rules the backtests table enforces. A study that is internally impossible
    # must not be storable just because each run it spawns would be caught one layer down —
    # by then a hundred jobs are on the queue.
    op.create_check_constraint(_DATE_RANGE, "studies", "date_to >= date_from")
    op.create_check_constraint(_CAPITAL_POSITIVE, "studies", "initial_capital > 0")
    op.create_check_constraint(_TIMEFRAME_CHECK, "studies", f"timeframe IN ({_TIMEFRAME_VALUES})")
    # A study that varies nothing is a backtest, and `POST /backtests` already exists. Checked
    # here as well as in the router because an empty grid stored here is a study whose runs
    # cannot be explained by anything: the row would claim an experiment and describe none.
    op.create_check_constraint(
        _GRID_NOT_EMPTY, "studies", "jsonb_typeof(grid) = 'object' AND grid <> '{}'::jsonb"
    )

    op.add_column("backtests", sa.Column("study_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_backtests_study_id_studies",
        "backtests",
        "studies",
        ["study_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Reading a study means "every run with this study_id", which is the only query this column
    # exists to serve. Without the index it is a seq scan of every backtest ever run.
    op.create_index("ix_backtests_study_id", "backtests", ["study_id"])


def downgrade() -> None:
    op.drop_index("ix_backtests_study_id", table_name="backtests")
    op.drop_constraint("fk_backtests_study_id_studies", "backtests", type_="foreignkey")
    op.drop_column("backtests", "study_id")
    op.drop_table("studies")
