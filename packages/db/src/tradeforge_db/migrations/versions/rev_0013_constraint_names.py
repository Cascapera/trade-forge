"""constraint names that carried their prefix twice

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-24

Thirty-one CHECK constraints in every database this project has ever built are named
`ck_<table>_ck_<table>_<rule>`. The models call them `ck_<table>_<rule>`, and have all along.

**How.** `base.NAMING_CONVENTION` spells `ck` as `ck_%(table_name)s_%(constraint_name)s` — it
*interpolates the name it is given*. `rev_0001` (and only `rev_0001`) declared its checks with
the prefix already written in, `name="ck_backtests_failed_needs_error"`, so the convention
prepended a second one. The `uq` and `ix` entries build their names from **columns** and ignore
whatever name is supplied, which is why nothing else doubled and why this went unnoticed.

**Why nobody noticed for a year.** `test_the_models_are_exactly_what_the_migration_built` diffs
the live database against the metadata on every push — and Alembic 1.18 does not compare CHECK
constraints at all. Alembic 1.19 does. The dependabot bump that raises it (PR #123) is not
breaking anything; it is the first thing in this repository ever able to see this.

**Fix forward, not in place.** The obvious alternative is to edit `rev_0001` and drop the extra
prefix. That would leave every existing database — including the one with 31 backtests and
24 001 trades in it — holding names no migration explains, and a fresh one holding different
names from an old one. Migrations are history; history is appended to. So `rev_0001` keeps
producing the doubled names, and this renames them, and both kinds of database end identical.

⚠️ **Renames, never drop-and-recreate.** `ALTER TABLE ... RENAME CONSTRAINT` is a catalogue
update: it does not re-validate the constraint against the table. Dropping and re-adding would
re-scan `trades` — and would leave a window, inside the transaction, where the rule protecting
24 001 rows is not installed. There is nothing to gain from that window.

Each rename is guarded on the name being there. A database built by some future SQLAlchemy that
stops doubling would have nothing to rename, and this must be a no-op there rather than an
error — `RENAME CONSTRAINT` has no `IF EXISTS`.
"""

import re
from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, the name the database has, the name the models declare).
#
# ⚠️ Four of these are **truncated**, not merely doubled: SQLAlchemy shortens a name past
# Postgres's 63-character limit and appends a hash, so `ck_backtest_metrics_max_dd_duration_086f`
# cannot be turned back into `..._non_negative` by string surgery. The pairs were built by
# matching each name in a real database against the model that produced it, and asserted to be
# one-to-one, rather than derived — see the PR.

