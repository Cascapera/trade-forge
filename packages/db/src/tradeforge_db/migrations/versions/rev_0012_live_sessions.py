"""live sessions, and a trade that belongs to one of two parents

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-24

The row a paper session is. `backtests` records a run that finished; this records one that is
still going, which is a different shape: no `date_to` (it has not ended), a `last_bar_time` that
advances, and a status that can be `running` for days.

**`trades.backtest_id` stops being NOT NULL, and this is the migration the model always said it
would be.** The `Trade` docstring, written in phase 0, reads: *"`backtest_id` is NOT NULL today.
Phase 2 adds live sessions and relaxes this into 'exactly one of backtest_id / live_session_id',
which is an honest migration then — as opposed to a nullable foreign key now, pointing at a table
that does not exist."* That is what happens here, and the CHECK is what makes "exactly one" a
fact rather than a comment. A trade with both parents, or with neither, is a row nobody can
attribute — and it would still be counted by every metric that reads the table.

⚠️ **`mode` accepts `live`, and a CHECK forbids it.** The value belongs in the enum because the
domain has two modes and a column that pretended otherwise would have to be migrated the day the
second one arrives. The CHECK exists because *today there are no safeguards*: no kill switch, no
executor, no daily-loss limit, no paper-first gate (sdd.md §11, AGENTS.md §5.7). Enforcing it in
the database rather than in a service means enabling real trading is a **migration** — visible,
reviewed, dated — instead of a config value somebody flips. PR-303/304 drop this constraint after
the safeguards exist, and dropping it is the moment to check that they do.

**The partial unique index is a correlation key, not decoration.** A live trade is written when it
opens and updated when it closes (a decision recorded in `specs/fase-3.md`), so the writer has to
find the open row again on the exit bar. It finds it by `(live_session_id, entry_time)`. That pair
is unique because the ledger refuses a second position and a bar admits one entry — but "is
unique" and "cannot be otherwise" are different claims, and an UPDATE that silently touched two
rows would corrupt two trades rather than fail. Partial, so nothing is asserted about the millions
of backtest rows already there.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from tradeforge_schema.models import TIMEFRAMES

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ⚠️ Read from the DSL rather than retyped — same reason as rev_0010 and rev_0011: a migration
# carrying its own copy of the legal timeframes stops agreeing the first time one is added.
_TIMEFRAME_VALUES = ", ".join(f"'{timeframe}'" for timeframe in TIMEFRAMES)


def upgrade() -> None:
    op.create_table(
        "live_sessions",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # RESTRICT, like `backtests`: deleting a strategy must not silently destroy the record
        # of it having traded. Indexed because "which sessions ran this strategy?" is the
        # question the promotion gate of PR-304 asks, and Postgres does not index a foreign key
        # for you.
        sa.Column(
            "strategy_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("strategies.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "instrument_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("instruments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("timeframe", sa.String(4), nullable=False),
        sa.Column(
            "mode",
            sa.Enum(
                "paper",
                "live",
                name="session_mode",
                native_enum=False,
                create_constraint=True,
                # ⚠️ 16, matching `models._enum`, which hard-codes it for every enum column.
                # `paper` needs 8 — and a migration that sized the column to what today's
                # values happen to need would diff against the models for ever after.
                length=16,
            ),
            nullable=False,
            server_default="paper",
        ),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "stopped",
                "failed",
                name="live_session_status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
            server_default="running",
        ),
        # ⚠️ `MONEY` from `base.py` — Numeric(20, 8), the same type `backtests.initial_capital`
        # uses. A migration that picked its own precision would diff against the models for
        # ever, and the drift test is what said so.
        sa.Column("initial_capital", sa.Numeric(precision=20, scale=8), nullable=False),
        # Same argument as `backtests.cost_model`: the cost model is plugged in (ADR-07), so its
        # configuration is a document and storing it per session is what makes the session
        # explicable afterwards. A paper session runs `BarSpreadCostModel`, which takes its
        # number from each bar (ADR-0022) — so what is stored here is *which model*, and the
        # numbers it charged live in the trades.
        sa.Column("cost_model", sa.dialects.postgresql.JSONB(), nullable=False),
        # Stored for the reason `backtests.engine_version` is: reproducing a result needs both
        # the strategy (what was decided) and the engine (how it was executed).
        sa.Column("engine_version", sa.String(32), nullable=False),
        # ⚠️ The bar the session last *finished*, not the one it last received. It advances after
        # the engine has processed a bar, so a session that died mid-bar leaves this pointing at
        # the last bar it completed — which is exactly what PR-302-C needs to reconcile against
        # the bar Redis re-delivers. NULL means no bar has been completed yet, which is the
        # honest state of a session that has only just started.
        sa.Column("last_bar_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        # ⚠️ Checks take **bare** names inside `create_table` — the `ck_%(table_name)s_%(name)s`
        # convention in `base.py` prepends to whatever is given (the asymmetry rev_0010 was
        # bitten by).
        sa.CheckConstraint(f"timeframe IN ({_TIMEFRAME_VALUES})", name="timeframe"),
        sa.CheckConstraint("initial_capital > 0", name="initial_capital_positive"),
        # See the module docstring. Dropped by PR-303/304, deliberately and visibly, once the
        # kill switch, the executor limits and the paper-first gate exist.
        sa.CheckConstraint("mode = 'paper'", name="only_paper_until_safeguards_exist"),
        sa.CheckConstraint(
            "stopped_at IS NULL OR stopped_at >= started_at", name="stopped_after_started"
        ),
        # A session that failed says why; one that did not, does not claim to have.
        sa.CheckConstraint("(status = 'failed') = (error IS NOT NULL)", name="error_iff_failed"),
    )
    op.create_index("ix_live_sessions_strategy_id", "live_sessions", ["strategy_id"])
    op.create_index("ix_live_sessions_started_at", "live_sessions", ["started_at"])

    # ------------------------------------------------------------------ #
    # trades: one parent, and now there are two kinds it could be         #
    # ------------------------------------------------------------------ #

    op.alter_column("trades", "backtest_id", existing_type=sa.UUID(as_uuid=True), nullable=True)
    op.add_column(
        "trades",
        sa.Column(
            "live_session_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("live_sessions.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    # CASCADE matches `backtest_id`: the trades of a run are part of the run, not independent
    # records that outlive it.
    op.create_index(
        "ix_trades_live_session_id_entry_time", "trades", ["live_session_id", "entry_time"]
    )
    # `<>` on two booleans is XOR: exactly one parent, never both, never neither. Written as a
    # named constraint so a violation says which rule was broken rather than reporting a NOT NULL
    # on a column that is legitimately null half the time.
    op.create_check_constraint(
        "exactly_one_parent",
        "trades",
        "(backtest_id IS NULL) <> (live_session_id IS NULL)",
    )
    # The correlation key the live writer updates by. Partial, so it asserts nothing about the
    # backtest rows that already exist.
    op.create_index(
        "uq_trades_live_session_entry",
        "trades",
        ["live_session_id", "entry_time"],
        unique=True,
        postgresql_where=sa.text("live_session_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_trades_live_session_entry", table_name="trades")
    # ⚠️ The **bare** name. `base.py`'s convention is `ck_%(table_name)s_%(constraint_name)s`
    # and it prepends to whatever is given, so passing the full name asks Postgres to drop
    # `ck_trades_ck_trades_exactly_one_parent`. The same asymmetry rev_0010 and rev_0011 were
    # each bitten by, and it only shows up on the downgrade — the half nobody runs.
    op.drop_constraint("exactly_one_parent", "trades", type_="check")
    op.drop_index("ix_trades_live_session_id_entry_time", table_name="trades")
    op.drop_column("trades", "live_session_id")
    # ⚠️ Fails loudly if any trade has no backtest — which is correct. By then those rows belong
    # to a live session whose column is about to be dropped, and inventing a parent for them
    # would be worse than refusing to go back.
    op.alter_column("trades", "backtest_id", existing_type=sa.UUID(as_uuid=True), nullable=False)

    op.drop_index("ix_live_sessions_started_at", table_name="live_sessions")
    op.drop_index("ix_live_sessions_strategy_id", table_name="live_sessions")
    op.drop_table("live_sessions")
