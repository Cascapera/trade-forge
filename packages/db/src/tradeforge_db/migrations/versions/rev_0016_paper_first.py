"""real trading stops being impossible and starts being earned

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-26

`rev_0012` gated `mode='live'` behind a CHECK that simply said `mode = 'paper'`, and its own
comment named the day this would be dropped: *"by PR-303/304, deliberately and visibly, once the
kill switch, the executor limits and the paper-first gate exist"*. The first two exist. This
revision is the third, and the drop.

**The CHECK is not merely removed — it is replaced.** A constraint that goes away leaves the
invariant it carried living in application code, and this project has twice decided the other way
(`order_audit`'s append-only trigger, `live_sessions`' error-iff-failed). The reason is the same
each time: a rule enforced only by the code that writes the row is a rule that a second writer, a
migration script, or a `psql` session at three in the morning does not know about.

**But the database enforces the *invariant*, not the *policy*.** Those are different, and putting
both here would be a mistake in the other direction:

* the **invariant** is that real money is never risked by a strategy that has never been paper
  traded at all. That is not a number anybody should be able to argue with, so it is a trigger.
* the **policy** is *how many* days — five, ten, a fortnight. That is a judgement that changes
  with confidence and with the strategy, and `specs/fase-3.md` calls it configurable. A number
  whose change requires a migration is not configurable; it is a schema decision wearing a
  configuration's name. That one lives in the application.

⚠️ **A paper session only counts if it processed a bar.** `last_bar_time IS NOT NULL` is the
whole of that test, and it exists because without it the gate is trivially defeated: start a
session, kill it, repeat. A row that never saw a candle is a process that started, not a day of
paper trading.

⚠️ **The count is per *strategy*, not per strategy-and-instrument.** That is a deliberate choice
by the operator of this system, made with the trade-off stated: paper trading EURUSD M15 will
unlock real trading of the same strategy on any instrument and timeframe, and the spread,
liquidity and noise it was measured against are not the ones it will then meet. The gate attests
that *this strategy has been watched running*; it does not attest that this **plan** has. Anyone
reading a live session's existence as evidence about its instrument is reading more than is here.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ⚠️ `RAISE EXCEPTION`, not `RETURN NULL`. A `BEFORE` trigger returning NULL drops the row in
# silence — the caller sees a successful insert and no session. Same reasoning rev_0015 records.
#
# ⚠️ And the check is on **INSERT only**. A row that is already live got past this once; refusing
# its updates would stop a live session from ever recording that it stopped, which is the one
# thing a running session must always be able to do.
_GATE_FUNCTION = """
CREATE OR REPLACE FUNCTION live_sessions_require_paper_first() RETURNS trigger AS $$
BEGIN
    IF NEW.mode <> 'live' THEN
        RETURN NEW;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM live_sessions
        WHERE strategy_id = NEW.strategy_id
          AND mode = 'paper'
          AND last_bar_time IS NOT NULL
    ) THEN
        RAISE EXCEPTION
            'strategy % has never completed a bar in paper; a live session is refused '
            '(see rev_0016)', NEW.strategy_id
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_GATE_TRIGGER = """
CREATE TRIGGER live_sessions_paper_first
BEFORE INSERT ON live_sessions
FOR EACH ROW EXECUTE FUNCTION live_sessions_require_paper_first();
"""


def upgrade() -> None:
    # ⚠️ The **bare** name, not the one Postgres shows. `base.py` sets a naming convention of
    # `ck_%(table_name)s_%(name)s`, and alembic applies it on the way *out* as well as in — so
    # passing the full `ck_live_sessions_only_paper_...` gets it prepended a second time and
    # truncated to a hash, and the drop fails against a constraint that never existed. rev_0012
    # records the same asymmetry from the create side; this is the other half of it.
    op.drop_constraint("only_paper_until_safeguards_exist", "live_sessions", type_="check")
    op.execute(_GATE_FUNCTION)
    op.execute(_GATE_TRIGGER)


def downgrade() -> None:
    """The exact inverse, and it **refuses** rather than pretending when it cannot be.

    ⚠️ Re-adding `mode = 'paper'` over a table that already holds live rows would fail anyway —
    Postgres validates a new CHECK against existing rows — but it would fail with a message about
    a constraint, which sends the reader looking at the constraint. The real situation is that
    this database has traded real money and the history saying so cannot be un-made by running a
    migration backwards. Saying that plainly is worth the four extra lines.
    """
    # ⚠️ **The refusal comes first**, before anything is dropped. Postgres runs DDL inside the
    # transaction, so a failure further down would roll the drops back anyway — verified, the
    # trigger survived a refused downgrade. But that is a property of this database rather than
    # of this function, and a reader should not have to know it to see that nothing is dismantled
    # before the decision to dismantle is taken.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM live_sessions WHERE mode = 'live') THEN
                RAISE EXCEPTION
                    'this database holds live sessions; rev_0016 cannot be undone without '
                    'deleting the record that real money was traded, which a migration will '
                    'not do for you';
            END IF;
        END $$;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS live_sessions_paper_first ON live_sessions")
    op.execute("DROP FUNCTION IF EXISTS live_sessions_require_paper_first()")
    op.create_check_constraint(
        "only_paper_until_safeguards_exist", "live_sessions", "mode = 'paper'"
    )