_RENAMES: tuple[tuple[str, str, str], ...] = (
    # backtest_metrics
    (
        "backtest_metrics",
        "ck_backtest_metrics_ck_backtest_metrics_gross_loss_sign",
        "ck_backtest_metrics_gross_loss_sign",
    ),
    (
        "backtest_metrics",
        "ck_backtest_metrics_ck_backtest_metrics_gross_profit_sign",
        "ck_backtest_metrics_gross_profit_sign",
    ),
    (
        "backtest_metrics",
        "ck_backtest_metrics_ck_backtest_metrics_max_dd_duration_086f",
        "ck_backtest_metrics_max_dd_duration_non_negative",
    ),
    (
        "backtest_metrics",
        "ck_backtest_metrics_ck_backtest_metrics_max_drawdown_ab_6738",
        "ck_backtest_metrics_max_drawdown_abs_non_negative",
    ),
    (
        "backtest_metrics",
        "ck_backtest_metrics_ck_backtest_metrics_max_drawdown_pc_8637",
        "ck_backtest_metrics_max_drawdown_pct_is_a_fraction",
    ),
    (
        "backtest_metrics",
        "ck_backtest_metrics_ck_backtest_metrics_net_profit_balances",
        "ck_backtest_metrics_net_profit_balances",
    ),
    (
        "backtest_metrics",
        "ck_backtest_metrics_ck_backtest_metrics_total_trades_no_cbc4",
        "ck_backtest_metrics_total_trades_non_negative",
    ),
    (
        "backtest_metrics",
        "ck_backtest_metrics_ck_backtest_metrics_trade_counts_balance",
        "ck_backtest_metrics_trade_counts_balance",
    ),
    (
        "backtest_metrics",
        "ck_backtest_metrics_ck_backtest_metrics_win_rate_is_a_fraction",
        "ck_backtest_metrics_win_rate_is_a_fraction",
    ),
    # backtests
    ("backtests", "ck_backtests_ck_backtests_date_range", "ck_backtests_date_range"),
    (
        "backtests",
        "ck_backtests_ck_backtests_failed_needs_error",
        "ck_backtests_failed_needs_error",
    ),
    (
        "backtests",
        "ck_backtests_ck_backtests_finished_after_started",
        "ck_backtests_finished_after_started",
    ),
    (
        "backtests",
        "ck_backtests_ck_backtests_finished_implies_started",
        "ck_backtests_finished_implies_started",
    ),
    (
        "backtests",
        "ck_backtests_ck_backtests_initial_capital_positive",
        "ck_backtests_initial_capital_positive",
    ),
    ("backtests", "ck_backtests_ck_backtests_timeframe", "ck_backtests_timeframe"),
    # datasets
    (
        "datasets",
        "ck_datasets_ck_datasets_candle_count_non_negative",
        "ck_datasets_candle_count_non_negative",
    ),
    ("datasets", "ck_datasets_ck_datasets_date_range", "ck_datasets_date_range"),
    ("datasets", "ck_datasets_ck_datasets_timeframe", "ck_datasets_timeframe"),
    # instruments
    (
        "instruments",
        "ck_instruments_ck_instruments_contract_size_positive",
        "ck_instruments_contract_size_positive",
    ),
    ("instruments", "ck_instruments_ck_instruments_digits_range", "ck_instruments_digits_range"),
    (
        "instruments",
        "ck_instruments_ck_instruments_tick_size_positive",
        "ck_instruments_tick_size_positive",
    ),
    (
        "instruments",
        "ck_instruments_ck_instruments_tick_value_positive",
        "ck_instruments_tick_value_positive",
    ),
    # strategies
    (
        "strategies",
        "ck_strategies_ck_strategies_lineage_starts_at_version_1",
        "ck_strategies_lineage_starts_at_version_1",
    ),
    (
        "strategies",
        "ck_strategies_ck_strategies_version_positive",
        "ck_strategies_version_positive",
    ),
    # trades
    ("trades", "ck_trades_ck_trades_costs_non_negative", "ck_trades_costs_non_negative"),
    ("trades", "ck_trades_ck_trades_entry_price_positive", "ck_trades_entry_price_positive"),
    ("trades", "ck_trades_ck_trades_exit_after_entry", "ck_trades_exit_after_entry"),
    ("trades", "ck_trades_ck_trades_exit_is_all_or_nothing", "ck_trades_exit_is_all_or_nothing"),
    ("trades", "ck_trades_ck_trades_exit_price_positive", "ck_trades_exit_price_positive"),
    ("trades", "ck_trades_ck_trades_net_pnl_balances", "ck_trades_net_pnl_balances"),
    ("trades", "ck_trades_ck_trades_volume_positive", "ck_trades_volume_positive"),
)


# Identifiers cannot be bound as parameters in SQL, so the names below are interpolated. They
# come from `_RENAMES`, a literal in this file — but "it is a literal" is an argument about
# today, and this is checked instead: an identifier that is not a plain lowercase name never
# reaches the string. That is what makes the `noqa` a claim rather than a silence.
_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


def _rename(table: str, old: str, new: str) -> None:
    """Rename `old` to `new` on `table`, if `old` is what is actually there."""
    for identifier in (table, old, new):
        if not _SAFE_IDENTIFIER.match(identifier):
            raise ValueError(f"refusing to interpolate {identifier!r} into DDL")

    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                JOIN pg_namespace n ON n.oid = t.relnamespace
                WHERE n.nspname = current_schema()
                  AND t.relname = '{table}'
                  AND c.conname = '{old}'
            ) THEN
                ALTER TABLE {table} RENAME CONSTRAINT "{old}" TO "{new}";
            END IF;
        END $$;
        """  # noqa: S608 — the three identifiers are checked above, not merely trusted
    )


def upgrade() -> None:
    for table, doubled, correct in _RENAMES:
        _rename(table, doubled, correct)


def downgrade() -> None:
    """Back to the doubled names, so a database stepped down still matches what `rev_0001`
    would have built for it. Not a fix being undone — a fact being restored."""
    for table, doubled, correct in _RENAMES:
        _rename(table, correct, doubled)
