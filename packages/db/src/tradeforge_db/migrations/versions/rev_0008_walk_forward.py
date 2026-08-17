"""walk forward

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-15

`studies` (rev_0006) can find the best point of a grid. It cannot say whether that
point is worth anything, because it searched and scored on the same candles — so its
winner is the luckiest arrangement of one sample, and the wider the grid the luckier.
These two tables are where the honest version of the question lives: choose the
parameters on one window, score them on the **next** one, which the choice never saw.

Two tables and not one, because a walk-forward has two kinds of fact. The header row
holds what was asked for — which study, how many folds, how the training window moves,
which metric ranks the points. Each fold row holds one train→test pair: the window that
was tested, the counts the split was cut from, and the single point the training grid
chose.

**A fold's training grid is a `studies` row**, not a shape invented here. That is what
makes each fold's heatmap readable through the endpoint that already exists — and fold
1's heatmap beside fold 5's is what over-fitting looks like, more plainly than any
summary number states it. It is also why the fold has no `train_from`/`train_to`: that
window is the study's own `date_from`/`date_to`, and writing it twice would let a fold
claim one window while the runs underneath it executed another.

The fold's `test_from`/`test_to` do live here, because the run that covers them does not
exist when the row is written — it cannot, since its strategy is whatever the training
grid picks, which is not known until the training has run.

`train_bars`/`test_bars` are stored for the same reason `backtests.candles_seen` is: the
boundaries were cut **by counting candles** and then expressed as dates. A market has
weekends, holidays and half-days, so equal stretches of calendar hold unequal amounts of
trading; cutting by time would hand one fold twice the evidence of its neighbour while
the row still read as an even split. These columns are what let a reader verify the
split instead of trusting it.

`walk_forwards.status` reuses the `queued → running → done | failed` vocabulary of a run
because this row goes through those states for the same reasons. Unlike `baskets` and
`studies`, which are inert groupings whose runs each succeed or fail alone, a
walk-forward *decides*: it reads the training metrics and creates the test run from the
answer. A fold whose grid finished but whose choice could not be made — every point
failed, or none traded — is a state no `backtests` row can express.

Nothing is added to `backtests`. A third grouping column was considered and rejected: the
one out-of-sample run per fold is reached from `walk_forward_folds.test_backtest_id`, and
a run log that hides a grid's runs by default already leaves those six visible, which is
exactly right. They are the runs a decision produced.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Bare names: alembic applies `base.py`'s naming convention on top. Passing the full name
# yields `ck_walk_forwards_ck_walk_forwards_…`, the mistake rev_0001 made and rev_0002
# documented.
_FOLDS_AT_LEAST_TWO = "folds_at_least_two"
_TRAIN_AT_LEAST_AS_LONG = "train_at_least_as_long_as_test"
_FAILED_NEEDS_ERROR = "failed_needs_error"
_FINISHED_IMPLIES_STARTED = "finished_implies_started"
_FINISHED_AFTER_STARTED = "finished_after_started"
_INDEX_NON_NEGATIVE = "index_non_negative"
_TEST_RANGE = "test_range"
_WINDOWS_HAVE_BARS = "windows_have_bars"
_TEST_RUN_IMPLIES_A_CHOICE = "test_run_implies_a_choice"

# Spelled out rather than imported from the models, on the rule every migration here
# follows: a migration is a historical record and must keep applying to an old database
# exactly as it did the day it was written, whatever the enum grows into later.
_STATUS = sa.Enum(
    "queued",
    "running",
    "done",
    "failed",
    name="walk_forward_status",
    native_enum=False,
    create_constraint=True,
    length=16,
)
_METRIC = sa.Enum(
    "net_profit",
    "profit_factor",
    "sharpe",
    "expectancy",
    name="selection_metric",
    native_enum=False,
    create_constraint=True,
    length=16,
)


def upgrade() -> None:
    op.create_table(
        "walk_forwards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # RESTRICT, like every other pointer at a study or a strategy. A walk-forward whose
        # grid had been deleted would state a result about an experiment nobody could
        # describe any more.
        sa.Column(
            "study_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("studies.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("folds", sa.Integer(), nullable=False),
        sa.Column("train_multiple", sa.Integer(), nullable=False),
        # True: every fold trains on all history before its test window (the training set
        # grows). False: a fixed-length window slides forward. The two ask different
        # questions, so the answer is stored rather than assumed.
        sa.Column("anchored", sa.Boolean(), nullable=False),
        sa.Column("metric", _METRIC, nullable=False),
        sa.Column("status", _STATUS, nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    # One fold is a single train/test split. Useful, but not a walk-forward: it cannot say
    # whether a choice was stable, which is half of what this experiment reports.
    op.create_check_constraint(_FOLDS_AT_LEAST_TWO, "walk_forwards", "folds >= 2")
    op.create_check_constraint(_TRAIN_AT_LEAST_AS_LONG, "walk_forwards", "train_multiple >= 1")
    op.create_check_constraint(
        _FAILED_NEEDS_ERROR, "walk_forwards", "status <> 'failed' OR error IS NOT NULL"
    )
    op.create_check_constraint(
        _FINISHED_IMPLIES_STARTED, "walk_forwards", "finished_at IS NULL OR started_at IS NOT NULL"
    )
    op.create_check_constraint(
        _FINISHED_AFTER_STARTED, "walk_forwards", "finished_at IS NULL OR finished_at >= started_at"
    )
    # "Which walk-forwards were run on this study?" is asked by the study screen on every
    # poll; Postgres does not index a foreign key for you.
    op.create_index("ix_walk_forwards_study_id", "walk_forwards", ["study_id"])

    op.create_table(
        "walk_forward_folds",
        # A composite primary key, not a surrogate id: the pair *is* the identity. There is
        # exactly one fold 3 of a given walk-forward and no other row can be it — and the
        # index a fold is ordered by is then already part of its key.
        sa.Column(
            "walk_forward_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("walk_forwards.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("index", sa.Integer(), primary_key=True),
        # The in-sample grid over this fold's training window: a full study, with its own
        # heatmap. Unique, so a study cannot be quietly shared between folds.
        sa.Column(
            "study_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("studies.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("test_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("test_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("train_bars", sa.Integer(), nullable=False),
        sa.Column("test_bars", sa.Integer(), nullable=False),
        # Null until the fold is decided — and null is also a terminal answer, for a fold
        # whose every point failed or whose every point took no trades. Inventing a winner
        # there would put a run on the record that nothing chose.
        sa.Column(
            "chosen_strategy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategies.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        # SET NULL on the same principle as `backtests.basket_id`: deleting a run must not
        # take the fold's record of what was decided with it.
        sa.Column(
            "test_backtest_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backtests.id", ondelete="SET NULL"),
            nullable=True,
            unique=True,
        ),
    )
    op.create_check_constraint(_INDEX_NON_NEGATIVE, "walk_forward_folds", "index >= 0")
    op.create_check_constraint(_TEST_RANGE, "walk_forward_folds", "test_to >= test_from")
    # A window is cut by counting candles, so a window of none of them was never cut.
    op.create_check_constraint(
        _WINDOWS_HAVE_BARS, "walk_forward_folds", "train_bars > 0 AND test_bars > 0"
    )
    # A test run exists only because a choice was made. The reverse is allowed and happens:
    # the winner is recorded, then its run is created and executed.
    op.create_check_constraint(
        _TEST_RUN_IMPLIES_A_CHOICE,
        "walk_forward_folds",
        "test_backtest_id IS NULL OR chosen_strategy_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_table("walk_forward_folds")
    op.drop_index("ix_walk_forwards_study_id", table_name="walk_forwards")
    op.drop_table("walk_forwards")
